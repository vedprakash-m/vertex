"""Unit tests for QG-20, QG-21, QG-22 quality gates — spec §11."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from src.core.quality_gates import evaluate_chart_gates


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
    # chart_png_base64 is non-None so the section is visible to gate evaluation
    section.chart_png_base64 = "dGVzdA=="
    return section


def _eval(sections, edition_charts_enabled=True):
    return evaluate_chart_gates(
        tuple(sections),
        current_time=_NOW,
        edition_charts_enabled=edition_charts_enabled,
    )


# ---------------------------------------------------------------------------
# QG-20: Chart data freshness (advisory)
# ---------------------------------------------------------------------------

def test_qg20_passes_when_not_degraded():
    section = _make_section(is_degraded=False)
    report = _eval([section])
    passing = [r for r in report.results if r.gate_id == "QG-20"]
    assert all(r.passed for r in passing)


def test_qg20_advisory_on_stale_cache():
    # cache age > ttl * 1.5
    stale_time = _NOW - timedelta(hours=40)  # ttl=26, threshold=39h
    section = _make_section(
        is_degraded=True,
        cache_captured_at=stale_time,
        chart_cache_ttl_hours=26,
    )
    report = _eval([section])
    qg20 = [r for r in report.results if r.gate_id == "QG-20"]
    assert qg20, "QG-20 evaluation must be present"
    assert not qg20[0].passed


def test_qg20_within_ttl_passes():
    # age = 25h < ttl=26
    recent_time = _NOW - timedelta(hours=25)
    section = _make_section(
        is_degraded=True,
        cache_captured_at=recent_time,
        chart_cache_ttl_hours=26,
    )
    report = _eval([section])
    qg20 = [r for r in report.results if r.gate_id == "QG-20"]
    # Advisory — should not fail for age < ttl
    assert all(r.passed for r in qg20)


def test_qg20_forceable():
    """QG-20 must be forceable (exit 2 advisory)."""
    stale_time = _NOW - timedelta(hours=50)
    section = _make_section(is_degraded=True, cache_captured_at=stale_time, chart_cache_ttl_hours=26)
    report = _eval([section])
    qg20 = [r for r in report.results if r.gate_id == "QG-20" and not r.passed]
    if qg20:
        assert qg20[0].forceable


# ---------------------------------------------------------------------------
# QG-21: PNG size gate (non-forceable hard block)
# ---------------------------------------------------------------------------

def test_qg21_passes_under_limit():
    section = _make_section(chart_png_size_bytes=80_000)
    report = _eval([section])
    qg21 = [r for r in report.results if r.gate_id == "QG-21"]
    assert all(r.passed for r in qg21)


def test_qg21_blocks_over_limit():
    section = _make_section(chart_png_size_bytes=110_000)
    report = _eval([section])
    qg21 = [r for r in report.results if r.gate_id == "QG-21"]
    assert qg21, "QG-21 must be evaluated"
    assert not qg21[0].passed


def test_qg21_non_forceable():
    section = _make_section(chart_png_size_bytes=110_000)
    report = _eval([section])
    qg21 = [r for r in report.results if r.gate_id == "QG-21" and not r.passed]
    if qg21:
        assert not qg21[0].forceable


# ---------------------------------------------------------------------------
# QG-22: Blocking freshness (chart_blocks_publish=True)
# ---------------------------------------------------------------------------

def test_qg22_blocks_when_publish_blocked_and_stale():
    stale_time = _NOW - timedelta(hours=50)
    section = _make_section(
        is_degraded=True,
        cache_captured_at=stale_time,
        chart_cache_ttl_hours=26,
        chart_blocks_publish=True,
    )
    report = _eval([section])
    qg22 = [r for r in report.results if r.gate_id == "QG-22"]
    assert qg22, "QG-22 must be evaluated when chart_blocks_publish=True"
    assert not qg22[0].passed


def test_qg22_passes_when_not_degraded():
    section = _make_section(is_degraded=False, chart_blocks_publish=True)
    report = _eval([section])
    qg22 = [r for r in report.results if r.gate_id == "QG-22"]
    assert all(r.passed for r in qg22)


def test_qg22_passes_when_blocks_publish_false():
    stale_time = _NOW - timedelta(hours=50)
    section = _make_section(
        is_degraded=True,
        cache_captured_at=stale_time,
        chart_blocks_publish=False,
    )
    report = _eval([section])
    qg22 = [r for r in report.results if r.gate_id == "QG-22" and not r.passed]
    assert not qg22  # QG-22 must pass (advisory via QG-20 only)


# ---------------------------------------------------------------------------
# Non-chart sections are ignored
# ---------------------------------------------------------------------------

def test_non_chart_sections_ignored():
    section = _make_section(render_mode="table", chart_png_size_bytes=200_000)
    report = _eval([section])
    qg21 = [r for r in report.results if r.gate_id == "QG-21" and not r.passed]
    assert not qg21  # table sections never trigger QG-21


# ---------------------------------------------------------------------------
# Empty sections
# ---------------------------------------------------------------------------

def test_no_sections_all_pass():
    report = _eval([])
    for ev in report.results:
        if ev.gate_id in ("QG-20", "QG-21", "QG-22"):
            assert ev.passed
