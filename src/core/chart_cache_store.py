# Phase 1 — Chart cache store (Zone A)
"""
Zone A chart cache store. Persists pre-scrubbed chart row data as JSON.
No AI or M365 imports — this module must stay in Zone A.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataclasses import dataclass

from src.core.models_v2 import IntegrationError

logger = logging.getLogger(__name__)

MAX_CACHE_ENTRY_BYTES = 5 * 1024 * 1024  # 5 MiB

# Allowed Kusto column types for cache storage
_ALLOWED_COLUMN_TYPES = (int, float, str, bool)


def _serialize_datetime(value: datetime) -> str:
    """Serialize datetime to ISO 8601 string."""
    return _normalize_datetime(value).isoformat()


def _is_allowed_type(value: Any) -> bool:
    """Check if value is an allowed JSON-serializable type."""
    return isinstance(value, _ALLOWED_COLUMN_TYPES)


def _sanitize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sanitize rows: only allow JSON-safe scalar types.
    Drop dynamic columns, log warnings.
    Returns a new list of sanitized row dicts.
    """
    sanitized = []
    for row in rows:
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if _is_allowed_type(value):
                clean[key] = value
            elif isinstance(value, datetime):
                clean[key] = _serialize_datetime(value)
            else:
                logger.warning(
                    "chart_cache_store: dropping column '%s' of type %s — not JSON-safe",
                    key,
                    type(value).__name__,
                )
        sanitized.append(clean)
    return sanitized


def write_chart_cache(
    program_id: str,
    query_id: str,
    rows: list[dict[str, Any]],
    captured_at: datetime,
    chart_config_hash: str,
    *,
    programs_root: Path,
    pii_prescrubbed: bool,
) -> Path | None:
    """
    Write pre-sanitized rows to atomic JSON sidecar.

    Raises ValueError if pii_prescrubbed is False (security invariant).

    Returns Path to the written cache file, or None if entry was skipped
    due to size exceeding MAX_CACHE_ENTRY_BYTES.
    """
    if not pii_prescrubbed:
        raise ValueError(
            "chart cache write requires pre-scrubbed data — "
            "call PII scrubber in gather pipeline before writing"
        )

    cache_dir = programs_root / program_id / "chart_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{query_id}.json"

    sanitized_rows = _sanitize_rows(rows)

    entry = {
        "schema_version": "1",
        "program_id": program_id,
        "query_id": query_id,
        "captured_at": _serialize_datetime(captured_at),
        "chart_config_hash": chart_config_hash,
        "rows": sanitized_rows,
        "row_count": len(sanitized_rows),
    }

    serialized = json.dumps(entry, indent=2).encode("utf-8")
    if len(serialized) > MAX_CACHE_ENTRY_BYTES:
        logger.error(
            "chart_cache_store: cache entry for %s/%s exceeds %d bytes — skipping write",
            program_id,
            query_id,
            MAX_CACHE_ENTRY_BYTES,
        )
        return None

    tmp_path = cache_path.with_suffix(".json.tmp")
    tmp_path.write_bytes(serialized)
    os.replace(str(tmp_path), str(cache_path))

    logger.info(
        "chart_cache_store: wrote cache %s/%s (%d rows, %d bytes)",
        program_id,
        query_id,
        len(sanitized_rows),
        len(serialized),
    )
    return cache_path


