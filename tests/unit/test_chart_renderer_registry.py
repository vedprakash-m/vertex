"""Unit tests for chart_renderer_registry.py — spec §11."""
from __future__ import annotations

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
