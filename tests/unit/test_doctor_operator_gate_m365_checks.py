from __future__ import annotations

from src.commands.doctor_checks.operator_gate_m365_checks import (
    build_missing_id_diagnostics,
    operator_gate_m365_ids_check,
    summarize_missing_id_diagnostics,
)
from src.m365.agency_bridge import AgencyCapabilities


def test_summarize_missing_id_diagnostics_counts_known_statuses() -> None:
    summary = summarize_missing_id_diagnostics(
        [
            {"status": "runtime_blocked"},
            {"status": "runtime_blocked"},
            {"status": "not_probed_yet"},
        ]
    )

    assert "2 artifact(s) are currently blocked by discovery runtime/auth failures" in summary
    assert "1 artifact(s) are still awaiting the first active discovery pass" in summary


def test_build_missing_id_diagnostics_reports_runtime_blocked() -> None:
    diagnostics = build_missing_id_diagnostics(
        missing_id_artifacts=[{"artifact_id": "chat:demo", "artifact_type": "teams_chat"}],
        m365_discovery={
            "active": True,
            "first_discovery_completed_at": "2026-06-05T10:00:00Z",
            "discovery_last_error": "mcp timed out",
        },
        agency_caps=AgencyCapabilities(available=True, has_workiq=True, server_tools={"workiq": ("ask_work_iq",)}),
    )

    assert diagnostics == [
        {
            "artifact_id": "chat:demo",
            "artifact_type": "teams_chat",
            "inferred_workstream": None,
            "status": "runtime_blocked",
            "detail": "WorkIQ Teams discovery failed: mcp timed out",
        }
    ]


def test_operator_gate_m365_ids_check_is_ok_when_m365_is_disabled(tmp_path) -> None:
    check = operator_gate_m365_ids_check(
        program_id="demo",
        programs_root=tmp_path / "programs",
        edition_name="demo",
        registry_review=None,
        m365_discovery=None,
        agency_caps=AgencyCapabilities(),
    )

    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["commands"][0] == "vertex doctor --operator-gates --edition demo"
