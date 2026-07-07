# Phase 1 — Chart renderer registry
"""
Chart renderer registry with discoverable platform and program renderers.
All chart renderers live in Zone A — no AI or M365 imports.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

# Chart output: (png_bytes, metadata_dict)
ChartOutput = tuple[bytes, dict[str, Any]] | None


class ChartBuilder(Protocol):
    """Protocol for chart renderer builders."""

    def build(
        self,
        query_id: str,
        chart_config: Mapping[str, Any],
        rows: tuple[dict[str, Any], ...],
        columns: tuple[str, ...],
        theme: Any,  # ThemeContext — imported at runtime to avoid circular
    ) -> ChartOutput: ...


class ChartRendererRegistry:
    """
    Instance-based chart renderer registry.
    Tests create isolated instances; production uses a singleton.
    """

    def __init__(self) -> None:
        self._renderers: dict[str, ChartBuilder] = {}

    def register(self, renderer_id: str, renderer: ChartBuilder) -> None:
        """Register a named chart renderer. Raises ValueError on duplicate ID or missing namespace."""
        if "::" not in renderer_id:
            raise ValueError(
                f"Renderer ID must be program-namespaced (e.g., '<program>::name'): {renderer_id}"
            )
        if renderer_id in self._renderers:
            raise ValueError(f"Duplicate chart_renderer_id: {renderer_id}")
        logger.debug("chart_renderer_registry: registered '%s'", renderer_id)
        self._renderers[renderer_id] = renderer

    def get_renderer(self, renderer_id: str | None) -> ChartBuilder:
        """Return the registered renderer, or the default declarative builder if None/not found."""
        if renderer_id and renderer_id in self._renderers:
            return self._renderers[renderer_id]
        # Return the declarative builder (lazy import to avoid circular)
        return _get_declarative_builder()

    def merge(self, other: ChartRendererRegistry) -> None:
        """Merge another registry into this one. Raises ValueError on ID collisions."""
        for renderer_id, renderer in other._renderers.items():
            if renderer_id in self._renderers:
                raise ValueError(f"Duplicate renderer ID during merge: {renderer_id}")
            self._renderers[renderer_id] = renderer


# Module-level default registry for production use
_default_registry: ChartRendererRegistry | None = None


def get_default_registry() -> ChartRendererRegistry:
    """Get or build the singleton default registry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry


def reset_default_registry() -> None:
    """Reset the default registry (for testing only)."""
    global _default_registry
    _default_registry = None


def build_default_registry() -> ChartRendererRegistry:
    """
    Construct registry with all known platform renderers.
    Called once at app startup. Discovers renderers from src/core/charts/.
    """
    registry = ChartRendererRegistry()

    # Discover platform renderers from src/core/charts/
    import importlib
    import pkgutil
    from pathlib import Path

    charts_path = Path(__file__).parent / "charts"
    if charts_path.exists():
        for _module_info in pkgutil.iter_modules([str(charts_path)]):
            if _module_info.name.startswith("_"):
                continue
            try:
                _mod = importlib.import_module(f"src.core.charts.{_module_info.name}")
                if hasattr(_mod, "CHART_RENDERERS"):
                    for rid, builder in _mod.CHART_RENDERERS.items():
                        registry.register(rid, builder)
            except Exception as exc:
                logger.warning(
                    "chart_renderer_registry: failed to load renderer module '%s' — %s",
                    _module_info.name,
                    exc,
                )

    return registry


def _get_declarative_builder() -> ChartBuilder:
    """Lazily import the declarative builder to avoid circular import."""
    from src.core.charts.declarative import DeclarativeChartBuilder
    return DeclarativeChartBuilder()