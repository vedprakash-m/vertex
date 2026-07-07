"""Local KB file ingestion pipeline (BL-25).

Reads local Markdown/YAML/text files as Enrichment records with body_text.
Zone C — reads local filesystem; passes body_text as plain strings to callers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from src.core.models import Enrichment


log = logging.getLogger(__name__)
_KB_MAX_FILE_SIZE_BYTES = 100 * 1024   # 100 KB
_KB_MAX_BODY_CHARS = 8000
_SUPPORTED_EXTENSIONS = frozenset({".md", ".txt", ".yaml", ".yml"})


def read_local_kb_enrichments(
    *,
    kb_paths: list[str | Path],
    stale_threshold_days: int = 90,
    as_of: datetime | None = None,
) -> tuple[Enrichment, ...]:
    """Enumerate local KB files and return them as Enrichment records with body_text.

    Files older than stale_threshold_days or larger than 100 KB are skipped.
    Each Enrichment has source="local_kb" and body_text set to file contents
    (truncated to _KB_MAX_BODY_CHARS).
    """
    now = as_of or datetime.now(timezone.utc)
    enrichments: list[Enrichment] = []

    for raw_path in kb_paths:
        base = Path(raw_path).expanduser()
        if not base.exists():
            log.warning("Local KB path not found: %s", base)
            continue
        candidates = list(base.rglob("*")) if base.is_dir() else [base]
        for path in candidates:
            enrichment = _try_read_kb_file(path, now=now, stale_threshold_days=stale_threshold_days)
            if enrichment is not None:
                enrichments.append(enrichment)

    return tuple(enrichments)


def _try_read_kb_file(
    path: Path,
    *,
    now: datetime,
    stale_threshold_days: int,
) -> Enrichment | None:
    if not path.is_file():
        return None
    if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return None
    try:
        size = path.stat().st_size
        if size > _KB_MAX_FILE_SIZE_BYTES:
            log.info("Local KB: skipping large file %s (%d bytes)", path, size)
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        days_old = (now - mtime).days
        if days_old > stale_threshold_days:
            log.info("Local KB: skipping stale file %s (%d days old)", path, days_old)
            return None
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Local KB: cannot read %s: %s", path, exc)
        return None

    body_trimmed = body.strip()[:_KB_MAX_BODY_CHARS] if body.strip() else None
    source_id = str(path)
    return Enrichment(
        source="local_kb",
        source_id=source_id,
        author="local_file",
        timestamp=mtime,
        excerpt=path.name,
        permalink=None,
        body_text=body_trimmed,
    )
