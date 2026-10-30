from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.exceptions import ConfigError
from src.core.models import EvidenceBinding, RunEvidenceSnapshot, RunManifest, Snapshot, SourceScopeContract

if TYPE_CHECKING:
    from src.core.gather_run_manifest import GatherRunManifest
    from src.core.program_fact_store import FactSnapshotPin, ProgramFactSnapshot


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


def hash_jsonable(value: Any) -> str:
    payload = json.dumps(_to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hash_content(payload)


_hash_jsonable = hash_jsonable


def build_run_evidence_snapshot(
    pin: FactSnapshotPin | None, snapshot: ProgramFactSnapshot | None,
    gather: GatherRunManifest | None, manifest: RunManifest, report_snapshot: Snapshot,
) -> RunEvidenceSnapshot:
    if pin is None or snapshot is None or pin.program_id != snapshot.program_id:
        raise ConfigError("Run evidence retained fact capture is missing or inconsistent.")
    if (
        (gather.run_id if gather else None, gather.manifest_hash if gather else None)
        != (manifest.gather_run_id, manifest.gather_run_hash)
        or (gather is not None and gather.program_id != pin.program_id)
        or hash_jsonable(report_snapshot) != manifest.snapshot_hash
    ):
        raise ConfigError("Run evidence source or rendered snapshot identity is inconsistent.")
    capture = RunEvidenceSnapshot(
        program_id=pin.program_id, fact_snapshot_id=pin.snapshot_id,
        fact_content_sha256=pin.content_sha256 or "",
        as_of=_capture_clock(snapshot.as_of), valid_at=_capture_clock(snapshot.valid_at),
        gather_run_id=manifest.gather_run_id, gather_run_hash=manifest.gather_run_hash,
        source_scopes=tuple(sorted((
            SourceScopeContract(row.query_id, row.scope_id, row.wiql_hash)
            for row in (gather.query_results if gather else ())
        ), key=lambda row: (row.query_id, row.scope_id))),
        binding_declarations=tuple(sorted((
            EvidenceBinding(fact.revision_id, tuple(sorted(set(fact.entity_refs))))
            for fact in snapshot.facts
        ), key=lambda row: row.fact_revision_id)),
        fetched_item_ids=tuple(sorted(item.id for item in report_snapshot.items)),
        report_snapshot_hash=manifest.snapshot_hash, config_hash=manifest.config_hash, sha256="",
    )
    capture = replace(capture, sha256=hash_jsonable(_capture_content(capture)))
    return _decode_run_capture(_to_jsonable(capture))


def _capture_content(capture: RunEvidenceSnapshot) -> dict[str, Any]:
    payload = asdict(capture)
    payload.pop("sha256")
    return payload


def _capture_clock(value: Any) -> datetime:
    clock = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("Invalid capture clock")
    return clock.astimezone(timezone.utc)


def _decode_run_capture(value: Any) -> RunEvidenceSnapshot:
    try:
        payload = dict(value)
        if set(payload) != {field.name for field in fields(RunEvidenceSnapshot)}:
            raise ValueError("Invalid capture fields")
        payload["as_of"] = _capture_clock(payload["as_of"])
        payload["valid_at"] = _capture_clock(payload["valid_at"])
        payload["source_scopes"] = tuple(SourceScopeContract(**row) for row in payload["source_scopes"])
        payload["binding_declarations"] = tuple(
            EvidenceBinding(**{**row, "entity_refs": tuple(row["entity_refs"])})
            for row in payload["binding_declarations"]
        )
        payload["fetched_item_ids"] = tuple(payload["fetched_item_ids"])
        payload["limitations"] = tuple(payload["limitations"])
        capture = RunEvidenceSnapshot(**payload)
        texts = (
            capture.program_id, capture.fact_snapshot_id, capture.report_snapshot_hash, capture.config_hash,
            *(text for row in capture.source_scopes for text in (row.query_id, row.scope_id, row.query_hash)),
            *(text for row in capture.binding_declarations for text in (row.fact_revision_id, *row.entity_refs)),
        )
        scopes = tuple((row.query_id, row.scope_id) for row in capture.source_scopes)
        revisions = tuple(row.fact_revision_id for row in capture.binding_declarations)
        if (
            any(not isinstance(text, str) or not text.strip() for text in texts)
            or not isinstance(capture.fact_content_sha256, str)
            or len(capture.fact_content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in capture.fact_content_sha256)
            or any(type(item) is not int or item <= 0 for item in capture.fetched_item_ids)
            or capture.fetched_item_ids != tuple(sorted(set(capture.fetched_item_ids)))
            or scopes != tuple(sorted(set(scopes))) or revisions != tuple(sorted(set(revisions)))
            or any(row.entity_refs != tuple(sorted(set(row.entity_refs))) for row in capture.binding_declarations)
            or (capture.gather_run_id is None) != (capture.gather_run_hash is None)
            or any(value is not None and (not isinstance(value, str) or not value.strip())
                   for value in (capture.gather_run_id, capture.gather_run_hash))
            or capture.schema_version != "run-evidence-capture.v1"
            or capture.capture_state != "unqualified_capture"
            or capture.limitations != RunEvidenceSnapshot.__dataclass_fields__["limitations"].default
            or capture.sha256 != hash_jsonable(_capture_content(capture))
        ):
            raise ValueError("Invalid capture content")
        return capture
    except (TypeError, ValueError, KeyError) as error:
        raise ConfigError("Declared run evidence capture is invalid. Regenerate and review the draft.") from error


def load_run_evidence_snapshot(
    manifest_payload: Mapping[str, Any], draft_state: Mapping[str, Any],
) -> RunEvidenceSnapshot | None:
    metadata = manifest_payload.get("metadata") or {}
    reference = draft_state.get("run_evidence_snapshot_sha256")
    if not isinstance(metadata, dict):
        raise ConfigError("Declared run evidence metadata is invalid.")
    if "run_evidence_snapshot" not in metadata and reference is None:
        return None
    capture = _decode_run_capture(metadata.get("run_evidence_snapshot"))
    pin = draft_state.get("program_fact_snapshot")
    if (
        not isinstance(pin, dict) or reference != capture.sha256
        or (pin.get("program_id"), pin.get("snapshot_id")) != (capture.program_id, capture.fact_snapshot_id)
        or manifest_payload.get("config_hash") != capture.config_hash
        or manifest_payload.get("snapshot_hash") != capture.report_snapshot_hash
        or any(
            (payload.get("gather_run_id"), payload.get("gather_run_hash"))
            != (capture.gather_run_id, capture.gather_run_hash)
            for payload in (manifest_payload, draft_state)
        )
    ):
        raise ConfigError("Run evidence capture does not match the captured draft and manifest.")
    return capture


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