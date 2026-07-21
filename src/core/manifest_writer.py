from __future__ import annotations

import hashlib
import json
import os
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.models import RunManifest, Snapshot


REPO_ROOT = Path(__file__).resolve().parents[2]
from src.core.edition_resolver import PROGRAMS_ROOT, get_program_output_dir


def get_output_root(edition: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root)


def get_manifest_path(edition: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_output_root(edition, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"


def build_run_manifest(
    manifest_id: str,
    issue_number: int,
    edition: str,
    started_at: datetime,
    ended_at: datetime,
    config_payload: Any,
    snapshot: Snapshot,
    html_content: str,
    markdown_content: str,
    ado_calls: int,
    ai_calls: int,
    ai_cost_usd: float,
    freshness_summary: dict[str, int],
    qg_results: dict[str, bool],
    git_sha: str | None,
    ai_cost_by_model: dict[str, float] | None = None,
    notes: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    gather_run_id: str | None = None,
    gather_run_hash: str | None = None,
) -> RunManifest:
    return RunManifest(
        manifest_id=manifest_id,
        issue_number=issue_number,
        edition=edition,
        started_at=started_at,
        ended_at=ended_at,
        config_hash=_hash_jsonable(config_payload),
        snapshot_hash=_hash_jsonable(snapshot),
        html_hash=hash_content(html_content),
        md_hash=hash_content(markdown_content),
        ado_calls=ado_calls,
        ai_calls=ai_calls,
        ai_cost_usd=ai_cost_usd,
        ai_cost_by_model=dict(ai_cost_by_model or {}),
        freshness_summary=dict(freshness_summary),
        qg_results=dict(qg_results),
        git_sha=git_sha,
        notes=tuple(notes),
        metadata=dict(metadata or {}),
        gather_run_id=gather_run_id,
        gather_run_hash=gather_run_hash,
    )


def write_run_manifest(
    edition: str,
    issue_number: int,
    manifest: RunManifest,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_manifest_path(edition, issue_number, programs_root=programs_root)
    payload = _to_jsonable(manifest)
    _write_atomic_json(path, payload)
    return path


def hash_content(content: str | bytes) -> str:
    raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    return f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"


def _hash_jsonable(value: Any) -> str:
    payload = json.dumps(_to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hash_content(payload)


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value