from __future__ import annotations

from src.commands.doctor_checks.decision_checks import run_decision_doctor


def test_run_decision_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_decision_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        load_milestone_owner_aliases_fn=lambda program_id: (),
    )

    assert report.checks[0].label == "Decisions"
    assert report.checks[0].status == "fail"
