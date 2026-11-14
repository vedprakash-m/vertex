"""Unit tests for chart_renderer_registry.py — spec §11."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

import pytest

from src.core.chart_renderer_registry import ChartRendererRegistry, build_default_registry


class _StubRenderer:
    def build(self, query_id, chart_config, rows, columns, theme) -> tuple[bytes, dict[str, Any]] | None:
        return b"\x89PNG", {"renderer": "stub"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registry_register_and_get():
    reg = ChartRendererRegistry()
    stub = _StubRenderer()
    reg.register("acme::stub", stub)
    assert reg.get_renderer("acme::stub") is stub


def test_registry_duplicate_id_raises_value_error():
    reg = ChartRendererRegistry()
    stub = _StubRenderer()
    reg.register("acme::stub", stub)
    with pytest.raises(ValueError, match="Duplicate"):
        reg.register("acme::stub", _StubRenderer())


def test_registry_namespace_enforced():
    reg = ChartRendererRegistry()
    with pytest.raises(ValueError, match="namespaced"):
        reg.register("no_namespace", _StubRenderer())


def test_registry_get_unknown_returns_declarative():
    reg = ChartRendererRegistry()
    builder = reg.get_renderer("acme::nonexistent")
    # Should return the declarative builder, not None
    assert builder is not None
    assert hasattr(builder, "build")


def test_registry_get_none_returns_declarative():
    reg = ChartRendererRegistry()
    builder = reg.get_renderer(None)
    assert builder is not None
    assert hasattr(builder, "build")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_registry_instances_are_isolated():
    reg1 = ChartRendererRegistry()
    reg2 = ChartRendererRegistry()
    reg1.register("acme::r1", _StubRenderer())
    # reg2 must not see reg1's renderer
    builder = reg2.get_renderer("acme::r1")
    # Should fall back to declarative, not the stub
    from src.core.charts.declarative import DeclarativeChartBuilder
    assert isinstance(builder, DeclarativeChartBuilder)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def test_registry_merge_succeeds():
    reg1 = ChartRendererRegistry()
    reg2 = ChartRendererRegistry()
    reg1.register("acme::r1", _StubRenderer())
    reg2.register("acme::r2", _StubRenderer())
    reg1.merge(reg2)
    assert reg1.get_renderer("acme::r2") is not None


def test_registry_merge_collision_raises():
    reg1 = ChartRendererRegistry()
    reg2 = ChartRendererRegistry()
    reg1.register("acme::r1", _StubRenderer())
    reg2.register("acme::r1", _StubRenderer())
    with pytest.raises(ValueError, match="Duplicate"):
        reg1.merge(reg2)


# ---------------------------------------------------------------------------
# Default registry (auto-discovery)
# ---------------------------------------------------------------------------

def test_build_default_registry_returns_registry():
    reg = build_default_registry()
    assert isinstance(reg, ChartRendererRegistry)


def test_build_default_registry_includes_declarative():
    reg = build_default_registry()
    # vertex::declarative is auto-discovered from charts/declarative.py
    builder = reg.get_renderer("vertex::declarative")
    assert builder is not None
    assert hasattr(builder, "build")


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
@pytest.mark.parametrize("failure", ["invalid_color", "savefig"])
def test_builtin_renderer_failure_log_does_not_expose_exception_text(
    renderer_id, failure, monkeypatch, caplog,
):
    pytest.importorskip("matplotlib")
    from matplotlib.figure import Figure
    from src.core.theme_context import ThemeContext

    sample = "renderer-sample@example.invalid"
    config = {"type": "line", "x_axis": "day", "y_axes": ["value"]}
    if failure == "invalid_color":
        config["palette"] = [sample]
    else:
        def fail(*args, **kwargs):
            raise RuntimeError(sample)

        monkeypatch.setattr(Figure, "savefig", fail)
    rows = ({"day": "first", "value": 1.0}, {"day": "last", "value": 2.0})
    caplog.set_level("WARNING")

    result = build_default_registry().get_renderer(renderer_id).build(
        "synthetic-chart", config, rows, ("day", "value"), ThemeContext(),
    )

    assert result is None
    assert "rendering failed" in caplog.text
    assert "synthetic-chart" in caplog.text
    assert sample not in caplog.text


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
@pytest.mark.parametrize("series", ["p50", "p75"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), 10**400],
    ids=["nan", "positive-infinity", "negative-infinity", "overflow"],
)
def test_builtin_renderers_refuse_unusable_numeric_values(renderer_id, series, value, caplog):
    pytest.importorskip("matplotlib")
    from src.core.theme_context import ThemeContext

    builder = build_default_registry().get_renderer(renderer_id)
    rows = (
        {"day": "first", "p50": 1.0, "p75": 2.0},
        {"day": "last", "p50": 3.0, "p75": 4.0, series: value},
    )
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50", "p75"]}

    result = builder.build("synthetic-chart", config, rows, ("day", "p50", "p75"), ThemeContext())

    assert result is None
    assert "numeric chart value is not finite or representable" in caplog.text
    assert "synthetic-chart" not in caplog.text


@pytest.mark.parametrize(
    ("renderer_id", "chart_type"),
    [
        ("vertex::declarative", "line"),
        ("vertex::declarative", "bar"),
        ("vertex::declarative", "stacked_bar"),
        ("vertex::declarative", "scatter"),
        ("vertex::declarative", "combined"),
        ("core::deployment_velocity", "line"),
    ],
)
def test_builtin_renderers_plot_decimal_values_like_floats(renderer_id, chart_type):
    pytest.importorskip("matplotlib")
    from src.core.theme_context import ThemeContext

    builder = build_default_registry().get_renderer(renderer_id)
    rows = (
        {"day": "first", "p50": Decimal("0.1"), "p75": 2.0},
        {"day": "middle", "p50": 2.0, "p75": Decimal("3.5")},
        {"day": "last", "p50": Decimal("2.5"), "p75": Decimal("4.0")},
    )
    float_rows = tuple({key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()} for row in rows)
    config = {"type": chart_type, "x_axis": "day", "y_axes": ["p50", "p75"]}
    columns = ("day", "p50", "p75")

    result = builder.build("synthetic-chart", config, rows, columns, ThemeContext())
    expected = builder.build("synthetic-chart", config, float_rows, columns, ThemeContext())

    assert expected is not None
    assert result == expected


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
@pytest.mark.parametrize("value", ["0", "-0", "1e-300", "-1e-300"])
def test_builtin_renderers_accept_zero_and_representable_small_decimals(renderer_id, value):
    pytest.importorskip("matplotlib")
    from src.core.theme_context import ThemeContext

    builder = build_default_registry().get_renderer(renderer_id)
    decimal_rows = ({"day": "first", "p50": Decimal(value)}, {"day": "last", "p50": 2.0})
    float_rows = ({"day": "first", "p50": float(Decimal(value))}, {"day": "last", "p50": 2.0})
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50"]}
    columns = ("day", "p50")

    result = builder.build("synthetic-chart", config, decimal_rows, columns, ThemeContext())
    expected = builder.build("synthetic-chart", config, float_rows, columns, ThemeContext())

    assert expected is not None
    assert result == expected


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
@pytest.mark.parametrize("series", ["p50", "p75"])
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity", "1e10000", "1e-10000", "-1e-10000"])
def test_builtin_renderers_refuse_unusable_decimal_values(renderer_id, series, value, caplog):
    pytest.importorskip("matplotlib")
    from src.core.theme_context import ThemeContext

    builder = build_default_registry().get_renderer(renderer_id)
    rows = (
        {"day": "first", "p50": 1.0, "p75": 2.0},
        {"day": "last", "p50": 3.0, "p75": 4.0, series: Decimal(value)},
    )
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50", "p75"]}

    result = builder.build("synthetic-chart", config, rows, ("day", "p50", "p75"), ThemeContext())

    assert result is None
    assert "numeric chart value is not finite or representable" in caplog.text
    assert "synthetic-chart" not in caplog.text


@pytest.mark.parametrize(
    ("renderer_id", "expected"),
    [
        ("vertex::declarative", "p50 trending up (+100.0%)"),
        ("core::deployment_velocity", "Insufficient data for trend"),
    ],
)
def test_builtin_renderers_preserve_missing_values_as_gaps(renderer_id, expected):
    pytest.importorskip("matplotlib")
    from src.core.theme_context import ThemeContext

    builder = build_default_registry().get_renderer(renderer_id)
    rows = ({"day": "first", "p50": 2.0}, {"day": "gap", "p50": None}, {"day": "last", "p50": 4.0})
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50"]}

    result = builder.build("synthetic-chart", config, rows, ("day", "p50"), ThemeContext())

    assert result is not None
    assert result[1]["trend_description"] == expected


@pytest.mark.parametrize(
    ("renderer", "values", "expected"),
    [
        ("declarative", [1e-308, 1e308], "No trend data available"),
        ("velocity", [1e-308, 1e308, 1e308], "No trend data"),
    ],
)
def test_builtin_renderers_withhold_unrepresentable_trend(renderer, values, expected, caplog):
    if renderer == "declarative":
        from src.core.charts.declarative import DeclarativeChartBuilder

        trend = DeclarativeChartBuilder()._derive_trend_description([values], ["p50"])
    else:
        from src.core.charts.deployment_velocity import _derive_trend

        trend = _derive_trend([values], ["p50"])

    assert trend == expected
    assert "trend calculation is not finite" in caplog.text


@pytest.mark.parametrize(
    ("renderer_id", "expected"),
    [
        ("vertex::declarative", "No trend data available"),
        ("core::deployment_velocity", "No trend data"),
    ],
)
def test_builtin_renderers_withhold_unrepresentable_trend_in_chart(renderer_id, expected):
    pytest.importorskip("matplotlib")
    from src.core.theme_context import ThemeContext

    builder = build_default_registry().get_renderer(renderer_id)
    rows = ({"day": "first", "p50": 1e-300}, {"day": "middle", "p50": 1e10}, {"day": "last", "p50": 1e10})
    config = {"type": "line", "x_axis": "day", "y_axes": ["p50"]}

    result = builder.build("synthetic-chart", config, rows, ("day", "p50"), ThemeContext())

    assert result is not None
    assert result[1]["trend_description"] == expected
