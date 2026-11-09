"""ADF-W2.8 (specs/arch-data-fix.md Section 8.9.4, Appendix A.2): tests for
the AI Release Audit lifecycle -- QG-29's real implementation."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.exceptions import ConfigError
from src.core.quality_gates.ai_release_audit import (
    AIReleaseAuditError,
    AIRunState,
    ApplicationReceipt,
    ReleaseTerminal,
    assert_ai_output_released_or_raise,
    evaluate_ai_release_gate,
    is_ai_output_released,
    new_ai_run_id,
    record_ai_application_receipt,
    record_ai_release_decision,
    record_ai_run_lifecycle,
    released_terminal_for_run,
)
from src.core.ledger.event_log import get_event_logs_dir, read_events


def test_new_ai_run_id_is_unique() -> None:
    assert new_ai_run_id() != new_ai_run_id()


def test_no_terminal_decision_means_not_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    assert released_terminal_for_run(ai_run_id, program_id="samplealpha", programs_root=programs_root) is None
    assert is_ai_output_released(ai_run_id, program_id="samplealpha", programs_root=programs_root) is False


def test_absent_release_ledger_is_not_created_by_release_checks(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = "synthetic-run"
    assert released_terminal_for_run(ai_run_id, program_id="samplealpha", programs_root=programs_root) is None
    assert is_ai_output_released(ai_run_id, program_id="samplealpha", programs_root=programs_root) is False
    assert evaluate_ai_release_gate(ai_run_id, program_id="samplealpha", programs_root=programs_root).passed is False
    with pytest.raises(AIReleaseAuditError):
        assert_ai_output_released_or_raise(ai_run_id, program_id="samplealpha", programs_root=programs_root)
    assert not programs_root.exists()


def test_released_terminal_recorded_and_readable(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="all validators passed", validator_finding_count=0,
        released_content_hash="abc123", programs_root=programs_root,
    )
    assert released_terminal_for_run(ai_run_id, program_id="samplealpha", programs_root=programs_root) is ReleaseTerminal.RELEASED
    assert is_ai_output_released(ai_run_id, program_id="samplealpha", programs_root=programs_root) is True


def test_rejected_terminal_is_not_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.REJECTED,
        reason="semantic validator found a prohibited claim", validator_finding_count=1,
        programs_root=programs_root,
    )
    assert is_ai_output_released(ai_run_id, program_id="samplealpha", programs_root=programs_root) is False


def test_assert_ai_output_released_or_raise_passes_when_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="ok", validator_finding_count=0, programs_root=programs_root,
    )
    assert_ai_output_released_or_raise(ai_run_id, program_id="samplealpha", programs_root=programs_root)  # must not raise


def test_assert_ai_output_released_or_raise_blocks_when_no_terminal(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    with pytest.raises(AIReleaseAuditError, match="no terminal decision recorded"):
        assert_ai_output_released_or_raise(ai_run_id, program_id="samplealpha", programs_root=programs_root)


def test_assert_ai_output_released_or_raise_blocks_when_discarded(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
        reason="schema validation failed twice", validator_finding_count=0, programs_root=programs_root,
    )
    with pytest.raises(AIReleaseAuditError, match="terminal=discarded"):
        assert_ai_output_released_or_raise(ai_run_id, program_id="samplealpha", programs_root=programs_root)


def test_evaluate_ai_release_gate_matches_qg29(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    evaluation = evaluate_ai_release_gate(ai_run_id, program_id="samplealpha", programs_root=programs_root)
    assert evaluation.gate_id == "QG-29"
    assert evaluation.passed is False
    assert evaluation.forceable is False


def test_most_recent_terminal_wins_when_multiple_recorded(tmp_path: Path) -> None:
    """A retried/re-evaluated run can record more than one terminal over
    time (e.g. fallback then a later successful release); the most recent
    one is authoritative."""
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.FALLBACK,
        reason="first attempt used deterministic fallback", validator_finding_count=0, programs_root=programs_root,
    )
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="retry succeeded", validator_finding_count=0, programs_root=programs_root,
    )
    assert released_terminal_for_run(ai_run_id, program_id="samplealpha", programs_root=programs_root) is ReleaseTerminal.RELEASED


def test_different_ai_run_ids_are_isolated(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    released_run = new_ai_run_id()
    other_run = new_ai_run_id()
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=released_run, terminal=ReleaseTerminal.RELEASED,
        reason="ok", validator_finding_count=0, programs_root=programs_root,
    )
    assert is_ai_output_released(released_run, program_id="samplealpha", programs_root=programs_root) is True
    assert is_ai_output_released(other_run, program_id="samplealpha", programs_root=programs_root) is False


@pytest.mark.parametrize(
    "check",
    [released_terminal_for_run, is_ai_output_released, evaluate_ai_release_gate, assert_ai_output_released_or_raise],
)
@pytest.mark.parametrize("damage", ["missing_reason", "missing_count", "bad_reason", "bool_count", "bad_hash"])
def test_release_checks_reject_matching_schema_invalid_payload(
    tmp_path: Path, check: Callable[..., object], damage: str,
) -> None:
    programs_root = tmp_path / "programs"
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="synthetic-run", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=programs_root,
    )
    event_dir = get_event_logs_dir("samplealpha", programs_root=programs_root)
    path = next(event_dir.glob("*.events.jsonl"))
    row = json.loads(path.read_text(encoding="utf-8"))
    if damage == "missing_reason":
        del row["payload"]["reason"]
    elif damage == "missing_count":
        del row["payload"]["validator_finding_count"]
    elif damage == "bad_reason":
        row["payload"]["reason"] = ["synthetic-private-canary"]
    elif damage == "bool_count":
        row["payload"]["validator_finding_count"] = True
    else:
        row["payload"]["released_content_hash"] = []
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = {
        p.relative_to(programs_root): p.read_bytes() if p.is_file() else None
        for p in programs_root.rglob("*")
    }

    with pytest.raises(ConfigError, match="release decision") as raised:
        check("synthetic-run", program_id="samplealpha", programs_root=programs_root)

    assert "synthetic-private-canary" not in str(raised.value)
    assert {
        p.relative_to(programs_root): p.read_bytes() if p.is_file() else None
        for p in programs_root.rglob("*")
    } == before


@pytest.mark.parametrize("invalid_first", [False, True])
def test_matching_schema_invalid_release_cannot_be_hidden_by_another_release(
    tmp_path: Path, invalid_first: bool,
) -> None:
    programs_root = tmp_path / "programs"
    for reason in ("first", "second"):
        record_ai_release_decision(
            program_id="samplealpha", ai_run_id="synthetic-run", terminal=ReleaseTerminal.RELEASED,
            reason=reason, validator_finding_count=0, programs_root=programs_root,
        )
    path = next(get_event_logs_dir("samplealpha", programs_root=programs_root).glob("*.events.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    del rows[0 if invalid_first else 1]["payload"]["validator_finding_count"]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ConfigError, match="release decision"):
        is_ai_output_released("synthetic-run", program_id="samplealpha", programs_root=programs_root)

    assert path.read_bytes() == before


def test_release_payload_validation_propagates_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="synthetic-run", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=tmp_path,
    )
    error = RuntimeError("validator implementation error")
    monkeypatch.setattr("src.core.quality_gates.ai_release_audit.validate_event_payload", Mock(side_effect=error))

    with pytest.raises(RuntimeError) as raised:
        is_ai_output_released("synthetic-run", program_id="samplealpha", programs_root=tmp_path)

    assert raised.value is error


def test_unknown_matching_terminal_retains_existing_value_error(tmp_path: Path) -> None:
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="synthetic-run", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=tmp_path,
    )
    path = next(get_event_logs_dir("samplealpha", programs_root=tmp_path).glob("*.events.jsonl"))
    row = json.loads(path.read_text(encoding="utf-8"))
    row["payload"]["terminal"] = "unknown-terminal"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ValueError):
        is_ai_output_released("synthetic-run", program_id="samplealpha", programs_root=tmp_path)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "check",
    [released_terminal_for_run, is_ai_output_released, evaluate_ai_release_gate, assert_ai_output_released_or_raise],
)
@pytest.mark.parametrize("damage", ["json", "nonobject", "incomplete", "unicode", "foreign", "later_partition"])
def test_release_checks_reject_damaged_ledger_without_repair(
    tmp_path: Path, check: Callable[..., object], damage: str,
) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = "synthetic-run"
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=programs_root,
    )
    event_dir = get_event_logs_dir("samplealpha", programs_root=programs_root)
    path = next(event_dir.glob("*.events.jsonl"))
    if damage == "foreign":
        foreign = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        foreign["program_id"] = "other_program"
        path.write_text(json.dumps(foreign) + "\n", encoding="utf-8")
    elif damage == "later_partition":
        (event_dir / "9999-12.events.jsonl").write_bytes(b'{"broken":')
    else:
        fragment = {
            "json": b'{"broken":',
            "nonobject": b"[]\n",
            "incomplete": b'{"event_id":"incomplete"}\n',
            "unicode": b"\xff",
        }[damage]
        path.write_bytes(path.read_bytes() + fragment)
    before = {
        p.relative_to(programs_root): p.read_bytes() if p.is_file() else None
        for p in programs_root.rglob("*")
    }

    with pytest.raises(UnicodeError if damage == "unicode" else ConfigError):
        check(ai_run_id, program_id="samplealpha", programs_root=programs_root)

    assert {
        p.relative_to(programs_root): p.read_bytes() if p.is_file() else None
        for p in programs_root.rglob("*")
    } == before


def test_corrupt_only_release_ledger_is_not_treated_as_absent(tmp_path: Path) -> None:
    event_dir = get_event_logs_dir("samplealpha", programs_root=tmp_path)
    event_dir.mkdir(parents=True)
    path = event_dir / "2026-01.events.jsonl"
    path.write_bytes(b'{"broken":')
    with pytest.raises(ConfigError):
        is_ai_output_released("synthetic-run", program_id="samplealpha", programs_root=tmp_path)
    assert list(event_dir.iterdir()) == [path]
    assert path.read_bytes() == b'{"broken":'


@pytest.mark.parametrize("error", [PermissionError("read failed"), UnicodeError("decode failed"), RuntimeError("bug")])
def test_release_check_propagates_read_errors_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    reader = Mock(side_effect=error)
    monkeypatch.setattr("src.core.quality_gates.ai_release_audit.read_events", reader)
    with pytest.raises(type(error)) as raised:
        released_terminal_for_run("synthetic-run", program_id="samplealpha", programs_root=tmp_path)
    assert raised.value is error
    reader.assert_called_once_with("samplealpha", programs_root=tmp_path, repair_corruption=False)


def test_record_ai_run_lifecycle_emits_valid_event(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_run_lifecycle(
        program_id="samplealpha", ai_run_id=ai_run_id, feature="decision_brief_advisor",
        state=AIRunState.PLANNED, prompt_version="v1", policy_version="p1",
        programs_root=programs_root,
    )
    events = read_events("samplealpha", programs_root=programs_root)
    lifecycle_events = [e for e in events if e.event_type == "ai.run_lifecycle.v1"]
    assert len(lifecycle_events) == 1
    assert lifecycle_events[0].payload["ai_run_id"] == ai_run_id
    assert lifecycle_events[0].payload["state"] == "planned"
    assert lifecycle_events[0].payload["feature"] == "decision_brief_advisor"


def test_record_ai_application_receipt_emits_valid_event(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()
    record_ai_application_receipt(
        program_id="samplealpha", ai_run_id=ai_run_id, receipt=ApplicationReceipt.RENDERED,
        artifact_ref="narratives/issue_079/exec_summary.md", programs_root=programs_root,
    )
    events = read_events("samplealpha", programs_root=programs_root)
    receipt_events = [e for e in events if e.event_type == "ai.application_receipt.v1"]
    assert len(receipt_events) == 1
    assert receipt_events[0].payload["receipt"] == "rendered"
    assert receipt_events[0].payload["artifact_ref"] == "narratives/issue_079/exec_summary.md"


def test_full_lifecycle_sequence_end_to_end(tmp_path: Path) -> None:
    """A realistic sequence: plan -> request -> respond -> schema-validate
    -> semantically-validate -> release -> render receipt."""
    programs_root = tmp_path / "programs"
    ai_run_id = new_ai_run_id()

    for state in AIRunState:
        record_ai_run_lifecycle(
            program_id="samplealpha", ai_run_id=ai_run_id, feature="test_feature",
            state=state, prompt_version="v1", policy_version="p1", programs_root=programs_root,
        )

    with pytest.raises(AIReleaseAuditError):
        assert_ai_output_released_or_raise(ai_run_id, program_id="samplealpha", programs_root=programs_root)

    record_ai_release_decision(
        program_id="samplealpha", ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="all checks passed", validator_finding_count=0,
        released_content_hash="deadbeef", programs_root=programs_root,
    )
    assert_ai_output_released_or_raise(ai_run_id, program_id="samplealpha", programs_root=programs_root)

    record_ai_application_receipt(
        program_id="samplealpha", ai_run_id=ai_run_id, receipt=ApplicationReceipt.APPLIED,
        proposal_id="prop-1", programs_root=programs_root,
    )

    events = read_events("samplealpha", programs_root=programs_root)
    assert sum(1 for e in events if e.event_type == "ai.run_lifecycle.v1") == 5
    assert sum(1 for e in events if e.event_type == "ai.release_decision.v1") == 1
    assert sum(1 for e in events if e.event_type == "ai.application_receipt.v1") == 1
