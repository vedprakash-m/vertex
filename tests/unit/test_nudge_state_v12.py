"""Tests for nudge state store schema 1.2 read-time reinterpretation (D-5).

Verifies that:
1. Schema 1.1 bare ISO string values load with origin=legacy_unknown
2. Schema 1.2 dict values with triggered_at/origin/run_id load correctly
3. Mixed 1.1/1.2 files load correctly
4. NudgeStateEntry now carries origin and run_id fields
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.nudge_state_store import NudgeStateEntry, load_nudge_state


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Schema 1.1 backward compatibility
# ---------------------------------------------------------------------------


class TestSchema11BareStrings:
    def test_bare_string_gets_legacy_unknown_origin(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.1",
            "item:1001": "2026-06-20T10:00:00+00:00",
        })
        entries = load_nudge_state(state_path)
        assert len(entries) == 1
        e = entries[0]
        assert e.work_item_id == 1001
        assert e.origin == "legacy_unknown"
        assert e.run_id is None

    def test_multiple_bare_strings(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.1",
            "item:1001": "2026-06-20T10:00:00Z",
            "item:1002": "2026-06-21T10:00:00Z",
        })
        entries = load_nudge_state(state_path)
        assert len(entries) == 2
        for e in entries:
            assert e.origin == "legacy_unknown"
            assert e.run_id is None

    def test_legacy_numeric_keys(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.1",
            "1001": "2026-06-20T10:00:00+00:00",
        })
        entries = load_nudge_state(state_path)
        assert len(entries) == 1
        assert entries[0].work_item_id == 1001
        assert entries[0].origin == "legacy_unknown"


# ---------------------------------------------------------------------------
# Schema 1.2 dict values
# ---------------------------------------------------------------------------


class TestSchema12DictValues:
    def test_dict_value_with_all_fields(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.2",
            "item:2001": {
                "triggered_at": "2026-06-22T09:00:00Z",
                "origin": "mark_sent",
                "run_id": "nudge-run-abc123",
            },
        })
        entries = load_nudge_state(state_path)
        assert len(entries) == 1
        e = entries[0]
        assert e.work_item_id == 2001
        assert e.origin == "mark_sent"
        assert e.run_id == "nudge-run-abc123"
        assert e.nudged_at == datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc)

    def test_dict_value_generated_origin(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.2",
            "item:3001": {
                "triggered_at": "2026-06-15T08:00:00+00:00",
                "origin": "generated",
                "run_id": "run-xyz",
            },
        })
        entries = load_nudge_state(state_path)
        e = entries[0]
        assert e.origin == "generated"
        assert e.run_id == "run-xyz"

    def test_dict_value_missing_origin_defaults(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.2",
            "item:4001": {
                "triggered_at": "2026-06-22T09:00:00Z",
                # no origin field
            },
        })
        entries = load_nudge_state(state_path)
        assert entries[0].origin == "legacy_unknown"

    def test_dict_value_missing_run_id_is_none(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.2",
            "item:5001": {
                "triggered_at": "2026-06-22T09:00:00Z",
                "origin": "import_sent",
                # no run_id
            },
        })
        entries = load_nudge_state(state_path)
        assert entries[0].run_id is None
        assert entries[0].origin == "import_sent"

    def test_invalid_triggered_at_raises(self, tmp_path: Path):
        from src.core.exceptions import ConfigError
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.2",
            "item:6001": {
                "triggered_at": "not-a-date",
                "origin": "mark_sent",
            },
        })
        with pytest.raises(ConfigError):
            load_nudge_state(state_path)


# ---------------------------------------------------------------------------
# Mixed 1.1/1.2 file (upgrade in progress)
# ---------------------------------------------------------------------------


class TestMixedSchema:
    def test_mixed_str_and_dict_values(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.2",
            "item:7001": "2026-06-10T10:00:00Z",        # legacy bare string
            "item:7002": {                               # schema 1.2 dict
                "triggered_at": "2026-06-22T09:00:00Z",
                "origin": "mark_sent",
                "run_id": "run-abc",
            },
        })
        entries = load_nudge_state(state_path)
        assert len(entries) == 2
        by_id = {e.work_item_id: e for e in entries}
        assert by_id[7001].origin == "legacy_unknown"
        assert by_id[7002].origin == "mark_sent"
        assert by_id[7002].run_id == "run-abc"

    def test_latest_timestamp_wins_across_mixed_keys(self, tmp_path: Path):
        state_path = tmp_path / "nudge_state.json"
        _write_state(state_path, {
            "schema_version": "1.2",
            "item:8001": "2026-06-10T10:00:00Z",        # older legacy
            "8001": {                                    # newer dict (bare numeric key)
                "triggered_at": "2026-06-22T09:00:00Z",
                "origin": "mark_sent",
            },
        })
        entries = load_nudge_state(state_path)
        assert len(entries) == 1
        e = entries[0]
        assert e.nudged_at == datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc)
        assert e.origin == "mark_sent"


# ---------------------------------------------------------------------------
# NudgeStateEntry provenance fields
# ---------------------------------------------------------------------------


class TestNudgeStateEntryFields:
    def test_origin_defaults_to_none(self):
        e = NudgeStateEntry(
            work_item_id=1,
            nudged_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        assert e.origin is None
        assert e.run_id is None

    def test_origin_and_run_id_settable(self):
        e = NudgeStateEntry(
            work_item_id=2,
            nudged_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
            origin="mark_sent",
            run_id="run-xyz",
        )
        assert e.origin == "mark_sent"
        assert e.run_id == "run-xyz"
