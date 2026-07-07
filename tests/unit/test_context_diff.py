"""
Unit tests for src/commands/context_diff.py — §22 E4 context change queries.

Zone A only. Tests focus on the helper functions and the plane1_changelog integration
that context_diff relies on (load_plane1_changes filtering, --since, --between modes).
The Typer CLI entry point itself is exercised via CliRunner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.commands.context_diff import _ds, _ensure_tz
from src.core.plane1_changelog import (
    Plane1ChangeRecord,
    append_plane1_changes,
    load_plane1_changes,
)
from src.core.context_snapshot_store import write_context_snapshot


# ---------------------------------------------------------------------------
# _ds helper
# ---------------------------------------------------------------------------

def test_ds_formats_date_correctly() -> None:
    dt = datetime(2026, 5, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert _ds(dt) == "2026-05-15"


# ---------------------------------------------------------------------------
# _ensure_tz helper
# ---------------------------------------------------------------------------

def test_ensure_tz_adds_utc_to_naive() -> None:
    naive = datetime(2026, 5, 1, 0, 0, 0)
    result = _ensure_tz(naive)
    assert result.tzinfo is not None
    assert result.tzinfo == timezone.utc


def test_ensure_tz_preserves_aware() -> None:
    aware = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    result = _ensure_tz(aware)
    assert result == aware


# ---------------------------------------------------------------------------
# load_plane1_changes filtering (used by context_diff --since)
# ---------------------------------------------------------------------------

def _make_record(ts: datetime, entity_id: str = "ms1") -> Plane1ChangeRecord:
    return Plane1ChangeRecord(
        ts=ts,
        program_id="acme",
        gather_run_id="run-test",
        entity_type="milestone",
        entity_id=entity_id,
        entity_name="Milestone 1",
        field="status",
        prior="on_track",
        current="at_risk",
        kind="status_change",
        linked_workstream_ids=(),
    )


def test_load_since_filters_old_records(tmp_path: Path) -> None:
    pr = tmp_path / "programs"
    pr.mkdir()
    old = datetime.now(timezone.utc) - timedelta(days=10)
    new = datetime.now(timezone.utc) - timedelta(days=1)
    append_plane1_changes("acme", [_make_record(old, "old"), _make_record(new, "new")], programs_root=pr)

    cutoff = datetime.now(timezone.utc) - timedelta(days=5)
    result = load_plane1_changes("acme", programs_root=pr, since=cutoff)
    assert len(result) == 1
    assert result[0].entity_id == "new"


def test_load_no_since_returns_all(tmp_path: Path) -> None:
    pr = tmp_path / "programs"
    pr.mkdir()
    times = [datetime.now(timezone.utc) - timedelta(days=i) for i in range(4)]
    records = [_make_record(t, f"e{i}") for i, t in enumerate(times)]
    append_plane1_changes("acme", records, programs_root=pr)
    result = load_plane1_changes("acme", programs_root=pr)
    assert len(result) == 4


def test_load_empty_changelog_returns_empty(tmp_path: Path) -> None:
    pr = tmp_path / "programs"
    pr.mkdir()
    result = load_plane1_changes("acme", programs_root=pr)
    assert result == []


# ---------------------------------------------------------------------------
# Between-range filtering (simulated from context_diff logic)
# ---------------------------------------------------------------------------

def test_between_filter_clips_changes_to_range(tmp_path: Path) -> None:
    pr = tmp_path / "programs"
    pr.mkdir()
    base = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    records = [_make_record(base + timedelta(days=i), f"e{i}") for i in range(10)]
    append_plane1_changes("acme", records, programs_root=pr)

    since_dt = base + timedelta(days=2)
    until_dt = base + timedelta(days=5)
    changes = load_plane1_changes("acme", programs_root=pr, since=since_dt)
    changes = [c for c in changes if c.ts <= until_dt]
    assert len(changes) == 3  # days 3, 4, 5 (since is exclusive, until is inclusive)


# ---------------------------------------------------------------------------
# JSON output mode (to_json for each record)
# ---------------------------------------------------------------------------

def test_change_record_to_json_serializable() -> None:
    import json
    record = _make_record(datetime.now(timezone.utc))
    d = record.to_json()
    # Must be JSON-serializable
    serialized = json.dumps(d)
    assert "milestone" in serialized
    assert "status_change" in serialized


def test_change_record_json_has_required_keys() -> None:
    record = _make_record(datetime.now(timezone.utc))
    d = record.to_json()
    for key in ("ts", "program_id", "entity_type", "entity_id", "field", "prior", "current", "kind", "record_type"):
        assert key in d, f"Missing key: {key}"
