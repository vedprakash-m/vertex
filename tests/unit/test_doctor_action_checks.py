from __future__ import annotations

from src.commands.doctor_checks.action_checks import run_action_doctor


def test_run_action_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_action_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        load_milestone_owner_aliases_fn=lambda program_id: (),
    )

    assert report.checks[0].label == "Actions"
    assert report.checks[0].status == "fail"
