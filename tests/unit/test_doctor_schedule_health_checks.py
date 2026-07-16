"""ADF-W5.10: doctor wiring for ``src/core/schedule_health.py``.

These tests verify the doctor *presentation mapping* (primitive findings ->
DoctorCheck rows -> DoctorReport), not the underlying primitive's freshness
logic itself (that lives in ``tests/unit/test_schedule_health.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands.doctor_checks.schedule_health_checks import (
    run_schedule_health_doctor,
)
from src.core.schedule_health import ScheduleHealthFinding

_NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _check(report, label: str):
    matches = [c for c in report.checks if c.label == label]
    assert matches, f"No check with label {label!r} in {[c.label for c in report.checks]}"
    return matches[-1]


def test_both_missing_maps_to_info_rows(programs_root: Path) -> None:
    report = run_schedule_health_doctor(program_id="xpf", programs_root=programs_root, now=_NOW)
    assert report.edition == "xpf"
    # Missing is downgraded to info (never opt-in is not a health failure).
    summary = _check(report, "Schedule Health")
    assert summary.status == "info"
    assert "prefetch=missing" in summary.detail and "cockpit_html=missing" in summary.detail
    assert _check(report, "Scheduled Prefetch").status == "info"
    assert _check(report, "Scheduled Cockpit").status == "info"


def test_stale_artifact_surfaces_warn_and_next_command(programs_root: Path) -> None:
    # Plant a stale cockpit.html so the cockpit finding is warn (not missing).
    cockpit_dir = programs_root / "xpf" / "runtime" / "cockpit"
    cockpit_dir.mkdir(parents=True)
    cockpit_html = cockpit_dir / "cockpit.html"
    cockpit_html.write_text("<html></html>")
    # Backdate its mtime by ~48h (well past the 30h cockpit budget).
    stale = _NOW.timestamp() - 48 * 3600
    import os
    os.utime(cockpit_html, (stale, stale))

    report = run_schedule_health_doctor(program_id="xpf", programs_root=programs_root, now=_NOW)
    cockpit = _check(report, "Scheduled Cockpit")
    assert cockpit.status == "warn"
    assert "48.0h old" in cockpit.detail
    summary = _check(report, "Schedule Health")
    assert summary.status == "warn"  # worst of (missing->info, warn)


def test_fresh_artifacts_report_ok(programs_root: Path) -> None:
    # Plant a fresh cockpit.html (within budget) and inject a fresh prefetch
    # finding via the dependency seam so both findings are ok.
    cockpit_dir = programs_root / "xpf" / "runtime" / "cockpit"
    cockpit_dir.mkdir(parents=True)
    cockpit_html = cockpit_dir / "cockpit.html"
    cockpit_html.write_text("<html></html>")

    def fake_evaluate(program_id, *, programs_root, now):
        return (
            ScheduleHealthFinding("prefetch", "ok", "prefetch fresh", 0.5),
            ScheduleHealthFinding("cockpit_html", "ok", "cockpit fresh", 1.0),
        )

    report = run_schedule_health_doctor(
        program_id="xpf",
        programs_root=programs_root,
        now=_NOW,
        evaluate_fn=fake_evaluate,
    )
    summary = _check(report, "Schedule Health")
    assert summary.status == "ok"
    assert _check(report, "Scheduled Prefetch").status == "ok"
    assert _check(report, "Scheduled Cockpit").status == "ok"


def test_evaluator_failure_is_reported_as_fail_not_raised(programs_root: Path) -> None:
    def raising_evaluate(program_id, *, programs_root, now):
        raise OSError("disk read error")

    report = run_schedule_health_doctor(
        program_id="xpf",
        programs_root=programs_root,
        now=_NOW,
        evaluate_fn=raising_evaluate,
    )
    assert len(report.checks) == 1
    assert report.checks[0].status == "fail"
    assert "Could not evaluate schedule health" in report.checks[0].detail


def test_metadata_carries_age_hours_when_present(programs_root: Path) -> None:
    def fake_evaluate(program_id, *, programs_root, now):
        return (ScheduleHealthFinding("prefetch", "ok", "fresh", 2.5),)

    report = run_schedule_health_doctor(
        program_id="xpf",
        programs_root=programs_root,
        now=_NOW,
        evaluate_fn=fake_evaluate,
    )
    row = _check(report, "Scheduled Prefetch")
    assert row.metadata == {"age_hours": 2.5}


@pytest.fixture
def programs_root(tmp_path: Path) -> Path:
    (tmp_path / "xpf").mkdir()
    return tmp_path
