from __future__ import annotations

import base64
import logging
import os
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import re
from typing import Any, Callable, Mapping

from src.core.config_loader import KustoQuerySettings, KustoSettings
from src.core.exceptions import AuthError, QueryError
from src.core.kusto_templates import KustoTemplateContext, render_kusto_query
from src.core.view_models import KustoMetric, KustoSectionData, KustoTableCell

logger = logging.getLogger(__name__)

# Feature flag — checked at the top of chart processing
_VERTEX_CHARTS_ENABLED = os.environ.get("VERTEX_CHARTS", "1") == "1"

# Chart pipeline size thresholds (bytes, decoded PNG)
_CHART_PNG_TARGET = 80_000
_CHART_PNG_HARD_GATE = 102_400

# Chart cache TTL default (hours)
_DEFAULT_CHART_CACHE_TTL_HOURS = 26


ChartCacheLoader = Callable[[str, str], Any]  # (program_id, query_id) -> ChartCacheEntry | None


KustoQueryExecutor = Callable[[KustoQuerySettings], list[dict[str, Any]]]
ChartBuilder = Callable[[KustoQuerySettings, tuple[dict[str, Any], ...]], str | None]


@dataclass(frozen=True, slots=True)
class TelemetryObservation:
    query_id: str
    cluster: str
    database: str
    confidence: str
    kusto_section_validates_slice: bool
    execution_state: str
    observed_at: datetime
    last_successful_fetch_at: datetime | None
    message: str | None = None


