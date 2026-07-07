from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.channel_composition import run_channel_doctor
from src.commands.doctor_checks.models import DoctorCheck


def test_run_channel_doctor_summarizes_channel_completeness_and_injected_checks(tmp_path: Path) -> None:
    gather_state = SimpleNamespace(
        gathered_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        gather_flags={"workiq": True},
        channels={
            "ado": {"active": True, "meets_expected_min": True, "signal_count": 3, "expected_min": 1},
            "workiq": {"active": True, "meets_expected_min": False, "signal_count": 0, "expected_min": 1},
        },
        query_states={
            "q1": {"last_cycle_succeeded": False},
            "q2": {"last_cycle_succeeded": True, "row_count": 0, "zero_rows_ok": False},
            "q3": {"data_freshness_ok": False},
            "q4": {"value_frozen_warning": True},
        },
        integration_errors=0,
        m365_discovery={"active": True},
        previous_m365_discovery=None,
        previous_gathered_at=None,
        previous_channels=None,
        previous_query_states={},
    )
    report = run_channel_doctor(
        edition_name="demo_weekly",
        reports_root=tmp_path / "reports",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        resolve_edition_fn=lambda edition_name, **kwargs: SimpleNamespace(
            program=SimpleNamespace(id="demo", min_channel_completeness_pct=100, m365=SimpleNamespace(enabled=True)),
            workstreams=["ws"],
        ),
        load_gather_state_fn=lambda program_id, **kwargs: gather_state,
        load_bundle_fn=lambda *args, **kwargs: SimpleNamespace(
            slice_contracts=["slice"],
            config=SimpleNamespace(edition=SimpleNamespace(type="deck")),
        ),
        current_doctor_kusto_targets_fn=lambda **kwargs: {"q1"},
        channel_last_error_fn=lambda channel_name, entry, **kwargs: "auth failed" if channel_name == "workiq" else None,
        channel_auth_failure_detail_fn=lambda channel_name, last_error: last_error,
        build_m365_registry_review_metadata_fn=lambda program_id, **kwargs: {"has_issues": True},
        summarize_m365_discovery_fn=lambda entry: "Discovery active.",
        summarize_m365_registry_review_fn=lambda review: "Review queue present.",
        slice_source_health_check_fn=lambda *args, **kwargs: DoctorCheck("Source Health", "warn", "stale"),
        load_source_waivers_fn=lambda *args, **kwargs: (),
        source_health_function_name_for_edition_fn=lambda edition_type: "deck",
        conversion_fidelity_check_fn=lambda *args, **kwargs: DoctorCheck("Conversion Fidelity", "ok", "ok"),
        eta_credibility_check_fn=lambda *args, **kwargs: DoctorCheck("ETA Credibility", "ok", "ok"),
        m365_discovery_check_fn=lambda *args, **kwargs: DoctorCheck("M365 Discovery", "warn", "warn"),
        m365_registry_review_check_fn=lambda *args, **kwargs: DoctorCheck("M365 Review", "warn", "warn"),
        m365_registry_promotion_check_fn=lambda *args, **kwargs: DoctorCheck("M365 Promotion", "warn", "warn"),
        uil_registry_checks_fn=lambda entries: [DoctorCheck("UIL Registry", "ok", "ok")],
        ado_pr_coverage_check_fn=lambda workstreams: DoctorCheck("ADO PR Coverage", "ok", "ok"),
        channel_delta_check_fn=lambda **kwargs: DoctorCheck("Channel Delta", "warn", "delta"),
        channel_detail_check_fn=lambda channel_name, entry, **kwargs: DoctorCheck(f"Channel:{channel_name}", "ok", "ok"),
    )

    summary = report.checks[0]
    assert summary.label == "Channels"
    assert summary.status == "warn"
    assert "Channel completeness 50%" in summary.detail
    assert "Channel access issues: workiq: auth failed." in summary.detail
    assert "Active degraded channels: workiq." in summary.detail
    assert "Zero-row queries: q2." in summary.detail
    assert "Stale queries: q3." in summary.detail
    assert "Frozen metric queries: q4." in summary.detail
