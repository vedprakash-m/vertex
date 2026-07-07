from __future__ import annotations

from src.commands.doctor_checks.escalation_checks import run_escalation_doctor


def test_run_escalation_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_escalation_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    assert report.checks[0].label == "Escalations"
    assert report.checks[0].status == "fail"
