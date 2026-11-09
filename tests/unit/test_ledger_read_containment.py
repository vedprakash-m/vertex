from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import src.commands.cockpit as cockpit_command
import src.core.cockpit_builder as cockpit_builder
import src.core.ledger.event_log as event_log
import src.core.outcome_metrics as outcome_metrics
from src.commands.cockpit_tui import run_cockpit_tui_loop
from src.core.cockpit_html import render_cockpit_html
from src.core.cockpit_models import ValueConfidence
from src.core.exceptions import ConfigError
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.quality_gates.ai_release_audit import (
    ApplicationReceipt,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_application_receipt,
    record_ai_release_decision,
    released_terminal_for_run,
)


PROGRAM = "fixture_prog"


def _record_run(root: Path, terminal: ReleaseTerminal | None = ReleaseTerminal.RELEASED) -> str:
    run_id = new_ai_run_id()
    if terminal is not None:
        record_ai_release_decision(
            program_id=PROGRAM, ai_run_id=run_id, terminal=terminal, reason="Synthetic decision",
            validator_finding_count=0, programs_root=root,
        )
    record_ai_application_receipt(
        program_id=PROGRAM, ai_run_id=run_id, receipt=ApplicationReceipt.APPLIED, programs_root=root,
    )
    return run_id


def _event_path(root: Path) -> Path:
    return sorted(event_log.get_event_logs_dir(PROGRAM, programs_root=root).glob("*.events.jsonl"))[0]


def _files(root: Path) -> dict[Path, bytes]:
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _damage(root: Path, fragment: bytes = b'{"broken":') -> None:
    path = _event_path(root)
    path.write_bytes(path.read_bytes() + fragment)


def test_cockpit_rejects_corruption_without_repair_or_false_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    _damage(tmp_path)
    before = _files(tmp_path)
    downstream = Mock(side_effect=AssertionError("No downstream summary after invalid ledger"))
    monkeypatch.setattr(cockpit_builder, "_build_economics_summary", downstream)

    with pytest.raises(ConfigError):
        cockpit_builder.build_cockpit_snapshot(PROGRAM, programs_root=tmp_path)

    downstream.assert_not_called()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("fragment", [b'{"broken":', b"[]\n", b'{"event_id":"incomplete"}\n', b"\xff"])
def test_non_repairing_reads_leave_corrupt_ledgers_byte_identical(tmp_path: Path, fragment: bytes) -> None:
    _record_run(tmp_path)
    _damage(tmp_path, fragment)
    before = _files(tmp_path)
    with pytest.raises((ConfigError, UnicodeError)):
        event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    assert _files(tmp_path) == before


def test_strict_failure_does_not_remove_legacy_repair_option(tmp_path: Path) -> None:
    _record_run(tmp_path)
    _damage(tmp_path)
    with pytest.raises(ConfigError, match="line 3"):
        event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    events = event_log.read_events(PROGRAM, programs_root=tmp_path)
    assert len(events) == 2
    assert list((event_log.get_event_logs_dir(PROGRAM, programs_root=tmp_path) / "quarantine").glob("*.jsonl"))


def test_strict_reads_reject_another_program_without_repair(tmp_path: Path) -> None:
    _record_run(tmp_path)
    path = _event_path(tmp_path)
    path.write_bytes(path.read_bytes().replace(PROGRAM.encode(), b"another_program"))
    before = _files(tmp_path)
    with pytest.raises(ConfigError, match="program"):
        event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    assert _files(tmp_path) == before


def test_empty_and_absent_ledgers_remain_empty_without_creating_files(tmp_path: Path) -> None:
    assert event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False) == ()
    assert _files(tmp_path) == {}
    directory = event_log.get_event_logs_dir(PROGRAM, programs_root=tmp_path)
    directory.mkdir(parents=True)
    (directory / "2026-10.events.jsonl").write_text("\n \n", encoding="utf-8")
    before = _files(tmp_path)
    assert event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False) == ()
    assert _files(tmp_path) == before


