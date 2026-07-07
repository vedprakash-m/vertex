from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.default_report_composition import run_default_doctor_report
from src.commands.doctor_checks.models import ADOProbeResult, DoctorCheck, DoctorReport


def test_run_default_doctor_report_builds_expected_default_checks(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    report = run_default_doctor_report(
        edition_name="demo_weekly",
        reports_root=tmp_path / "reports",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        archive_root=archive_root,
        templates_root=tmp_path / "templates",
        fix=False,
        ado_probe=None,
        load_bundle_fn=lambda *args, **kwargs: SimpleNamespace(
            config=SimpleNamespace(schema_version="2.0", edition=SimpleNamespace(type="deck")),
            program_context=SimpleNamespace(workstreams=[1, 2], people=[1]),
            slice_contracts=["slice"],
            editorial_rules=SimpleNamespace(banned_phrases=("custom phrase",)),
        ),
        validate_slice_contracts_fn=lambda slice_contracts: SimpleNamespace(slice_count=1, failure_count=0, warning_count=0, failures=(), warnings=()),
        run_id_doctor=lambda **kwargs: DoctorReport("demo_weekly", (DoctorCheck("IDs", "ok", "ok"),)),
        probe_ado_access_fn=lambda bundle: ADOProbeResult(True, "azurecli", 3, 47, "ADO reachable"),
        token_check_fn=lambda probe_result: DoctorCheck("ADO Token", "ok", "ok"),
        mail_preview_check_fn=lambda: DoctorCheck("Mail Preview", "warn", "missing graph"),
        resolve_edition_fn=lambda *args, **kwargs: SimpleNamespace(program=SimpleNamespace(id="demo"), raw_program={}, workstreams=["ws"]),
        template_contract_edition_check_fn=lambda *args, **kwargs: DoctorCheck("Template Contract", "ok", "ok"),
        config_governance_check_fn=lambda **kwargs: DoctorCheck("Config Governance", "ok", "ok"),
        latest_gather_integration_check_fn=lambda *args, **kwargs: DoctorCheck("Gather", "ok", "ok"),
        slice_telemetry_runtime_check_fn=lambda *args, **kwargs: DoctorCheck("Slice Telemetry", "warn", "warn"),
        capability_review_check_fn=lambda *args, **kwargs: None,
        hygiene_nudge_check_fn=lambda **kwargs: DoctorCheck("Hygiene Nudge", "warn", "warn"),
        audit_hygiene_check_fn=lambda **kwargs: DoctorCheck("Audit Hygiene", "ok", "ok"),
        read_archive_index_fn=lambda *args, **kwargs: SimpleNamespace(issues=[]),
        get_archive_root_fn=lambda edition, archive_root: archive_root,
        latest_snapshot_check_fn=lambda *args, **kwargs: DoctorCheck("Latest Snapshot", "ok", "ok"),
        semantic_index_enabled_fn=lambda raw_program: False,
        build_semantic_index_checks_fn=lambda **kwargs: (),
        load_overrides_fn=lambda *args, **kwargs: {"seeded": True},
        seed_overrides_fn=lambda *args, **kwargs: Path("reports/demo_weekly/overrides.yaml"),
        template_check_fn=lambda templates_root: DoctorCheck("Templates", "ok", "ok"),
        recurring_gate_failures_check_fn=lambda *args, **kwargs: DoctorCheck("Recurring Gates", "warn", "warn"),
        override_streak_check_fn=lambda *args, **kwargs: None,
        external_dependencies_check_fn=lambda *args, **kwargs: DoctorCheck("External Dependencies", "ok", "ok"),
        directory_size_fn=lambda path: 2048,
        format_bytes_fn=lambda size: "2.0KB",
        default_banned_phrases=("default phrase",),
        candidate_queue_backlog_check_fn=lambda *args, **kwargs: None,
        claim_freshness_check_fn=lambda *args, **kwargs: None,
        coverage_range_check_fn=lambda *args, **kwargs: None,
        degraded_confirm_check_fn=lambda *args, **kwargs: None,
        ledger_health_check_fn=lambda *args, **kwargs: None,
        runtime_layout_check_fn=lambda program_id, programs_root: DoctorCheck("DC-02 Runtime Layout", "info", "pre_migration"),
    )

    labels = [check.label for check in report.checks]
    assert labels[:5] == ["Config", "Context", "Slices", "IDs", "ADO Access"]
    assert "Overrides" in labels
    assert "Disk" in labels
    assert any(check.label == "Editorial" and check.status == "warn" for check in report.checks)


def test_doctor_report_counts_error_status_as_a_failure() -> None:
    """DC-02 (specs/declutter.md): a ``status="error"`` check is a blocking
    state. DoctorReport.failures must count it (and overall go UNHEALTHY) so
    no blocking state slips through silently — regardless of which severity
    label a check family uses (``"fail"`` or ``"error"``)."""
    report = DoctorReport(
        "demo_weekly",
        (
            DoctorCheck("Ok Check", "ok", "fine"),
            DoctorCheck("Warn Check", "warn", "watch"),
            DoctorCheck("Error Check", "error", "blocking via error severity"),
            DoctorCheck("Fail Check", "fail", "blocking via fail severity"),
        ),
    )
    assert report.warnings == 1
    assert report.failures == 2  # both "error" and "fail" count
    assert report.overall == "UNHEALTHY"


def test_doctor_report_healthy_when_only_ok_and_warn() -> None:
    report = DoctorReport(
        "demo_weekly",
        (DoctorCheck("Ok", "ok", "fine"), DoctorCheck("Watch", "warn", "watch")),
    )
    assert report.failures == 0
    assert report.warnings == 1
    assert report.overall == "HEALTHY"
