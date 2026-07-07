# Phase 2 — Chart gather helper (R3)
"""
Chart-gather orchestration: executes chart--configured KPI queries, scrubs PII,
truncates rows, writes chart cache, and manages eviction.

Zone B — may call the PII scrubber; writes to Zone A chart_cache_store.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.chart_cache_store import (
    ChartCacheEntry,
    evict_stale_caches,
    write_chart_cache,
)
from src.core.exceptions import AuthError, QueryError
from src.core.integration_types import IntegrationError
from src.core.knowledge_store import load_program_knowledge
from src.core.kusto_query_loader import load_kpi_queries
from src.core.models_v2 import KustoQuery

logger = logging.getLogger(__name__)

# Feature flag — checked at top of gather
_VERTEX_CHARTS_ENABLED = os.environ.get("VERTEX_CHARTS", "1") == "1"

# Max rows for non-aggregated queries; aggregated (SUMMARIZE) queries pass through
_ROW_CAP = 500

# Eviction horizon: max(ttl * 3, 168) hours — spec §5.4
_EVICTION_GRACE_HOURS = 168

# Types
KustoQueryExecutor = Any  # Callable[[KustoQuerySettings], list[dict[str, Any]]]
ChartCacheLoader = Any  # Callable[[str, str], ChartCacheEntry | None]


@dataclass(frozen=True, slots=True)
class ChartGatherResult:
    """Outcome of a single chart gather attempt."""

    query_id: str
    cluster: str
    database: str
    rows_written: int | None  # None means skip/no-write
    cache_hit: bool
    error: str | None


def gather_chart_data(
    program_id: str,
    *,
    programs_root: Path,
    program: Any,  # Program from models_v2
    workstreams: tuple[Any, ...],  # Workstream tuples
    executor: KustoQueryExecutor,
    current_time: datetime | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
) -> tuple[tuple[ChartGatherResult, ...], tuple[IntegrationError, ...]]:
    """
    Execute chart-configured KPI queries and populate the chart cache.

    Returns (results, errors). Errors are also appended to integration_error_sink
    when provided.

    Pipeline:
      1. Load KPI queries (chart_image render_as + chart_config)
      2. Filter to those scoped to this program's workstreams
      3. Execute live, truncate, sanitize (PII scrub), write cache
      4. Evict stale caches for this program
    """
    if not _VERTEX_CHARTS_ENABLED:
        logger.debug("gather_chart_data: VERTEX_CHARTS=0, skipping")
        return (), ()

    if programs_root is None:
        return (), ()

    now = current_time or datetime.now(timezone.utc)
    results: list[ChartGatherResult] = []
    errors: list[IntegrationError] = []

    # Load chart-configured KPI queries
    queries = _load_chart_queries(program_id, workstreams, programs_root=programs_root)
    if not queries:
        logger.debug("gather_chart_data: no chart queries for %s", program_id)
        return (), ()

    # Pre-load existing cache entries for quick lookup
    cache_loader: ChartCacheLoader | None = None
    try:
        from src.core.chart_cache_store import load_chart_cache
        cache_loader = load_chart_cache
    except ImportError:
        pass

    for query in queries:
        result = _gather_single_chart(
            query,
            program_id,
            executor,
            now,
            cache_loader,
            programs_root,
        )
        results.append(result)
        if result.error:
            err = IntegrationError(
                source="charts",
                stage="gather",
                message=result.error,
                retryable=True,
                operator_action=(
                    f"Check Kusto access for {result.cluster}/{result.database} "
                    "or run 'vertex admin auth setup'"
                ),
            )
            errors.append(err)
            if integration_error_sink is not None:
                integration_error_sink.append(err)

    # Evict stale caches for this program
    try:
        _evict_chart_caches(program_id, programs_root)
    except Exception as exc:
        logger.warning("gather_chart_data: cache eviction failed for %s — %s", program_id, exc)

    return tuple(results), tuple(errors)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_chart_queries(
    program_id: str,
    workstreams: tuple[Any, ...],
    *,
    programs_root: Path,
) -> tuple[KustoQuery, ...]:
    """Load KPI queries that have chart_config and are scoped to this gather."""

    all_queries = load_kpi_queries(program_id, programs_root=programs_root)

    chart_queries: list[KustoQuery] = []
    workstream_ids = {ws.id for ws in workstreams}

    for query in all_queries:
        # Must be a chart image with chart config
        if query.render_as != "chart_image" or not query.chart_config:
            continue

        # Must be kusto engine
        if query.engine != "kusto":
            continue

        # Must be scoped to at least one of the current workstreams
        if query.workstream_ids and not (set(query.workstream_ids) & workstream_ids):
            continue

        chart_queries.append(query)

    return tuple(chart_queries)


def _gather_single_chart(
    query: KustoQuery,
    program_id: str,
    executor: KustoQueryExecutor,
    now: datetime,
    cache_loader: ChartCacheLoader | None,
    programs_root: Path,
) -> ChartGatherResult:
    """Execute one chart query and write to cache."""

    # Check cache freshness first
    if cache_loader is not None:
        try:
            existing = cache_loader(program_id, query.id)
            if existing is not None:
                from src.core.chart_cache_store import chart_cache_age_hours
                age_h = chart_cache_age_hours(existing, now=now)
                max_age = _eviction_grace_hours_for_ttl(getattr(query, "chart_cache_ttl_hours", 26))
                if age_h < max_age:
                    # Fresh enough — skip live execution
                    return ChartGatherResult(
                        query_id=query.id,
                        cluster=query.cluster,
                        database=query.database,
                        rows_written=None,
                        cache_hit=True,
                        error=None,
                    )
        except Exception:
            pass

    # Execute live query
    rows: list[dict[str, Any]]
    try:
        rows = list(executor(query))
    except (AuthError, QueryError) as exc:
        return ChartGatherResult(
            query_id=query.id,
            cluster=query.cluster,
            database=query.database,
            rows_written=None,
            cache_hit=False,
            error=f"Kusto execution failed: {exc}",
        )

    # Truncate non-aggregated queries
    truncated, was_truncated = _truncate_rows(rows, query.kql)
    if was_truncated:
        logger.warning(
            "gather_chart_data: %s truncated %d rows to %d (SUMMARIZE not detected)",
            query.id,
            len(rows),
            _ROW_CAP,
        )
        rows = truncated

    # Sanitize: drop 'dynamic' columns with a warning, cap entry size
    sanitized = _sanitize_rows(rows)

    # Scrub PII from text fields (Zone B operation)
    scrubbed = _scrub_pii_rows(sanitized)

    # Write to chart cache
    try:
        import hashlib
        from src.core.charts.chart_config_schema import FORBIDDEN_CHART_CONFIG_KEYS

        chart_config = query.chart_config or {}
        safe_config = {k: v for k, v in chart_config.items() if k not in FORBIDDEN_CHART_CONFIG_KEYS}
        config_str = str(sorted(safe_config.items()))
        config_hash = hashlib.md5(config_str.encode()).hexdigest()

        write_chart_cache(
            program_id=program_id,
            query_id=query.id,
            rows=scrubbed,
            captured_at=now,
            chart_config_hash=config_hash,
            programs_root=programs_root,
            pii_prescrubbed=True,
        )
    except Exception as exc:
        return ChartGatherResult(
            query_id=query.id,
            cluster=query.cluster,
            database=query.database,
            rows_written=None,
            cache_hit=False,
            error=f"Cache write failed: {exc}",
        )

    return ChartGatherResult(
        query_id=query.id,
        cluster=query.cluster,
        database=query.database,
        rows_written=len(scrubbed),
        cache_hit=False,
        error=None,
    )


def _truncate_rows(rows: list[dict[str, Any]], kql: str) -> tuple[list[dict[str, Any]], bool]:
    """
    Truncate rows to _ROW_CAP if the query does not contain SUMMARIZE.

    Returns (truncated_rows, was_truncated).
    """
    if not rows:
        return rows, False

    # Check for SUMMARIZE operator (case-insensitive) to detect aggregation
    if re.search(r"\bSUMMARIZE\b", kql, re.IGNORECASE):
        return rows, False

    if len(rows) <= _ROW_CAP:
        return rows, False

    return rows[:_ROW_CAP], True


def _sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sanitize row values: drop 'dynamic' columns (cannot be serialized to JSON),
    coerce datetime to ISO string, allow int/float/bool/str, cap total entry size.
    Returns a new list (does not mutate input).
    """
    import datetime as dt

    if not rows:
        return rows

    max_entry_bytes = 5 * 1024 * 1024  # 5 MiB per entry

    sanitized: list[dict[str, Any]] = []
    for row in rows:
        clean: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, dt.datetime):
                clean[k] = v.isoformat()
            elif isinstance(v, dt.date):
                clean[k] = v.isoformat()
            elif isinstance(v, (int, float, bool, str)) or v is None:
                clean[k] = v
            elif isinstance(v, dict) or isinstance(v, list):
                # 'dynamic' — skip with warning logged at call site
                logger.warning("_sanitize_rows: dropping dynamic column '%s'", k)
                continue
            else:
                clean[k] = str(v)
        entry_size = len(str(clean).encode("utf-8"))
        if entry_size > max_entry_bytes:
            logger.warning(
                "_sanitize_rows: entry %s exceeds %d bytes (%d), truncating string values",
                list(clean.keys())[:3],
                max_entry_bytes,
                entry_size,
            )
            # Truncate string values in oversized entries
            for k, v in list(clean.items()):
                if isinstance(v, str) and len(v.encode("utf-8")) > 10_000:
                    clean[k] = v[:10_000] + "...[truncated]"
        sanitized.append(clean)
    return sanitized


