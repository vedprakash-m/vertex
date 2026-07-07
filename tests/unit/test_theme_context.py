"""Unit tests for theme_context.py — spec §11."""
from __future__ import annotations

import pytest

from src.core.theme_context import ChartThemeContext, ThemeContext, build_theme_context


def test_theme_context_defaults():
    theme = ThemeContext()
    assert theme.content_width_px == 608
    assert theme.text_color == "#111827"
    assert theme.secondary_text_color == "#6B7280"
    assert theme.border_color == "#E5E7EB"
    assert theme.grid_color == "#E5E7EB"
    assert theme.warning_bg_color == "#FFFBEB"
    assert theme.warning_border_color == "#F59E0B"
    assert theme.warning_text_color == "#92400E"
    assert isinstance(theme.palette, tuple)
    assert len(theme.palette) >= 5
    assert theme.palette[0] == "#2563EB"


def test_theme_context_has_chart_sub_context():
    theme = ThemeContext()
    assert isinstance(theme.chart, ChartThemeContext)
    assert theme.chart.dpi == 150
    assert theme.chart.figure_facecolor == "#FFFFFF"


def test_theme_context_is_frozen():
    theme = ThemeContext()
    with pytest.raises((AttributeError, TypeError)):
        theme.text_color = "#000000"  # type: ignore[misc]


def test_build_theme_context_returns_theme():
    theme = build_theme_context()
    assert isinstance(theme, ThemeContext)


def test_build_theme_context_with_kwargs():
    theme = build_theme_context(edition_charts_enabled=False, edition_brand_name="Acme")
    # R3: brand does not alter chart rendering
    assert isinstance(theme, ThemeContext)
    assert theme.content_width_px == 608


def test_theme_context_palette_matches_ux_spec_tokens():
    """Palette colors must match UX spec design tokens."""
    theme = ThemeContext()
    assert "#2563EB" in theme.palette  # Primary blue
    assert "#DC2626" in theme.palette  # Red (risk)
    assert "#059669" in theme.palette  # Green (healthy)
