"""Tests for src/core/people_legacy_reference_metrics.py's BL-J1 horizon
logic (evaluate_schema_3_0_horizon). The raw counter itself is already
covered by tests/unit/test_doctor_kb_checks.py and test_people_query.py per
WO-6; this file covers the horizon-decision math added after the operator
ratified "zero reads across 8 consecutive weeks" (2026-07-22)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.people_legacy_reference_metrics import (
    HORIZON_WINDOW_WEEKS,
    INSTRUMENTATION_LIVE_SINCE,
    evaluate_schema_3_0_horizon,
    get_legacy_reference_log_path,
)

_LIVE_SINCE_UTC = datetime(
    INSTRUMENTATION_LIVE_SINCE.year, INSTRUMENTATION_LIVE_SINCE.month, INSTRUMENTATION_LIVE_SINCE.day,
    tzinfo=timezone.utc,
)


def _write_entries(knowledge_root: Path, *timestamps: datetime) -> None:
    path = get_legacy_reference_log_path(knowledge_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"recorded_at": ts.isoformat(), "entity_type": "person", "ref": "P:alice"})
        for ts in timestamps
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_horizon_not_met_when_instrumentation_too_new(tmp_path: Path) -> None:
    now = _LIVE_SINCE_UTC + timedelta(weeks=1)
    status = evaluate_schema_3_0_horizon(tmp_path, now=now)
    assert status.met is False
    assert "instrumentation has only been live" in status.reason
    assert status.weeks_since_last_legacy_read is None


def test_horizon_met_with_no_log_after_window(tmp_path: Path) -> None:
    now = _LIVE_SINCE_UTC + timedelta(weeks=HORIZON_WINDOW_WEEKS + 1)
    status = evaluate_schema_3_0_horizon(tmp_path, now=now)
    assert status.met is True
    assert "no legacy-alias reads ever recorded" in status.reason


def test_horizon_met_when_last_read_predates_window(tmp_path: Path) -> None:
    now = _LIVE_SINCE_UTC + timedelta(weeks=HORIZON_WINDOW_WEEKS + 4)
    old_read = now - timedelta(weeks=HORIZON_WINDOW_WEEKS + 1)
    _write_entries(tmp_path, old_read)

    status = evaluate_schema_3_0_horizon(tmp_path, now=now)
    assert status.met is True
    assert "past the" in status.reason
    assert status.weeks_since_last_legacy_read is not None
    assert status.weeks_since_last_legacy_read >= HORIZON_WINDOW_WEEKS


def test_horizon_not_met_when_last_read_within_window(tmp_path: Path) -> None:
    now = _LIVE_SINCE_UTC + timedelta(weeks=HORIZON_WINDOW_WEEKS + 4)
    recent_read = now - timedelta(weeks=2)
    _write_entries(tmp_path, recent_read)

    status = evaluate_schema_3_0_horizon(tmp_path, now=now)
    assert status.met is False
    assert "within the" in status.reason
    assert status.weeks_since_last_legacy_read is not None
    assert status.weeks_since_last_legacy_read < HORIZON_WINDOW_WEEKS


def test_horizon_uses_most_recent_of_multiple_entries(tmp_path: Path) -> None:
    """An old entry plus a recent one must not be masked by the old one --
    the most recent read is what matters for the trailing window."""
    now = _LIVE_SINCE_UTC + timedelta(weeks=HORIZON_WINDOW_WEEKS + 4)
    old_read = now - timedelta(weeks=HORIZON_WINDOW_WEEKS + 10)
    recent_read = now - timedelta(weeks=1)
    _write_entries(tmp_path, old_read, recent_read)

    status = evaluate_schema_3_0_horizon(tmp_path, now=now)
    assert status.met is False
    assert status.weeks_since_last_legacy_read is not None
    assert status.weeks_since_last_legacy_read < 2


def test_horizon_ignores_unparseable_lines(tmp_path: Path) -> None:
    now = _LIVE_SINCE_UTC + timedelta(weeks=HORIZON_WINDOW_WEEKS + 1)
    path = get_legacy_reference_log_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n\n", encoding="utf-8")

    status = evaluate_schema_3_0_horizon(tmp_path, now=now)
    assert status.met is True
    assert status.weeks_since_last_legacy_read is None
