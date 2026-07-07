"""
Generic Deployment Velocity chart renderer — spec §3.3, §12.

This renderer is program-neutral: title, alt-text, percentiles, goal lines,
and annotation labels are all sourced from the chart_config or program config.
It is registered under the canonical `core::deployment_velocity` id.

Renders P50/P75 deployment cycle time as a line chart with:
- Hour-formatted Y-axis labels (e.g., "24h" not "24.0")
- Goal line support (e.g., "Target: 24h")
- Annotation markers for notable events (hotfix spikes, deploys)
- Accessible alt text with last-point values

Zone A — no AI or M365 imports permitted.
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Any, Mapping

logger = logging.getLogger(__name__)

PNG_HARD_GATE_BYTES = 102_400
PNG_TARGET_BYTES = 80_000

# Canonical (program-neutral) renderer id — see WS-11.
CANONICAL_RENDERER_ID = "core::deployment_velocity"
LEGACY_ALIAS_IDS: tuple[str, ...] = ()


def _chart_subject(chart_config: Mapping[str, Any]) -> str:
    """Return the program-neutral subject phrase for titles/alt text.

    Prefers an explicit `chart_config["subject"]` (authored in YAML); falls
    back to a generic "deployment cycle time" if absent.
    """
    subject = chart_config.get("subject")
    if isinstance(subject, str) and subject.strip():
        return subject.strip()
    return "deployment cycle time"


class DeploymentVelocityChartBuilder:
    """
    Program-neutral chart renderer for deployment cycle time percentiles.

    Expected chart_config keys (beyond the standard schema):
      type        : "line" (ignored — always renders as line)
      subject     : str, optional — overrides the alt-text subject phrase
      x_axis      : column name for the time axis (e.g. "CompletionDay")
      y_axes      : column names for percentile series (e.g. ["P50_hrs", "P75_hrs"])
      goal_lines  : list of {label, value} hour-threshold lines
      annotations : list of {x, label} event markers

    Gracefully degrades to the standard declarative renderer output format.
    """

    def build(
        self,
        query_id: str,
        chart_config: Mapping[str, Any],
        rows: tuple[dict[str, Any], ...],
        columns: tuple[str, ...],
        theme: Any,
    ) -> tuple[bytes, dict[str, Any]] | None:
        if not rows:
            return None

        try:
            import matplotlib
            matplotlib.use("Agg")
            from matplotlib.figure import Figure
        except ImportError:
            logger.warning("deployment_velocity: matplotlib not available")
            return None

        x_axis = chart_config.get("x_axis")
        y_axes = list(chart_config.get("y_axes", []))

        if not x_axis or not y_axes:
            logger.warning("deployment_velocity: missing x_axis or y_axes")
            return None

        col_index = {col: i for i, col in enumerate(columns)}
        if x_axis not in col_index:
            logger.warning("deployment_velocity: x_axis '%s' not in columns", x_axis)
            return None

        x_idx = col_index[x_axis]
        y_indices: list[int] = []
        valid_y_axes: list[str] = []
        for y_col in y_axes:
            if y_col in col_index:
                y_indices.append(col_index[y_col])
                valid_y_axes.append(y_col)
            else:
                logger.warning("deployment_velocity: y_axis '%s' not found — skipping", y_col)

        if not y_indices:
            logger.warning("deployment_velocity: no valid y_axes")
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

        try:
            dpi = getattr(theme.chart, "dpi", 150) if hasattr(theme, "chart") else 150
            facecolor = getattr(theme.chart, "figure_facecolor", "#FFFFFF") if hasattr(theme, "chart") else "#FFFFFF"
            grid_color = getattr(theme, "grid_color", "#E5E7EB")
            grid_alpha = getattr(theme.chart, "grid_alpha", 0.3) if hasattr(theme, "chart") else 0.3

            default_palette = list(getattr(theme, "palette", ("#2563EB", "#DC2626", "#059669", "#D97706", "#7C3AED")))
            palette = chart_config.get("palette", default_palette)

            fig = Figure(figsize=(6.08, 2.8), dpi=dpi, facecolor=facecolor)
            axis = fig.subplots()

            x_pos = list(range(len(x_labels)))
            for yi, y_vals in enumerate(y_data):
                color = palette[yi % len(palette)]
                label = valid_y_axes[yi]
                # Dashed for P75+, solid for P50
                linestyle = "--" if yi > 0 else "-"
                y_vals_safe = [float("nan") if v is None else v for v in y_vals]
                axis.plot(x_pos, y_vals_safe, marker="o", markersize=3, linewidth=1.5,
                          label=label, color=color, linestyle=linestyle)

            # Y-axis: hour labels ("24h", "48h", etc.)
            y_all = [v for series in y_data for v in series if v is not None]
            if y_all:
                y_max = max(y_all)
                y_min = min(y_all)
                step = max(1.0, round((y_max - y_min) / 5, 0)) if y_max != y_min else 8.0
                import math
                tick_vals = [
                    round(y_min + step * i, 0)
                    for i in range(math.ceil((y_max - y_min + step) / step) + 1)
                ]
                axis.set_yticks([t for t in tick_vals if t >= 0])
                axis.set_yticklabels([f"{int(t)}h" for t in tick_vals if t >= 0], fontsize=7)

            # X-axis: rotate, compact if >10 labels
            axis.set_xticks(x_pos)
            if len(x_labels) > 10:
                step_x = max(1, len(x_labels) // 8)
                sparse = ["" if i % step_x != 0 else lbl for i, lbl in enumerate(x_labels)]
                axis.set_xticklabels(sparse, rotation=45, ha="right", fontsize=7)
            else:
                axis.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=7)

            axis.grid(axis="y", color=grid_color, linewidth=0.8, alpha=grid_alpha)
            axis.set_facecolor(facecolor)
            axis.legend(fontsize=7, loc="upper right")

            # Goal lines
            for gl in chart_config.get("goal_lines", []):
                try:
                    axis.axhline(
                        y=gl["value"],
                        color="#D97706",
                        linewidth=1.2,
                        linestyle=":",
                        alpha=0.9,
                        label=gl.get("label", "Target"),
                    )
                except (KeyError, ValueError):
                    pass

            # Annotation markers — vertical dashed lines with text
            for ann in chart_config.get("annotations", []):
                x_label = ann.get("x", "")
                ann_label = ann.get("label", "")
                if x_label in x_labels:
                    idx = x_labels.index(x_label)
                    axis.axvline(x=idx, color="#6B7280", linewidth=0.8, linestyle=":", alpha=0.6)
                    if ann_label:
                        axis.text(
                            idx, axis.get_ylim()[1] * 0.95 if y_all else 0,
                            ann_label, fontsize=6, color="#6B7280",
                            ha="center", va="top",
                            rotation=90,
                        )

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
            del fig

            # Hard gate check
            if len(png_bytes) > PNG_HARD_GATE_BYTES:
                logger.warning(
                    "deployment_velocity: PNG %d bytes exceeds hard gate %d",
                    len(png_bytes), PNG_HARD_GATE_BYTES,
                )
                return None

        except Exception as exc:
            logger.warning("deployment_velocity: rendering failed for %s — %s", query_id, exc)
            return None

        # Metadata — program-neutral subject phrase
        subject = _chart_subject(chart_config)
        last_values: dict[str, float] = {
            valid_y_axes[yi]: v
            for yi in range(len(valid_y_axes))
            if y_data[yi] and (v := y_data[yi][-1]) is not None
        }
        alt_parts = [f"{k}: {int(v)}h" for k, v in last_values.items()]
        alt_text = (
            f"{subject.capitalize()} percentiles. Latest: {', '.join(alt_parts)}."
            if alt_parts
            else f"{subject.capitalize()} chart — no data this period."
        )

        trend_description = _derive_trend(y_data, valid_y_axes)

        warnings: list[str] = []
        if len(png_bytes) > PNG_TARGET_BYTES:
            warnings.append(f"PNG size {len(png_bytes):,} bytes approaches Outlook limit")

        return png_bytes, {
            "alt_text": alt_text,
            "trend_description": trend_description,
            "warnings": warnings,
            "series": valid_y_axes,
            "renderer_id": CANONICAL_RENDERER_ID,
            "last_values": last_values,
        }


def _derive_trend(y_data: list[list[float | None]], y_axes: list[str]) -> str:
    if not y_data or not y_data[0]:
        return "No trend data"
    vals = [v for v in y_data[0] if v is not None]
    if len(vals) < 3:
        return "Insufficient data for trend"
    first_avg = sum(vals[: len(vals) // 2]) / max(len(vals) // 2, 1)
    last_avg = sum(vals[len(vals) // 2 :]) / max(len(vals) - len(vals) // 2, 1)
    if first_avg == 0:
        return "No trend data"
    pct = ((last_avg - first_avg) / first_avg) * 100
    direction = "up" if pct > 2 else "down" if pct < -2 else "stable"
    name = y_axes[0] if y_axes else "P50"
    return f"{name} trending {direction} ({pct:+.1f}%)"


# Registry export — auto-discovered by build_default_registry().
CHART_RENDERERS: dict[str, Any] = {
    CANONICAL_RENDERER_ID: DeploymentVelocityChartBuilder(),
}

