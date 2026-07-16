"""ADF-W1.5 (specs/arch-data-fix.md Appendix A.7): prefetch snapshot store.

A content-addressed, payload-then-manifest-commit snapshot store so a slow
or optional channel (WorkIQ NL foremost) can be acquired out-of-band by
``vertex prefetch`` and then consumed by report/gather as a bounded,
non-blocking read -- never a live inline call (Section 8.3.2 / INV-ADF-2).

Layout::

    programs/<id>/runtime/prefetch/<channel>/<snapshot_id>/payload.json
    programs/<id>/runtime/prefetch/<channel>/<snapshot_id>/manifest.json
    programs/<id>/runtime/prefetch/<channel>/latest.json

Commit protocol: the payload file is written and fsynced *first*; the
manifest is written *last* via atomic rename and is the sole commit marker.
A snapshot directory without ``manifest.json`` is always treated as absent
by every reader here -- an in-flight or interrupted write is never visible.
The ``latest.json`` pointer is only rewritten (also atomically) after the
manifest exists, so a reader can never be pointed at an uncommitted
snapshot.

This module is the storage primitive only. The `vertex prefetch` CLI
command (writer side) and gather's live-WorkIQ-attempt replacement
(consumer side beyond the read API below) are tracked as a follow-up to
this module, not included here.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.edition_resolver import PROGRAMS_ROOT

PREFETCH_SCHEMA_VERSION = "1"

_PAYLOAD_FILENAME = "payload.json"
_MANIFEST_FILENAME = "manifest.json"
_LATEST_POINTER_FILENAME = "latest.json"

#: PrefetchSnapshotManifest.completeness (Appendix A.7).
COMPLETENESS_STATES = frozenset({"complete", "partial", "degraded"})


@dataclass(frozen=True, slots=True)
class PrefetchSnapshotManifest:
    schema_version: str
    program_id: str
    channel: str
    snapshot_id: str  # sha256 of the payload content
    created_at: datetime
    expires_at: datetime
    watermark: str | None
    completeness: str
    latency_ms: float
    source_identities: tuple[str, ...]
    payload_path: str  # relative to the snapshot directory

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "program_id": self.program_id,
            "channel": self.channel,
            "snapshot_id": self.snapshot_id,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "watermark": self.watermark,
            "completeness": self.completeness,
            "latency_ms": self.latency_ms,
            "source_identities": list(self.source_identities),
            "payload_path": self.payload_path,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "PrefetchSnapshotManifest":
        return PrefetchSnapshotManifest(
            schema_version=str(payload["schema_version"]),
            program_id=str(payload["program_id"]),
            channel=str(payload["channel"]),
            snapshot_id=str(payload["snapshot_id"]),
            created_at=_parse_iso(str(payload["created_at"])),
            expires_at=_parse_iso(str(payload["expires_at"])),
            watermark=payload.get("watermark"),
            completeness=str(payload["completeness"]),
            latency_ms=float(payload["latency_ms"]),
            source_identities=tuple(str(v) for v in payload.get("source_identities") or ()),
            payload_path=str(payload["payload_path"]),
        )

    def is_expired(self, *, at: datetime | None = None) -> bool:
        now = at or datetime.now(timezone.utc)
        return now >= self.expires_at


def _iso(value: datetime) -> str:
    as_utc = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return as_utc.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _channel_dir(program_id: str, channel: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "runtime" / "prefetch" / channel


def _snapshot_dir(program_id: str, channel: str, snapshot_id: str, *, programs_root: Path) -> Path:
    return _channel_dir(program_id, channel, programs_root=programs_root) / snapshot_id


def compute_snapshot_id(payload_bytes: bytes) -> str:
    return hashlib.sha256(payload_bytes).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def write_prefetch_snapshot(
    *,
    program_id: str,
    channel: str,
    payload: dict[str, Any],
    watermark: str | None,
    completeness: str,
    latency_ms: float,
    ttl_seconds: int,
    source_identities: tuple[str, ...] = (),
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> PrefetchSnapshotManifest:
    """Write a new content-addressed prefetch snapshot (payload-then-manifest commit)."""
    if completeness not in COMPLETENESS_STATES:
        raise ValueError(f"completeness must be one of {sorted(COMPLETENESS_STATES)}, got {completeness!r}")
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")

    created_at = now or datetime.now(timezone.utc)
    expires_at = created_at + timedelta(seconds=ttl_seconds)
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    snapshot_id = compute_snapshot_id(payload_bytes)

    snapshot_dir = _snapshot_dir(program_id, channel, snapshot_id, programs_root=programs_root)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Payload first, fsynced -- readers only ever look for it via a manifest
    # they already trust to exist.
    payload_path = snapshot_dir / _PAYLOAD_FILENAME
    with payload_path.open("wb") as handle:
        handle.write(payload_bytes)
        handle.flush()
        os.fsync(handle.fileno())

    manifest = PrefetchSnapshotManifest(
        schema_version=PREFETCH_SCHEMA_VERSION,
        program_id=program_id,
        channel=channel,
        snapshot_id=snapshot_id,
        created_at=created_at,
        expires_at=expires_at,
        watermark=watermark,
        completeness=completeness,
        latency_ms=latency_ms,
        source_identities=source_identities,
        payload_path=_PAYLOAD_FILENAME,
    )
    # Manifest last -- the sole commit marker (atomic rename).
    _atomic_write_json(snapshot_dir / _MANIFEST_FILENAME, manifest.to_dict())

    # Only now, after the manifest is committed, point "latest" at it.
    _atomic_write_json(
        _channel_dir(program_id, channel, programs_root=programs_root) / _LATEST_POINTER_FILENAME,
        {"snapshot_id": snapshot_id},
    )
    return manifest


def read_latest_committed_snapshot(
    program_id: str,
    channel: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> PrefetchSnapshotManifest | None:
    """Return the newest *committed* snapshot's manifest, or None.

    A snapshot directory without ``manifest.json`` (in-flight, interrupted,
    or corrupted) is always treated as absent, never as a success-shaped
    empty result.
    """
    channel_dir = _channel_dir(program_id, channel, programs_root=programs_root)
    pointer_path = channel_dir / _LATEST_POINTER_FILENAME
    if not pointer_path.exists():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        snapshot_id = str(pointer["snapshot_id"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    manifest_path = channel_dir / snapshot_id / _MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    try:
        return PrefetchSnapshotManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def read_snapshot_payload(
    program_id: str,
    channel: str,
    manifest: PrefetchSnapshotManifest,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any]:
    snapshot_dir = _snapshot_dir(program_id, channel, manifest.snapshot_id, programs_root=programs_root)
    payload_path = snapshot_dir / manifest.payload_path
    return json.loads(payload_path.read_text(encoding="utf-8"))


def read_unexpired_committed_snapshot(
    program_id: str,
    channel: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> PrefetchSnapshotManifest | None:
    """Section 10.6: "gather consumes an unexpired committed snapshot before
    any live WorkIQ attempt." The one function a consumer should call.

    Returns None (never a stale/expired manifest) if the newest committed
    snapshot has already passed its ``expires_at``.
    """
    manifest = read_latest_committed_snapshot(program_id, channel, programs_root=programs_root)
    if manifest is None:
        return None
    if manifest.is_expired(at=now):
        return None
    return manifest


__all__ = [
    "PREFETCH_SCHEMA_VERSION",
    "COMPLETENESS_STATES",
    "PrefetchSnapshotManifest",
    "compute_snapshot_id",
    "write_prefetch_snapshot",
    "read_latest_committed_snapshot",
    "read_snapshot_payload",
    "read_unexpired_committed_snapshot",
]
