from __future__ import annotations

from src.commands.doctor_checks.assumption_checks import run_assumption_doctor


def test_run_assumption_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_assumption_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        load_milestone_owner_aliases_fn=lambda program_id: (),
        load_current_milestones_fn=lambda program_id: (),
    )

    assert report.checks[0].label == "Assumptions"
    assert report.checks[0].status == "fail"
