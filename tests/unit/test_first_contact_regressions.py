from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.commands.counterfactual_render import build_counterfactual_pair
from src.commands.ledger import triage_approve
from src.core.ledger import candidate_sqlite_store as candidate_sqlite_store
from src.core.ledger.candidate_store import (
    CandidateDecisionRecord,
    CandidateEntityResolution,
    CandidateEvent,
    _candidate_to_record,
    append_triage_decision,
)
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
from src.core.ledger.fact_bridge import (
    append_bridged_milestone_event,
    build_bridge_fact_input,
    build_bridge_milestone_fact_input,
)
from src.core.ledger.source_refs import EmailRef, LTDeckRef, OperatorAssertionRef, source_document_key
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore, project_milestones
from src.core.program_reality import ProgramReality

NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def _programs_root(tmp_path: Path) -> Path:
    root = tmp_path / "programs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _email_ref(*, message_id: str = "message-1@example.com") -> EmailRef:
    return EmailRef(
        subject="Status update",
        sent_at=NOW,
        sender="pm@example.com",
        message_id=message_id,
        vault_hash="sha256:vault-email-1",
    )


def _legacy_candidate(candidate_id: str = "cand-legacy") -> CandidateEvent:
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id="acme",
        proposed_event_type="milestone.date_revised.v1",
        proposed_payload={"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
        proposed_occurred_at=NOW,
        proposed_temporal_confidence="approximate",
        proposed_confidence="ai_extracted",
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=NOW.date(), slide_number=9),
        pipeline="lt_deck",
        extraction_confidence=0.9,
        entity_resolution=(
            CandidateEntityResolution(
                raw_name="Gen9",
                resolved_entity_id="milestone:m1",
                match_kind="exact",
                score=1.0,
            ),
        ),
        dedupe_key="sha256:legacy-dedupe",
        dedupe_core_hash="sha256:legacy-core",
        source_document_key="lt_deck:deck.pptx:2026-07-07:9",
        corroborating_refs=(),
        batch_id="batch-1",
        staged_at=NOW,
    )


