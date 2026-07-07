from __future__ import annotations

from datetime import datetime, timezone

from src.commands.doctor_checks.cadence_checks import describe_cadence_status, run_cadence_doctor


def test_describe_cadence_status_reports_on_track_for_same_day() -> None:
    now = datetime(2026, 6, 5, 18, 0, tzinfo=timezone.utc)

    assert describe_cadence_status("weekly", now, now) == "on track"


def test_describe_cadence_status_reports_unknown_window_for_unrecognized_cadence() -> None:
    now = datetime(2026, 6, 5, 18, 0, tzinfo=timezone.utc)
    last_confirmed_at = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)

    assert describe_cadence_status("quarterly", last_confirmed_at, now) == "cadence window unknown"


def test_run_cadence_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_cadence_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
        describe_cadence_status_fn=lambda cadence, last_confirmed_at, now: "",
    )

    assert report.checks[0].label == "Cadence"
    assert report.checks[0].status == "fail"