def build_kusto_sections(
    settings: KustoSettings,
    query_executor: KustoQueryExecutor,
    *,
    theme_context: Any = None,  # ThemeContext | None — imported lazily
    chart_registry: Any = None,  # ChartRendererRegistry | None
    chart_cache_loader: ChartCacheLoader | None = None,
    chart_builder: ChartBuilder | None = None,
    observed_at: datetime | None = None,
    template_context: KustoTemplateContext | None = None,
    programs_root: Any = None,  # Path | None — for chart cache writeback
    program_id: str | None = None,  # Active program id for cache writeback
) -> tuple[tuple[KustoSectionData, ...], tuple[TelemetryObservation, ...], tuple[str, ...]]:
    if not settings.enabled:
        return (), (), ()

    sections: list[KustoSectionData] = []
    observations: list[TelemetryObservation] = []
    warnings: list[str] = []
    build_chart = chart_builder or _build_chart_image_data_url
    observed_time = observed_at or datetime.now(timezone.utc)

    for configured_query in settings.queries:
        query = render_kusto_query(configured_query, context=template_context) if template_context is not None else configured_query

        # Chart pipeline: determine if this is a registry-path chart
        is_registry_chart = (
            _VERTEX_CHARTS_ENABLED
            and query.render_as == "chart_image"
            and getattr(query, "chart_config", None) is not None
        )

        try:
            rows = tuple(query_executor(query))
        except (AuthError, QueryError) as exc:
            is_auth = isinstance(exc, AuthError)
            degraded = _build_degraded_section(
                query,
                message=(
                    "Kusto authentication failed. Run vertex admin auth setup, then use the reference "
                    "dashboard until access is restored."
                    if is_auth else
                    "Live Kusto data is unavailable right now. Use the reference dashboard while the query path recovers."
                ),
            )
            if degraded is not None:
                sections.append(degraded)
            observations.append(
                TelemetryObservation(
                    query_id=query.id,
                    cluster=query.cluster,
                    database=query.database,
                    confidence=query.confidence,
                    kusto_section_validates_slice=query.kusto_section_validates_slice,
                    execution_state="degraded",
                    observed_at=observed_time,
                    last_successful_fetch_at=None,
                    message=degraded.message if degraded is not None else str(exc),
                )
            )
            warnings.append(
                f"Kusto auth failed for {query.id}. Run `vertex admin auth setup` to restore query execution."
                if is_auth else
                f"Kusto query {query.id} degraded: {exc}"
            )
            continue

        if not rows:
            # Zero rows from live query is a valid data state — spec §5.5.
            # FR-SG-31: if fallback_kql is configured, attempt the secondary query.
            fallback_rows: tuple[dict[str, Any], ...] | None = None
            fallback_kql = getattr(query, "fallback_kql", None)
            if fallback_kql:
                try:
                    fallback_q = dataclasses.replace(query, kql=fallback_kql, id=f"{query.id}__fallback")
                    fallback_rows = tuple(query_executor(fallback_q))
                except Exception:
                    fallback_rows = None
            if fallback_rows:
                rows = fallback_rows
                query = dataclasses.replace(query, id=f"{query.id}__fallback")
                # fall through to normal render path below; quality_state=FALLBACK is embedded in observation
            else:
                # Cache is NOT consulted. Render "No data available" placeholder.
                _handle_zero_rows(query, observed_time, sections, observations)
                continue

        if query.render_as == "metric_highlight":
            sections.append(_build_metric_section(query, rows))
            observations.append(
                TelemetryObservation(
                    query_id=query.id,
                    cluster=query.cluster,
                    database=query.database,
                    confidence=query.confidence,
                    kusto_section_validates_slice=query.kusto_section_validates_slice,
                    execution_state="success",
                    observed_at=observed_time,
                    last_successful_fetch_at=observed_time,
                    message=None,
                )
            )
            continue

        if query.render_as == "chart_image":
            # Determine path: registry (chart_config) or legacy
            if is_registry_chart:
                section, obs = _build_registry_chart_section(
                    query, rows, observed_time,
                    chart_registry=chart_registry,
                    theme_context=theme_context,
                    chart_cache_loader=chart_cache_loader,
                )
                if section is not None:
                    sections.append(section)
                    observations.append(obs)
                    _write_back_chart_cache(query, rows, observed_time, chart_cache_loader, programs_root, program_id)
                    continue
                warnings.append(f"Kusto chart rendering fell back to table for {query.id}.")
            else:
                # Legacy path
                image_data_url = build_chart(query, rows)
                if image_data_url is not None:
                    sections.append(
                        KustoSectionData(
                            section_id=_section_id(query.id),
                            title=query.section,
                            query_id=query.id,
                            render_mode="chart_image",
                            source_label=_source_label(query),
                            confidence=query.confidence,
                            columns=tuple(rows[0].keys()),
                            rows=_table_rows(rows),
                            metrics=(),
                            image_data_url=image_data_url,
                            reference_url=query.reference_url,
                            caveats=query.caveats,
                            message=None,
                            is_degraded=False,
                        )
                    )
                    observations.append(_make_observation(query, observed_time, "success", None))
                    continue
                warnings.append(f"Kusto chart rendering fell back to table for {query.id}.")

        sections.append(_build_table_section(query, rows, message=None, degraded=False))
        observations.append(
            TelemetryObservation(
                query_id=query.id,
                cluster=query.cluster,
                database=query.database,
                confidence=query.confidence,
                kusto_section_validates_slice=query.kusto_section_validates_slice,
                execution_state="success",
                observed_at=observed_time,
                last_successful_fetch_at=observed_time,
                message=None,
            )
        )

    return tuple(sections), tuple(observations), tuple(warnings)


def _build_metric_section(query: KustoQuerySettings, rows: tuple[dict[str, Any], ...]) -> KustoSectionData:
    metrics: list[KustoMetric] = []
    if len(rows) == 1:
        for key, value in rows[0].items():
            if _is_url_key(key):
                continue
            metrics.append(KustoMetric(label=_display_label(key), value=_display_value(value)))
    else:
        group_key = next(iter(rows[0].keys()))
        for row in rows:
            group_value = _display_value(row.get(group_key))
            for key, value in row.items():
                if key == group_key or _is_url_key(key):
                    continue
                metrics.append(KustoMetric(label=f"{group_value} {_display_label(key)}", value=_display_value(value)))

    return KustoSectionData(
        section_id=_section_id(query.id),
        title=query.section,
        query_id=query.id,
        render_mode="metric_highlight",
        source_label=_source_label(query),
        confidence=query.confidence,
        columns=tuple(rows[0].keys()),
        rows=_table_rows(rows),
        metrics=tuple(metrics),
        image_data_url=None,
        reference_url=query.reference_url,
        caveats=query.caveats,
        message=None,
        is_degraded=False,
    )