def _create_pre_s1_candidate_db(db_dir: Path, candidate: CandidateEvent) -> Path:
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = candidate_sqlite_store.candidate_db_path(db_dir)
    record = _candidate_to_record(candidate)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE _meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE candidates (
                candidate_id        TEXT PRIMARY KEY,
                program_id          TEXT NOT NULL,
                batch_id            TEXT NOT NULL,
                source_document_key TEXT NOT NULL,
                dedupe_key          TEXT NOT NULL,
                staged_at           TEXT NOT NULL,
                payload_json        TEXT NOT NULL
            );
            CREATE TABLE candidate_decisions (
                decision_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id   TEXT NOT NULL,
                kind           TEXT NOT NULL,
                decided_at     TEXT NOT NULL,
                triage_actor   TEXT NOT NULL,
                payload_json   TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO _meta (key, value) VALUES ('schema_version', '0')"
        )
        connection.execute(
            """
            INSERT INTO candidates
                (candidate_id, program_id, batch_id, source_document_key, dedupe_key, staged_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.candidate_id,
                candidate.program_id,
                candidate.batch_id,
                candidate.source_document_key,
                candidate.dedupe_key,
                str(record["staged_at"]),
                json.dumps(record, sort_keys=True),
            ),
        )
        connection.commit()
    return db_path


def _milestone_completed_event(
    *,
    event_id: str,
    milestone_id: str = "milestone:m1",
    completed_on: str = "2026-07-01",
    source_ref: object | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        program_id="acme",
        event_type="milestone.completed.v1",
        occurred_at=NOW,
        recorded_at=NOW,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="rev-mail",
        payload={
            "milestone_id": milestone_id,
            "completed_on": completed_on,
            "evidence": "Completed from source email",
        },
        source_ref=source_ref if source_ref is not None else _email_ref(message_id=f"{event_id}@example.com"),
        prev_event_hash="sha256:prev",
        content_hash="sha256:content",
    )


def _milestone_fact_input(
    *,
    milestone_id: str,
    name: str,
    target_date: str,
    source_document_key_value: str | None = None,
    approval_event_id: str | None = None,
    domain_event_id: str | None = None,
) -> ProgramFactInput:
    return ProgramFactInput(
        fact_type="milestone.entry",
        entity_refs=(f"MILESTONE:{milestone_id}",),
        payload={
            "id": milestone_id,
            "program_id": "acme",
            "name": name,
            "target_date": target_date,
            "owner_alias": "unknown",
            "status": "completed",
            "exit_criteria": [],
            "linked_workstream_ids": [],
            "linked_work_item_ids": [],
            "notes": "Completed from approved source",
            "last_reviewed_date": NOW.date().isoformat(),
        },
        confidence=ConfidenceTier.OPERATOR_CONFIRMED.value,
        created_by="test",
        write_authority="human",
        source_document_key=source_document_key_value,
        approval_event_id=approval_event_id,
        domain_event_id=domain_event_id,
    )


def test_candidate_store_schema_drift_approval_initializes_outbox_table(tmp_path: Path) -> None:
    """1. Candidate-store schema drift.

    Minimal failing input: a ``candidates.db`` containing only ``_meta``,
    ``candidates``, and ``candidate_decisions`` plus one staged candidate row
    (no ``projection_outbox`` table). ``triage_approve(...)`` must still
    succeed and create ``projection_outbox`` before writing to it.
    """

    programs_root = _programs_root(tmp_path)
    candidate = _legacy_candidate()
    db_dir = programs_root / "acme" / "ledger" / "candidates"
    db_path = _create_pre_s1_candidate_db(db_dir, candidate)

    triage_approve(
        program="acme",
        candidate_id=candidate.candidate_id,
        actor="operator",
        programs_root=programs_root,
    )

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        outbox_rows = connection.execute(
            "SELECT status, source_event_id FROM projection_outbox"
        ).fetchall()

    assert "projection_outbox" in tables
    assert len(outbox_rows) == 1
    assert outbox_rows[0][0] == "projected"


def test_milestone_completed_stub_falls_back_to_completion_date(tmp_path: Path) -> None:
    """2. Milestone stub ``target_date`` fallback.

    Minimal failing input: ``EventEnvelope(event_type="milestone.completed.v1",
    payload={"milestone_id": "milestone:stub", "completed_on": "2026-07-02"})``
    with no prior ``milestone.created.v1`` / ``current_fact``. The bridged fact
    must synthesize ``target_date == "2026-07-02"`` so the downstream
    milestone projector can deserialize it.
    """

    event = _milestone_completed_event(
        event_id="evt-milestone-stub",
        milestone_id="milestone:stub",
        completed_on="2026-07-02",
    )

    fact_input = build_bridge_milestone_fact_input(event)
    assert fact_input.payload["target_date"] == "2026-07-02"

    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(fact_input, recorded_at=NOW)
    milestones = project_milestones(store.snapshot())

    assert len(milestones) == 1
    assert milestones[0].target_date.isoformat() == "2026-07-02"


def test_program_reality_joins_prefixed_entity_refs_by_bare_record_id(
    tmp_path: Path, monkeypatch
) -> None:
    """3. Fact↔record join used the wrong key.

    Minimal failing input: ``ProgramFactInput(entity_refs=("MILESTONE:milestone:abc",),
    payload["id"] == "milestone:abc")``. ``ProgramReality.load()`` must still
    resolve the projected milestone back to the fact via the unprefixed suffix.
    """

    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    programs_root = _programs_root(tmp_path)
    store = ProgramFactStore("acme", db_root=tmp_path)
    result = store.append_fact(
        _milestone_fact_input(
            milestone_id="milestone:abc",
            name="Joined milestone",
            target_date="2026-07-15",
            source_document_key_value="email:join@example.com:2026-07-07T20:00:00Z",
            domain_event_id="evt-join-001",
        ),
        recorded_at=NOW,
    )

    assessment = ProgramReality.load("acme", programs_root=programs_root).milestones()[0]

    assert assessment.record.id == "milestone:abc"
    assert assessment.fact_id == result.revision.fact_id
    assert assessment.lineage is not None
    assert assessment.lineage.domain_event_id == "evt-join-001"


def test_build_bridge_fact_input_wires_source_document_key_and_degrades_gracefully() -> None:
    """4. ``source_document_key`` never wired.

    Minimal failing inputs: (a) ``EventEnvelope(source_ref=EmailRef(message_id="msg-4@example.com"))``
    must populate ``ProgramFactInput.source_document_key``; and (b)
    ``EventEnvelope(source_ref=object())`` must not raise, instead leaving
    ``source_document_key is None``.
    """

    good_ref = _email_ref(message_id="msg-4@example.com")
    good_event = _milestone_completed_event(
        event_id="evt-source-key-good",
        source_ref=good_ref,
    )
    good_fact = build_bridge_fact_input(
        good_event,
        fact_type="action.item",
        entity_refs=("WI:123",),
        payload={"source": "email", "title": "Follow up"},
    )

    bad_event = replace(good_event, event_id="evt-source-key-bad", source_ref=object())
    bad_fact = build_bridge_fact_input(
        bad_event,
        fact_type="action.item",
        entity_refs=("WI:124",),
        payload={"source": "email", "title": "Fallback"},
    )

    assert good_fact.source_document_key == source_document_key(good_ref)
    assert bad_fact.source_document_key is None


def test_counterfactual_render_produces_non_empty_attributable_diff(
    tmp_path: Path, monkeypatch
) -> None:
    """5. Counterfactual-proof harness wrong assessment type.

    Minimal failing input: one stored milestone fact that
    ``ProgramReality.load().milestones()`` returns as a ``FactAssessment``.
    ``build_counterfactual_pair(...)`` must render non-empty text for the
    with-fact arm instead of collapsing both arms to ``""``.
    """

    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    programs_root = _programs_root(tmp_path)
    store = ProgramFactStore("acme", db_root=tmp_path)
    revision = store.append_fact(
        _milestone_fact_input(
            milestone_id="milestone:cf",
            name="Counterfactual GA",
            target_date="2026-07-20",
            source_document_key_value="email:counterfactual@example.com:2026-07-07T20:00:00Z",
            approval_event_id="evt-approval-cf",
            domain_event_id="evt-domain-cf",
        ),
        recorded_at=NOW,
    ).revision

    pair = build_counterfactual_pair(
        program_id="acme",
        fact_id=revision.fact_id,
        programs_root=programs_root,
        as_of=NOW,
    )

    assert pair is not None
    assert pair.with_fact_text
    assert pair.without_fact_text == ""
    assert pair.differs is True
    assert pair.source_document_key == "email:counterfactual@example.com:2026-07-07T20:00:00Z"
    assert "Counterfactual GA" in pair.with_fact_text


def test_program_reality_reverse_looks_up_approval_event_id(
    tmp_path: Path, monkeypatch
) -> None:
    """6. ``approval_event_id`` reverse-lookup join.

    Minimal failing input: one ``commitment.entry`` fact with
    ``lineage.domain_event_id == "evt-approval-join"`` and
    ``approval_event_id is None``, plus a
    ``CandidateDecisionRecord(resulting_event_id="evt-approval-join",
    approval_event_id="evt-approval-001")``. The current code explicitly
    reassigns ``commitments_assessed`` after the reverse lookup, so
    ``ProgramReality.load().commitments()`` is the relevant surfaced accessor.
    """

    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    programs_root = _programs_root(tmp_path)
    ProgramFactStore("acme", db_root=tmp_path).append_fact(
        ProgramFactInput(
            fact_type="commitment.entry",
            entity_refs=("commitment:c1",),
            payload={
                "commitment_id": "commitment:c1",
                "title": "Close launch checklist",
                "dri": "alex",
                "due_date": "2026-07-10",
                "direction": "outbound",
                "status": "active",
                "description": "Launch commitment",
                "slip_history": [],
            },
            confidence=ConfidenceTier.OPERATOR_CONFIRMED.value,
            created_by="test",
            write_authority="human",
            source_document_key="email:approval-join@example.com:2026-07-07T20:00:00Z",
            domain_event_id="evt-approval-join",
        ),
        recorded_at=NOW,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-approval-join",
            kind="approved",
            decided_at=NOW,
            triage_actor="operator",
            batch_id="batch-1",
            resulting_event_id="evt-approval-join",
            approval_event_id="evt-approval-001",
        ),
        program_id="acme",
        programs_root=programs_root,
    )

    assessment = ProgramReality.load("acme", programs_root=programs_root).commitments()[0]

    assert assessment.lineage is not None
    assert assessment.lineage.domain_event_id == "evt-approval-join"
    assert assessment.lineage.approval_event_id == "evt-approval-001"


def test_append_bridged_milestone_event_is_idempotent_on_domain_event_id(
    tmp_path: Path,
) -> None:
    """7. Bridge idempotency.

    Minimal failing input: the exact same
    ``EventEnvelope(event_id="evt-idempotent-ms", event_type="milestone.completed.v1",
    payload={"milestone_id": "milestone:idem", "completed_on": "2026-07-03"})``
    appended twice. The fact store must contain exactly one matching fact.
    """

    event = _milestone_completed_event(
        event_id="evt-idempotent-ms",
        milestone_id="milestone:idem",
        completed_on="2026-07-03",
    )

    first = append_bridged_milestone_event(event, db_root=tmp_path)
    second = append_bridged_milestone_event(event, db_root=tmp_path)
    snapshot = ProgramFactStore("acme", db_root=tmp_path).snapshot()
    matching = [
        fact
        for fact in snapshot.facts
        if fact.fact_type == "milestone.entry" and fact.domain_event_id == event.event_id
    ]

    assert first.action == "created"
    assert second.action == "noop"
    assert len(matching) == 1
