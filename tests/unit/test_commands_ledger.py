from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
from src.commands.ledger import _write_candidate_audit_event, _write_candidate_event
from src.core.knowledge_claim_store import append_claim_revision
from src.core.knowledge.vault import ingest_knowledge_source, load_shared_vault_verify_status, write_shared_vault_verify_status
from src.core.ledger.candidate_store import CandidateDecisionRecord, CandidateEntityResolution, CandidateEvent, active_count, append_candidate, append_triage_decision, load_pending_candidates, load_triage_decisions
from src.core.ledger.evidence_vault import store_evidence_vault_bytes
from src.core.ledger.discovery_run_recorder import DiscoveryRunResult, GapDetail, record_discovery_run
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, read_events, write_event
from src.core.ledger.program_views import canonical_projection_dump, get_current_projection_path
from src.core.ledger.source_refs import KnowledgeDocumentRef, LTDeckRef, OperatorAssertionRef, WorkIQRef
from src.core.ledger.verify_status import write_ledger_verify_status
from src.core.program_fact_store import FactPrecedence, FactReviewState, ProgramFactInput, ProgramFactStore, load_program_facts


runner = CliRunner()


def test_ledger_triage_approve_writes_event_projection_and_history(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-1"), programs_root=programs_root)

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 0
    assert "Approved cand-1 ->" in approve_result.stdout

    status_result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    status_payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert status_payload["active_count"] == 0
    assert status_payload["decision_count"] == 1

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["milestone.date_revised.v1", "discovery.candidate_approved.v1"]

    history_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "history",
            "--program",
            "acme",
            "--entity",
            "milestone:m1",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    history_payload = json.loads(history_result.stdout)

    assert history_result.exit_code == 0
    assert [row["event_type"] for row in history_payload["events"]] == ["milestone.date_revised.v1"]
    assert history_payload["events"][0]["source_ref_type"] == "lt_deck"
    assert history_payload["events"][0]["source_document_key"] == "lt_deck:deck.pptx:2025-03-20:9"

    decisions = load_triage_decisions("acme", programs_root=programs_root)
    assert decisions[0].candidate_id == "cand-1"
    assert decisions[0].kind == "approved"
    assert decisions[0].resulting_event_id == events[0].event_id
    assert decisions[0].approval_event_id == events[1].event_id


def test_ledger_triage_revoke_tombstones_approved_event_and_reprojects(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-revoke"), programs_root=programs_root)

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-revoke",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0
    approved_event_id = read_events("acme", programs_root=programs_root)[0].event_id

    revoke_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "revoke",
            "--program",
            "acme",
            "--candidate",
            "cand-revoke",
            "--actor",
            "operator",
            "--reason",
            "operator withdrew approval",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert revoke_result.exit_code == 0
    assert f"Revoked cand-revoke: {approved_event_id} ->" in revoke_result.stdout
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "milestone.date_revised.v1",
        "discovery.candidate_approved.v1",
        "operator.correction.v1",
        "discovery.candidate_revoked.v1",
    ]
    assert events[2].payload == {
        "corrects_event_id": approved_event_id,
        "corrected_payload": None,
        "reason": "triage revoke cand-revoke: operator withdrew approval",
    }
    assert events[3].payload["candidate_id"] == "cand-revoke"
    assert events[3].payload["resulting_event_id"] == approved_event_id
    assert events[3].payload["revocation_event_id"] == events[2].event_id
    projection = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))
    assert projection["proj_milestone"] == []

    decisions = load_triage_decisions("acme", programs_root=programs_root)
    assert [decision.kind for decision in decisions] == ["approved", "revoked"]
    assert decisions[0].approval_event_id == events[1].event_id
    assert decisions[1].resulting_event_id == events[2].event_id
    assert decisions[1].approval_event_id == events[3].event_id


def test_ledger_triage_edit_writes_operator_confirmed_event_and_marks_audit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-edit"), programs_root=programs_root)

    edit_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "edit",
            "--program",
            "acme",
            "--candidate",
            "cand-edit",
            "--actor",
            "operator",
            "--payload-json",
            '{"milestone_id": "milestone:m1", "new_target_date": "2025-10-15"}',
            "--reason",
            "corrected from operator review",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert edit_result.exit_code == 0
    assert "Edited cand-edit ->" in edit_result.stdout

    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["milestone.date_revised.v1", "discovery.candidate_approved.v1"]
    assert events[0].payload["new_target_date"] == "2025-10-15"
    assert events[0].confidence.value == "operator_confirmed"
    assert events[1].payload["edited"] is True

    decisions = load_triage_decisions("acme", programs_root=programs_root)
    assert decisions[0].kind == "approved"
    assert decisions[0].edited is True
    assert decisions[0].reason == "corrected from operator review"


def test_ledger_triage_approve_blocks_locked_candidate_without_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-locked-blocked"), programs_root=programs_root)

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-locked-blocked",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 3
    assert "rerun with --override-lock" in approve_result.stdout
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["operator.field_lock.v1"]
    assert load_triage_decisions("acme", programs_root=programs_root) == ()


def test_ledger_triage_approve_override_lock_writes_unlock_event_relock_and_audit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-locked-override"), programs_root=programs_root)

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--valid-until",
            "2026-12-31T00:00:00+00:00",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-locked-override",
            "--actor",
            "operator",
            "--override-lock",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 0
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "operator.field_lock.v1",
        "operator.field_unlock.v1",
        "milestone.date_revised.v1",
        "operator.field_lock.v1",
        "discovery.candidate_approved.v1",
    ]
    override_session_id = events[1].payload["override_session_id"]
    assert events[2].payload["override_session_id"] == override_session_id
    assert events[3].payload["override_session_id"] == override_session_id
    assert events[3].payload["locked_value"] == "2025-09-30"
    assert events[3].payload["valid_until"] == "2026-12-31T00:00:00+00:00"
    projection = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))
    assert projection["proj_milestone"][0]["target_date"] == "2025-09-30"
    assert json.loads(projection["field_locks"][0]["locked_value"]) == "2025-09-30"


def test_ledger_triage_edit_override_lock_relocks_to_edited_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-locked-edit"), programs_root=programs_root)

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    edit_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "edit",
            "--program",
            "acme",
            "--candidate",
            "cand-locked-edit",
            "--actor",
            "operator",
            "--payload-json",
            '{"milestone_id": "milestone:m1", "new_target_date": "2025-11-15"}',
            "--override-lock",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert edit_result.exit_code == 0
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "operator.field_lock.v1",
        "operator.field_unlock.v1",
        "milestone.date_revised.v1",
        "operator.field_lock.v1",
        "discovery.candidate_approved.v1",
    ]
    assert events[2].payload["new_target_date"] == "2025-11-15"
    assert events[2].confidence.value == "operator_confirmed"
    assert events[3].payload["locked_value"] == "2025-11-15"
    assert events[4].payload["edited"] is True


def test_ledger_triage_approve_override_lock_supports_multiple_locked_fields(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_decision_candidate("cand-decision-locks"), programs_root=programs_root)

    for field_name, locked_value in (("title", '"Pinned title"'), ("decision_text", '"Pinned text"')):
        lock_result = runner.invoke(
            app,
            [
                "--no-catchup",
                "ledger",
                "lock",
                "--program",
                "acme",
                "--entity-id",
                "decision:d-lock",
                "--field",
                field_name,
                "--actor",
                "operator",
                "--locked-value-json",
                locked_value,
                "--programs-root",
                str(programs_root),
            ],
        )
        assert lock_result.exit_code == 0

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-decision-locks",
            "--actor",
            "operator",
            "--override-lock",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 0
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "operator.field_lock.v1",
        "operator.field_lock.v1",
        "operator.field_unlock.v1",
        "operator.field_unlock.v1",
        "decision.made.v1",
        "operator.field_lock.v1",
        "operator.field_lock.v1",
        "discovery.candidate_approved.v1",
    ]
    override_session_id = events[2].payload["override_session_id"]
    assert all(event.payload.get("override_session_id") == override_session_id for event in events[2:7])
    relocked_values = {
        (event.payload["entity_id"], event.payload["field"]): event.payload["locked_value"]
        for event in events[5:7]
    }
    assert relocked_values == {
        ("decision:d-lock", "decision_text"): "Ship with dual lock support.",
        ("decision:d-lock", "title"): "Decision lock test",
    }
    projection = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))
    assert projection["proj_decision"][0]["title"] == "Decision lock test"
    assert projection["proj_decision"][0]["decision_text"] == "Ship with dual lock support."


def test_ledger_triage_approve_ignores_unset_optional_locked_fields(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_decision_candidate("cand-decision-forum", forum=None), programs_root=programs_root)

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "decision:d-lock",
            "--field",
            "forum",
            "--actor",
            "operator",
            "--locked-value-json",
            '"Pinned forum"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-decision-forum",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 0
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "operator.field_lock.v1",
        "decision.made.v1",
        "discovery.candidate_approved.v1",
    ]