def test_valid_strict_reads_match_legacy_order_and_values(tmp_path: Path) -> None:
    _record_run(tmp_path)
    before = _files(tmp_path)
    strict = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    assert strict == event_log.read_events(PROGRAM, programs_root=tmp_path)
    assert len(strict) == 2
    assert _files(tmp_path) == before


def test_corruption_in_later_partition_does_not_return_a_valid_subset(tmp_path: Path) -> None:
    _record_run(tmp_path)
    path = event_log.get_event_logs_dir(PROGRAM, programs_root=tmp_path) / "9999-12.events.jsonl"
    path.write_text('{"broken":', encoding="utf-8")
    before = _files(tmp_path)
    with pytest.raises(ConfigError, match="9999-12.events.jsonl"):
        event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("arguments", [
    ["show", "--no-persist"], ["show", "--format", "json"], ["build", "--open"],
    ["explain", "--finding", "reliability.om4_audit_coverage"],
])
def test_fresh_cli_cannot_repair_ledger_or_publish_clean_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    _record_run(tmp_path)
    _damage(tmp_path)
    before = _files(tmp_path)
    monkeypatch.setattr(cockpit_command, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(cockpit_command, "_emit_projection_lag_alert_best_effort", Mock())
    browser = Mock()
    monkeypatch.setattr(cockpit_command.typer, "launch", browser)

    result = CliRunner().invoke(cockpit_command.app, [*arguments, "--program", PROGRAM])

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert "Wrote " not in result.output
    browser.assert_not_called()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("fragment", [b'{"broken":', b"[]\n", b"\xff"])
def test_om4_does_not_measure_a_repaired_subset(tmp_path: Path, fragment: bytes) -> None:
    _record_run(tmp_path)
    _damage(tmp_path, fragment)
    before = _files(tmp_path)
    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)
    assert result.confidence == ValueConfidence.UNAVAILABLE
    assert result.value is None
    assert "Could not read" in result.detail
    assert result.evidence_refs == ()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("error", [OSError("Synthetic read failure"), ConfigError("Invalid ledger"), UnicodeError("Invalid encoding")])
def test_om4_reports_expected_read_failure_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setattr(outcome_metrics, "read_events", Mock(side_effect=error))
    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)
    assert result.value is None
    assert result.confidence == ValueConfidence.UNAVAILABLE


def test_om4_does_not_hide_implementation_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outcome_metrics, "read_events", Mock(side_effect=RuntimeError("Implementation defect")))
    with pytest.raises(RuntimeError):
        outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)


@pytest.mark.parametrize("first", list(ReleaseTerminal))
@pytest.mark.parametrize("last", list(ReleaseTerminal))
def test_om4_latest_decision_wins_with_one_ledger_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: ReleaseTerminal, last: ReleaseTerminal,
) -> None:
    run_id = _record_run(tmp_path, first)
    record_ai_release_decision(
        program_id=PROGRAM, ai_run_id=run_id, terminal=last, reason="Revised synthetic decision",
        validator_finding_count=0, programs_root=tmp_path,
    )
    expected_terminal = released_terminal_for_run(run_id, program_id=PROGRAM, programs_root=tmp_path)
    reader = Mock(wraps=event_log.read_events)
    monkeypatch.setattr(outcome_metrics, "read_events", reader)
    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)
    assert result.value == float(expected_terminal is ReleaseTerminal.RELEASED)
    reader.assert_called_once_with(PROGRAM, programs_root=tmp_path, repair_corruption=False)


def test_om4_reads_each_partition_once_for_multiple_consumed_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    _record_run(tmp_path, ReleaseTerminal.REJECTED)
    _record_run(tmp_path, None)
    partition_count = len(list(event_log.get_event_logs_dir(PROGRAM, programs_root=tmp_path).glob("*.events.jsonl")))
    reader = Mock(wraps=event_log._read_events_from_path)
    monkeypatch.setattr(event_log, "_read_events_from_path", reader)

    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)

    assert result.value == 0.3333
    assert reader.call_count == partition_count
    assert all(call.kwargs["repair_corruption"] is False for call in reader.call_args_list)


