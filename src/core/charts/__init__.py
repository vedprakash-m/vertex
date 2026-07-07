# Phase 1 — charts package init
"""
Chart renderer discovery package.
Each renderer module in this package must expose CHART_RENDERERS: dict[str, ChartBuilder].
"""
from src.core.charts.declarative import CHART_RENDERERS as declarative

# Export all discovered renderers for registry discovery
__all__ = ["declarative"]