def test_ledger_triage_approve_override_lock_supports_non_tombstone_corrections(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    write_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "milestone.date_revised.v1",
            "--occurred-at",
            "2025-03-20T00:00:00+00:00",
            "--actor",
            "operator",
            "--payload-json",
            '{"milestone_id":"milestone:m1","new_target_date":"2025-09-30"}',
            "--source-ref-json",
            '{"ref_type":"operator_assertion","asserted_by":"operator","asserted_at":"2026-06-11T00:00:00+00:00"}',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert write_result.exit_code == 0
    written_event_id = read_events("acme", programs_root=programs_root)[0].event_id

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    append_candidate(
        _correction_candidate(
            "cand-correction-override",
            corrects_event_id=written_event_id,
            corrected_payload={"milestone_id": "milestone:m1", "new_target_date": "2025-11-15"},
        ),
        programs_root=programs_root,
    )

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-correction-override",
            "--actor",
            "operator",
            "--override-lock",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 0
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "milestone.date_revised.v1",
        "operator.field_lock.v1",
        "operator.field_unlock.v1",
        "operator.correction.v1",
        "operator.field_lock.v1",
        "discovery.candidate_approved.v1",
    ]
    override_session_id = events[2].payload["override_session_id"]
    assert events[3].payload["override_session_id"] == override_session_id
    assert events[4].payload["override_session_id"] == override_session_id
    assert events[4].payload["locked_value"] == "2025-11-15"
    projection = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))
    assert projection["proj_milestone"][0]["target_date"] == "2025-11-15"
    assert json.loads(projection["field_locks"][0]["locked_value"]) == "2025-11-15"


def test_ledger_triage_approve_override_lock_rejects_tombstone_corrections(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    write_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "milestone.date_revised.v1",
            "--occurred-at",
            "2025-03-20T00:00:00+00:00",
            "--actor",
            "operator",
            "--payload-json",
            '{"milestone_id":"milestone:m1","new_target_date":"2025-09-30"}',
            "--source-ref-json",
            '{"ref_type":"operator_assertion","asserted_by":"operator","asserted_at":"2026-06-11T00:00:00+00:00"}',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert write_result.exit_code == 0
    written_event_id = read_events("acme", programs_root=programs_root)[0].event_id

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    append_candidate(
        _correction_candidate(
            "cand-correction-tombstone",
            corrects_event_id=written_event_id,
            corrected_payload=None,
        ),
        programs_root=programs_root,
    )

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-correction-tombstone",
            "--actor",
            "operator",
            "--override-lock",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert approve_result.exit_code == 2
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "milestone.date_revised.v1",
        "operator.field_lock.v1",
    ]
    assert load_triage_decisions("acme", programs_root=programs_root) == ()


def test_ledger_triage_reject_writes_audit_and_keeps_candidate_out_of_finalizable_queue(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-2"), programs_root=programs_root)

    reject_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "reject",
            "--program",
            "acme",
            "--candidate",
            "cand-2",
            "--actor",
            "operator",
            "--reason",
            "bad extraction",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert reject_result.exit_code == 0
    assert "Rejected cand-2" in reject_result.stdout

    status_result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    status_payload = json.loads(status_result.stdout)

    assert status_payload["active_count"] == 0
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == ["discovery.candidate_rejected.v1"]

    decisions = load_triage_decisions("acme", programs_root=programs_root)
    assert decisions[0].kind == "rejected"
    assert decisions[0].reason == "bad extraction"


def test_ledger_triage_skip_keeps_candidate_active_and_records_audit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-skip"), programs_root=programs_root)

    skip_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "skip",
            "--program",
            "acme",
            "--candidate",
            "cand-skip",
            "--actor",
            "operator",
            "--reason",
            "needs more evidence",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert skip_result.exit_code == 0
    assert "Skipped cand-skip" in skip_result.stdout

    status_result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    status_payload = json.loads(status_result.stdout)

    assert status_payload["active_count"] == 1
    decisions = load_triage_decisions("acme", programs_root=programs_root)
    assert decisions[0].kind == "skipped"
    assert decisions[0].reason == "needs more evidence"


def test_ledger_status_reports_coverage_and_batch_progress(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    append_candidate(_candidate("cand-staged"), programs_root=programs_root)
    append_candidate(_candidate("cand-approved", batch_id="batch-approved"), programs_root=programs_root)
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-approved",
            kind="approved",
            decided_at=datetime(2026, 6, 12, 12, 1, tzinfo=timezone.utc),
            triage_actor="operator",
            batch_id="batch-approved",
        ),
        program_id="acme",
        programs_root=programs_root,
    )
    append_candidate(_candidate("cand-quarantined", batch_id="batch-quarantined"), programs_root=programs_root)
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-quarantined",
            kind="rejected",
            decided_at=datetime(2026, 6, 12, 12, 2, tzinfo=timezone.utc),
            triage_actor="operator",
            batch_id="batch-quarantined",
            reason="quarantined: bad extraction",
        ),
        program_id="acme",
        programs_root=programs_root,
    )

    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="pipeline.gap_detected.v1",
            occurred_at=datetime(2026, 6, 7, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 12, 12, 3, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={
                "pipeline": "workiq",
                "gap_kind": "missing_series_registration",
                "detail": "series id missing",
                "window_start": "2026-06-01T00:00:00+00:00",
                "window_end": "2026-06-07T00:00:00+00:00",
            },
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 12, 12, 3, tzinfo=timezone.utc), context="gap detected"),
            dedupe_payload={
                "pipeline": "workiq",
                "gap_kind": "missing_series_registration",
                "window_start": "2026-06-01T00:00:00+00:00",
                "window_end": "2026-06-07T00:00:00+00:00",
            },
        ),
        programs_root=programs_root,
    )
    acknowledged_gap = write_event(
        build_event_envelope(
            program_id="acme",
            event_type="pipeline.gap_detected.v1",
            occurred_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 12, 12, 4, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={
                "pipeline": "newsletter",
                "gap_kind": "missed_window",
                "detail": "issue missing",
                "window_start": "2026-06-08T00:00:00+00:00",
                "window_end": "2026-06-09T00:00:00+00:00",
            },
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 12, 12, 4, tzinfo=timezone.utc), context="gap detected"),
            dedupe_payload={
                "pipeline": "newsletter",
                "gap_kind": "missed_window",
                "window_start": "2026-06-08T00:00:00+00:00",
                "window_end": "2026-06-09T00:00:00+00:00",
            },
        ),
        programs_root=programs_root,
    ).envelope
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id=f"gap:{acknowledged_gap.event_id}",
            kind="gap_acknowledged",
            decided_at=datetime(2026, 6, 12, 12, 5, tzinfo=timezone.utc),
            triage_actor="operator",
            gap_event_id=acknowledged_gap.event_id,
        ),
        program_id="acme",
        programs_root=programs_root,
    )

    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.date_revised.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
            source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=9),
            dedupe_payload={"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
        ),
        programs_root=programs_root,
    )
    from src.core.ledger.program_views import project_program_events
    project_program_events("acme", programs_root=programs_root)

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["coverage_earliest"] == "2025-03-20T00:00:00+00:00"
    assert payload["coverage_latest"] == "2026-06-09T00:00:00+00:00"
    assert payload["gap_count"] == 1
    assert payload["gaps_by_pipeline"] == {"workiq": 1}
    assert payload["oldest_gap_pipeline"] == "workiq"
    assert payload["oldest_gap_kind"] == "missing_series_registration"
    assert payload["oldest_gap_window_start"] == "2026-06-01T00:00:00+00:00"
    assert payload["oldest_gap_window_end"] == "2026-06-07T00:00:00+00:00"
    assert payload["batch_count"] == 3
    assert payload["staged_batch_count"] == 1
    assert payload["approved_batch_count"] == 1
    assert payload["quarantined_batch_count"] == 1
    assert {batch["batch_id"]: batch["status"] for batch in payload["batches"]} == {
        "batch-1": "staged",
        "batch-approved": "approved",
        "batch-quarantined": "quarantined",
    }

    assert status_text.exit_code == 0
    assert "gaps_by_pipeline=workiq=1 oldest_gap_pipeline=workiq oldest_gap_kind=missing_series_registration" in status_text.output
    assert "oldest_gap_window_start=2026-06-01T00:00:00+00:00 oldest_gap_window_end=2026-06-07T00:00:00+00:00" in status_text.output
    assert "coverage_earliest=2025-03-20T00:00:00+00:00 coverage_latest=2026-06-09T00:00:00+00:00" in status_text.output
    assert "batches_total=3 batches_staged=1 batches_approved=1 batches_quarantined=1" in status_text.output
    assert "batch=batch-quarantined status=quarantined" in status_text.output


def test_ledger_status_reports_active_and_expiring_field_locks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime.now(timezone.utc)

    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.created.v1",
            occurred_at=now - timedelta(days=2),
            recorded_at=now - timedelta(days=2),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
            source_ref=LTDeckRef(file_path="deck1.pptx", deck_date=(now - timedelta(days=2)).date(), slide_number=5),
            dedupe_payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
        ),
        programs_root=programs_root,
    )
    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.created.v1",
            occurred_at=now - timedelta(days=1),
            recorded_at=now - timedelta(days=1),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"milestone_id": "milestone:m2", "name": "Preview", "target_date": "2026-10-15"},
            source_ref=LTDeckRef(file_path="deck2.pptx", deck_date=(now - timedelta(days=1)).date(), slide_number=6),
            dedupe_payload={"milestone_id": "milestone:m2", "name": "Preview", "target_date": "2026-10-15"},
        ),
        programs_root=programs_root,
    )
    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="operator.field_lock.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={
                "entity_id": "milestone:m1",
                "field": "target_date",
                "locked_value": "2026-10-15",
                "valid_until": (now + timedelta(days=5)).isoformat(),
                "override_session_id": "session-expiring",
            },
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now, context="operator.field_lock.v1"),
            dedupe_payload={"entity_id": "milestone:m1", "field": "target_date", "override_session_id": "lock:session-expiring"},
        ),
        programs_root=programs_root,
    )
    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="operator.field_lock.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={
                "entity_id": "milestone:m2",
                "field": "target_date",
                "locked_value": "2026-11-01",
                "valid_until": (now + timedelta(days=20)).isoformat(),
                "override_session_id": "session-stable",
            },
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now, context="operator.field_lock.v1"),
            dedupe_payload={"entity_id": "milestone:m2", "field": "target_date", "override_session_id": "lock:session-stable"},
        ),
        programs_root=programs_root,
    )

    from src.core.ledger.program_views import project_program_events
    project_program_events("acme", programs_root=programs_root)

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["active_lock_count"] == 2
    assert payload["expiring_lock_count"] == 1
    assert payload["expiring_locks"] == ["milestone:m1.target_date"]

    assert status_text.exit_code == 0
    assert "locks_active=2 locks_expiring_within_7d=1 expiring_locks=milestone:m1.target_date" in status_text.output