def test_om4_counts_unique_consumed_runs_and_missing_release_as_unaudited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _record_run(tmp_path)
    _record_run(tmp_path, None)
    record_ai_application_receipt(
        program_id=PROGRAM, ai_run_id=run_id, receipt=ApplicationReceipt.RENDERED, programs_root=tmp_path,
    )
    records = event_log.read_events(PROGRAM, programs_root=tmp_path)
    reader = Mock(side_effect=AssertionError("Shared snapshot must not reread the ledger"))
    monkeypatch.setattr(outcome_metrics, "read_events", reader)
    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path, events=records)
    assert result.value == 0.5
    assert len(result.evidence_refs) == 2
    reader.assert_not_called()


def test_om4_shared_events_still_enforce_program_scope(tmp_path: Path) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path)
    result = outcome_metrics.compute_om4_audit_coverage("another_program", programs_root=tmp_path, events=records)
    assert result.value is None
    assert result.confidence == ValueConfidence.UNAVAILABLE


def test_om4_ignores_unrelated_release_payload_and_other_events(tmp_path: Path) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path)
    unrelated = replace(records[0], payload={"ai_run_id": "not-consumed", "terminal": "invalid"})
    other_event = replace(records[0], event_type="pipeline.gap_detected.v1")
    result = outcome_metrics.compute_om4_audit_coverage(
        PROGRAM, programs_root=tmp_path, events=(*records, unrelated, other_event),
    )
    assert result.value == 1.0


def test_invalid_consumed_terminal_is_not_reported_as_measured(tmp_path: Path) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path)
    malformed = replace(records[0], payload={**records[0].payload, "terminal": "invalid"})
    with pytest.raises(ValueError):
        outcome_metrics.compute_om4_audit_coverage(
            PROGRAM, programs_root=tmp_path, events=(*records, malformed),
        )


@pytest.mark.parametrize("payload", [
    pytest.param({}, id="missing-fields"),
    pytest.param({"ai_run_id": "run", "receipt": None}, id="null-receipt"),
    pytest.param({"ai_run_id": "run", "receipt": 7}, id="numeric-receipt"),
    pytest.param({"ai_run_id": "run", "receipt": []}, id="list-receipt"),
    pytest.param({"ai_run_id": "run", "receipt": {}}, id="object-receipt"),
    pytest.param({"ai_run_id": "run", "receipt": "synthetic-private-canary"}, id="unknown-receipt"),
    pytest.param({"receipt": "rendered"}, id="missing-run-id"),
    pytest.param({"ai_run_id": "", "receipt": "applied"}, id="empty-consumed-run-id"),
    pytest.param({"ai_run_id": "", "receipt": "failed"}, id="empty-nonconsumed-run-id"),
    pytest.param({"ai_run_id": None, "receipt": "proposed"}, id="null-run-id"),
    pytest.param({"ai_run_id": 7, "receipt": "rendered"}, id="numeric-run-id"),
    pytest.param({"ai_run_id": [], "receipt": "applied"}, id="list-run-id"),
])
@pytest.mark.parametrize("include_valid_run", [False, True])
def test_om4_invalid_receipt_never_measures_a_subset(
    tmp_path: Path, payload: dict[str, object], include_valid_run: bool,
) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    receipt = next(event for event in records if event.event_type == "ai.application_receipt.v1")
    invalid = replace(receipt, payload=payload)
    before = _files(tmp_path)

    result = outcome_metrics.compute_om4_audit_coverage(
        PROGRAM, programs_root=tmp_path, events=(*records, invalid) if include_valid_run else (invalid,),
    )

    assert result.confidence is ValueConfidence.UNAVAILABLE
    assert result.value is None
    assert "receipt" in result.detail.lower()
    assert "synthetic-private-canary" not in result.detail
    assert result.evidence_refs == ()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("receipt", list(ApplicationReceipt))