def _scrub_pii_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Scrub PII from text field values in rows.

    Applies email, phone, and SSN redaction using the safety PII scrubber.
    Returns a new list with scrubbed string values.
    """
    try:
        from src.ai.safety.pii_scrubber import scan_text
    except ImportError:
        # PII scrubber not available — return rows as-is
        return rows

    scrubbed: list[dict[str, Any]] = []
    for row in rows:
        clean: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, str) and v.strip():
                result = scan_text(v)
                clean[k] = result.scrubbed_text
            else:
                clean[k] = v
        scrubbed.append(clean)
    return scrubbed


def _eviction_grace_hours_for_ttl(ttl_hours: int) -> int:
    """Return eviction horizon: max(ttl * 3, 168)."""
    return max(ttl_hours * 3, _EVICTION_GRACE_HOURS)


def _evict_chart_caches(program_id: str, programs_root: Path) -> int:
    """Evict stale chart caches for program_id. Returns count of entries removed.

    Uses the minimum TTL across all chart queries as the eviction horizon to
    ensure no chart's cache outlives its configured freshness window by more
    than the 3x grace multiplier.
    """
    all_queries = load_kpi_queries(program_id, programs_root=programs_root)
    chart_ttls = [
        getattr(query, "chart_cache_ttl_hours", 26)
        for query in all_queries
        if query.render_as == "chart_image" and query.chart_config
    ]

    if not chart_ttls:
        # Fall back to default eviction horizon when no chart queries exist
        return evict_stale_caches(program_id, programs_root=programs_root, max_age_hours=_EVICTION_GRACE_HOURS)

    # Use minimum TTL across queries to be conservative — evict once per gather
    min_ttl = min(chart_ttls)
    max_age = _eviction_grace_hours_for_ttl(min_ttl)
    return evict_stale_caches(program_id, programs_root=programs_root, max_age_hours=max_age)
