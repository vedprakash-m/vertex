"""RELO-W2.4/W2.5: downgrade-only effective completeness projection.

Combines ``RealityCompletenessVector`` with the run's ``GatherRunManifest`` so no
live surface claims FULL completeness while any source in the run was partial,
truncated, failed or quarantined. Existing fields keep their meaning; this
projection never raises the vector's value.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.gather_run_manifest import (
    GatherRunManifest,
    GatherRunStatus,
    RequiredScopeStatus,
    resolve_latest_committed_manifest,
)
from src.core.reality_completeness import RealityCompletenessVector


@dataclass(frozen=True, slots=True)
class EffectiveCompleteness:
    base: float | None
    value: float | None
    downgraded: bool
    reasons: tuple[str, ...]


def project_effective_completeness(
    vector: RealityCompletenessVector | None,
    manifest: GatherRunManifest | None,
    *,
    extra_reasons: tuple[str, ...] = (),
) -> EffectiveCompleteness:
    reasons: list[str] = list(extra_reasons)
    if vector is not None:
        reasons += [
            f"source_{sv.status}:{sv.surface}"
            for sv in vector.context_coverage.source_visibility
            if sv.status in ("partial", "unknown")
        ]
    if manifest is not None:
        if manifest.status is not GatherRunStatus.COMMITTED:
            reasons.append(f"manifest_status:{manifest.status.value}")
        if manifest.required_scope_status is RequiredScopeStatus.PARTIAL:
            reasons.append("required_scope_partial")
        for entry in manifest.query_results:
            if entry.cap_reached:
                reasons.append(f"query_truncated:{entry.scope_id}")
            if entry.completeness_state != "FULL":
                reasons.append(f"query_incomplete:{entry.scope_id}:{entry.completeness_state}")
            if entry.failure_category:
                reasons.append(f"query_failed:{entry.scope_id}")
        reasons += [f"channel_degraded:{c.channel}" for c in manifest.channel_outcomes if c.degraded]
        if manifest.failed_refs:
            reasons.append(f"failed_refs:{len(manifest.failed_refs)}")
    base = vector.context_coverage.enumeration_completeness if vector is not None else None
    ordered = tuple(sorted(set(reasons)))
    value = None if ordered and base is not None and base >= 1.0 else base
    return EffectiveCompleteness(base=base, value=value, downgraded=value != base, reasons=ordered)


def load_effective_completeness(
    vector: RealityCompletenessVector | None, program_id: str, *, programs_root: Path
) -> EffectiveCompleteness:
    """Resolve the latest committed manifest and project; unreadable evidence is a reason."""
    try:
        manifest = resolve_latest_committed_manifest(program_id, programs_root=programs_root)
    except (OSError, ValueError, KeyError, TypeError):
        return project_effective_completeness(vector, None, extra_reasons=("manifest_unreadable",))
    return project_effective_completeness(vector, manifest)


__all__ = ["EffectiveCompleteness", "load_effective_completeness", "project_effective_completeness"]