def test_om4_recognized_receipts_keep_existing_measurement(
    tmp_path: Path, receipt: ApplicationReceipt,
) -> None:
    run_id = "synthetic-valid-run"
    record_ai_release_decision(
        program_id=PROGRAM, ai_run_id=run_id, terminal=ReleaseTerminal.RELEASED, reason="Synthetic decision",
        validator_finding_count=0, programs_root=tmp_path,
    )
    record_ai_application_receipt(
        program_id=PROGRAM, ai_run_id=run_id, receipt=receipt, programs_root=tmp_path,
    )
    before = _files(tmp_path)

    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)

    if receipt in (ApplicationReceipt.RENDERED, ApplicationReceipt.PROPOSED, ApplicationReceipt.APPLIED):
        assert result.confidence is ValueConfidence.MEASURED
        assert result.value == 1.0
    else:
        assert result.confidence is ValueConfidence.UNAVAILABLE
        assert result.value is None
        assert "No AI outputs have been consumed" in result.detail
    assert _files(tmp_path) == before


@pytest.mark.parametrize("damage", ["missing_reason", "missing_count", "bad_reason", "bool_count", "bad_hash"])
@pytest.mark.parametrize("route", ["direct", "composite", "tuple"])
def test_om4_matching_schema_invalid_release_is_unavailable(
    tmp_path: Path, damage: str, route: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    release = next(event for event in records if event.event_type == "ai.release_decision.v1")
    payload = dict(release.payload)
    if damage == "missing_reason":
        del payload["reason"]
    elif damage == "missing_count":
        del payload["validator_finding_count"]
    elif damage == "bad_reason":
        payload["reason"] = ["synthetic-private-canary"]
    elif damage == "bool_count":
        payload["validator_finding_count"] = True
    else:
        payload["released_content_hash"] = []
    invalid = replace(release, payload=payload)
    modified = tuple(invalid if event.event_id == release.event_id else event for event in records)
    if route != "tuple":
        path = _event_path(tmp_path)
        path.write_text("".join(json.dumps(event.to_dict()) + "\n" for event in modified), encoding="utf-8")
    before = _files(tmp_path)

    if route == "tuple":
        reader = Mock(side_effect=AssertionError("Shared event tuples must not reread the ledger"))
        monkeypatch.setattr(outcome_metrics, "read_events", reader)
        result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path, events=modified)
        reader.assert_not_called()
    elif route == "composite":
        composite = outcome_metrics.compute_all_outcome_metrics(PROGRAM, programs_root=tmp_path)
        assert "0 search-before-create prevention" in composite[1].detail
        result = composite[2]
    else:
        result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)

    assert result.confidence is ValueConfidence.UNAVAILABLE
    assert result.value is None
    assert "release decision" in result.detail.lower()
    assert "synthetic-private-canary" not in result.detail
    assert result.evidence_refs == ()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("invalid_first", [False, True])
def test_om4_cannot_hide_a_matching_invalid_release(
    tmp_path: Path, invalid_first: bool,
) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    release = next(event for event in records if event.event_type == "ai.release_decision.v1")
    payload = dict(release.payload)
    del payload["validator_finding_count"]
    invalid = replace(release, payload=payload)
    events = (invalid, *records) if invalid_first else (*records, invalid)

    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path, events=events)

    assert result.confidence is ValueConfidence.UNAVAILABLE
    assert result.value is None
    assert "release decision" in result.detail.lower()
    assert result.evidence_refs == ()


def test_om4_release_payload_validation_propagates_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    error = RuntimeError("validator implementation error")
    monkeypatch.setattr(outcome_metrics, "validate_event_payload", Mock(side_effect=error))

    with pytest.raises(RuntimeError) as raised:
        outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)

    assert raised.value is error


def test_om4_no_consumption_stays_unavailable_with_an_invalid_release(tmp_path: Path) -> None:
    record_ai_release_decision(
        program_id=PROGRAM, ai_run_id="synthetic-run", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=tmp_path,
    )
    path = _event_path(tmp_path)
    row = json.loads(path.read_text(encoding="utf-8"))
    del row["payload"]["validator_finding_count"]
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = _files(tmp_path)

    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)

    assert result.confidence is ValueConfidence.UNAVAILABLE
    assert result.value is None
    assert "No AI outputs have been consumed" in result.detail
    assert _files(tmp_path) == before


