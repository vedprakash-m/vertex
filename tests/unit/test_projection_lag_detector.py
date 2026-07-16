"""ADF-W5.8 (Section 8.2.5): ``projection_lag`` detection logic.

Tests the pure read-and-compare detector; the cockpit wiring (best-effort
alert emission) is covered by ``test_cockpit_projection_lag_alert.py``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.projection_lag_detector import (
    DEFAULT_MAX_LAG_MINUTES,
    build_projection_lag_alert_message,
    detect_projection_lag,
)
from src.core.reality_store import get_program_reality_db_path
from src.core.run_telemetry import run_telemetry_path

_NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _touch(path: Path, at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    ts = at.timestamp()
    os.utime(path, (ts, ts))


def test_no_underlying_artifacts_is_not_lagging(tmp_path: Path) -> None:
    (tmp_path / "xpf").mkdir()
    finding = detect_projection_lag("xpf", projection_at=_NOW, programs_root=tmp_path)
    assert not finding.is_lagging
    assert finding.freshest_artifact is None


def test_projection_newer_than_data_is_not_lagging(tmp_path: Path) -> None:
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW - timedelta(hours=1))
    finding = detect_projection_lag("xpf", projection_at=_NOW, programs_root=tmp_path)
    assert not finding.is_lagging
    assert finding.freshest_artifact == "fact_store"


def test_projection_older_than_data_over_budget_is_lagging(tmp_path: Path) -> None:
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW)
    finding = detect_projection_lag(
        "xpf", projection_at=_NOW - timedelta(hours=3), programs_root=tmp_path,
    )
    assert finding.is_lagging
    assert finding.freshest_artifact == "fact_store"
    assert finding.lag_minutes is not None and finding.lag_minutes >= 179


def test_lag_within_budget_is_not_lagging(tmp_path: Path) -> None:
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW)
    # 30 minutes behind, default budget 60 -> within budget.
    finding = detect_projection_lag(
        "xpf", projection_at=_NOW - timedelta(minutes=30), programs_root=tmp_path,
    )
    assert not finding.is_lagging


def test_custom_max_lag_minutes_respected(tmp_path: Path) -> None:
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW)
    # 15 min behind, but a 10-min budget -> lagging.
    finding = detect_projection_lag(
        "xpf",
        projection_at=_NOW - timedelta(minutes=15),
        programs_root=tmp_path,
        max_lag_minutes=10.0,
    )
    assert finding.is_lagging


def test_run_telemetry_can_be_the_freshest_source(tmp_path: Path) -> None:
    # Fact store is old, run_telemetry is fresh -> telemetry is the freshest signal.
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW - timedelta(hours=10))
    _touch(run_telemetry_path("xpf", tmp_path), _NOW)
    finding = detect_projection_lag(
        "xpf", projection_at=_NOW - timedelta(hours=2), programs_root=tmp_path,
    )
    assert finding.is_lagging
    assert finding.freshest_artifact == "run_telemetry"


def test_naive_projection_at_treated_as_utc(tmp_path: Path) -> None:
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW)
    naive = (_NOW - timedelta(hours=3)).replace(tzinfo=None)
    finding = detect_projection_lag("xpf", projection_at=naive, programs_root=tmp_path)
    assert finding.is_lagging


def test_alert_message_includes_lag_and_artifact(tmp_path: Path) -> None:
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW)
    finding = detect_projection_lag(
        "xpf", projection_at=_NOW - timedelta(hours=2), programs_root=tmp_path,
    )
    assert finding.is_lagging
    message, next_command = build_projection_lag_alert_message(finding)
    assert "Projection lag" in message
    assert "fact_store" in message
    assert "rebuild" in next_command.lower()


def test_alert_message_raises_on_non_lagging_finding(tmp_path: Path) -> None:
    _touch(get_program_reality_db_path("xpf", programs_root=tmp_path), _NOW)
    finding = detect_projection_lag("xpf", projection_at=_NOW, programs_root=tmp_path)
    assert not finding.is_lagging
    with pytest.raises(AssertionError):
        build_projection_lag_alert_message(finding)


def test_default_budget_is_60_minutes() -> None:
    assert DEFAULT_MAX_LAG_MINUTES == 60.0
