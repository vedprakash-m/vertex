# Phase 1 — Theme context for chart rendering
"""
ThemeContext and ChartThemeContext dataclasses for chart renderers.
Kept in Zone A — no AI or M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ChartThemeContext:
    """Chart-specific rendering parameters derived from edition config."""
    matplotlib_font_family: str = "sans-serif"
    matplotlib_font_precedence: tuple[str, ...] = (
        "Segoe UI",
        "DejaVu Sans",
        "Helvetica",
        "Arial",
    )
    dpi: int = 150
    figure_facecolor: str = "#FFFFFF"
    grid_alpha: float = 0.3


@dataclass(frozen=True, slots=True)
class ThemeContext:
    """Unified theme for chart renderers. content_width_px is binding at 608."""
    font_family: str = "Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif"
    content_width_px: int = 608
    text_color: str = "#111827"
    secondary_text_color: str = "#6B7280"
    border_color: str = "#E5E7EB"
    grid_color: str = "#E5E7EB"
    palette: tuple[str, ...] = ("#2563EB", "#DC2626", "#059669", "#D97706", "#7C3AED")
    warning_bg_color: str = "#FFFBEB"
    warning_border_color: str = "#F59E0B"
    warning_text_color: str = "#92400E"
    chart: ChartThemeContext = field(default_factory=ChartThemeContext)


def build_theme_context(
    edition_charts_enabled: bool = True,
    edition_brand_name: str | None = None,
    edition_brand_header_url: str | None = None,
) -> ThemeContext:
    """
    Build a ThemeContext from edition configuration.
    R3: brand values are passed through but do not alter chart rendering
    (brand headers are surface-level, not chart-level).
    """
    # edition_charts_enabled and brand values are available for future use
    # but R3 does not alter chart rendering from edition config in this function.
    # Theme values are the defaults specified in ThemeContext dataclass.
    return ThemeContext()