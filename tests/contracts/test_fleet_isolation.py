"""S-11a: Contract test — program fleet isolation.

Spec reference: .archive/specs/consolidated.md §S-11a (local-only); core spec: vertex-tech-spec.md §13.6.
Rule: Each program must have an independent candidates.db, event log, and fact store.
      Writes to program A must not be visible in program B's reads.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.ledger.candidate_store import (
    get_candidate_db_dir,
    load_pending_candidates,
)
from src.core.ledger.candidate_sqlite_store import (
    DB_FILENAME,
    candidate_db_path,
    init_candidate_db,
    sqlite_insert_candidate,
    sqlite_load_candidates,
)
from src.core.program_fact_store import (
    FactLifecycleState,
    FactPrecedence,
    FactReviewState,
    ProgramFactRevision,
    ProgramFactSnapshot,
    project_milestones,
)


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_two_programs_have_independent_db_paths() -> None:
    """S-11a: get_candidate_db_dir returns distinct paths for distinct program IDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        path_a = get_candidate_db_dir("prog-a", programs_root=root)
        path_b = get_candidate_db_dir("prog-b", programs_root=root)
        assert path_a != path_b, "Fleet isolation broken: programs share the same DB dir"
        assert "prog-a" in str(path_a)
        assert "prog-b" in str(path_b)


def test_candidate_db_paths_are_under_program_root() -> None:
    """Each program's DB must live under its own program directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for prog_id in ("prog-a", "prog-b", "prog-c"):
            db_dir = get_candidate_db_dir(prog_id, programs_root=root)
            assert str(db_dir).startswith(str(root / prog_id)), (
                f"Fleet isolation: {prog_id} db_dir {db_dir} not under {root / prog_id}"
            )


def test_sqlite_inserts_are_isolated_between_programs() -> None:
    """S-11a: A row inserted into prog-A's DB must not appear in prog-B's reads."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        dir_a = get_candidate_db_dir("prog-a", programs_root=root)
        dir_b = get_candidate_db_dir("prog-b", programs_root=root)
        dir_a.mkdir(parents=True, exist_ok=True)
        dir_b.mkdir(parents=True, exist_ok=True)

        init_candidate_db(dir_a)
        init_candidate_db(dir_b)

        cand_id = f"cand-{uuid.uuid4().hex[:8]}"
        sqlite_insert_candidate(
            dir_a,
            candidate_id=cand_id,
            program_id="prog-a",
            batch_id="batch-test",
            source_document_key="sha256:abc",
            dedupe_key=f"prog-a:{cand_id}",
            staged_at=_NOW.isoformat(),
            payload_json=json.dumps({"candidate_id": cand_id, "program_id": "prog-a"}),
        )

        rows_a = sqlite_load_candidates(dir_a)
        rows_b = sqlite_load_candidates(dir_b)

        assert len(rows_a) == 1, f"Expected 1 row in prog-a, got {len(rows_a)}"
        assert len(rows_b) == 0, (
            f"Fleet isolation broken: prog-b sees prog-a row. Got: {rows_b}"
        )


def test_db_filename_constant_is_stable() -> None:
    """All programs must use the same DB_FILENAME — changing it breaks fleet isolation."""
    assert DB_FILENAME == "candidates.db", (
        "DB_FILENAME must be 'candidates.db' — changing it silently breaks existing DBs"
    )