def test_ledger_status_reports_pending_candidate_age_and_pipeline_counts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime.now(timezone.utc)
    oldest_staged_at = now - timedelta(days=3)

    append_candidate(
        _candidate(
            "cand-oldest",
            pipeline="lt_deck",
            staged_at=oldest_staged_at,
        ),
        programs_root=programs_root,
    )
    append_candidate(
        _candidate(
            "cand-second",
            batch_id="batch-2",
            pipeline="newsletter",
            staged_at=now - timedelta(days=1),
        ),
        programs_root=programs_root,
    )

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["pending_candidates_by_pipeline"] == {"lt_deck": 1, "newsletter": 1}
    assert payload["oldest_active_candidate_staged_at"] == oldest_staged_at.isoformat()
    assert payload["oldest_active_candidate_age_seconds"] is not None
    assert payload["oldest_active_candidate_age_seconds"] >= 3 * 24 * 60 * 60 - 5

    assert status_text.exit_code == 0
    assert "pending_by_pipeline=lt_deck=1,newsletter=1" in status_text.output
    assert f"oldest_active_candidate_staged_at={oldest_staged_at.isoformat()}" in status_text.output


def test_ledger_status_reports_projection_freshness_against_ledger_head(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime.now(timezone.utc)

    first_event = write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.created.v1",
            occurred_at=now - timedelta(days=2),
            recorded_at=now - timedelta(days=2),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
            source_ref=LTDeckRef(file_path="deck1.pptx", deck_date=(now - timedelta(days=2)).date(), slide_number=5),
            dedupe_payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
        ),
        programs_root=programs_root,
    ).envelope

    from src.core.ledger.program_views import project_program_events
    project_program_events("acme", programs_root=programs_root)

    second_event = write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.date_revised.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={"milestone_id": "milestone:m1", "new_target_date": "2026-10-15"},
            source_ref=LTDeckRef(file_path="deck2.pptx", deck_date=now.date(), slide_number=6),
            dedupe_payload={"milestone_id": "milestone:m1", "new_target_date": "2026-10-15"},
        ),
        programs_root=programs_root,
    ).envelope

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["projection_current"] is False
    assert payload["projection_watermark"] == first_event.event_id
    assert payload["ledger_head"] == second_event.event_id

    assert status_text.exit_code == 0
    assert "projection_status=stale" in status_text.output
    assert f"projection_watermark={first_event.event_id}" in status_text.output
    assert f"ledger_head={second_event.event_id}" in status_text.output


def test_ledger_status_reports_chain_head_and_latest_verify_status(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime.now(timezone.utc)

    written_event = write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.created.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
            source_ref=LTDeckRef(file_path="deck.pptx", deck_date=now.date(), slide_number=5),
            dedupe_payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
        ),
        programs_root=programs_root,
    ).envelope

    verify_result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "verify", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    verify_payload = json.loads(verify_result.stdout)

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert verify_result.exit_code == 0
    assert verify_payload["ok"] is True

    assert status_json.exit_code == 0
    assert payload["chain_head"] == written_event.event_id
    assert payload["last_verify_ok"] is True
    assert payload["last_verify_deep"] is False
    assert payload["last_verify_checked_event_count"] == 1
    assert payload["last_verify_at"] is not None
    assert payload["last_verify_age_seconds"] is not None
    assert payload["last_verify_age_seconds"] >= 0

    assert status_text.exit_code == 0
    assert f"chain_head={written_event.event_id}" in status_text.output
    assert "last_verify_ok=True" in status_text.output


def test_ledger_status_reports_open_material_conflicts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    ProgramFactStore("acme", db_root=programs_root.parent).append_fact(
        ProgramFactInput(
            fact_type="fact.conflict",
            natural_key="conflict:commitment:1",
            entity_refs=("COMMIT-1",),
            payload={
                "family": "commitment",
                "description": "ADO due date disagrees with Teams due date.",
                "resolved": False,
                "is_material": True,
            },
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
        )
    )

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["open_conflict_count"] == 1
    assert payload["open_conflict_previews"] == ["commitment: ADO due date disagrees with Teams due date."]

    assert status_text.exit_code == 0
    assert "open_conflicts=1" in status_text.output
    assert "commitment: ADO due date disagrees with Teams due date." in status_text.output


def test_ledger_status_reports_latest_triage_session_summary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-session-1"), programs_root=programs_root)
    append_candidate(_candidate("cand-session-2"), programs_root=programs_root)

    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-session-1",
            kind="approved",
            decided_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
            triage_actor="operator",
            resulting_event_id="evt-1",
        ),
        program_id="acme",
        programs_root=programs_root,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-session-2",
            kind="rejected",
            decided_at=datetime(2026, 6, 12, 10, 1, tzinfo=timezone.utc),
            triage_actor="operator",
            reason="not needed",
        ),
        program_id="acme",
        programs_root=programs_root,
    )

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["latest_triage_decision_at"] == "2026-06-12T10:01:00+00:00"
    assert payload["latest_triage_session_actor"] == "operator"
    assert payload["latest_triage_session_started_at"] == "2026-06-12T10:00:00+00:00"
    assert payload["latest_triage_session_ended_at"] == "2026-06-12T10:01:00+00:00"
    assert payload["latest_triage_session_decision_count"] == 2
    assert payload["latest_triage_session_duration_seconds"] == 60
    assert payload["latest_triage_session_throughput_per_minute"] == 2.0
    assert payload["triage_session_gap_minutes"] == 30

    assert status_text.exit_code == 0
    assert "latest_triage_actor=operator" in status_text.output
    assert "latest_triage_session_decisions=2" in status_text.output
    assert "latest_triage_session_duration_seconds=60" in status_text.output
    assert "latest_triage_throughput_per_minute=2.0" in status_text.output


def test_ledger_status_reports_event_counts_by_type_and_confidence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime.now(timezone.utc)

    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="milestone.created.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
            source_ref=LTDeckRef(file_path="deck-1.pptx", deck_date=now.date(), slide_number=4),
            dedupe_payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
        ),
        programs_root=programs_root,
    )
    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="decision.made.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={
                "decision_id": "decision:d1",
                "title": "Ship GA in Q3",
                "decision_text": "Ship GA in Q3 after storage signoff",
                "decided_by": ["operator"],
            },
            source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now, context="manual"),
            dedupe_payload={
                "decision_id": "decision:d1",
                "decision_text": "Ship GA in Q3 after storage signoff",
            },
        ),
        programs_root=programs_root,
    )

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["event_count"] == 2
    assert payload["event_count_by_type"] == {"decision.made.v1": 1, "milestone.created.v1": 1}
    assert payload["event_count_by_confidence"] == {
        ConfidenceTier.OPERATOR_CONFIRMED.value: 1,
        ConfidenceTier.SOURCE_AUTHORITATIVE.value: 1,
    }

    assert status_text.exit_code == 0
    assert "events_total=2" in status_text.output
    assert "events_by_type=decision.made.v1=1,milestone.created.v1=1" in status_text.output
    assert "events_by_confidence=operator_confirmed=1,source_authoritative=1" in status_text.output


def test_ledger_status_reports_vault_sizes_and_deep_verify_snapshots(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    now = datetime.now(timezone.utc)

    store_evidence_vault_bytes(
        program_id="acme",
        content_bytes=b"evidence-bytes",
        content_type="text/plain",
        original_filename="evidence.txt",
        origin_path="origin/evidence.txt",
        programs_root=programs_root,
    )

    knowledge_source = tmp_path / "knowledge-source.md"
    knowledge_source.write_text("# Knowledge\n", encoding="utf-8")
    ingest_knowledge_source(knowledge_source, scope="program:acme", programs_root=programs_root, ingested_at=now)

    write_ledger_verify_status(
        "acme",
        verified_at=now,
        ok=True,
        deep=True,
        checked_event_count=0,
        programs_root=programs_root,
    )
    write_shared_vault_verify_status(
        verified_at=now,
        ok=True,
        issue_records=(),
        program_id="acme",
        programs_root=programs_root,
    )

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["evidence_vault_file_count"] == 1
    assert payload["evidence_vault_total_bytes"] == len(b"evidence-bytes")
    assert payload["evidence_vault_last_deep_verify_ok"] is True
    assert payload["evidence_vault_last_deep_verify_at"] == now.isoformat()
    assert payload["evidence_vault_last_deep_verify_age_seconds"] is not None
    assert payload["knowledge_vault_file_count"] == 1
    assert payload["knowledge_vault_total_bytes"] == knowledge_source.stat().st_size
    assert payload["knowledge_vault_last_deep_verify_ok"] is True
    assert payload["knowledge_vault_last_deep_verify_at"] == now.isoformat()
    assert payload["knowledge_vault_last_deep_verify_age_seconds"] is not None

    assert status_text.exit_code == 0
    assert "evidence_vault_files=1" in status_text.output
    assert "knowledge_vault_files=1" in status_text.output


def test_ledger_status_reports_backfill_batches_by_tier(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    append_candidate(_candidate("cand-tier-a", batch_id="batch-tier-a", pipeline="lt_deck"), programs_root=programs_root)
    append_candidate(_candidate("cand-tier-b", batch_id="batch-tier-b", pipeline="newsletter"), programs_root=programs_root)
    append_candidate(_candidate("cand-tier-c", batch_id="batch-tier-c", pipeline="kb_extract"), programs_root=programs_root)

    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-tier-b",
            kind="approved",
            decided_at=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
            triage_actor="operator",
            batch_id="batch-tier-b",
            resulting_event_id="evt-tier-b",
        ),
        program_id="acme",
        programs_root=programs_root,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-tier-c",
            kind="rejected",
            decided_at=datetime(2026, 6, 12, 10, 1, tzinfo=timezone.utc),
            triage_actor="operator",
            batch_id="batch-tier-c",
            reason="quarantined: malformed extracted batch",
        ),
        program_id="acme",
        programs_root=programs_root,
    )

    status_json = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    payload = json.loads(status_json.stdout)

    status_text = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root)],
    )

    assert status_json.exit_code == 0
    assert payload["backfill_batches_by_tier"] == {"tier_a": 1, "tier_b": 1, "tier_c": 1}

    assert status_text.exit_code == 0
    assert "backfill_batches_by_tier=tier_a=1,tier_b=1,tier_c=1" in status_text.output


