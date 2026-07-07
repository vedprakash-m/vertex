"""WS-11/WS-16 chart-id migration log.

Spec section WS-16 says the legacy program-prefixed chart renderer alias
(`<program>::deployment_velocity`) must be rewritten to the canonical
renderer id (`core::deployment_velocity`) on every migrate, and the
rewrite MUST be auditable via a `programs/<program>/migration_log.jsonl`
sidecar. Each row records:

    {
      "id": "<unique id, e.g. uuid4>",
      "timestamp": "<ISO8601 UTC>",
      "kind": "chart_id_alias" | "schema_major" | "...",
      "source_id": "<legacy id, e.g. '<program>::deployment_velocity'>",
      "target_id": "<new id, e.g. 'core::deployment_velocity'>",
      "files_touched": ["<relpath>", ...],
      "dry_run": <bool>,
      "operator": "<who triggered, default 'vertex migrate'>",
    }

The sidecar is append-only and JSONL-formatted (one JSON object per line),
matching the convention used by every other append-only audit sidecar in
Vertex (actions, ai_proposals, risk_updates, autonomy_audit, etc.).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import portalocker

from src.core.jsonl_utils import parse_jsonl_line


_MIGRATION_LOG_FILENAME = "migration_log.jsonl"


@dataclass(frozen=True, slots=True)
class MigrationLogEntry:
    id: str
    timestamp: str
    kind: str
    source_id: str
    target_id: str
    files_touched: tuple[str, ...]
    dry_run: bool
    operator: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "files_touched": list(self.files_touched),
            "dry_run": self.dry_run,
            "operator": self.operator,
        }


def migration_log_path(program_id: str, programs_root: Path) -> Path:
    """Return the canonical path of the migration log for a program."""
    return programs_root / program_id / _MIGRATION_LOG_FILENAME


def append_migration_log(
    *,
    program_id: str,
    kind: str,
    source_id: str,
    target_id: str,
    files_touched: Iterable[str] = (),
    dry_run: bool = False,
    operator: str = "vertex migrate",
    programs_root: Path,
    now: datetime | None = None,
) -> MigrationLogEntry:
    """Append a single row to `programs/<id>/migration_log.jsonl`. The write
    is guarded by `portalocker.LOCK_EX` and `os.fsync` to match the
    Tier-1 audit pattern (PB-37)."""
    entry = MigrationLogEntry(
        id=str(uuid.uuid4()),
        timestamp=(now or datetime.now(timezone.utc)).isoformat(),
        kind=kind,
        source_id=source_id,
        target_id=target_id,
        files_touched=tuple(files_touched),
        dry_run=dry_run,
        operator=operator,
    )
    log_path = migration_log_path(program_id, programs_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        portalocker.lock(fh, portalocker.LOCK_EX)
        try:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            portalocker.unlock(fh)
    return entry


def read_migration_log(
    program_id: str, programs_root: Path
) -> tuple[MigrationLogEntry, ...]:
    """Return all entries in the migration log for a program, oldest first.
    Returns an empty tuple if the log doesn't exist (pre-migration era)."""
    log_path = migration_log_path(program_id, programs_root)
    if not log_path.exists():
        return ()
    out: list[MigrationLogEntry] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        payload = parse_jsonl_line(line)
        out.append(
            MigrationLogEntry(
                id=str(payload["id"]),
                timestamp=str(payload["timestamp"]),
                kind=str(payload["kind"]),
                source_id=str(payload["source_id"]),
                target_id=str(payload["target_id"]),
                files_touched=tuple(payload.get("files_touched", ())),
                dry_run=bool(payload.get("dry_run", False)),
                operator=str(payload.get("operator", "vertex migrate")),
            )
        )
    return tuple(out)
