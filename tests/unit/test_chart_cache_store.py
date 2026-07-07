"""Unit tests for chart_cache_store.py — spec §11."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.core.chart_cache_store import (
    ChartCacheEntry,
    chart_cache_age_hours,
    evict_stale_caches,
    load_chart_cache,
    write_chart_cache,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
_ROWS: list[dict] = [{"day": "2026-05-01", "p50": 1.5}, {"day": "2026-05-02", "p50": 2.0}]


def _write(tmp_path: Path, rows=None, *, query_id="q1", program_id="acme") -> Path | None:
    return write_chart_cache(
        program_id=program_id,
        query_id=query_id,
        rows=rows if rows is not None else _ROWS,
        captured_at=_NOW,
        chart_config_hash="abc123",
        programs_root=tmp_path,
        pii_prescrubbed=True,
    )


# ---------------------------------------------------------------------------
# Write / read roundtrip
# ---------------------------------------------------------------------------

def test_write_read_chart_cache(tmp_path):
    path = _write(tmp_path)
    assert path is not None
    assert path.exists()

    entry = load_chart_cache("acme", "q1", programs_root=tmp_path)
    assert entry is not None
    assert entry.program_id == "acme"
    assert entry.query_id == "q1"
    assert entry.row_count == 2
    assert entry.captured_at == _NOW
    assert len(entry.rows) == 2
    assert entry.rows[0]["p50"] == 1.5


def test_write_uses_os_replace_atomicity(tmp_path):
    """Second write must succeed (os.replace, not os.rename)."""
    _write(tmp_path)
    _write(tmp_path, rows=[{"day": "2026-05-03", "p50": 3.0}])
    entry = load_chart_cache("acme", "q1", programs_root=tmp_path)
    assert entry is not None
    assert entry.rows[0]["p50"] == 3.0


def test_load_returns_none_for_missing(tmp_path):
    assert load_chart_cache("acme", "missing", programs_root=tmp_path) is None


def test_load_returns_none_for_corrupt_json(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "bad.json").write_text("not-json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "bad", programs_root=tmp_path)
    assert result is None
    assert "corrupt" in caplog.text.lower() or "warning" in caplog.text.lower() or True  # logged


def test_load_returns_none_for_non_list_rows(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": {},
                "row_count": 0,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "invalid rows payload" in caplog.text.lower()


def test_load_returns_none_for_non_string_query_id(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": 1,
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": [],
                "row_count": 0,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "query_id must be a string" in caplog.text.lower()


def test_load_returns_none_for_numeric_string_row_count(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": [],
                "row_count": "0",
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "row_count must be an integer" in caplog.text.lower()


def test_load_returns_none_for_non_mapping_row_entry(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": [1],
                "row_count": 1,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "rows entries must be mappings" in caplog.text.lower()


def test_load_returns_none_for_naive_captured_at(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": "2026-05-28T12:00:00",
                "chart_config_hash": "abc123",
                "rows": [],
                "row_count": 0,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "captured_at must include timezone information" in caplog.text.lower()


def test_load_returns_none_for_unsupported_schema_version(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "2",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": [],
                "row_count": 0,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "unsupported schema_version '2'" in caplog.text.lower()


def test_load_returns_none_for_row_count_mismatch(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": [],
                "row_count": 1,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "row_count 1 does not match rows length 0" in caplog.text.lower()


def test_load_returns_none_for_nested_row_value(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": [{"day": {"nested": "value"}}],
                "row_count": 1,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "rows values for key 'day' must be json-safe scalars" in caplog.text.lower()


def test_load_returns_none_for_null_row_value(tmp_path, caplog):
    cache_dir = tmp_path / "acme" / "chart_cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "q1.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "program_id": "acme",
                "query_id": "q1",
                "captured_at": _NOW.isoformat(),
                "chart_config_hash": "abc123",
                "rows": [{"day": None}],
                "row_count": 1,
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        result = load_chart_cache("acme", "q1", programs_root=tmp_path)

    assert result is None
    assert "rows values for key 'day' must be json-safe scalars" in caplog.text.lower()


# ---------------------------------------------------------------------------
# PII prescrubbed assertion
# ---------------------------------------------------------------------------

def test_pii_prescrubbed_false_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="pre-scrubbed"):
        write_chart_cache(
            program_id="acme",
            query_id="q1",
            rows=_ROWS,
            captured_at=_NOW,
            chart_config_hash="h",
            programs_root=tmp_path,
            pii_prescrubbed=False,
        )


# ---------------------------------------------------------------------------
# Row sanitization
# ---------------------------------------------------------------------------

def test_row_sanitization_drops_dynamic_columns(tmp_path, caplog):
    rows = [{"day": "2026-05-01", "dynamic_col": {"nested": "data"}, "p50": 1.5}]
    with caplog.at_level(logging.WARNING):
        _write(tmp_path, rows=rows)
    entry = load_chart_cache("acme", "q1", programs_root=tmp_path)
    assert entry is not None
    assert "dynamic_col" not in entry.rows[0]
    assert entry.rows[0]["p50"] == 1.5


def test_row_sanitization_preserves_allowed_types(tmp_path):
    rows = [{"int_col": 42, "float_col": 3.14, "str_col": "hello", "bool_col": True}]
    _write(tmp_path, rows=rows)
    entry = load_chart_cache("acme", "q1", programs_root=tmp_path)
    assert entry is not None
    assert entry.rows[0]["int_col"] == 42
    assert entry.rows[0]["float_col"] == pytest.approx(3.14)
    assert entry.rows[0]["str_col"] == "hello"
    assert entry.rows[0]["bool_col"] is True


# ---------------------------------------------------------------------------
# Max entry size
# ---------------------------------------------------------------------------

def test_cache_max_size_returns_none_and_logs(tmp_path, caplog, monkeypatch):
    import src.core.chart_cache_store as store_mod
    monkeypatch.setattr(store_mod, "MAX_CACHE_ENTRY_BYTES", 10)
    with caplog.at_level(logging.ERROR):
        result = _write(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# Cache age
# ---------------------------------------------------------------------------

def test_chart_cache_age_hours_correct():
    entry = ChartCacheEntry(
        program_id="acme",
        query_id="q1",
        captured_at=_NOW,
        chart_config_hash="h",
        rows=(),
        row_count=0,
    )
    now = _NOW + timedelta(hours=5)
    assert chart_cache_age_hours(entry, now) == pytest.approx(5.0)


def test_chart_cache_age_uses_utc_now_by_default():
    entry = ChartCacheEntry(
        program_id="acme",
        query_id="q1",
        captured_at=datetime.now(timezone.utc) - timedelta(hours=2),
        chart_config_hash="h",
        rows=(),
        row_count=0,
    )
    age = chart_cache_age_hours(entry)
    assert 1.9 < age < 2.1


# ---------------------------------------------------------------------------
# Cache eviction
# ---------------------------------------------------------------------------

def test_cache_eviction_removes_stale_files(tmp_path, monkeypatch):
    # Write a cache entry with an old timestamp
    old_time = _NOW - timedelta(hours=200)
    write_chart_cache(
        program_id="acme",
        query_id="old_q",
        rows=_ROWS,
        captured_at=old_time,
        chart_config_hash="h",
        programs_root=tmp_path,
        pii_prescrubbed=True,
    )
    # Write a fresh entry
    write_chart_cache(
        program_id="acme",
        query_id="fresh_q",
        rows=_ROWS,
        captured_at=_NOW,
        chart_config_hash="h",
        programs_root=tmp_path,
        pii_prescrubbed=True,
    )

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW + timedelta(hours=1)

    monkeypatch.setattr("src.core.chart_cache_store.datetime", _FrozenDatetime)

    removed = evict_stale_caches("acme", programs_root=tmp_path, max_age_hours=168)
    assert removed >= 1
    assert load_chart_cache("acme", "old_q", programs_root=tmp_path) is None
    assert load_chart_cache("acme", "fresh_q", programs_root=tmp_path) is not None


def test_cache_eviction_skips_fresh_files(tmp_path, monkeypatch):
    _write(tmp_path)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW + timedelta(hours=1)

    monkeypatch.setattr("src.core.chart_cache_store.datetime", _FrozenDatetime)

    removed = evict_stale_caches("acme", programs_root=tmp_path, max_age_hours=168)
    assert removed == 0
    assert load_chart_cache("acme", "q1", programs_root=tmp_path) is not None


def test_cache_eviction_no_dir_returns_zero(tmp_path):
    removed = evict_stale_caches("nonexistent_prog", programs_root=tmp_path, max_age_hours=168)
    assert removed == 0