def _build_table_section(
    query: KustoQuerySettings,
    rows: tuple[dict[str, Any], ...],
    *,
    message: str | None,
    degraded: bool,
) -> KustoSectionData:
    return KustoSectionData(
        section_id=_section_id(query.id),
        title=query.section,
        query_id=query.id,
        render_mode="table",
        source_label=_source_label(query),
        confidence=query.confidence,
        columns=tuple(rows[0].keys()) if rows else (),
        rows=_table_rows(rows),
        metrics=(),
        image_data_url=None,
        reference_url=query.reference_url,
        caveats=query.caveats,
        message=message,
        is_degraded=degraded,
    )


def _build_degraded_section(query: KustoQuerySettings, *, message: str) -> KustoSectionData | None:
    if not query.reference_url:
        return None
    return KustoSectionData(
        section_id=_section_id(query.id),
        title=query.section,
        query_id=query.id,
        render_mode="table",
        source_label=_source_label(query),
        confidence=query.confidence,
        columns=(),
        rows=(),
        metrics=(),
        image_data_url=None,
        reference_url=query.reference_url,
        caveats=query.caveats,
        message=message,
        is_degraded=True,
    )


def _table_rows(rows: tuple[dict[str, Any], ...]) -> tuple[tuple[KustoTableCell, ...], ...]:
    if not rows:
        return ()
    columns = tuple(rows[0].keys())
    return tuple(
        tuple(
            KustoTableCell(
                text=("Open" if _is_url_key(column) and row.get(column) else _display_value(row.get(column))),
                href=(str(row.get(column)) if _is_url_key(column) and row.get(column) else None),
            )
            for column in columns
        )
        for row in rows
    )


def _source_label(query: KustoQuerySettings) -> str:
    cluster_host = re.sub(r"^https?://", "", query.cluster).rstrip("/")
    cluster_label = re.sub(r"\.kusto\.windows\.net$", "", cluster_host)
    return f"{cluster_label}/{query.database}"


def _section_id(query_id: str) -> str:
    return f"kusto-{query_id.lower()}"


def _display_label(value: str) -> str:
    label = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return label.replace("_", " ").replace("Url", " URL")


def _display_value(value: Any) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return str(value)


def _is_url_key(key: str) -> bool:
    normalized = key.lower()
    return normalized.endswith("url") or normalized.endswith("link")


def _build_chart_image_data_url(query: KustoQuerySettings, rows: tuple[dict[str, Any], ...]) -> str | None:
    del query
    if not rows:
        return None
    columns = tuple(rows[0].keys())
    if len(columns) < 2:
        return None
    x_key = columns[0]
    y_keys = tuple(column for column in columns[1:] if all(_coerce_float(row.get(column)) is not None for row in rows))
    if not y_keys:
        return None

    try:
        from matplotlib.figure import Figure
    except ImportError:
        return None

    figure = Figure(figsize=(5.6, 2.6), dpi=100)
    axis = figure.subplots()
    x_labels = [str(row.get(x_key, "")) for row in rows]
    x_positions = list(range(len(rows)))
    for key in y_keys[:3]:
        axis.plot(x_positions, [_coerce_float(row.get(key)) for row in rows], marker="o", linewidth=1.8, label=_display_label(key))  # type: ignore[arg-type]
    axis.set_xticks(x_positions)
    axis.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
    axis.grid(axis="y", color="#E5E7EB", linewidth=0.8)
    axis.legend(fontsize=8)
    axis.set_facecolor("#FFFFFF")
    figure.tight_layout()

    buffer = BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _make_observation(query, observed_time, state, message) -> TelemetryObservation:
    return TelemetryObservation(
        query_id=query.id,
        cluster=query.cluster,
        database=query.database,
        confidence=query.confidence,
        kusto_section_validates_slice=query.kusto_section_validates_slice,
        execution_state=state,
        observed_at=observed_time,
        last_successful_fetch_at=observed_time,
        message=message,
    )


