"""Guards the D-09 / Phase 3 peel of the chart gate cluster."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.core.quality_gates import evaluate_chart_gates
from src.core.quality_gates.chart import evaluate_chart_gates as chart_module_entry


_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


def _make_section(
    *,
    query_id: str = "q1",
    render_mode: str = "chart",
    is_degraded: bool = False,
    chart_png_size_bytes: int = 50_000,
    chart_cache_ttl_hours: int = 26,
    chart_blocks_publish: bool = False,
    captured_at: datetime | None = None,
    cache_captured_at: datetime | None = None,
):
    section = MagicMock()
    section.query_id = query_id
    section.render_mode = render_mode
    section.is_degraded = is_degraded
    section.chart_png_size_bytes = chart_png_size_bytes
    section.chart_cache_ttl_hours = chart_cache_ttl_hours
    section.chart_blocks_publish = chart_blocks_publish
    section.captured_at = captured_at or _NOW
    section.cache_captured_at = cache_captured_at
    section.chart_png_base64 = "dGVzdA=="
    return section


def test_chart_entry_point_is_reexported() -> None:
    assert evaluate_chart_gates is chart_module_entry


def test_chart_gates_skip_when_feature_disabled() -> None:
    report = chart_module_entry((), current_time=_NOW, edition_charts_enabled=False)

    assert report.results == ()


def test_chart_gates_flag_stale_and_oversized_charts() -> None:
    section = _make_section(
        is_degraded=True,
        cache_captured_at=_NOW - timedelta(hours=50),
        chart_png_size_bytes=120_000,
        chart_blocks_publish=True,
    )

    report = chart_module_entry((section,), current_time=_NOW)

    assert report.qg_results["QG-20"] is False
    assert report.qg_results["QG-21"] is False
    assert report.qg_results["QG-22"] is False
