"""ADF-W5.13: src/core/weekly_metrics_store.py."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.weekly_metrics_store import (
    compute_weekly_rollup,
    query_weekly_aggregates,
    rollup_jsonl_family_for_week,
)

_ISO_YEAR = 2026
_ISO_WEEK = 28  # arbitrary


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_compute_weekly_rollup_returns_none_when_no_rows_in_week() -> None:
    from datetime import date as _date

    week_start, week_end = _date.fromisocalendar(_ISO_YEAR, _ISO_WEEK, 1), None
    out_of_week_ts = "2020-01-01T00:00:00Z"
    result = compute_weekly_rollup(
        [{"recorded_at": out_of_week_ts, "latency_ms": 10}],
        program_id="xpf", measurement_family="tier_decisions",
        iso_year=_ISO_YEAR, iso_week=_ISO_WEEK, timestamp_field="recorded_at",
        numeric_fields=("latency_ms",),
    )
    assert result is None


def test_compute_weekly_rollup_counts_and_averages_numeric_fields() -> None:
    week_start = date.fromisocalendar(_ISO_YEAR, _ISO_WEEK, 3)  # Wednesday of that week
    ts = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    rows = [
        {"recorded_at": ts, "latency_ms": 10.0},
        {"recorded_at": ts, "latency_ms": 20.0},
        {"recorded_at": ts, "latency_ms": 30.0},
    ]
    result = compute_weekly_rollup(
        rows, program_id="xpf", measurement_family="tier_decisions",
        iso_year=_ISO_YEAR, iso_week=_ISO_WEEK, timestamp_field="recorded_at",
        numeric_fields=("latency_ms",),
    )
    assert result is not None
    assert result.record_count == 3
    assert result.metrics["latency_ms_mean"] == 20.0
    assert result.metrics["latency_ms_max"] == 30.0
    assert result.iso_year == _ISO_YEAR
    assert result.iso_week == _ISO_WEEK


def test_rollup_jsonl_family_for_week_writes_and_is_queryable(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    source_path = programs_root / "xpf" / "runtime" / "tier_decisions.jsonl"
    week_start = date.fromisocalendar(_ISO_YEAR, _ISO_WEEK, 2)
    ts = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    _write_jsonl(source_path, [
        {"recorded_at": ts, "feature": "claim_extractor"},
        {"recorded_at": ts, "feature": "risk_proposal_generator"},
    ])

    record = rollup_jsonl_family_for_week(
        source_path, program_id="xpf", measurement_family="tier_decisions",
        iso_year=_ISO_YEAR, iso_week=_ISO_WEEK, timestamp_field="recorded_at",
        programs_root=programs_root,
    )
    assert record is not None
    assert record.record_count == 2

    queried = query_weekly_aggregates("xpf", "tier_decisions", programs_root=programs_root)
    assert len(queried) == 1
    assert queried[0].record_count == 2


def test_rollup_returns_none_when_source_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    result = rollup_jsonl_family_for_week(
        programs_root / "xpf" / "runtime" / "does_not_exist.jsonl",
        program_id="xpf", measurement_family="ai_telemetry",
        iso_year=_ISO_YEAR, iso_week=_ISO_WEEK, timestamp_field="ts",
        programs_root=programs_root,
    )
    assert result is None


def test_query_returns_empty_tuple_when_no_aggregates(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert query_weekly_aggregates("xpf", "ai_telemetry", programs_root=programs_root) == ()


def test_query_respects_since_weeks_window(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    source_path = programs_root / "xpf" / "runtime" / "tier_decisions.jsonl"

    # Week far in the past (outside a 4-week query window).
    old_week_start = date.fromisocalendar(2020, 1, 2)
    old_ts = datetime.combine(old_week_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    _write_jsonl(source_path, [{"recorded_at": old_ts}])
    rollup_jsonl_family_for_week(
        source_path, program_id="xpf", measurement_family="tier_decisions",
        iso_year=2020, iso_week=1, timestamp_field="recorded_at", programs_root=programs_root,
    )

    recent = query_weekly_aggregates(
        "xpf", "tier_decisions", programs_root=programs_root, since_weeks=4,
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    assert recent == ()  # the 2020 week is well outside a 4-week window from 2026


def test_multiple_weeks_query_sorted_oldest_first(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    source_path = programs_root / "xpf" / "runtime" / "tier_decisions.jsonl"
    week1_start = date.fromisocalendar(_ISO_YEAR, _ISO_WEEK, 2)
    week2_start = date.fromisocalendar(_ISO_YEAR, _ISO_WEEK + 1, 2)
    ts1 = datetime.combine(week1_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    ts2 = datetime.combine(week2_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    _write_jsonl(source_path, [{"recorded_at": ts1}, {"recorded_at": ts2}])

    rollup_jsonl_family_for_week(
        source_path, program_id="xpf", measurement_family="tier_decisions",
        iso_year=_ISO_YEAR, iso_week=_ISO_WEEK + 1, timestamp_field="recorded_at", programs_root=programs_root,
    )
    rollup_jsonl_family_for_week(
        source_path, program_id="xpf", measurement_family="tier_decisions",
        iso_year=_ISO_YEAR, iso_week=_ISO_WEEK, timestamp_field="recorded_at", programs_root=programs_root,
    )

    queried = query_weekly_aggregates("xpf", "tier_decisions", programs_root=programs_root)
    assert len(queried) == 2
    assert queried[0].iso_week == _ISO_WEEK
    assert queried[1].iso_week == _ISO_WEEK + 1
