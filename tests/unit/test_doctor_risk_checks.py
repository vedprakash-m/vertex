from __future__ import annotations

from src.commands.doctor_checks.risk_checks import run_risk_doctor


def test_run_risk_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_risk_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        load_milestone_owner_aliases_fn=lambda program_id: (),
        load_current_milestones_fn=lambda program_id: (),
    )

    assert report.checks[0].label == "Risks"
    assert report.checks[0].status == "fail"
