from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import portalocker
import yaml


def load_advisory_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected mapping in {path}")
    return payload


def write_advisory_yaml(
    path: Path,
    payload: dict[str, Any],
    *,
    module_name: str,
    evidence_hash: str,
    generation_run_id: str | None = None,
    timestamp: datetime | None = None,
) -> Path:
    _write_atomic_yaml(path, payload)
    _append_feedback_audit(
        path.parent / "_audit.jsonl",
        {
            "ts": _ensure_utc(timestamp or _utc_now()).isoformat(),
            "module": module_name,
            "file": path.name,
            "evidence_hash": evidence_hash,
            "generation_run_id": generation_run_id or str(uuid4()),
        },
    )
    return path


def _write_atomic_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _append_feedback_audit(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(path, mode="a", timeout=5, encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)