def test_ledger_triage_expire_skips_materializes_rejection_records(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-expired"), programs_root=programs_root)
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id="cand-expired",
            kind="skipped",
            decided_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            triage_actor="operator",
            batch_id="batch-1",
            reason="waiting for more evidence",
        ),
        program_id="acme",
        programs_root=programs_root,
    )

    expire_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "expire-skips",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert expire_result.exit_code == 0
    assert "Materialized 1 expired skipped candidate(s)." in expire_result.stdout
    assert active_count("acme", programs_root=programs_root) == 0

    decisions = load_triage_decisions("acme", programs_root=programs_root)
    assert [decision.kind for decision in decisions] == ["skipped", "rejected"]
    assert decisions[-1].reason == "skip expired after 90 days"

    second_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "expire-skips",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert second_result.exit_code == 0
    assert "No expired skipped candidates to materialize." in second_result.stdout



def test_ledger_replay_builds_projection_and_verify_deep_checks_projection_parity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-3"), programs_root=programs_root)

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-3",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0

    replay_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "replay",
            "--program",
            "acme",
            "--reindex",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    replay_payload = json.loads(replay_result.stdout)

    assert replay_result.exit_code == 0
    assert replay_payload["event_count"] == 2
    assert replay_payload["reindexed_event_count"] == 2
    assert get_current_projection_path("acme", programs_root=programs_root).exists()

    verify_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "verify",
            "--program",
            "acme",
            "--deep",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    verify_payload = json.loads(verify_result.stdout)

    assert verify_result.exit_code == 0
    assert verify_payload["ok"] is True
    assert verify_payload["deep_projection_match"] is True
    assert verify_payload["missing_from_index"] == []
    assert verify_payload["extra_in_index"] == []
    assert verify_payload["evidence_vault_issues"] == []
    assert verify_payload["knowledge_vault_issues"] == []


def test_ledger_replay_uses_incremental_projection(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    captured: dict[str, object] = {}
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="operator",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=date(2026, 1, 1), slide_number=1),
    )
    write_event(event, programs_root=programs_root)

    def _fake_incremental(program_id, events, *, projection_path, programs_root, as_of=None, knowledge_as_of=None):
        event_tuple = tuple(events)
        captured["program_id"] = program_id
        captured["event_count"] = len(event_tuple)
        captured["projection_path"] = projection_path
        captured["programs_root"] = programs_root
        captured["as_of"] = as_of
        captured["knowledge_as_of"] = knowledge_as_of
        return SimpleNamespace(
            program_id=program_id,
            projection_path=projection_path,
            event_watermark=event_tuple[-1].event_id,
            event_count=len(event_tuple),
            coverage_earliest="2026-01-01T00:00:00+00:00",
            coverage_latest="2026-01-01T00:00:00+00:00",
        )

    monkeypatch.setattr("src.commands.ledger.project_events_incremental_to_sqlite", _fake_incremental)

    replay_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "replay",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    replay_payload = json.loads(replay_result.stdout)

    assert replay_result.exit_code == 0
    assert captured["program_id"] == "acme"
    assert captured["event_count"] == 1
    assert captured["projection_path"] == get_current_projection_path("acme", programs_root=programs_root)
    assert captured["programs_root"] == programs_root
    assert replay_payload["event_count"] == 1


def test_ledger_verify_deep_fails_on_missing_evidence_vault_entry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.AI_EXTRACTED,
        actor="workiq_discovery",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=WorkIQRef(
            artifact_id="mail-123",
            artifact_kind="email_excerpt",
            retrieved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            query="What changed?",
            vault_hash="sha256:deadbeef",
        ),
    )
    write_event(event, programs_root=programs_root)

    verify_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "verify",
            "--program",
            "acme",
            "--deep",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    verify_payload = json.loads(verify_result.stdout)

    assert verify_result.exit_code == 4
    assert verify_payload["ok"] is False
    assert verify_payload["evidence_vault_issues"] == [
        {
            "kind": "missing",
            "ref_owner_id": verify_payload["evidence_vault_issues"][0]["ref_owner_id"],
            "ref_role": "source_ref",
            "vault_hash": "sha256:deadbeef",
        }
    ]
    assert verify_payload["knowledge_vault_issues"] == []


def test_ledger_verify_deep_fails_on_missing_knowledge_vault_reference(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2026, 1, 1, tzinfo=timezone.utc).date()),
        ),
        programs_root=programs_root,
    )

    runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "replay",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    append_claim_revision(
        scope="domain:storage-platform",
        subject="sku_generation:gen9",
        predicate="first_deployment",
        value="2025-H2",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_until=None,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=KnowledgeDocumentRef(
            vault_hash="sha256:missing-vault-hash",
            original_filename="demo.md",
            origin_kind="knowledge_markdown",
            origin_path="demo.md",
            ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            section="document",
        ),
        knowledge_root=programs_root.parent / "knowledge",
        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    verify_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "verify",
            "--program",
            "acme",
            "--deep",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    verify_payload = json.loads(verify_result.stdout)

    assert verify_result.exit_code == 1
    assert verify_payload["ok"] is False
    assert verify_payload["evidence_vault_issues"] == []
    assert verify_payload["knowledge_vault_issues"] == [{"kind": "missing_claim_ref", "count": 1}]


def test_ledger_verify_deep_reports_missing_knowledge_source_registry_reference(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    write_event(
        build_event_envelope(
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2026, 1, 1, tzinfo=timezone.utc).date()),
        ),
        programs_root=programs_root,
    )

    runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "replay",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
        ],
    )

    source_path = tmp_path / "gen9.md"
    source_path.write_text("# Gen9\n", encoding="utf-8")
    entry = ingest_knowledge_source(source_path, scope="domain:storage-platform", programs_root=programs_root)
    entry.content_path.unlink()
    entry.metadata_path.unlink()

    verify_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "verify",
            "--program",
            "acme",
            "--deep",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    verify_payload = json.loads(verify_result.stdout)

    assert verify_result.exit_code == 1
    assert verify_payload["ok"] is False
    assert verify_payload["evidence_vault_issues"] == []
    assert verify_payload["knowledge_vault_issues"] == [{"kind": "missing_source_record", "count": 1}]
    verify_status = load_shared_vault_verify_status(programs_root=programs_root)
    assert verify_status is not None
    assert verify_status.ok is False
    assert verify_status.program_id == "acme"
    assert verify_status.issue_records == ({"kind": "missing_source_record", "count": 1},)


def test_ledger_gaps_lists_projection_backed_gap_rows(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    record_discovery_run(
        "acme",
        DiscoveryRunResult(
            pipeline="lt_deck",
            batch_id="batch-77",
            candidates_written=0,
            gaps=(
                GapDetail(
                    gap_kind="missing_series_registration",
                    window_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    window_end=datetime(2026, 6, 7, tzinfo=timezone.utc),
                    detail="Missing series registration for LT deck ingest.",
                ),
            ),
            heartbeat=False,
        ),
        programs_root=programs_root,
    )

    gaps_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "gaps",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    gaps_payload = json.loads(gaps_result.stdout)

    assert gaps_result.exit_code == 0
    assert gaps_payload["gaps"] == [
        {
            "acknowledged": 0,
            "detail": "Missing series registration for LT deck ingest.",
            "event_id": read_events("acme", programs_root=programs_root)[0].event_id,
            "gap_kind": "missing_series_registration",
            "pipeline": "lt_deck",
            "window_end": "2026-06-07T00:00:00+00:00",
            "window_start": "2026-06-01T00:00:00+00:00",
        }
    ]


def test_ledger_gap_acknowledgement_persists_across_replay(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    record_discovery_run(
        "acme",
        DiscoveryRunResult(
            pipeline="lt_deck",
            batch_id="batch-gap-ack",
            candidates_written=0,
            gaps=(
                GapDetail(
                    gap_kind="missing_series_registration",
                    window_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    window_end=datetime(2026, 6, 7, tzinfo=timezone.utc),
                    detail="Missing series registration for LT deck ingest.",
                ),
            ),
            heartbeat=False,
        ),
        programs_root=programs_root,
    )
    gap_event_id = read_events("acme", programs_root=programs_root)[0].event_id

    ack_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "gaps",
            "--program",
            "acme",
            "--ack",
            gap_event_id,
            "--actor",
            "operator",
            "--all",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    ack_payload = json.loads(ack_result.stdout)

    assert ack_result.exit_code == 0
    assert ack_payload["gaps"][0]["acknowledged"] == 1

    replay_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "replay",
            "--program",
            "acme",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert replay_result.exit_code == 0

    replayed_gaps = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))["gaps"]
    assert replayed_gaps[0]["event_id"] == gap_event_id
    assert replayed_gaps[0]["acknowledged"] == 1