def _handle_zero_rows(query, observed_time, sections, observations) -> None:
    """Handle zero-row case for both registry and legacy paths."""
    # Valid zero-row state — render a PNG placeholder card (spec §5.5)
    png_bytes = _build_zero_row_placeholder()
    b64 = base64.b64encode(png_bytes).decode("ascii") if png_bytes else ""
    image_data_url = f"data:image/png;base64,{b64}" if b64 else None
    chart_blocks_publish = getattr(query, "chart_blocks_publish", False)
    cache_ttl_hours = getattr(query, "chart_cache_ttl_hours", _DEFAULT_CHART_CACHE_TTL_HOURS)
    attachment = getattr(query, "attachment", None)
    section_placement = getattr(attachment, "target", "standalone") if attachment else "standalone"

    sections.append(
        KustoSectionData(
            section_id=_section_id(query.id),
            title=query.section,
            query_id=query.id,
            render_mode="chart_image",
            source_label=_source_label(query),
            confidence=query.confidence,
            columns=(),
            rows=(),
            metrics=(),
            image_data_url=image_data_url,
            reference_url=query.reference_url,
            caveats=query.caveats,
            message="No data available for this period.",
            is_degraded=False,
            captured_at=observed_time,
            chart_png_base64=b64 or None,
            chart_png_size_bytes=len(png_bytes) if png_bytes else 0,
            chart_blocks_publish=chart_blocks_publish,
            chart_cache_ttl_hours=cache_ttl_hours,
            section_placement=section_placement,
        )
    )
    observations.append(
        TelemetryObservation(
            query_id=query.id,
            cluster=query.cluster,
            database=query.database,
            confidence=query.confidence,
            kusto_section_validates_slice=query.kusto_section_validates_slice,
            execution_state="empty",
            observed_at=observed_time,
            last_successful_fetch_at=observed_time,
            message="No data available for this period.",
        )
    )