def test_fact_snapshots_are_scoped_to_program() -> None:
    """S-11a: ProgramFactSnapshot.program_id scopes facts; two snapshots share no facts."""
    def _ms_fact(program_id: str, ms_id: str) -> ProgramFactRevision:
        return ProgramFactRevision(
            revision_id=f"rev-{ms_id}",
            fact_id=ms_id,
            program_id=program_id,
            natural_key=f"milestone.entry|{ms_id}",
            fact_type="milestone.entry",
            scope="program",
            entity_refs=(ms_id,),
            payload={
                "id": ms_id,
                "program_id": program_id,
                "name": f"Milestone {ms_id}",
                "target_date": "2026-09-01",
                "owner_alias": "tpm",
                "status": "on_track",
            },
            source_signal_ids=(),
            confidence=None,
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
            lifecycle_state=FactLifecycleState.ACTIVE,
            valid_from=None,
            valid_until=None,
            recorded_at=_NOW,
            superseded_at=None,
            projection_history=(),
            proposed_against_revision_id=None,
            created_by="test",
        )

    snap_a = ProgramFactSnapshot(
        program_id="prog-a",
        as_of=_NOW,
        facts=(_ms_fact("prog-a", "ms-a1"), _ms_fact("prog-a", "ms-a2")),
    )
    snap_b = ProgramFactSnapshot(
        program_id="prog-b",
        as_of=_NOW,
        facts=(_ms_fact("prog-b", "ms-b1"),),
    )

    milestones_a = project_milestones(snap_a)
    milestones_b = project_milestones(snap_b)

    assert len(milestones_a) == 2
    assert len(milestones_b) == 1

    ids_a = {m.id for m in milestones_a}
    ids_b = {m.id for m in milestones_b}
    assert ids_a.isdisjoint(ids_b), (
        f"Fleet isolation broken at snapshot level: shared milestone IDs: {ids_a & ids_b}"
    )


def test_bridge_appender_isolates_facts_between_two_concurrent_programs(tmp_path: Path) -> None:
    """fix-data-flow.md Track A / PR-6 (R9): a `program_id`-filtering bug in a
    bridge appender could leak facts from one program into another's
    `ProgramReality` snapshot once the bridge is default-on for every
    REV-configured program, not just one explicitly opted-in program. This
    runs the real `append_bridged_milestone_event` appender for two distinct
    programs against a *shared* `db_root` (mirroring how a single fact-store
    backend serves the whole fleet) and asserts each program's snapshot sees
    only its own fact.
    """
    from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
    from src.core.ledger.fact_bridge import append_bridged_milestone_event
    from src.core.ledger.source_refs import EmailRef
    from src.core.program_fact_store import ProgramFactStore

    db_root = tmp_path / "vertex-db"

    def _milestone_completed_event(*, program_id: str, event_id: str, milestone_id: str) -> "EventEnvelope":
        return EventEnvelope(
            event_id=event_id,
            program_id=program_id,
            event_type="milestone.completed.v1",
            occurred_at=_NOW,
            recorded_at=_NOW,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="rev-mail",
            payload={"milestone_id": milestone_id, "completed_on": "2026-05-30"},
            source_ref=EmailRef(
                subject="Milestone complete",
                sent_at=_NOW,
                sender="pm@example.com",
                message_id=f"{event_id}@example.com",
                vault_hash=f"sha256:vault-{event_id}",
            ),
            prev_event_hash="sha256:prev",
            content_hash=f"sha256:content-{event_id}",
        )

    append_bridged_milestone_event(
        _milestone_completed_event(program_id="prog-a", event_id="evt-a1", milestone_id="milestone:shared-id"),
        db_root=db_root,
    )
    append_bridged_milestone_event(
        _milestone_completed_event(program_id="prog-b", event_id="evt-b1", milestone_id="milestone:shared-id"),
        db_root=db_root,
    )

    snapshot_a = ProgramFactStore("prog-a", db_root=db_root).snapshot()
    snapshot_b = ProgramFactStore("prog-b", db_root=db_root).snapshot()

    assert all(fact.program_id == "prog-a" for fact in snapshot_a.facts), (
        "Fleet isolation broken: prog-a's snapshot contains a fact from another program"
    )
    assert all(fact.program_id == "prog-b" for fact in snapshot_b.facts), (
        "Fleet isolation broken: prog-b's snapshot contains a fact from another program"
    )
    milestones_a = project_milestones(snapshot_a)
    milestones_b = project_milestones(snapshot_b)
    assert len(milestones_a) == 1
    assert len(milestones_b) == 1
