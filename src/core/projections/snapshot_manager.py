from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import shutil
from typing import Any, Iterable

from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence, build_event_envelope, compute_envelope_hash
from src.core.ledger.source_refs import SourceRef


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"
PROJECTION_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class ProjectionSnapshotPaths:
    snapshot_path: Path
    manifest_path: Path
    snapshot_hash: str


def get_snapshot_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "ledger" / "projections" / "snapshots"


def write_projection_snapshot(
    program_id: str,
    issue_number: int,
    projection_result: Any,
    *,
    events: Iterable[EventEnvelope],
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ProjectionSnapshotPaths:
    snapshot_dir = get_snapshot_dir(program_id, programs_root=programs_root)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_hash = compute_snapshot_hash(projection_result.projection_path)
    stem = f"issue_{issue_number:03d}-{snapshot_hash}"
    snapshot_path = snapshot_dir / f"{stem}.sqlite3"
    manifest_path = snapshot_dir / f"{stem}.manifest.json"
    shutil.copy2(projection_result.projection_path, snapshot_path)
    snapshot_path.chmod(snapshot_path.stat().st_mode & ~stat.S_IWRITE)
    manifest_payload = build_snapshot_manifest(
        issue_number=issue_number,
        snapshot_hash=snapshot_hash,
        projection_result=projection_result,
        events=tuple(events),
        as_of=as_of,
    )
    manifest_path.write_text(json.dumps(manifest_payload, sort_keys=True, indent=2), encoding="utf-8")
    return ProjectionSnapshotPaths(snapshot_path=snapshot_path, manifest_path=manifest_path, snapshot_hash=snapshot_hash)


def build_baseline_hardlock_event(
    program_id: str,
    issue_number: int,
    snapshot_paths: ProjectionSnapshotPaths,
    projection_result: Any,
    *,
    source_ref: SourceRef,
    actor: str,
    recorded_at: datetime | None = None,
) -> EventEnvelope:
    return build_event_envelope(
        program_id=program_id,
        event_type="operator.baseline_hardlock.v1",
        occurred_at=recorded_at or datetime.now(timezone.utc),
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor=actor,
        payload={
            "issue_number": issue_number,
            "snapshot_hash": snapshot_paths.snapshot_hash,
            "event_id_watermark": projection_result.event_watermark,
            "contributing_event_count": projection_result.event_count,
        },
        source_ref=source_ref,
    )


def build_snapshot_manifest(
    *,
    issue_number: int,
    snapshot_hash: str,
    projection_result: Any,
    events: tuple[EventEnvelope, ...],
    as_of: datetime | None,
) -> dict[str, Any]:
    head_hash = compute_envelope_hash(events[-1]) if events else None
    return {
        "issue_number": issue_number,
        "snapshot_hash": snapshot_hash,
        "event_id_watermark": projection_result.event_watermark,
        "contributing_event_count": projection_result.event_count,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat() if as_of is not None else None,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "hash_chain_head": head_hash,
        "coverage_earliest": projection_result.coverage_earliest,
        "coverage_latest": projection_result.coverage_latest,
    }


def compute_snapshot_hash(projection_path: Path) -> str:
    from src.core.ledger.program_views import canonical_projection_dump

    canonical = json.dumps(canonical_projection_dump(projection_path), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]