def test_ledger_history_surfaces_shadow_annotations(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    first_write = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "risk.raised.v1",
            "--occurred-at",
            "2025-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps({"risk_id": "risk:r1", "title": "Old risk", "severity": "medium"}),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2025-03-20", "slide_number": 9}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )
    second_write = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "risk.raised.v1",
            "--occurred-at",
            "2025-01-07T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps({"risk_id": "risk:r1", "title": "New risk", "severity": "high"}),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2025-03-20", "slide_number": 9}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert first_write.exit_code == 0
    assert second_write.exit_code == 0

    history_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "history",
            "--program",
            "acme",
            "--entity",
            "risk:r1",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    history_payload = json.loads(history_result.stdout)

    assert history_result.exit_code == 0


def test_ledger_write_does_not_bridge_to_fact_store_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("VERTEX_LEDGER_FACT_BRIDGE", raising=False)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "risk.raised.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps({"risk_id": "risk:r-bridge-off", "title": "Bridge off", "severity": "medium"}),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("risk.entry",))

    assert result.exit_code == 0
    assert snapshot.facts == ()


def test_ledger_write_bridges_risk_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "risk.raised.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "risk_id": "risk:r-bridge-on",
                    "title": "Bridge on",
                    "severity": "high",
                    "description": "Fact bridge should materialize this risk.",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("risk.entry",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["id"] == "risk:r-bridge-on"
    assert snapshot.facts[0].created_by == "ledger_bridge"
    assert snapshot.facts[0].write_authority == "bridge"


def test_ledger_write_bridges_decision_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "decision.made.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "decision_id": "decision:d-bridge-on",
                    "title": "Bridge decision",
                    "decision_text": "Bridge should materialize this decision.",
                    "decided_by": ["operator"],
                    "forum": "LT",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("decision.entry",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["id"] == "decision:d-bridge-on"
    assert snapshot.facts[0].created_by == "ledger_bridge"


def test_ledger_write_bridges_assumption_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "assumption.stated.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "assumption_id": "assumption:a-bridge-on",
                    "statement": "Capacity holds",
                    "validation_plan": "Load test",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("assumption.entry",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["id"] == "assumption:a-bridge-on"
    assert snapshot.facts[0].created_by == "ledger_bridge"


def test_ledger_write_bridges_milestone_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "milestone.created.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "milestone_id": "milestone:m-bridge-on",
                    "name": "Pilot ready",
                    "target_date": "2026-07-01",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("milestone.entry",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["id"] == "milestone:m-bridge-on"
    assert snapshot.facts[0].created_by == "ledger_bridge"


def test_ledger_write_bridges_dependency_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "dependency.declared.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "dependency_id": "dependency:d-bridge-on",
                    "from_entity": "workstream:ws-bridge",
                    "to_entity": "milestone:m-bridge",
                    "description": "Launch depends on GA milestone",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("dependency.link",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["id"] == "dependency:d-bridge-on"
    assert snapshot.facts[0].created_by == "ledger_bridge"


def test_ledger_write_bridges_workstream_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "workstream.created.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "workstream_id": "ws-bridge-on",
                    "name": "Bridge Workstream",
                    "owner_person_id": "person:alice",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("workstream.entry",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["id"] == "ws-bridge-on"
    assert snapshot.facts[0].payload["status"] == "active"
    assert snapshot.facts[0].created_by == "ledger_bridge"


def test_ledger_write_bridges_commitment_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "entities.yaml").write_text(
        "entities:\n"
        "  - entity_id: 'person:alice'\n"
        "    entity_type: 'person'\n"
        "    canonical_name: 'Alice Vance'\n"
        "    aliases: ['alice']\n"
        "    scope: 'program'\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "commitment.made.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "commitment_id": "commitment:c-bridge-on",
                    "text": "Ship pilot",
                    "owner_person_id": "person:alice",
                    "due_date": "2026-07-01",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("commitment.entry",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["commitment_id"] == "commitment:c-bridge-on"
    assert snapshot.facts[0].payload["direction"] == "outbound"
    assert snapshot.facts[0].created_by == "ledger_bridge"


def test_ledger_write_bridges_commitment_event_to_fact_store_when_flag_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VERTEX_LEDGER_FACT_BRIDGE", "1")
    programs_root = tmp_path / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "entities.yaml").write_text(
        "entities:\n"
        "  - entity_id: 'person:alice'\n"
        "    entity_type: 'person'\n"
        "    canonical_name: 'Alice Vance'\n"
        "    aliases: []\n"
        "    scope: 'program'\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "commitment.made.v1",
            "--occurred-at",
            "2026-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps(
                {
                    "commitment_id": "commitment:c-bridge-on",
                    "text": "Ship pilot exit",
                    "owner_person_id": "person:alice",
                    "due_date": "2026-07-01",
                }
            ),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2026-01-05"}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    snapshot = load_program_facts("acme", db_root=programs_root.parent, programs_root=programs_root, fact_types=("commitment.entry",))

    assert result.exit_code == 0
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].payload["commitment_id"] == "commitment:c-bridge-on"
    assert snapshot.facts[0].payload["direction"] == "outbound"
    assert snapshot.facts[0].created_by == "ledger_bridge"


def test_ledger_history_surfaces_orphan_annotations(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    create_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "risk.raised.v1",
            "--occurred-at",
            "2025-01-05T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps({"risk_id": "risk:r1", "title": "Risk one", "severity": "high"}),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2025-03-20", "slide_number": 9}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )
    update_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "risk.status_changed.v1",
            "--occurred-at",
            "2025-01-07T00:00:00+00:00",
            "--actor",
            "import",
            "--payload-json",
            json.dumps({"risk_id": "risk:r1", "new_status": "active"}),
            "--source-ref-json",
            json.dumps({"ref_type": "lt_deck", "file_path": "deck.pptx", "deck_date": "2025-03-20", "slide_number": 9}),
            "--confidence",
            "source_authoritative",
            "--temporal-confidence",
            "exact",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert create_result.exit_code == 0
    assert update_result.exit_code == 0

    created_event_id = read_events("acme", programs_root=programs_root)[0].event_id
    tombstone_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "correct",
            "--program",
            "acme",
            "--event-id",
            created_event_id,
            "--actor",
            "operator",
            "--reason",
            "invalid creation",
            "--corrected-payload-json",
            "null",
            "--programs-root",
            str(programs_root),
        ],
    )

    history_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "history",
            "--program",
            "acme",
            "--entity",
            "risk:r1",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    history_payload = json.loads(history_result.stdout)
    tombstone_event_id = read_events("acme", programs_root=programs_root)[-1].event_id

    assert tombstone_result.exit_code == 0
    assert history_result.exit_code == 0
    assert len(history_payload["events"]) == 2
    assert history_payload["events"][0]["orphaned_by"] is None
    assert history_payload["events"][1]["orphaned_by"] == tombstone_event_id


def test_ledger_diff_surfaces_projection_table_changes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-4"), programs_root=programs_root)
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-4",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0

    diff_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "diff",
            "--program",
            "acme",
            "--from",
            "2025-03-01T00:00:00+00:00",
            "--to",
            "2025-04-01T00:00:00+00:00",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    diff_payload = json.loads(diff_result.stdout)

    assert diff_result.exit_code == 0
    assert any(table["table"] == "proj_milestone" for table in diff_payload["tables"])


def test_ledger_export_writes_jsonl_and_sqlite_outputs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-5"), programs_root=programs_root)
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-5",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0

    jsonl_path = tmp_path / "exports" / "ledger.jsonl"
    sqlite_path = tmp_path / "exports" / "ledger.sqlite3"
    export_jsonl_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "export",
            "--program",
            "acme",
            "--format",
            "jsonl",
            "--out",
            str(jsonl_path),
            "--programs-root",
            str(programs_root),
        ],
    )
    export_sqlite_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "export",
            "--program",
            "acme",
            "--format",
            "sqlite",
            "--out",
            str(sqlite_path),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert export_jsonl_result.exit_code == 0
    assert export_sqlite_result.exit_code == 0
    assert jsonl_path.exists()
    assert sqlite_path.exists()
    assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 2
    assert sqlite_path.stat().st_size > 0


def test_ledger_write_and_tombstone_correct_update_projection(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    write_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "milestone.date_revised.v1",
            "--occurred-at",
            "2025-03-20T00:00:00+00:00",
            "--actor",
            "operator",
            "--payload-json",
            '{"milestone_id":"milestone:m1","new_target_date":"2025-09-30"}',
            "--source-ref-json",
            '{"ref_type":"operator_assertion","asserted_by":"operator","asserted_at":"2026-06-11T00:00:00+00:00"}',
            "--programs-root",
            str(programs_root),
        ],
    )

    assert write_result.exit_code == 0
    written_event_id = read_events("acme", programs_root=programs_root)[0].event_id

    correct_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "correct",
            "--program",
            "acme",
            "--event-id",
            written_event_id,
            "--actor",
            "operator",
            "--reason",
            "tombstone bad write",
            "--corrected-payload-json",
            "null",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert correct_result.exit_code == 0
    projection = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))
    assert projection["proj_milestone"] == []