def test_om4_unknown_matching_terminal_retains_existing_value_error(tmp_path: Path) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    release = next(event for event in records if event.event_type == "ai.release_decision.v1")
    payload = {**release.payload, "terminal": "unknown-terminal"}
    invalid = replace(release, payload=payload)
    modified = tuple(invalid if event.event_id == release.event_id else event for event in records)

    with pytest.raises(ValueError):
        outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path, events=modified)


def test_om4_matching_invalid_release_is_unavailable_in_fresh_cockpit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    path = _event_path(tmp_path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    release = next(row for row in rows if row["event_type"] == "ai.release_decision.v1")
    del release["payload"]["validator_finding_count"]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    before = path.read_bytes()

    snapshot = cockpit_builder.build_cockpit_snapshot(PROGRAM, programs_root=tmp_path)

    assert snapshot.reliability_summary.audit_coverage is None
    finding = next(f for f in snapshot.findings if f.finding_id == "reliability.om4_audit_coverage")
    assert "unavailable" in finding.summary.lower()
    assert "release decision" in finding.detail.lower()
    assert finding.evidence_refs == ()
    monkeypatch.setattr(cockpit_command, "PROGRAMS_ROOT", tmp_path)
    rendered = CliRunner().invoke(
        cockpit_command.app, ["show", "--program", PROGRAM, "--format", "json", "--no-persist"],
    )
    assert rendered.exit_code == 0, repr(rendered.exception)
    payload = json.loads(rendered.stdout)
    assert payload["reliability_summary"]["audit_coverage"] is None
    rendered_finding = next(row for row in payload["findings"] if row["finding_id"] == "reliability.om4_audit_coverage")
    assert "release decision" in rendered_finding["detail"].lower()
    assert path.read_bytes() == before


@pytest.mark.parametrize("invalid", ["empty-id", "unknown-receipt"])
def test_om4_invalid_writer_receipt_is_unavailable_in_direct_and_composite_results(
    tmp_path: Path, invalid: str,
) -> None:
    _record_run(tmp_path)
    if invalid == "empty-id":
        record_ai_application_receipt(
            program_id=PROGRAM, ai_run_id="", receipt=ApplicationReceipt.RENDERED, programs_root=tmp_path,
        )
    else:
        now = datetime.now(timezone.utc)
        event = event_log.build_event_envelope(
            program_id=PROGRAM, event_type="ai.application_receipt.v1",
            occurred_at=now, recorded_at=now, temporal_confidence=event_log.TemporalConfidence.EXACT,
            confidence=event_log.ConfidenceTier.SOURCE_AUTHORITATIVE, actor="synthetic",
            payload={"ai_run_id": "synthetic-private-canary", "receipt": "synthetic-private-canary"},
            source_ref=OperatorAssertionRef(asserted_by="synthetic", asserted_at=now),
        )
        event_log.write_event(event, programs_root=tmp_path)
    before = _files(tmp_path)

    direct = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)
    composite = outcome_metrics.compute_all_outcome_metrics(PROGRAM, programs_root=tmp_path)

    for result in (direct, composite[2]):
        assert result.confidence is ValueConfidence.UNAVAILABLE
        assert result.value is None
        assert "receipt" in result.detail.lower()
        assert "synthetic-private-canary" not in result.detail
        assert result.evidence_refs == ()
    assert "0 search-before-create prevention" in composite[1].detail
    assert _files(tmp_path) == before


@pytest.mark.parametrize("payload", [
    pytest.param({"ai_run_id": "run"}, id="missing-receipt"),
    pytest.param({"ai_run_id": "run", "receipt": []}, id="list-receipt"),
    pytest.param({"ai_run_id": None, "receipt": "applied"}, id="null-run-id"),
])
def test_om4_invalid_persisted_receipt_does_not_change_ledger(
    tmp_path: Path, payload: dict[str, object],
) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    receipt = next(event for event in records if event.event_type == "ai.application_receipt.v1")
    invalid = replace(receipt, event_id="synthetic-invalid-receipt", payload=payload)
    path = _event_path(tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(invalid.to_dict()) + "\n")
    before = _files(tmp_path)

    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)

    assert result.confidence is ValueConfidence.UNAVAILABLE
    assert result.value is None
    assert "receipt" in result.detail.lower()
    assert result.evidence_refs == ()
    assert _files(tmp_path) == before


