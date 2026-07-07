"""Stable freshness identities for per-thread WorkIQ extraction."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


FRESHNESS_ALGORITHM_VERSION = "workiq-thread-v1"


def workiq_thread_freshness_hash(
    *, conversation_id: str, message_count: int, newest_message_identity: str
) -> str:
    if not conversation_id.strip() or not newest_message_identity.strip() or message_count < 0:
        raise ValueError("Freshness identity requires conversation id, non-negative count, and newest message identity")
    payload = "|".join(
        (FRESHNESS_ALGORITHM_VERSION, conversation_id.strip(), str(message_count), newest_message_identity.strip())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_workiq_freshness_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_workiq_freshness_cache(path: Path, payload: dict[str, dict[str, Any]]) -> None:
    """Atomically replace a local cache; callers persist only successful extraction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def mark_workiq_freshness_success(path: Path, source_id: str) -> None:
    """Promote a retrieved cache entry only after safe evidence persistence."""

    payload = load_workiq_freshness_cache(path)
    record = payload.get(source_id)
    if not isinstance(record, dict) or not record.get("freshness_hash"):
        return
    record["status"] = "success"
    payload[source_id] = record
    write_workiq_freshness_cache(path, payload)