def test_ledger_write_touching_locked_field_stages_candidate_instead_of_event(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-locked-write"), programs_root=programs_root)

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-locked-write",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    write_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "milestone.date_revised.v1",
            "--occurred-at",
            "2025-03-21T00:00:00+00:00",
            "--actor",
            "operator",
            "--payload-json",
            '{"milestone_id":"milestone:m1","new_target_date":"2025-11-30"}',
            "--source-ref-json",
            '{"ref_type":"operator_assertion","asserted_by":"operator","asserted_at":"2026-06-11T00:00:00+00:00"}',
            "--programs-root",
            str(programs_root),
        ],
    )

    assert write_result.exit_code == 0
    assert "Locked field conflict; staged candidate" in write_result.stdout
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "milestone.date_revised.v1",
        "discovery.candidate_approved.v1",
        "operator.field_lock.v1",
    ]
    active = load_pending_candidates("acme", programs_root=programs_root)
    diverted = active[-1]
    assert diverted.proposed_event_type == "milestone.date_revised.v1"
    assert diverted.pipeline == "operator_direct_write"
    assert diverted.proposed_payload["new_target_date"] == "2025-11-30"
    assert diverted.entity_resolution[0].resolved_entity_id == "milestone:m1"


def test_ledger_correct_touching_locked_field_stages_candidate_instead_of_event(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-locked-correct"), programs_root=programs_root)

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-locked-correct",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0
    written_event_id = read_events("acme", programs_root=programs_root)[0].event_id

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    correct_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "correct",
            "--program",
            "acme",
            "--event-id",
            written_event_id,
            "--actor",
            "operator",
            "--reason",
            "needs lock-aware review",
            "--corrected-payload-json",
            "null",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert correct_result.exit_code == 0
    assert "Locked field conflict; staged candidate" in correct_result.stdout
    events = read_events("acme", programs_root=programs_root)
    assert [event.event_type for event in events] == [
        "milestone.date_revised.v1",
        "discovery.candidate_approved.v1",
        "operator.field_lock.v1",
    ]
    diverted = load_pending_candidates("acme", programs_root=programs_root)[-1]
    assert diverted.proposed_event_type == "operator.correction.v1"
    assert diverted.pipeline == "operator_direct_write"
    assert diverted.proposed_payload["corrects_event_id"] == written_event_id

    batch_status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "batch-status",
            "--program",
            "acme",
            "--batch-id",
            diverted.batch_id,
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    assert batch_status_result.exit_code == 0
    batch_status = json.loads(batch_status_result.stdout)
    assert batch_status["lock_conflict_gate"] is False
    assert batch_status["lock_conflict_candidates"] == [diverted.candidate_id]


def test_ledger_write_paths_pass_grounded_in_validator(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.commands.ledger.project_program_events", lambda *_args, **_kwargs: None)

    observed: list[object] = []

    def _fake_write_event(envelope, *, grounded_in_validator=None, **_kwargs):
        observed.append(grounded_in_validator)
        return SimpleNamespace(envelope=envelope)

    monkeypatch.setattr("src.commands.ledger.write_event", _fake_write_event)

    programs_root = tmp_path / "programs"

    write_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "write",
            "--program",
            "acme",
            "--event-type",
            "risk.raised.v1",
            "--occurred-at",
            "2025-03-20T00:00:00+00:00",
            "--actor",
            "operator",
            "--payload-json",
            '{"risk_id":"risk:r1","title":"Risk one","severity":"high","grounded_in":["claim-1"]}',
            "--source-ref-json",
            '{"ref_type":"operator_assertion","asserted_by":"operator","asserted_at":"2026-06-11T00:00:00+00:00"}',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert write_result.exit_code == 0

    correct_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "correct",
            "--program",
            "acme",
            "--event-id",
            "evt-1",
            "--actor",
            "operator",
            "--reason",
            "fix",
            "--corrected-payload-json",
            "null",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert correct_result.exit_code == 0

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    unlock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "unlock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert unlock_result.exit_code == 0

    candidate = _candidate("cand-validator")
    _write_candidate_event(candidate, actor="operator", programs_root=programs_root)
    _write_candidate_audit_event(
        candidate,
        actor="operator",
        event_type="discovery.candidate_rejected.v1",
        payload={"candidate_id": candidate.candidate_id, "triage_actor": "operator", "reason": "bad extraction"},
        programs_root=programs_root,
    )

    assert len(observed) == 6
    assert all(callable(validator) for validator in observed)


def test_ledger_lock_and_unlock_pin_and_restore_projection_field(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-6"), programs_root=programs_root)
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-6",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-10-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0
    locked_projection = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))
    assert locked_projection["proj_milestone"][0]["target_date"] == "2025-10-31"

    unlock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "unlock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert unlock_result.exit_code == 0
    unlocked_projection = canonical_projection_dump(get_current_projection_path("acme", programs_root=programs_root))
    assert unlocked_projection["proj_milestone"][0]["target_date"] == "2025-09-30"


def test_ledger_import_dry_run_reports_sample_without_staging(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "legacy-events.jsonl"
    source_path.write_text(
        json.dumps(
            {
                "event_id": "01TESTEVENT",
                "program_id": "legacy",
                "event_type": "milestone.date_revised.v1",
                "occurred_at": "2025-03-20T00:00:00+00:00",
                "recorded_at": "2026-06-11T00:00:00+00:00",
                "temporal_confidence": "approximate",
                "confidence": "source_authoritative",
                "actor": "import",
                "payload": {"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
                "source_ref": {
                    "ref_type": "lt_deck",
                    "file_path": "deck.pptx",
                    "deck_date": "2025-03-20",
                    "slide_number": 9,
                    "slide_title": None,
                    "vault_hash": None,
                },
                "corroborating_refs": [],
                "prev_event_hash": "sha256:prev",
                "content_hash": "sha256:content",
            }
        ) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "import",
            "--program",
            "acme",
            "--source",
            str(source_path),
            "--dry-run",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "DRY-RUN: would stage 1 candidates" in result.stdout
    assert load_pending_candidates("acme", programs_root=programs_root) == ()


def test_ledger_import_stages_candidates_and_prints_batch_review_hint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_path = tmp_path / "legacy-events.jsonl"
    source_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "01TESTEVENT1",
                        "program_id": "legacy",
                        "event_type": "milestone.date_revised.v1",
                        "occurred_at": "2025-03-20T00:00:00+00:00",
                        "recorded_at": "2026-06-11T00:00:00+00:00",
                        "temporal_confidence": "approximate",
                        "confidence": "source_authoritative",
                        "actor": "import",
                        "payload": {"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
                        "source_ref": {
                            "ref_type": "lt_deck",
                            "file_path": "deck.pptx",
                            "deck_date": "2025-03-20",
                            "slide_number": 9,
                            "slide_title": None,
                            "vault_hash": None,
                        },
                        "corroborating_refs": [],
                        "prev_event_hash": "sha256:prev",
                        "content_hash": "sha256:content",
                    }
                ),
                json.dumps(
                    {
                        "event_id": "01TESTEVENT2",
                        "program_id": "legacy",
                        "event_type": "milestone.date_revised.v1",
                        "occurred_at": "2025-04-20T00:00:00+00:00",
                        "recorded_at": "2026-06-11T00:00:00+00:00",
                        "temporal_confidence": "approximate",
                        "confidence": "source_authoritative",
                        "actor": "import",
                        "payload": {"milestone_id": "milestone:m2", "new_target_date": "2025-10-30"},
                        "source_ref": {
                            "ref_type": "lt_deck",
                            "file_path": "deck-2.pptx",
                            "deck_date": "2025-04-20",
                            "slide_number": 3,
                            "slide_title": None,
                            "vault_hash": None,
                        },
                        "corroborating_refs": [],
                        "prev_event_hash": "sha256:prev2",
                        "content_hash": "sha256:content2",
                    }
                ),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "import",
            "--program",
            "acme",
            "--source",
            str(source_path),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "Staged 2 candidates in batch" in result.stdout
    assert "vertex ledger triage list --program acme --batch-id" in result.stdout
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 2
    assert {candidate.pipeline for candidate in pending} == {"backfill_import"}
    assert len({candidate.batch_id for candidate in pending}) == 1


def test_ledger_batch_status_reports_resolution_and_sample_gates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-b1", batch_id="batch-qg", resolved_entity_id="milestone:m1"), programs_root=programs_root)
    append_candidate(_candidate("cand-b2", batch_id="batch-qg", resolved_entity_id=None), programs_root=programs_root)

    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-b1",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0

    status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "batch-status",
            "--program",
            "acme",
            "--batch-id",
            "batch-qg",
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    payload = json.loads(status_result.stdout)

    assert status_result.exit_code == 0
    assert payload["total_candidates"] == 2
    assert payload["approved_sample_count"] == 1
    assert payload["required_sample_count"] == 2
    assert payload["entity_resolution_rate"] == 0.5
    assert payload["entity_resolution_gate"] is False
    assert payload["sample_gate"] is False
    assert payload["lock_conflict_gate"] is True
    assert payload["lock_conflict_gate_evaluated"] is True
    assert payload["lock_conflict_candidates"] == []


def test_ledger_quarantine_batch_rejects_pending_batch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-q1", batch_id="batch-drop"), programs_root=programs_root)
    append_candidate(_candidate("cand-q2", batch_id="batch-drop", resolved_entity_id="milestone:m2", milestone_id="milestone:m2"), programs_root=programs_root)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "quarantine-batch",
            "--program",
            "acme",
            "--batch-id",
            "batch-drop",
            "--actor",
            "operator",
            "--reason",
            "bad backfill batch",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "Quarantined 2 candidates from batch batch-drop." in result.stdout
    assert active_count("acme", programs_root=programs_root, batch_id="batch-drop") == 0
    decisions = load_triage_decisions("acme", programs_root=programs_root)
    assert {decision.kind for decision in decisions} == {"rejected"}
    assert all(decision.reason == "quarantined: bad backfill batch" for decision in decisions)


def test_ledger_quarantine_batch_blocks_post_approval_cleanup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-approved", batch_id="batch-approved"), programs_root=programs_root)
    approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "approve",
            "--program",
            "acme",
            "--candidate",
            "cand-approved",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert approve_result.exit_code == 0

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "quarantine-batch",
            "--program",
            "acme",
            "--batch-id",
            "batch-approved",
            "--actor",
            "operator",
            "--reason",
            "bad backfill batch",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0
    assert "already contains approved candidates" in result.stdout


def test_ledger_backfill_quarantine_batch_reuses_quarantine_flow(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-bf-q1", batch_id="batch-backfill"), programs_root=programs_root)
    append_candidate(
        _candidate(
            "cand-bf-q2",
            batch_id="batch-backfill",
            resolved_entity_id="milestone:m2",
            milestone_id="milestone:m2",
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "backfill",
            "--program",
            "acme",
            "--quarantine-batch",
            "batch-backfill",
            "--actor",
            "operator",
            "--reason",
            "discard staged deck batch",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "Quarantined 2 candidates from batch batch-backfill." in result.stdout
    assert active_count("acme", programs_root=programs_root, batch_id="batch-backfill") == 0


def test_ledger_backfill_quarantine_batch_rejects_staging_flags(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "Monthly_LT_Review"
    source_dir.mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "backfill",
            "--program",
            "acme",
            "--source-dir",
            str(source_dir),
            "--quarantine-batch",
            "batch-backfill",
            "--actor",
            "operator",
            "--reason",
            "discard staged deck batch",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 2


def test_ledger_batch_approve_is_resumable_and_skips_already_approved_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    for index in range(10):
        append_candidate(
            _candidate(
                f"cand-resume-{index}",
                batch_id="batch-resume",
                resolved_entity_id=f"milestone:m{index}",
                milestone_id=f"milestone:m{index}",
            ),
            programs_root=programs_root,
        )
    for index in range(10, 12):
        append_candidate(
            _candidate(
                f"cand-resume-{index}",
                batch_id="batch-resume",
                resolved_entity_id=f"milestone:m{index}",
                milestone_id=f"milestone:m{index}",
            ),
            programs_root=programs_root,
        )

    for index in range(10):
        approve_result = runner.invoke(
            app,
            [
                "--no-catchup",
                "ledger",
                "triage",
                "approve",
                "--program",
                "acme",
                "--candidate",
                f"cand-resume-{index}",
                "--actor",
                "operator",
                "--programs-root",
                str(programs_root),
            ],
        )
        assert approve_result.exit_code == 0

    batch_approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "batch-approve",
            "--program",
            "acme",
            "--batch-id",
            "batch-resume",
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert batch_approve_result.exit_code == 0
    assert "Batch approval progress" in batch_approve_result.stdout
    assert "Approved 2 candidates from batch batch-resume." in batch_approve_result.stdout
    decisions = [decision for decision in load_triage_decisions("acme", programs_root=programs_root) if decision.batch_id == "batch-resume"]
    assert len([decision for decision in decisions if decision.kind == "approved"]) == 12
    assert active_count("acme", programs_root=programs_root, batch_id="batch-resume") == 0


def test_ledger_batch_approve_blocks_on_lock_conflicts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-lock-1", batch_id="batch-lock", resolved_entity_id="milestone:m1"), programs_root=programs_root)
    append_candidate(_candidate("cand-lock-2", batch_id="batch-lock", resolved_entity_id="milestone:m2", milestone_id="milestone:m2"), programs_root=programs_root)

    for candidate_id in ("cand-lock-1", "cand-lock-2"):
        approve_result = runner.invoke(
            app,
            [
                "--no-catchup",
                "ledger",
                "triage",
                "approve",
                "--program",
                "acme",
                "--candidate",
                candidate_id,
                "--actor",
                "operator",
                "--programs-root",
                str(programs_root),
            ],
        )
        assert approve_result.exit_code == 0

    import_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "import",
            "--program",
            "acme",
            "--source",
            str(_write_import_source(tmp_path, batch_name="batch-lock-import")),
            "--programs-root",
            str(programs_root),
        ],
    )
    assert import_result.exit_code == 0
    batch_id = load_pending_candidates("acme", programs_root=programs_root)[0].batch_id

    lock_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "lock",
            "--program",
            "acme",
            "--entity-id",
            "milestone:m1",
            "--field",
            "target_date",
            "--actor",
            "operator",
            "--locked-value-json",
            '"2025-12-31"',
            "--programs-root",
            str(programs_root),
        ],
    )
    assert lock_result.exit_code == 0

    batch_status_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "batch-status",
            "--program",
            "acme",
            "--batch-id",
            batch_id,
            "--programs-root",
            str(programs_root),
            "--format",
            "json",
        ],
    )
    batch_status = json.loads(batch_status_result.stdout)
    assert batch_status["lock_conflict_gate"] is False
    assert batch_status["lock_conflict_gate_evaluated"] is True

    batch_approve_result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "batch-approve",
            "--program",
            "acme",
            "--batch-id",
            batch_id,
            "--actor",
            "operator",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert batch_approve_result.exit_code != 0
    assert "has unresolved lock conflicts" in batch_approve_result.stdout


def test_ledger_backfill_dry_run_recursively_enumerates_year_dirs_and_excludes_root_copy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "Monthly_LT_Review"
    (source_dir / "2020").mkdir(parents=True)
    (source_dir / "2021").mkdir(parents=True)
    (source_dir / "2022").mkdir(parents=True)
    (source_dir / "2024").mkdir(parents=True)
    (source_dir / "2026").mkdir(parents=True)
    (source_dir / "2020" / "2020-11-03 Acme Review with Jordan and Tom.pptx").write_text("deck", encoding="utf-8")
    (source_dir / "2021" / "2021-01-15 Acme Review.pptx").write_text("deck", encoding="utf-8")
    (source_dir / "2022" / "20220110 - LT Update.pptx").write_text("deck", encoding="utf-8")
    (source_dir / "2024" / "202401 - LT Update.pptx").write_text("deck", encoding="utf-8")
    (source_dir / "2026" / "202603-01 - Acme LT Update.pptx").write_text("deck", encoding="utf-8")
    (source_dir / "2026-05-28- Acme LT Update_draft.pptx").write_text("working copy", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "backfill",
            "--program",
            "acme",
            "--source-dir",
            str(source_dir),
            "--from",
            "2020",
            "--dry-run",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "DRY-RUN: would stage 5 LT deck candidates" in result.stdout
    assert "2020-11-03 Acme Review with Jordan and Tom" in result.stdout
    assert "2026-05-28- Acme LT Update_draft" not in result.stdout
    assert load_pending_candidates("acme", programs_root=programs_root) == ()


def test_ledger_backfill_stages_artifact_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    source_dir = tmp_path / "Monthly_LT_Review"
    (source_dir / "2020").mkdir(parents=True)
    (source_dir / "2020" / "2020-11-03 Acme Review with Jordan and Tom.pptx").write_text("deck", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "backfill",
            "--program",
            "acme",
            "--source-dir",
            str(source_dir),
            "--from",
            "2020",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "Staged 1 LT deck candidates in batch" in result.stdout
    pending = load_pending_candidates("acme", programs_root=programs_root)
    assert len(pending) == 1
    candidate = pending[0]
    assert candidate.proposed_event_type == "artifact.published.v1"
    assert candidate.pipeline == "backfill_import"
    assert candidate.source_ref.file_path == "2020/2020-11-03 Acme Review with Jordan and Tom.pptx"


def _write_import_source(tmp_path: Path, *, batch_name: str) -> Path:
    source_path = tmp_path / f"{batch_name}.jsonl"
    source_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "01IMPORTLOCK1",
                        "program_id": "legacy",
                        "event_type": "milestone.date_revised.v1",
                        "occurred_at": "2025-03-20T00:00:00+00:00",
                        "recorded_at": "2026-06-11T00:00:00+00:00",
                        "temporal_confidence": "approximate",
                        "confidence": "source_authoritative",
                        "actor": "import",
                        "payload": {"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
                        "source_ref": {
                            "ref_type": "lt_deck",
                            "file_path": "deck.pptx",
                            "deck_date": "2025-03-20",
                            "slide_number": 9,
                            "slide_title": None,
                            "vault_hash": None,
                        },
                        "corroborating_refs": [],
                        "prev_event_hash": "sha256:prev",
                        "content_hash": "sha256:content",
                    }
                ),
                json.dumps(
                    {
                        "event_id": "01IMPORTLOCK2",
                        "program_id": "legacy",
                        "event_type": "milestone.date_revised.v1",
                        "occurred_at": "2025-04-20T00:00:00+00:00",
                        "recorded_at": "2026-06-11T00:00:00+00:00",
                        "temporal_confidence": "approximate",
                        "confidence": "source_authoritative",
                        "actor": "import",
                        "payload": {"milestone_id": "milestone:m2", "new_target_date": "2025-10-30"},
                        "source_ref": {
                            "ref_type": "lt_deck",
                            "file_path": "deck-2.pptx",
                            "deck_date": "2025-04-20",
                            "slide_number": 3,
                            "slide_title": None,
                            "vault_hash": None,
                        },
                        "corroborating_refs": [],
                        "prev_event_hash": "sha256:prev2",
                        "content_hash": "sha256:content2",
                    }
                ),
            ]
        ) + "\n",
        encoding="utf-8",
    )
    return source_path