@pytest.mark.parametrize(("released", "total", "expected", "status"), [
    (0, 1, 0.0, "info"),
    (1, 1, 1.0, "ok"),
    (1, 3, 0.3333, "info"),
    (1, 10000, 0.0001, "info"),
    (9999, 10000, 0.9999, "info"),
    (1, 20001, 1 / 20001, "info"),
    (20000, 20001, 20000 / 20001, "info"),
])
def test_om4_partial_ratios_do_not_round_to_false_endpoints(
    tmp_path: Path, released: int, total: int, expected: float, status: str,
) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    receipt = next(event for event in records if event.event_type == "ai.application_receipt.v1")
    decision = next(event for event in records if event.event_type == "ai.release_decision.v1")
    events = tuple(
        [replace(receipt, payload={"ai_run_id": str(index), "receipt": "applied"}) for index in range(total)]
        + [
            replace(decision, payload={**decision.payload, "ai_run_id": str(index)})
            for index in range(released)
        ]
    )
    before = _files(tmp_path)

    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path, events=events)
    summary, findings = cockpit_builder._build_reliability_summary(
        PROGRAM, programs_root=tmp_path, observed_at=datetime.now(timezone.utc), events=events,
    )

    assert result.confidence is ValueConfidence.MEASURED
    assert result.value == expected
    assert summary.audit_coverage == expected
    finding = next(row for row in findings if row.finding_id == "reliability.om4_audit_coverage")
    assert finding.status == status
    assert finding.detail == result.detail
    assert _files(tmp_path) == before


@pytest.mark.parametrize("terminal", [ReleaseTerminal.RELEASED, ReleaseTerminal.REJECTED])
def test_om4_detail_describes_recorded_consumption_and_latest_terminal(
    tmp_path: Path, terminal: ReleaseTerminal,
) -> None:
    _record_run(tmp_path, terminal)

    result = outcome_metrics.compute_om4_audit_coverage(PROGRAM, programs_root=tmp_path)

    assert "recorded as consumed" in result.detail
    assert "latest" in result.detail


def test_om4_invalid_receipt_is_unavailable_on_fresh_cockpit_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    now = datetime.now(timezone.utc)
    event = event_log.build_event_envelope(
        program_id=PROGRAM, event_type="ai.application_receipt.v1",
        occurred_at=now, recorded_at=now, temporal_confidence=event_log.TemporalConfidence.EXACT,
        confidence=event_log.ConfidenceTier.SOURCE_AUTHORITATIVE, actor="synthetic",
        payload={"ai_run_id": "synthetic-private-canary", "receipt": "synthetic-private-canary"},
        source_ref=OperatorAssertionRef(asserted_by="synthetic", asserted_at=now),
    )
    event_log.write_event(event, programs_root=tmp_path)
    ledger_before = _event_path(tmp_path).read_bytes()
    monkeypatch.setattr(cockpit_command, "PROGRAMS_ROOT", tmp_path)

    result = CliRunner().invoke(
        cockpit_command.app, ["show", "--program", PROGRAM, "--format", "json", "--no-persist"],
    )

    assert result.exit_code == 0, repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["reliability_summary"]["audit_coverage"] is None
    finding = next(row for row in payload["findings"] if row["finding_id"] == "reliability.om4_audit_coverage")
    detail = "Audit coverage is unavailable because an application receipt is invalid or unrecognized."
    assert "unavailable" in finding["summary"].lower()
    assert finding["detail"] == detail
    assert "synthetic-private-canary" not in result.stdout

    explain = CliRunner().invoke(
        cockpit_command.app,
        ["explain", "--program", PROGRAM, "--finding", "reliability.om4_audit_coverage"],
    )
    assert explain.exit_code == 0, repr(explain.exception)
    assert detail in explain.stdout
    assert "synthetic-private-canary" not in explain.stdout

    snapshot = cockpit_builder.build_cockpit_snapshot(PROGRAM, programs_root=tmp_path)
    assert snapshot.reliability_summary.audit_coverage is None
    html = render_cockpit_html(snapshot, retained=True)
    assert detail in html
    assert "synthetic-private-canary" not in html
    finding_number = next(
        index for index, row in enumerate(snapshot.findings, start=1)
        if row.finding_id == "reliability.om4_audit_coverage"
    )
    replies = iter((str(finding_number), "q"))
    output: list[str] = []
    run_cockpit_tui_loop(PROGRAM, programs_root=tmp_path, input_fn=lambda _: next(replies), output_fn=output.append)
    assert detail in "\n".join(output)
    assert "synthetic-private-canary" not in "\n".join(output)
    assert _event_path(tmp_path).read_bytes() == ledger_before


