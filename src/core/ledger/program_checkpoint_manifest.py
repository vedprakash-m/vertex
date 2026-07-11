"""ProgramCheckpointManifest (arch-fix.md Phase 1, CPK).

A single, consistent cross-store checkpoint: event log position, per-family
fact watermarks, projection versions, outbox high-watermarks, and content
hashes of the tracked store files — captured together so a restore either
reproduces this exact state or fails loudly (rather than silently mixing an
event log from time T with a fact-store snapshot from time T-1).

This module composes existing CPK primitives — it does not duplicate their
storage:
  - ``event_log.latest_event_chain_state()`` for event log position.
  - ``projection_checkpoint_store.list_checkpoints()`` for projection versions.
  - ``durable_outbox_store`` callers pass their own domain/db_path pairs
    (this module has no opinion on which outbox domains exist yet).
  - caller-supplied ``tracked_files`` for content hashes (deliberately not
    hardcoded — which store files exist/matter varies as CPK adoption grows;
    a fixed list here would need constant maintenance and silently miss new
    stores).

The manifest itself is content-addressed: ``manifest_hash`` is a hash of
its own canonical serialization, so two manifests can be compared for
equality by hash alone, and a manifest file's integrity can be checked by
recomputing the hash from its contents.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.ledger.event_log import latest_event_chain_state
from src.core.ledger.program_sequence import current_sequence
from src.core.ledger.projection_checkpoint_store import ProjectionCheckpoint, list_checkpoints


@dataclass(frozen=True, slots=True)
class ProgramCheckpointManifest:
    program_id: str
    captured_at: datetime
    event_log_sequence: int
    event_log_last_hash: str | None
    event_log_last_recorded_at: datetime | None
    projection_checkpoints: tuple[ProjectionCheckpoint, ...]
    outbox_watermarks: dict[str, int]
    tracked_file_hashes: dict[str, str]
    schema_versions: dict[str, str]
    manifest_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "captured_at": _wire(self.captured_at),
            "event_log_sequence": self.event_log_sequence,
            "event_log_last_hash": self.event_log_last_hash,
            "event_log_last_recorded_at": _wire(self.event_log_last_recorded_at) if self.event_log_last_recorded_at else None,
            "projection_checkpoints": [
                {
                    "projection_name": c.projection_name,
                    "watermark_event_id": c.watermark_event_id,
                    "watermark_recorded_at": _wire(c.watermark_recorded_at),
                    "projector_version": c.projector_version,
                    "policy_version": c.policy_version,
                    "checksum": c.checksum,
                    "updated_at": _wire(c.updated_at),
                }
                for c in self.projection_checkpoints
            ],
            "outbox_watermarks": dict(sorted(self.outbox_watermarks.items())),
            "tracked_file_hashes": dict(sorted(self.tracked_file_hashes.items())),
            "schema_versions": dict(sorted(self.schema_versions.items())),
            "manifest_hash": self.manifest_hash,
        }


def _wire(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def build_checkpoint_manifest(
    program_id: str,
    *,
    programs_root: Path,
    outbox_watermarks: dict[str, int] | None = None,
    tracked_files: dict[str, Path] | None = None,
    schema_versions: dict[str, str] | None = None,
) -> ProgramCheckpointManifest:
    """Compose a manifest from the current on-disk state.

    ``outbox_watermarks``: caller-supplied {domain: highest-seen attempt/seq}
    (this module doesn't enumerate outbox domains itself — see module docstring).
    ``tracked_files``: caller-supplied {logical_name: path} to content-hash.
    Missing files are simply omitted from the hash map (not an error — a
    program early in CPK adoption may not have every store yet).
    """
    chain_state = latest_event_chain_state(program_id, programs_root=programs_root)
    last_hash, last_recorded_at = chain_state if chain_state is not None else (None, None)

    file_hashes = {
        name: _hash_file(path)
        for name, path in (tracked_files or {}).items()
        if path.exists() and path.is_file()
    }

    provisional = ProgramCheckpointManifest(
        program_id=program_id,
        captured_at=datetime.now(timezone.utc),
        event_log_sequence=current_sequence(program_id, programs_root=programs_root),
        event_log_last_hash=last_hash,
        event_log_last_recorded_at=last_recorded_at,
        projection_checkpoints=list_checkpoints(program_id, programs_root=programs_root),
        outbox_watermarks=dict(outbox_watermarks or {}),
        tracked_file_hashes=file_hashes,
        schema_versions=dict(schema_versions or {}),
        manifest_hash="",
    )
    manifest_hash = "sha256:" + hashlib.sha256(_canonical_json(provisional.to_dict()).encode("utf-8")).hexdigest()
    return replace(provisional, manifest_hash=manifest_hash)


def write_manifest(manifest: ProgramCheckpointManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def read_manifest(path: Path) -> ProgramCheckpointManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    projection_checkpoints = tuple(
        ProjectionCheckpoint(
            projection_name=row["projection_name"],
            watermark_event_id=row["watermark_event_id"],
            watermark_recorded_at=_parse(row["watermark_recorded_at"]),
            projector_version=row["projector_version"],
            policy_version=row["policy_version"],
            checksum=row.get("checksum"),
            updated_at=_parse(row["updated_at"]),
        )
        for row in raw["projection_checkpoints"]
    )
    return ProgramCheckpointManifest(
        program_id=raw["program_id"],
        captured_at=_parse(raw["captured_at"]),
        event_log_sequence=int(raw["event_log_sequence"]),
        event_log_last_hash=raw.get("event_log_last_hash"),
        event_log_last_recorded_at=_parse(raw["event_log_last_recorded_at"]) if raw.get("event_log_last_recorded_at") else None,
        projection_checkpoints=projection_checkpoints,
        outbox_watermarks=dict(raw.get("outbox_watermarks") or {}),
        tracked_file_hashes=dict(raw.get("tracked_file_hashes") or {}),
        schema_versions=dict(raw.get("schema_versions") or {}),
        manifest_hash=raw["manifest_hash"],
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_manifest_self_hash(manifest: ProgramCheckpointManifest) -> bool:
    """Recompute ``manifest_hash`` from the manifest's own content and check
    it matches — detects a manifest file that was hand-edited or corrupted
    independent of any comparison to on-disk store state."""
    placeholder = replace(manifest, manifest_hash="")
    recomputed = "sha256:" + hashlib.sha256(_canonical_json(placeholder.to_dict()).encode("utf-8")).hexdigest()
    return recomputed == manifest.manifest_hash


def verify_manifest_against_disk(
    manifest: ProgramCheckpointManifest, *, tracked_files: dict[str, Path]
) -> tuple[str, ...]:
    """Re-hash ``tracked_files`` and report any mismatch/drift against the
    manifest's recorded hashes — the restore-drill check ("reproduces it or
    fails"). Returns an empty tuple when everything matches."""
    issues: list[str] = []
    for name, expected_hash in manifest.tracked_file_hashes.items():
        path = tracked_files.get(name)
        if path is None or not path.exists():
            issues.append(f"{name}: file missing on disk (manifest expected {expected_hash})")
            continue
        actual_hash = _hash_file(path)
        if actual_hash != expected_hash:
            issues.append(f"{name}: hash mismatch — manifest={expected_hash} disk={actual_hash}")
    return tuple(issues)