def _candidate(
    candidate_id: str,
    *,
    batch_id: str = "batch-1",
    pipeline: str = "lt_deck",
    resolved_entity_id: str | None = "milestone:m1",
    milestone_id: str = "milestone:m1",
    staged_at: datetime | None = None,
) -> CandidateEvent:
    source_document_key = "lt_deck:deck.pptx:2025-03-20:9"
    dedupe_core_hash = "sha256:core"
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id="acme",
        proposed_event_type="milestone.date_revised.v1",
        proposed_payload={"milestone_id": milestone_id, "new_target_date": "2025-09-30"},
        proposed_occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        proposed_temporal_confidence="approximate",
        proposed_confidence="ai_extracted",
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=9),
        pipeline=pipeline,
        extraction_confidence=0.9,
        entity_resolution=(
            CandidateEntityResolution(raw_name="Gen9", resolved_entity_id=resolved_entity_id, match_kind="exact", score=1.0),
        ),
        dedupe_key="sha256:dedupe",
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=source_document_key,
        corroborating_refs=(),
        batch_id=batch_id,
        staged_at=staged_at,
    )


def _decision_candidate(candidate_id: str, *, forum: str | None = "LT") -> CandidateEvent:
    payload: dict[str, object] = {
        "decision_id": "decision:d-lock",
        "title": "Decision lock test",
        "decision_text": "Ship with dual lock support.",
        "decided_by": ["person:p1"],
    }
    if forum is not None:
        payload["forum"] = forum
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id="acme",
        proposed_event_type="decision.made.v1",
        proposed_payload=payload,
        proposed_occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        proposed_temporal_confidence="approximate",
        proposed_confidence="ai_extracted",
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=10),
        pipeline="lt_deck",
        extraction_confidence=0.9,
        entity_resolution=(
            CandidateEntityResolution(raw_name="Decision lock", resolved_entity_id="decision:d-lock", match_kind="exact", score=1.0),
        ),
        dedupe_key="sha256:decision-dedupe",
        dedupe_core_hash="sha256:decision-core",
        source_document_key="lt_deck:deck.pptx:2025-03-20:10",
        corroborating_refs=(),
        batch_id="batch-1",
    )