def test_cockpit_reuses_observed_events_for_duplicate_and_audit_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    now = datetime.now(timezone.utc)
    duplicate = event_log.build_event_envelope(
        program_id=PROGRAM, event_type="actuation.duplicate_prevented.v1",
        occurred_at=now, recorded_at=now,
        temporal_confidence=event_log.TemporalConfidence.EXACT,
        confidence=event_log.ConfidenceTier.SOURCE_AUTHORITATIVE, actor="synthetic",
        payload={"operation_intent_id": "test-intent", "detection": "test", "evidence": "synthetic"},
        source_ref=OperatorAssertionRef(asserted_by="synthetic", asserted_at=now),
    )
    event_log.write_event(duplicate, programs_root=tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path)
    reader = Mock(return_value=records)
    audit_reader = Mock(side_effect=AssertionError("Audit computation must reuse cockpit events"))
    monkeypatch.setattr(cockpit_builder, "read_events", reader)
    monkeypatch.setattr(outcome_metrics, "read_events", audit_reader)

    snapshot = cockpit_builder.build_cockpit_snapshot(PROGRAM, programs_root=tmp_path)

    assert snapshot.reliability_summary.duplicate_preventions == 1
    assert snapshot.reliability_summary.audit_coverage == 1.0
    reader.assert_called_once_with(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    audit_reader.assert_not_called()


@pytest.mark.parametrize("fragment", [b'{"broken":', b"[]\n", b"\xff"])
@pytest.mark.parametrize("composite", [False, True])
def test_om2_and_composite_do_not_repair_or_claim_zero_preventions(
    tmp_path: Path, fragment: bytes, composite: bool,
) -> None:
    _record_run(tmp_path)
    _damage(tmp_path, fragment)
    before = _files(tmp_path)

    results = (
        outcome_metrics.compute_all_outcome_metrics(PROGRAM, programs_root=tmp_path)
        if composite
        else (outcome_metrics.compute_om2_duplicate_entities(PROGRAM, programs_root=tmp_path),)
    )

    for result in results:
        assert result.value is None
        assert result.confidence == ValueConfidence.UNAVAILABLE
        if result.metric_id in {"om2_duplicate_entities", "om4_audit_coverage"}:
            assert "Could not read" in result.detail
            assert "0 search-before-create prevention" not in result.detail
            assert result.evidence_refs == ()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("error", [
    OSError("Synthetic read failure"), ConfigError("Invalid ledger"), UnicodeError("Invalid encoding"),
])
@pytest.mark.parametrize("composite", [False, True])
def test_om2_or_composite_failed_read_is_once_and_not_an_empty_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, composite: bool,
) -> None:
    reader = Mock(side_effect=error)
    monkeypatch.setattr(outcome_metrics, "read_events", reader)

    results = (
        outcome_metrics.compute_all_outcome_metrics(PROGRAM, programs_root=tmp_path)
        if composite
        else (outcome_metrics.compute_om2_duplicate_entities(PROGRAM, programs_root=tmp_path),)
    )

    reader.assert_called_once_with(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    assert all(result.value is None for result in results)
    assert all(result.confidence == ValueConfidence.UNAVAILABLE for result in results)
    if composite:
        assert "REV extractor/judge" in results[0].detail
        assert "Could not read" in results[1].detail
        assert "Could not read" in results[2].detail
        assert "No measurement protocol" in results[3].detail
    else:
        assert "Could not read" in results[0].detail


@pytest.mark.parametrize("composite", [False, True])
def test_om2_and_composite_propagate_implementation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, composite: bool,
) -> None:
    error = RuntimeError("Implementation defect")
    monkeypatch.setattr(outcome_metrics, "read_events", Mock(side_effect=error))
    compute = outcome_metrics.compute_all_outcome_metrics if composite else outcome_metrics.compute_om2_duplicate_entities
    with pytest.raises(RuntimeError) as raised:
        compute(PROGRAM, programs_root=tmp_path)
    assert raised.value is error


