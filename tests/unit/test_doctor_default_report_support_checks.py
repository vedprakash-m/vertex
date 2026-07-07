from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.default_report_support_checks import (
    audit_hygiene_check,
    external_dependencies_check,
    latest_gather_integration_check,
    recurring_gate_failures_check,
    slice_telemetry_runtime_check,
)


def test_latest_gather_integration_check_surfaces_summary(monkeypatch, tmp_path: Path) -> None:
    gather_state = SimpleNamespace(
        integration_errors=2,
        gathered_at=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        integration_error_details=(
            SimpleNamespace(
                source="workiq",
                stage="discover",
                retryable=True,
                message="timeout",
                operator_action="retry",
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.default_report_support_checks.load_gather_state",
        lambda program_id, *, programs_root: gather_state,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.default_report_support_checks.build_gather_integration_summary",
        lambda state: "2 optional integration failures.",
    )

    check = latest_gather_integration_check("demo", tmp_path)

    assert check is not None
    assert check.status == "warn"
    assert check.detail == "Latest gather recorded 2 optional integration failures."
    assert check.metadata is not None
    assert check.metadata["integration_errors"] == 2


def test_slice_telemetry_runtime_check_warns_on_failed_and_stale_contracts(monkeypatch, tmp_path: Path) -> None:
    gather_state = SimpleNamespace()
    summary = SimpleNamespace(
        failed_contracts=({"slice_id": "exec", "query_id": "q1"},),
        stale_contracts=({"slice_id": "exec", "query_id": "q2", "age_hours": 25.0, "freshness_sla_hours": 24},),
        gathered_at=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.default_report_support_checks.load_gather_state",
        lambda program_id, *, programs_root: gather_state,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.default_report_support_checks.build_slice_telemetry_runtime_summary",
        lambda slice_contracts, current_gather_state: summary,
    )

    check = slice_telemetry_runtime_check(["exec"], "demo", tmp_path)

    assert check is not None
    assert check.status == "warn"
    assert "failed telemetry query state -> exec (q1)" in check.detail
    assert "stale against slice freshness SLA -> exec (q2, 25.0h > 24h)" in check.detail


def test_audit_hygiene_check_warns_when_rows_exceed_threshold(tmp_path: Path) -> None:
    audit_dir = tmp_path / "demo" / "journal"
    audit_dir.mkdir(parents=True)
    audit_path = audit_dir / "autonomy_audit.jsonl"
    audit_path.write_text("{}\n\n{}\n{}\n", encoding="utf-8")

    check = audit_hygiene_check(
        program_id="demo",
        raw_program={"audit": {"archive_threshold_rows": 2, "retention_days": 30}},
        programs_root=tmp_path,
    )

    assert check is not None
    assert check.status == "warn"
    assert "exceeds threshold 2" in check.detail
    assert check.metadata is not None
    assert check.metadata["row_count"] == 3


def test_recurring_gate_failures_check_warns_when_failures_repeat(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.commands.doctor_checks.default_report_support_checks.get_recurring_gate_failures",
        lambda program_id, min_occurrences, *, programs_root: [
            SimpleNamespace(gate_id="qg-8", cause="missing inputs", occurrence_count=4),
        ],
    )

    check = recurring_gate_failures_check("demo", tmp_path)

    assert check is not None
    assert check.status == "warn"
    assert check.detail == "1 gate(s) failing repeatedly: qg-8: missing inputs (4x)"


def test_external_dependencies_check_warns_on_stale_dependencies(monkeypatch, tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "src.commands.doctor_checks.default_report_support_checks.load_external_dependencies",
        lambda program_id, *, programs_root: [
            SimpleNamespace(dep_id="dep-1", team="Storage", last_seen=now - timedelta(days=20)),
            SimpleNamespace(dep_id="dep-2", team="Identity", last_seen=now - timedelta(days=2)),
        ],
    )

    check = external_dependencies_check("demo", tmp_path)

    assert check is not None
    assert check.status == "warn"
    assert "1 stale (>14d): dep-1 (Storage" in check.detail
    assert check.metadata == {"total": 2, "stale": 1}