def _correction_candidate(
    candidate_id: str,
    *,
    corrects_event_id: str,
    corrected_payload: dict[str, object] | None,
) -> CandidateEvent:
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id="acme",
        proposed_event_type="operator.correction.v1",
        proposed_payload={
            "corrects_event_id": corrects_event_id,
            "corrected_payload": corrected_payload,
            "reason": "override correction test",
        },
        proposed_occurred_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
        proposed_temporal_confidence="exact",
        proposed_confidence="operator_confirmed",
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 21, tzinfo=timezone.utc).date(), slide_number=11),
        pipeline="operator_direct_write",
        extraction_confidence=1.0,
        entity_resolution=(),
        dedupe_key="sha256:correction-dedupe",
        dedupe_core_hash="sha256:correction-core",
        source_document_key="lt_deck:deck.pptx:2025-03-21:11",
        corroborating_refs=(),
        batch_id="batch-1",
    )


# ---------------------------------------------------------------------------
# ledger redact / redact-vault tests (§10.8 compliance redaction)
# ---------------------------------------------------------------------------

def _write_test_event(programs_root: Path, *, program: str = "acme") -> str:
    """Write a single milestone event and return its event_id."""
    envelope = build_event_envelope(
        program_id=program,
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        recorded_at=None,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="test",
        payload={"milestone_id": "milestone:m-redact", "new_target_date": "2025-12-31"},
        source_ref=LTDeckRef(
            file_path="deck.pptx",
            deck_date=datetime(2025, 6, 1, tzinfo=timezone.utc).date(),
            slide_number=5,
        ),
    )
    result = write_event(envelope, programs_root=programs_root)
    return result.envelope.event_id


def test_ledger_redact_replaces_payload_and_registers_redaction(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    event_id = _write_test_event(programs_root)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "redact",
            "--program",
            "acme",
            "--event-id",
            event_id,
            "--reason",
            "GDPR removal",
            "--actor",
            "compliance-officer",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert f"Redacted event {event_id}" in result.stdout

    # Payload should be replaced with {"redacted": True}
    events = read_events("acme", programs_root=programs_root)
    assert len(events) == 1
    assert events[0].payload == {"redacted": True}

    # Redaction registry must record the original hash
    from src.core.ledger.redaction import load_redaction_registry
    registry = load_redaction_registry("acme", programs_root=programs_root)
    assert event_id in registry
    rec = registry[event_id]
    assert rec.actor == "compliance-officer"
    assert rec.reason == "GDPR removal"
    assert rec.original_envelope_hash.startswith("sha256:")


def test_ledger_redact_idempotent_on_already_redacted_event(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    event_id = _write_test_event(programs_root)

    # First redaction
    runner.invoke(
        app,
        ["--no-catchup", "ledger", "redact", "--program", "acme", "--event-id", event_id, "--reason", "first", "--actor", "a1", "--programs-root", str(programs_root)],
    )

    # Second invocation should report already-done and return 0
    result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "redact", "--program", "acme", "--event-id", event_id, "--reason", "duplicate", "--actor", "a1", "--programs-root", str(programs_root)],
    )
    assert result.exit_code == 0
    assert "already redacted" in result.stdout


def test_ledger_redact_unknown_event_id_exits_3(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "redact", "--program", "acme", "--event-id", "NONEXISTENT", "--reason", "test", "--actor", "a1", "--programs-root", str(programs_root)],
    )
    assert result.exit_code == 3
    assert "not found" in result.stdout


def test_ledger_redact_vault_deletes_entry_and_cascades_to_events(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    # Store a vault entry
    entry = store_evidence_vault_bytes(
        program_id="acme",
        content_bytes=b"slide content for redact-vault test",
        content_type="text/plain",
        original_filename="slide.txt",
        origin_path=None,
        programs_root=programs_root,
    )
    vault_hash = entry.vault_hash

    # Write an event that references the vault via source_ref
    envelope = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        recorded_at=None,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="test",
        payload={"milestone_id": "milestone:m-vault", "new_target_date": "2025-12-31"},
        source_ref=LTDeckRef(
            file_path="slide.pptx",
            deck_date=datetime(2025, 6, 1, tzinfo=timezone.utc).date(),
            slide_number=3,
            vault_hash=vault_hash,
        ),
    )
    write_result = write_event(envelope, programs_root=programs_root)
    event_id = write_result.envelope.event_id

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "redact-vault",
            "--program",
            "acme",
            "--vault-hash",
            vault_hash,
            "--reason",
            "PII in slide",
            "--actor",
            "compliance-officer",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert vault_hash in result.stdout

    # Vault files must be gone
    assert not entry.content_path.exists()
    assert not entry.metadata_path.exists()

    # The referencing event must be redacted (payload replaced)
    events = read_events("acme", programs_root=programs_root)
    assert events[0].payload == {"redacted": True}

    from src.core.ledger.redaction import load_redaction_registry
    registry = load_redaction_registry("acme", programs_root=programs_root)
    assert event_id in registry


def test_ledger_redact_vault_unknown_hash_exits_3(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "redact-vault",
            "--program",
            "acme",
            "--vault-hash",
            "sha256:deadbeef",
            "--reason",
            "test",
            "--actor",
            "a1",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert result.exit_code == 3
    assert "not found" in result.stdout


def test_ledger_verify_is_redaction_aware(monkeypatch, tmp_path: Path) -> None:
    """verify_event_log must treat redacted events as valid using original_envelope_hash."""
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"

    # Write two events to ensure the chain is tested
    _write_test_event(programs_root)
    _write_test_event(programs_root)

    # Verify passes before redaction
    pre_result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "verify", "--program", "acme", "--format", "json", "--programs-root", str(programs_root)],
    )
    pre_status = json.loads(pre_result.stdout)
    assert pre_status["ok"] is True

    # Redact the first event
    events_before = read_events("acme", programs_root=programs_root)
    first_event_id = events_before[0].event_id
    runner.invoke(
        app,
        ["--no-catchup", "ledger", "redact", "--program", "acme", "--event-id", first_event_id, "--reason", "test", "--actor", "a1", "--programs-root", str(programs_root)],
    )

    # Verify must still pass after redaction (chain continuity preserved via original_envelope_hash)
    post_result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "verify", "--program", "acme", "--format", "json", "--programs-root", str(programs_root)],
    )
    post_status = json.loads(post_result.stdout)
    assert post_status["ok"] is True, f"verify failed after redaction: {post_status}"


def test_ledger_triage_batch_reject_rejects_all_active_candidates(monkeypatch, tmp_path: Path) -> None:
    """activation.md §6.14.15 / O-21 — batch-triage reject for backlog ROI."""
    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    programs_root = tmp_path / "programs"
    append_candidate(_candidate("cand-reject-1", batch_id="batch-reject"), programs_root=programs_root)
    append_candidate(_candidate("cand-reject-2", batch_id="batch-reject"), programs_root=programs_root)

    result = runner.invoke(
        app,
        [
            "--no-catchup",
            "ledger",
            "triage",
            "batch-reject",
            "--program",
            "acme",
            "--batch-id",
            "batch-reject",
            "--actor",
            "operator",
            "--reason",
            "wrong entity",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Rejected 2 of 2 candidates from batch batch-reject" in result.stdout

    # Both candidates are now decided (rejected) → no active candidates remain.
    status_result = runner.invoke(
        app,
        ["--no-catchup", "ledger", "status", "--program", "acme", "--programs-root", str(programs_root), "--format", "json"],
    )
    assert status_result.exit_code == 0
    assert json.loads(status_result.stdout)["active_count"] == 0

    # Each rejection wrote a discovery.candidate_rejected.v1 audit event.
    events = read_events("acme", programs_root=programs_root)
    reject_events = [e for e in events if e.event_type == "discovery.candidate_rejected.v1"]
    assert len(reject_events) == 2