def test_om2_shared_events_are_scoped_and_do_not_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    records = event_log.read_events(PROGRAM, programs_root=tmp_path, repair_corruption=False)
    duplicate = replace(records[0], event_type="actuation.duplicate_prevented.v1")
    reader = Mock(side_effect=AssertionError("Shared events must not reread"))
    monkeypatch.setattr(outcome_metrics, "read_events", reader)

    valid = outcome_metrics.compute_om2_duplicate_entities(
        PROGRAM, programs_root=tmp_path, events=(*records, duplicate),
    )
    invalid = outcome_metrics.compute_om2_duplicate_entities(
        "another_program", programs_root=tmp_path, events=records,
    )

    assert valid.value is None
    assert valid.confidence == ValueConfidence.UNAVAILABLE
    assert "1 search-before-create prevention" in valid.detail
    assert invalid.value is None
    assert "Could not read" in invalid.detail
    reader.assert_not_called()


def test_composite_reads_partitions_once_and_shares_identical_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_run(tmp_path)
    _record_run(tmp_path, ReleaseTerminal.REJECTED)
    _record_run(tmp_path, None)
    path = _event_path(tmp_path)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:2]))
    (path.parent / "9999-12.events.jsonl").write_bytes(b"".join(lines[2:]))
    partition_count = len(list(path.parent.glob("*.events.jsonl")))
    before = _files(tmp_path)
    reader = Mock(wraps=event_log._read_events_from_path)
    om2 = Mock(wraps=outcome_metrics.compute_om2_duplicate_entities)
    om4 = Mock(wraps=outcome_metrics.compute_om4_audit_coverage)
    monkeypatch.setattr(event_log, "_read_events_from_path", reader)
    monkeypatch.setattr(outcome_metrics, "compute_om2_duplicate_entities", om2)
    monkeypatch.setattr(outcome_metrics, "compute_om4_audit_coverage", om4)

    results = outcome_metrics.compute_all_outcome_metrics(PROGRAM, programs_root=tmp_path)

    assert results[2].value == 0.3333
    assert reader.call_count == partition_count == 2
    assert all(call.kwargs["repair_corruption"] is False for call in reader.call_args_list)
    assert om2.call_args.kwargs["events"] is om4.call_args.kwargs["events"]
    assert _files(tmp_path) == before


def test_composite_empty_is_not_a_failed_read(tmp_path: Path) -> None:
    results = outcome_metrics.compute_all_outcome_metrics(PROGRAM, programs_root=tmp_path)
    assert "0 search-before-create prevention" in results[1].detail
    assert "No AI outputs have been consumed" in results[2].detail
    assert _files(tmp_path) == {}


def test_composite_rejects_mixed_programs_without_repair(tmp_path: Path) -> None:
    _record_run(tmp_path)
    path = _event_path(tmp_path)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(lines[0] + lines[1].replace(PROGRAM.encode(), b"another_program"))
    before = _files(tmp_path)

    results = outcome_metrics.compute_all_outcome_metrics(PROGRAM, programs_root=tmp_path)

    assert all("Could not read" in results[index].detail for index in (1, 2))
    assert _files(tmp_path) == before
