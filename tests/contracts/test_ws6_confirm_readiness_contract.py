"""WS-6 contract tests: doctor --confirm-readiness check.

Verifies that ``run_confirm_readiness_doctor`` correctly:
- Returns FAIL when no overrides file exists
- Returns FAIL when any dimension has 'Needs Input' risk
- Returns OK when overrides have all confirmed risks + fresh gather state
- Returns WARN when gather state is stale
- Returns FAIL when no gather state at all
- Archives info check is present (info-only, not a hard fail)
- run_doctor accepts confirm_readiness=True
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.commands.doctor_checks.confirm_readiness_checks import run_confirm_readiness_doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_gather_state(program_dir: Path, *, gathered_at: datetime) -> None:
    gs_path = program_dir / "gather_state.json"
    gs_path.parent.mkdir(parents=True, exist_ok=True)
    gs_path.write_text(
        json.dumps({
            "program_id": program_dir.name,
            "gathered_at": gathered_at.isoformat(),
            "scanned_items": 5,
            "discovered_signals": 2,
            "new_signals": 1,
            "pending_review": 0,
            "trajectory_updates": 0,
            "auto_reviews_written": 0,
            "ado_calls": 3,
            "archived_journal_files": 0,
            "background_proposals": 0,
        }),
        encoding="utf-8",
    )


def _write_overrides(program_dir: Path, dimensions: dict[str, str]) -> None:
    overrides_dir = program_dir / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    doc = {"dimensions": {name: {"risk": risk} for name, risk in dimensions.items()}}
    (overrides_dir / "issue_001.yaml").write_text(yaml.dump(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_confirm_readiness_fails_no_overrides(tmp_path: Path) -> None:
    """FAIL when there is no overrides file for the program."""
    programs_root = tmp_path / "programs"
    (programs_root / "myprog").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    _write_gather_state(programs_root / "myprog", gathered_at=now)
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    assert report.failures >= 1
    overrides_check = next(c for c in report.checks if c.label == "Overrides")
    assert overrides_check.status == "fail"


def test_confirm_readiness_fails_needs_input_dimension(tmp_path: Path) -> None:
    """FAIL when any dimension has Needs Input risk."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now)
    _write_overrides(prog_dir, {"performance": "❓ Needs input", "reliability": "medium"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    assert report.failures >= 1
    overrides_check = next(c for c in report.checks if c.label == "Overrides")
    assert overrides_check.status == "fail"
    assert overrides_check.metadata is not None
    assert "performance" in overrides_check.metadata["needs_input_dimensions"]


def test_confirm_readiness_ok_all_confirmed(tmp_path: Path) -> None:
    """OK when overrides have confirmed risks and gather state is fresh."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now - timedelta(days=1))
    _write_overrides(prog_dir, {"performance": "high", "reliability": "medium"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        cadence="weekly",
        now=now,
    )
    overrides_check = next(c for c in report.checks if c.label == "Overrides")
    gather_check = next(c for c in report.checks if c.label == "Gather State")
    assert overrides_check.status == "ok"
    assert gather_check.status == "ok"
    # No hard failures → overall is ok or warn
    assert report.failures == 0


def test_confirm_readiness_warn_stale_gather(tmp_path: Path) -> None:
    """WARN (not FAIL) when gather state is older than 2× cadence."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now - timedelta(days=20))  # 20d > 2×7=14
    _write_overrides(prog_dir, {"performance": "high"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        cadence="weekly",
        now=now,
    )
    gather_check = next(c for c in report.checks if c.label == "Gather State")
    assert gather_check.status == "warn"
    assert gather_check.metadata is not None
    assert gather_check.metadata["age_days"] >= 20


def test_confirm_readiness_fails_no_gather_state(tmp_path: Path) -> None:
    """FAIL when there is no gather state at all."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_overrides(prog_dir, {"performance": "high"})
    # No gather_state.json written
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    gather_check = next(c for c in report.checks if c.label == "Gather State")
    assert gather_check.status == "fail"


def test_confirm_readiness_archive_info_only(tmp_path: Path) -> None:
    """Archive check is info-only (no failure) when no confirmed issues yet."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now)
    _write_overrides(prog_dir, {"performance": "high"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    archive_check = next(c for c in report.checks if c.label == "Archive")
    assert archive_check.status == "info"
    # An info check must NOT be counted as a failure
    assert report.failures == 0


def test_confirm_readiness_archive_ok_when_index_exists(tmp_path: Path) -> None:
    """Archive check is OK when archive index has confirmed issues."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now)
    _write_overrides(prog_dir, {"performance": "high"})
    # Create a mock archive index
    archive_dir = tmp_path / "archive" / "test_ed"
    archive_dir.mkdir(parents=True)
    (archive_dir / "archive_index.json").write_text(
        json.dumps({"issues": [{"issue_number": 1}, {"issue_number": 2}]}),
        encoding="utf-8",
    )
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    archive_check = next(c for c in report.checks if c.label == "Archive")
    assert archive_check.status == "ok"
    assert archive_check.metadata is not None
    assert archive_check.metadata["confirmed_count"] == 2


def test_confirm_readiness_run_doctor_has_parameter() -> None:
    """run_doctor must accept confirm_readiness as a keyword argument."""
    import inspect
    from src.commands.doctor import run_doctor

    sig = inspect.signature(run_doctor)
    assert "confirm_readiness" in sig.parameters
    assert sig.parameters["confirm_readiness"].default is False
