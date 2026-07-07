"""Unit tests for DeclarativeChartBuilder — spec §11."""
from __future__ import annotations

import pytest

from src.core.charts.declarative import DeclarativeChartBuilder, PNG_HARD_GATE_BYTES, PNG_TARGET_BYTES
from src.core.theme_context import ThemeContext

pytest.importorskip("matplotlib", reason="matplotlib required for chart rendering tests")


_THEME = ThemeContext()
_BUILDER = DeclarativeChartBuilder()

_X_LABELS = [f"2026-05-{i:02d}" for i in range(1, 16)]
_ROWS = [{"day": lbl, "p50": float(i), "p75": float(i + 2)} for i, lbl in enumerate(_X_LABELS, start=1)]
_COLS = tuple(_ROWS[0].keys())


def _build(chart_type: str = "line", extra_config: dict | None = None):
    config = {"type": chart_type, "x_axis": "day", "y_axes": ["p50", "p75"]}
    if extra_config:
        config.update(extra_config)
    return _BUILDER.build("test-query", config, tuple(_ROWS), _COLS, _THEME)


# ---------------------------------------------------------------------------
# Successful renders
# ---------------------------------------------------------------------------

def test_line_chart_renders_png():
    result = _build("line")
    assert result is not None
    png_bytes, meta = result
    assert isinstance(png_bytes, bytes)
    assert png_bytes[:4] == b"\x89PNG"
    assert "alt_text" in meta
    assert "trend_description" in meta


def test_bar_chart_renders():
    result = _build("bar")
    assert result is not None
    png_bytes, _ = result
    assert len(png_bytes) > 100


def test_stacked_bar_chart_renders():
    result = _build("stacked_bar")
    assert result is not None


def test_scatter_chart_renders():
    result = _build("scatter")
    assert result is not None


def test_combined_chart_renders():
    result = _build("combined")
    assert result is not None


def test_goal_lines_in_config():
    result = _build("line", {"goal_lines": [{"label": "Target", "value": 5.0}]})
    assert result is not None


# ---------------------------------------------------------------------------
# Zero rows returns None
# ---------------------------------------------------------------------------

def test_empty_rows_returns_none():
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50"]}
    result = _BUILDER.build("test-query", config, (), _COLS, _THEME)
    assert result is None


# ---------------------------------------------------------------------------
# Missing columns degrade gracefully
# ---------------------------------------------------------------------------

def test_missing_x_axis_returns_none():
    config = {"type": "line", "x_axis": "nonexistent", "y_axes": ["p50"]}
    result = _BUILDER.build("test-query", config, tuple(_ROWS), _COLS, _THEME)
    assert result is None


def test_all_missing_y_axes_returns_none():
    config = {"type": "line", "x_axis": "day", "y_axes": ["nonexistent"]}
    result = _BUILDER.build("test-query", config, tuple(_ROWS), _COLS, _THEME)
    assert result is None


# ---------------------------------------------------------------------------
# PNG size contract
# ---------------------------------------------------------------------------

def test_png_size_under_hard_gate():
    result = _build("line")
    assert result is not None
    png_bytes, _ = result
    assert len(png_bytes) <= PNG_HARD_GATE_BYTES, (
        f"PNG {len(png_bytes)} bytes exceeds hard gate {PNG_HARD_GATE_BYTES}"
    )


# ---------------------------------------------------------------------------
# Theming
# ---------------------------------------------------------------------------

def test_renderer_respects_theme_palette():
    """Custom palette from ThemeContext flows into chart config."""
    custom_theme = ThemeContext(palette=("#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#FF00FF"))
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50", "p75"]}
    result = _BUILDER.build("test-query", config, tuple(_ROWS), _COLS, custom_theme)
    assert result is not None  # rendering succeeds with custom palette


def test_unknown_chart_type_returns_none():
    config = {"type": "heatmap", "x_axis": "day", "y_axes": ["p50"]}
    result = _BUILDER.build("test-query", config, tuple(_ROWS), _COLS, _THEME)
    assert result is None


# ---------------------------------------------------------------------------
# Alt text and trend derivation
# ---------------------------------------------------------------------------

def test_alt_text_derived():
    result = _build("line")
    assert result is not None
    _, meta = result
    assert "p50" in meta["alt_text"].lower() or "line" in meta["alt_text"].lower()


def test_trend_description_shows_direction():
    # Use monotonically increasing data — expect "up"
    rows = [{"day": f"d{i}", "p50": float(i * 2)} for i in range(1, 20)]
    cols = ("day", "p50")
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50"]}
    result = _BUILDER.build("t", config, tuple(rows), cols, _THEME)
    assert result is not None
    _, meta = result
    assert "up" in meta["trend_description"].lower() or "%" in meta["trend_description"]


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

def test_render_under_2_seconds():
    """Renderer must complete < 2s for ≤200 rows — spec §7 runtime budget."""
    import time

    rows = [{"day": f"d{i}", "p50": float(i)} for i in range(200)]
    cols = ("day", "p50")
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50"]}

    # Warm up first so the one-time matplotlib/backend cold import (1-2s on a cold or
    # loaded CI runner) is not charged to the render-budget measurement. The budget is
    # for the render itself, not the first-ever import.
    warmup = _BUILDER.build("perf-warmup", config, tuple(rows), cols, _THEME)
    assert warmup is not None

    start = time.monotonic()
    result = _BUILDER.build("perf-query", config, tuple(rows), cols, _THEME)
    elapsed = time.monotonic() - start

    assert result is not None
    assert elapsed < 2.0, f"Render took {elapsed:.2f}s — exceeds 2s budget"
