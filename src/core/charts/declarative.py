# Phase 1 — Declarative chart builder
"""
Declarative chart renderer that produces PNG from chart_config.
Uses matplotlib directly (not pyplot) to avoid global state.
All renderers must stay in Zone A — no AI or M365 imports.
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Target decoded PNG size (bytes) — spec §7
PNG_TARGET_BYTES = 80_000
PNG_HARD_GATE_BYTES = 102_400


class DeclarativeChartBuilder:
    """
    Declarative chart renderer: parses chart_config and produces PNG bytes.
    Used when chart_renderer_id is None or not found in registry.

    Raises ValueError for invalid config. Returns None on rendering failure.
    """

    def build(
        self,
        query_id: str,
        chart_config: Mapping[str, Any],
        rows: tuple[dict[str, Any], ...],
        columns: tuple[str, ...],
        theme: Any,  # ThemeContext from src.core.theme_context
    ) -> tuple[bytes, dict[str, Any]] | None:
        """
        Build a chart PNG and metadata from declarative config.

        Returns (png_bytes, metadata_dict) or None on failure.
        metadata_dict keys: alt_text, trend_description, warnings, series, placeholder_reason
        """
        if not rows:
            return None

        try:
            import matplotlib
            matplotlib.use("Agg")  # Non-interactive backend
            from matplotlib.figure import Figure
        except ImportError:
            logger.warning("declarative_chart: matplotlib not available")
            return None

        chart_type = chart_config.get("type", "line")
        x_axis = chart_config.get("x_axis")
        y_axes = chart_config.get("y_axes", [])

        if not x_axis or not y_axes:
            logger.warning("declarative_chart: missing x_axis or y_axes in config")
            return None

        # Find column indices
        col_index = {col: i for i, col in enumerate(columns)}
        if x_axis not in col_index:
            logger.warning("declarative_chart: x_axis '%s' not found in columns", x_axis)
            return None

        x_idx = col_index[x_axis]
        y_indices: list[int] = []
        for y_col in y_axes:
            if y_col in col_index:
                y_indices.append(col_index[y_col])
            else:
                logger.warning(
                    "declarative_chart: y_axis column '%s' not found in data — skipping",
                    y_col,
                )

        if not y_indices:
            logger.warning("declarative_chart: no valid y_axes found")
            return None

        # Extract data
        x_labels: list[str] = []
        y_data: list[list[float | None]] = [[] for _ in y_indices]

        for row in rows:
            x_val = row.get(columns[x_idx], "")
            x_labels.append(str(x_val) if x_val is not None else "")
            for yi, y_idx in enumerate(y_indices):
                y_val = row.get(columns[y_idx])
                if isinstance(y_val, (int, float)):
                    y_data[yi].append(float(y_val))
                else:
                    y_data[yi].append(None)

        # Build the chart
        try:
            dpi = getattr(theme.chart, "dpi", 150) if hasattr(theme, "chart") else 150
            facecolor = getattr(theme.chart, "figure_facecolor", "#FFFFFF") if hasattr(theme, "chart") else "#FFFFFF"
            font_family = getattr(theme, "font_family", "sans-serif") if hasattr(theme, "font_family") else "sans-serif"
            grid_color = getattr(theme, "grid_color", "#E5E7EB")

            fig = Figure(figsize=(6.08, 2.8), dpi=dpi, facecolor=facecolor)
            axis = fig.subplots()

            # Prefer explicit chart_config palette, then theme.palette, then defaults
            default_palette = list(getattr(theme, "palette", ("#2563EB", "#DC2626", "#059669", "#D97706", "#7C3AED")))
            palette = chart_config.get("palette", default_palette)
            grid_alpha = getattr(theme.chart, "grid_alpha", 0.3) if hasattr(theme, "chart") else 0.3

            if chart_type == "line":
                self._render_line(axis, x_labels, y_data, y_axes, palette, grid_alpha, grid_color=grid_color, facecolor=facecolor)
            elif chart_type == "bar":
                self._render_bar(axis, x_labels, y_data, y_axes, palette, grid_alpha, grid_color=grid_color, facecolor=facecolor)
            elif chart_type == "stacked_bar":
                self._render_stacked_bar(axis, x_labels, y_data, y_axes, palette, grid_alpha, grid_color=grid_color, facecolor=facecolor)
            elif chart_type == "scatter":
                self._render_scatter(axis, x_labels, y_data, y_axes, palette, facecolor=facecolor)
            elif chart_type == "combined":
                primary = chart_config.get("primary", {})
                secondary = chart_config.get("secondary", {})
                self._render_combined(axis, x_labels, y_data, y_axes, palette, grid_alpha, primary, secondary, grid_color=grid_color)
            else:
                logger.warning("declarative_chart: unknown chart type '%s'", chart_type)
                return None

            # Goal lines
            for gl in chart_config.get("goal_lines", []):
                try:
                    axis.axhline(y=gl["value"], color="#D97706", linewidth=1.2, linestyle="--", alpha=0.8)
                except (KeyError, ValueError):
                    pass

            fig.tight_layout(pad=0.1)

            buf = BytesIO()
            fig.savefig(
                buf,
                format="png",
                bbox_inches="tight",
                pad_inches=0.1,
                pil_kwargs={"optimize": True, "compress_level": 6},
            )
            buf.seek(0)
            png_bytes = buf.getvalue()

            # Check size — if over hard gate, return None
            if len(png_bytes) > PNG_HARD_GATE_BYTES:
                logger.warning(
                    "declarative_chart: PNG size %d exceeds hard gate %d — returning None",
                    len(png_bytes),
                    PNG_HARD_GATE_BYTES,
                )
                return None

            # Try at reduced height if over target
            if len(png_bytes) > PNG_TARGET_BYTES:
                fig2 = Figure(figsize=(6.08, 2.2), dpi=dpi, facecolor=facecolor)
                ax2 = fig2.subplots()
                if chart_type == "line":
                    self._render_line(ax2, x_labels, y_data, y_axes, palette, grid_alpha, grid_color=grid_color, facecolor=facecolor)
                elif chart_type == "bar":
                    self._render_bar(ax2, x_labels, y_data, y_axes, palette, grid_alpha, grid_color=grid_color, facecolor=facecolor)
                elif chart_type == "scatter":
                    self._render_scatter(ax2, x_labels, y_data, y_axes, palette, facecolor=facecolor)
                else:
                    # For stacked_bar and combined, keep original
                    del fig2
                    buf.seek(0)
                    png_bytes = buf.getvalue()
                if "fig2" in dir():
                    fig2.tight_layout(pad=0.1)
                    buf2 = BytesIO()
                    fig2.savefig(
                        buf2,
                        format="png",
                        bbox_inches="tight",
                        pad_inches=0.1,
                        pil_kwargs={"optimize": True, "compress_level": 9},
                    )
                    buf2.seek(0)
                    png_bytes = buf2.getvalue()
                    del fig2

            del fig

        except Exception as exc:
            logger.warning("declarative_chart: rendering failed for %s — %s", query_id, exc)
            return None

        # Build metadata
        alt_text = chart_config.get("alt_text") or self._derive_alt_text(chart_type, x_axis, y_axes)
        trend_description = self._derive_trend_description(y_data, y_axes)

        warnings: list[str] = []
        if len(png_bytes) > 80_000:
            warnings.append(f"PNG size {len(png_bytes):,} bytes approaches Outlook limit")

        metadata: dict[str, Any] = {
            "alt_text": alt_text,
            "trend_description": trend_description,
            "warnings": warnings,
            "series": list(y_axes),
            "renderer_id": "vertex::declarative",
        }

        return png_bytes, metadata

    def _render_line(self, axis, x_labels, y_data, y_axes, palette, grid_alpha, grid_color="#E5E7EB", facecolor="#FFFFFF"):
        x_pos = list(range(len(x_labels)))
        for yi, y_vals in enumerate(y_data):
            color = palette[yi % len(palette)]
            axis.plot(x_pos, y_vals, marker="o", linewidth=1.5, label=y_axes[yi], color=color)
        axis.set_xticks(x_pos)
        axis.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)
        axis.grid(axis="y", color=grid_color, linewidth=0.8, alpha=grid_alpha)
        axis.legend(fontsize=7, loc="best")
        axis.set_facecolor(facecolor)

    def _render_bar(self, axis, x_labels, y_data, y_axes, palette, grid_alpha, grid_color="#E5E7EB", facecolor="#FFFFFF"):
        x_pos = list(range(len(x_labels)))
        width = 0.6 / max(len(y_data), 1)
        for yi, y_vals in enumerate(y_data):
            color = palette[yi % len(palette)]
            offset = (yi - len(y_data) / 2 + 0.5) * width
            axis.bar([p + offset for p in x_pos], y_vals, width=width, label=y_axes[yi], color=color, alpha=0.9)
        axis.set_xticks(x_pos)
        axis.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)
        axis.grid(axis="y", color=grid_color, linewidth=0.8, alpha=grid_alpha)
        axis.legend(fontsize=7, loc="best")
        axis.set_facecolor(facecolor)

    def _render_stacked_bar(self, axis, x_labels, y_data, y_axes, palette, grid_alpha, grid_color="#E5E7EB", facecolor="#FFFFFF"):
        x_pos = list(range(len(x_labels)))
        bottom = [0.0] * len(x_labels)
        for yi, y_vals in enumerate(y_data):
            color = palette[yi % len(palette)]
            axis.bar(x_pos, y_vals, width=0.6, bottom=bottom, label=y_axes[yi], color=color, alpha=0.9)
            bottom = [b + (v or 0) for b, v in zip(bottom, y_vals)]
        axis.set_xticks(x_pos)
        axis.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)
        axis.grid(axis="y", color=grid_color, linewidth=0.8, alpha=grid_alpha)
        axis.legend(fontsize=7, loc="best")
        axis.set_facecolor(facecolor)

    def _render_scatter(self, axis, x_labels, y_data, y_axes, palette, facecolor="#FFFFFF"):
        for yi, y_vals in enumerate(y_data):
            x_vals = list(range(len(y_vals)))
            color = palette[yi % len(palette)]
            axis.scatter(x_vals, y_vals, label=y_axes[yi], color=color, s=20)
        axis.set_xticks(list(range(len(x_labels))))
        axis.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)
        axis.grid(axis="y", color="#E5E7EB", linewidth=0.8, alpha=0.3)
        axis.legend(fontsize=7, loc="best")
        axis.set_facecolor(facecolor)

    def _render_combined(self, axis, x_labels, y_data, y_axes, palette, grid_alpha, primary, secondary, grid_color="#E5E7EB"):
        # Simple combined: first y is line, rest are bar
        if len(y_data) < 2:
            self._render_line(axis, x_labels, y_data, y_axes, palette, grid_alpha, grid_color=grid_color)
            return
        self._render_line(axis, x_labels, [y_data[0]], [y_axes[0]], [palette[0]], grid_alpha, grid_color=grid_color)
        x_pos = list(range(len(x_labels)))
        width = 0.4 / (len(y_data) - 1)
        for yi in range(1, len(y_data)):
            offset = (yi - 0.5) * width
            axis.bar([p + offset for p in x_pos], y_data[yi], width=width, label=y_axes[yi], color=palette[yi % len(palette)], alpha=0.8)
        axis.legend(fontsize=7, loc="best")

    def _derive_alt_text(self, chart_type: str, x_axis: str, y_axes: list[str]) -> str:
        """Derive accessible alt text from chart data."""
        series_str = ", ".join(y_axes[:3])
        if len(y_axes) > 3:
            series_str += f" (+{len(y_axes) - 3} more)"
        return f"{chart_type.title()} chart showing {series_str} over {x_axis}"

    def _derive_trend_description(self, y_data: list[list[float | None]], y_axes: list[str]) -> str:
        """Derive a simple trend description from data."""
        if not y_data or not y_data[0]:
            return "No trend data available"
        first_vals = [v for v in y_data[0] if v is not None]
        last_vals = [v for v in y_data[0] if v is not None]
        if len(first_vals) < 2 or len(last_vals) < 2:
            return "Insufficient data for trend"
        first_half = first_vals[:len(first_vals)//2]
        last_half = first_vals[len(first_vals)//2:]
        avg_first = sum(first_half) / len(first_half)
        avg_last = sum(last_half) / len(last_half)
        if not avg_first:
            return "No trend data available"
        pct_change = ((avg_last - avg_first) / avg_first) * 100
        direction = "up" if pct_change > 1 else "down" if pct_change < -1 else "stable"
        return f"{y_axes[0]} trending {direction} ({pct_change:+.1f}%)"


# Module-level CHART_RENDERERS dict for discovery
CHART_RENDERERS: dict[str, Any] = {
    "vertex::declarative": DeclarativeChartBuilder(),
}