def load_chart_cache(
    program_id: str,
    query_id: str,
    *,
    programs_root: Path,
) -> ChartCacheEntry | None:
    """
    Load cached chart data. Returns None if file is missing or corrupt.
    Logs WARNING for corrupt files — never crashes report generation.
    """
    cache_path = programs_root / program_id / "chart_cache" / f"{query_id}.json"
    if not cache_path.exists():
        return None

    try:
        raw = cache_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "chart_cache_store: corrupt cache for %s/%s — %s",
            program_id,
            query_id,
            exc,
        )
        return None

    if not isinstance(data, dict):
        logger.warning(
            "chart_cache_store: invalid top-level cache payload for %s/%s",
            program_id,
            query_id,
        )
        return None

    try:
        captured_at = _required_datetime(data.get("captured_at"), field_name="captured_at")
    except (TypeError, ValueError) as exc:
        logger.warning(
            "chart_cache_store: invalid captured_at in %s/%s — %s",
            program_id,
            query_id,
            exc,
        )
        return None

    rows_raw = data.get("rows")
    if not isinstance(rows_raw, list):
        logger.warning(
            "chart_cache_store: invalid rows payload in %s/%s",
            program_id,
            query_id,
        )
        return None

    try:
        cache_program_id = _required_string(data.get("program_id"), field_name="program_id")
        cache_query_id = _required_string(data.get("query_id"), field_name="query_id")
        chart_config_hash = _required_string(data.get("chart_config_hash"), field_name="chart_config_hash")
        row_count = _required_int(data.get("row_count"), field_name="row_count")
        schema_version = _required_string(data.get("schema_version"), field_name="schema_version")
        if schema_version != "1":
            raise ValueError(f"unsupported schema_version {schema_version!r}")
        rows = _required_row_tuple(rows_raw)
        if row_count != len(rows):
            raise ValueError(f"row_count {row_count} does not match rows length {len(rows)}")
    except (TypeError, ValueError) as exc:
        logger.warning(
            "chart_cache_store: invalid cache metadata in %s/%s — %s",
            program_id,
            query_id,
            exc,
        )
        return None

    return ChartCacheEntry(
        program_id=cache_program_id,
        query_id=cache_query_id,
        captured_at=captured_at,
        chart_config_hash=chart_config_hash,
        rows=rows,
        row_count=row_count,
        schema_version=schema_version,
    )


def chart_cache_age_hours(entry: ChartCacheEntry, now: datetime | None = None) -> float:
    """Return age of cache entry in hours."""
    if now is None:
        now = datetime.now(timezone.utc)
    delta = now - entry.captured_at
    return delta.total_seconds() / 3600.0


def evict_stale_caches(
    program_id: str,
    *,
    programs_root: Path,
    max_age_hours: int = 168,
) -> int:
    """
    Remove cache files older than max_age_hours.
    Returns count of files removed.
    Called at end of each gather run.
    """
    cache_dir = programs_root / program_id / "chart_cache"
    if not cache_dir.exists():
        return 0

    now = datetime.now(timezone.utc)
    removed = 0
    for cache_file in cache_dir.glob("*.json"):
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            captured_str = data.get("captured_at", "")
            if not captured_str:
                # Can't determine age — skip
                continue
            captured = datetime.fromisoformat(captured_str)
            age_hours = (now - captured).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                cache_file.unlink()
                removed += 1
                logger.info(
                    "chart_cache_store: evicted stale cache %s (%.1f hours old)",
                    cache_file.name,
                    age_hours,
                )
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt file — remove it
            cache_file.unlink()
            removed += 1
            logger.warning(
                "chart_cache_store: removed corrupt cache %s",
                cache_file.name,
            )
    return removed


@dataclass
class ChartCacheEntry:
    """Cache entry for chart data. Not frozen due to tuple construction."""
    program_id: str
    query_id: str
    captured_at: datetime
    chart_config_hash: str
    rows: tuple[dict[str, Any], ...]
    row_count: int
    schema_version: str = "1"


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _required_row_tuple(value: list[Any]) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise TypeError("rows entries must be mappings")
        clean_row: dict[str, Any] = {}
        for key, cell in entry.items():
            if not isinstance(key, str):
                raise TypeError("rows keys must be strings")
            if not _is_allowed_type(cell):
                raise TypeError(f"rows values for key {key!r} must be JSON-safe scalars")
            clean_row[key] = cell
        rows.append(clean_row)
    return tuple(rows)


def _required_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return _normalize_datetime(parsed)