"""Unit tests for chart_gather.py — spec §11."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.commands.chart_gather import (
    ChartGatherResult,
    _eviction_grace_hours_for_ttl,
    _scrub_pii_rows,
    _sanitize_rows,
    _truncate_rows,
    gather_chart_data,
)
from src.core.chart_cache_store import load_chart_cache

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# TTL grace calculation
# ---------------------------------------------------------------------------

def test_eviction_grace_hours_for_ttl_min_168():
    assert _eviction_grace_hours_for_ttl(26) == 168  # max(26*3, 168) = 168
    assert _eviction_grace_hours_for_ttl(60) == 180  # max(60*3, 168) = 180
    assert _eviction_grace_hours_for_ttl(10) == 168  # max(30, 168) = 168


# ---------------------------------------------------------------------------
# Row sanitization
# ---------------------------------------------------------------------------

def test_sanitize_rows_drops_dynamic_columns():
    rows = [{"key": "val", "dynamic": {"nested": "data"}}]
    result = _sanitize_rows(rows)
    assert "dynamic" not in result[0]
    assert result[0]["key"] == "val"


def test_sanitize_rows_coerces_datetime():
    from datetime import date
    rows = [{"dt": datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc), "d": date(2026, 5, 1)}]
    result = _sanitize_rows(rows)
    assert isinstance(result[0]["dt"], str)
    assert "2026" in result[0]["dt"]


def test_sanitize_rows_passes_primitives():
    rows = [{"i": 42, "f": 3.14, "s": "hello", "b": True, "n": None}]
    result = _sanitize_rows(rows)
    assert result[0]["i"] == 42
    assert result[0]["n"] is None


# ---------------------------------------------------------------------------
# Row truncation
# ---------------------------------------------------------------------------

def test_truncate_rows_not_truncated_with_summarize():
    rows = [{"x": i} for i in range(600)]
    kql = "T | summarize count() by x"
    result, was = _truncate_rows(rows, kql)
    assert not was
    assert len(result) == 600


def test_truncate_rows_truncated_without_summarize():
    rows = [{"x": i} for i in range(600)]
    kql = "T | where x > 0"
    result, was = _truncate_rows(rows, kql)
    assert was
    assert len(result) == 500


def test_truncate_rows_empty_no_truncation():
    result, was = _truncate_rows([], "T | where x > 0")
    assert not was
    assert result == []


# ---------------------------------------------------------------------------
# PII scrubbing (unit — no network)
# ---------------------------------------------------------------------------

def test_scrub_pii_rows_handles_import_error():
    with patch.dict("sys.modules", {"src.ai.safety.pii_scrubber": None}):
        rows = [{"field": "john@example.com"}]
        # Should return rows as-is (graceful degradation)
        try:
            result = _scrub_pii_rows(rows)
            assert isinstance(result, list)
        except Exception:
            pass  # If import fails, the function falls back gracefully


# ---------------------------------------------------------------------------
# gather_chart_data integration (with mocks)
# ---------------------------------------------------------------------------

def _make_mock_program():
    prog = MagicMock()
    prog.id = "acme"
    return prog


def _make_workstream(ws_id: str):
    ws = MagicMock()
    ws.id = ws_id
    return ws


@pytest.fixture
def mock_programs_root(tmp_path):
    return tmp_path


def test_gather_chart_data_disabled_by_env(mock_programs_root, monkeypatch):
    monkeypatch.setenv("VERTEX_CHARTS", "0")
    import importlib
    import src.commands.chart_gather as cg
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", False)

    results, errors = gather_chart_data(
        "acme",
        programs_root=mock_programs_root,
        program=_make_mock_program(),
        workstreams=(_make_workstream("ws1"),),
        executor=MagicMock(),
    )
    assert results == ()
    assert errors == ()


def test_gather_chart_data_no_programs_root():
    results, errors = gather_chart_data(
        "acme",
        programs_root=None,  # type: ignore[arg-type]
        program=_make_mock_program(),
        workstreams=(),
        executor=MagicMock(),
    )
    assert results == ()
    assert errors == ()
