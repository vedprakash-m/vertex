from __future__ import annotations

from src.commands import watch as watch_command
from src.commands.doctor_checks.watch_source_checks import run_watch_source_doctor


def test_run_watch_source_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_watch_source_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        selected_sources=(watch_command.WatchSource.ADO,),
    )

    assert report.checks[0].label == "Watch Sources"
    assert report.checks[0].status == "fail"
