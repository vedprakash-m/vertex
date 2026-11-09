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


@pytest.mark.parametrize("chart_type", ["line", "bar", "stacked_bar", "scatter", "combined"])
@pytest.mark.parametrize("missing_index", [0, 1, 2])
def test_missing_y_axis_keeps_rendered_series_and_metadata_aligned(chart_type, missing_index, caplog, monkeypatch):
    from matplotlib.figure import Figure

    original_savefig = Figure.savefig
    legends = []

    def capture_legend(figure, buffer, *args, **kwargs):
        legend = figure.axes[0].get_legend()
        legends.append([text.get_text() for text in legend.get_texts()] if legend else [])
        return original_savefig(figure, buffer, *args, **kwargs)

    monkeypatch.setattr(Figure, "savefig", capture_legend)
    rows = tuple(_ROWS[:6])
    valid_axes = ["p50", "p75"]
    partial_axes = valid_axes.copy()
    partial_axes.insert(missing_index, "unavailable")
    config = {"type": chart_type, "x_axis": "day", "y_axes": valid_axes}

    expected = _BUILDER.build("valid", config, rows, _COLS, _THEME)
    actual = _BUILDER.build("partial", {**config, "y_axes": partial_axes}, rows, _COLS, _THEME)

    assert expected is not None and actual is not None
    assert legends == [valid_axes, valid_axes]
    assert actual[1]["series"] == valid_axes
    assert actual[1]["trend_description"].startswith("p50 trending ")
    assert "unavailable" not in actual[1]["alt_text"]
    assert actual[1] == expected[1]
    assert actual[0] == expected[0]
    assert "y_axis column 'unavailable' not found" in caplog.text


def test_missing_y_axis_preserves_explicit_alt_text():
    config = {
        "type": "line",
        "x_axis": "day",
        "y_axes": ["unavailable", "p50"],
        "alt_text": "Authored chart description",
    }

    result = _BUILDER.build("partial", config, tuple(_ROWS[:6]), _COLS, _THEME)

    assert result is not None
    assert result[1]["alt_text"] == "Authored chart description"
    assert result[1]["series"] == ["p50"]


@pytest.mark.parametrize("chart_type", ["line", "bar", "scatter"])
def test_goal_lines_survive_reduced_height_render(chart_type, monkeypatch):
    from importlib import import_module
    from matplotlib.figure import Figure

    declarative = import_module("src.core.charts.declarative")
    original_savefig = Figure.savefig
    goal_values = []

    def capture_goals(figure, buffer, *args, **kwargs):
        goal_values.append([
            tuple(line.get_ydata())
            for line in figure.axes[0].lines
            if line.get_linestyle() == "--"
        ])
        return original_savefig(figure, buffer, *args, **kwargs)

    monkeypatch.setattr(declarative, "PNG_TARGET_BYTES", 1)
    monkeypatch.setattr(Figure, "savefig", capture_goals)
    config = {
        "type": chart_type,
        "x_axis": "day",
        "y_axes": ["p50"],
        "goal_lines": [
            {"label": "Target", "value": 2.5},
            {"label": "Stretch", "value": 4.5},
            {"label": "Missing value"},
        ],
    }

    result = _BUILDER.build("goals", config, tuple(_ROWS[:6]), _COLS, _THEME)

    assert result is not None
    assert len(result[0]) <= PNG_HARD_GATE_BYTES
    assert goal_values == [[(2.5, 2.5), (4.5, 4.5)], [(2.5, 2.5), (4.5, 4.5)]]


@pytest.mark.parametrize("chart_type", ["stacked_bar", "combined"])
def test_non_resizable_charts_keep_their_original_goal_lines(chart_type, monkeypatch):
    from importlib import import_module
    from matplotlib.figure import Figure

    declarative = import_module("src.core.charts.declarative")
    original_savefig = Figure.savefig
    goal_values = []

    def capture_goals(figure, buffer, *args, **kwargs):
        goal_values.append([
            tuple(line.get_ydata())
            for line in figure.axes[0].lines
            if line.get_linestyle() == "--"
        ])
        return original_savefig(figure, buffer, *args, **kwargs)

    monkeypatch.setattr(declarative, "PNG_TARGET_BYTES", 1)
    monkeypatch.setattr(Figure, "savefig", capture_goals)
    config = {
        "type": chart_type,
        "x_axis": "day",
        "y_axes": ["p50", "p75"],
        "goal_lines": [{"label": "Target", "value": 2.5}],
    }

    result = _BUILDER.build("goals", config, tuple(_ROWS[:6]), _COLS, _THEME)

    assert result is not None
    assert goal_values == [[(2.5, 2.5)]]


@pytest.mark.parametrize("chart_type", ["line", "bar", "scatter"])
@pytest.mark.parametrize(
    ("sizes", "expected_size"),
    [
        ((PNG_TARGET_BYTES,), PNG_TARGET_BYTES),
        ((PNG_TARGET_BYTES + 1, PNG_HARD_GATE_BYTES), PNG_HARD_GATE_BYTES),
        ((PNG_TARGET_BYTES + 1, PNG_HARD_GATE_BYTES + 1), None),
        ((PNG_HARD_GATE_BYTES, PNG_TARGET_BYTES), PNG_TARGET_BYTES),
        ((PNG_HARD_GATE_BYTES + 1,), None),
    ],
)
def test_final_png_respects_existing_size_gate(chart_type, sizes, expected_size, monkeypatch, caplog):
    from matplotlib.figure import Figure

    calls = []

    def save_sized_png(_figure, buffer, *args, **kwargs):
        size = sizes[len(calls)]
        calls.append(size)
        buffer.write(b"\x89PNG" + b"x" * (size - 4))

    monkeypatch.setattr(Figure, "savefig", save_sized_png)
    config = {"type": chart_type, "x_axis": "day", "y_axes": ["p50"]}

    result = _BUILDER.build("size", config, tuple(_ROWS[:6]), _COLS, _THEME)

    assert calls == list(sizes)
    if expected_size is None:
        assert result is None
        assert "exceeds hard gate" in caplog.text
    else:
        assert result is not None
        assert len(result[0]) == expected_size


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
