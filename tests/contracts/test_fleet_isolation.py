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