def _build_zero_row_placeholder() -> bytes | None:
    """
    Render a valid zero-row PNG placeholder card per spec §5.5.

    608×200px, #F3F4F6 background, centered text
    "No data available for this period" in 14px #6B7280.
    Returns PNG bytes or None if rendering fails.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
    except ImportError:
        return None

    fig = Figure(figsize=(6.08, 2.0), dpi=100, facecolor="#F3F4F6")
    axis = fig.subplots()
    axis.set_facecolor("#F3F4F6")
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.axis("off")
    axis.text(
        0.5,
        0.5,
        "No data available for this period.",
        ha="center",
        va="center",
        fontsize=14,
        color="#6B7280",
        transform=axis.transAxes,
    )
    fig.tight_layout(pad=0.1)
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.1)
    buf.seek(0)
    result = buf.getvalue()
    del fig
    return result


def _build_registry_chart_section(
    query,
    rows,
    observed_time,
    *,
    chart_registry,
    theme_context,
    chart_cache_loader,
):
    """Build a chart section using the chart registry pipeline."""
    chart_config = getattr(query, "chart_config", None) or {}
    chart_renderer_id = getattr(query, "chart_renderer_id", None)
    cache_ttl_hours = getattr(query, "chart_cache_ttl_hours", _DEFAULT_CHART_CACHE_TTL_HOURS)
    chart_blocks_publish = getattr(query, "chart_blocks_publish", False)

    if chart_registry is None:
        # Lazy load default registry
        from src.core.chart_renderer_registry import get_default_registry
        chart_registry = get_default_registry()

    if theme_context is None:
        from src.core.theme_context import ThemeContext
        theme_context = ThemeContext()

    builder = chart_registry.get_renderer(chart_renderer_id)
    columns = tuple(rows[0].keys()) if rows else ()

    result = builder.build(query.id, chart_config, tuple(rows), columns, theme_context)
    if result is None:
        return None, _make_observation(query, observed_time, "renderer_failed", "Chart renderer returned None")

    png_bytes, metadata = result
    png_size = len(png_bytes)

    # Encode to base64 data URL
    b64 = base64.b64encode(png_bytes).decode("ascii")
    image_data_url = f"data:image/png;base64,{b64}"

    # Determine section placement
    attachment = getattr(query, "attachment", None)
    if attachment and hasattr(attachment, "target"):
        section_placement = attachment.target
    else:
        section_placement = "standalone"

    # Determine message/trend
    message = metadata.get("trend_description") or metadata.get("alt_text")

    # Determine is_degraded (not degraded for live render)
    is_degraded = False

    section = KustoSectionData(
        section_id=_section_id(query.id),
        title=query.section,
        query_id=query.id,
        render_mode="chart",
        source_label=_source_label(query),
        confidence=query.confidence,
        columns=columns,
        rows=(),
        metrics=(),
        image_data_url=image_data_url,
        reference_url=query.reference_url,
        caveats=query.caveats,
        message=message,
        is_degraded=is_degraded,
        # Chart pipeline fields
        captured_at=observed_time,
        chart_png_base64=b64,
        chart_png_size_bytes=png_size,
        chart_blocks_publish=chart_blocks_publish,
        chart_cache_ttl_hours=cache_ttl_hours,
        section_placement=section_placement,
        cache_captured_at=None,
    )

    obs = _make_observation(query, observed_time, "success", None)
    return section, obs


def _build_chart_section_from_cache(query, cache_entry, observed_time, fallback_on_empty) -> KustoSectionData | None:
    """Build a chart section from cached data."""
    if not cache_entry.rows:
        return None

    chart_config = getattr(query, "chart_config", None) or {}
    chart_renderer_id = getattr(query, "chart_renderer_id", None)

    from src.core.chart_renderer_registry import get_default_registry
    from src.core.theme_context import ThemeContext

    registry = get_default_registry()
    theme = ThemeContext()
    builder = registry.get_renderer(chart_renderer_id)

    rows = cache_entry.rows
    columns = tuple(rows[0].keys()) if rows else ()

    result = builder.build(query.id, chart_config, tuple(rows), columns, theme)
    if result is None:
        return None

    png_bytes, metadata = result
    b64 = base64.b64encode(png_bytes).decode("ascii")

    attachment = getattr(query, "attachment", None)
    section_placement = attachment.target if attachment and hasattr(attachment, "target") else "standalone"

    return KustoSectionData(
        section_id=_section_id(query.id),
        title=query.section,
        query_id=query.id,
        render_mode="chart",
        source_label=_source_label(query),
        confidence=query.confidence,
        columns=columns,
        rows=(),
        metrics=(),
        image_data_url=f"data:image/png;base64,{b64}",
        reference_url=query.reference_url,
        caveats=query.caveats,
        message=metadata.get("trend_description") or metadata.get("alt_text"),
        is_degraded=True,
        captured_at=cache_entry.captured_at,
        chart_png_base64=b64,
        chart_png_size_bytes=len(png_bytes),
        chart_blocks_publish=getattr(query, "chart_blocks_publish", False),
        chart_cache_ttl_hours=getattr(query, "chart_cache_ttl_hours", _DEFAULT_CHART_CACHE_TTL_HOURS),
        section_placement=section_placement,
        cache_captured_at=cache_entry.captured_at,
    )


def _write_back_chart_cache(query, rows, observed_time, chart_cache_loader, programs_root, program_id) -> None:
    """Write successful live render back to cache for future offline use."""
    if chart_cache_loader is None or program_id is None:
        return
    try:
        from src.core.chart_cache_store import write_chart_cache
        from src.core.charts.chart_config_schema import FORBIDDEN_CHART_CONFIG_KEYS
        import hashlib

        chart_config = getattr(query, "chart_config", None) or {}
        # Build config hash excluding forbidden keys
        safe_config = {k: v for k, v in chart_config.items() if k not in FORBIDDEN_CHART_CONFIG_KEYS}
        config_str = str(sorted(safe_config.items()))
        config_hash = hashlib.md5(config_str.encode()).hexdigest()

        write_chart_cache(
            program_id=program_id,
            query_id=query.id,
            rows=list(rows),
            captured_at=observed_time,
            chart_config_hash=config_hash,
            programs_root=programs_root,
            pii_prescrubbed=True,
        )
    except Exception as exc:
        logger.warning("_write_back_chart_cache: failed to write cache for %s/%s — %s", program_id, query.id, exc)