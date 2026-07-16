"""ADF-W0.8 cockpit value objects (specs/arch-data-fix.md Section 9.1 + Appendix A.6).

Read-only projection models. ``CockpitSnapshot`` and its nested summaries are
never an authoritative store (INV-ADF-10): they are built by
:mod:`src.core.cockpit_builder` from existing readers/measurement stores and
serialized verbatim to ``runtime/cockpit/latest.json`` plus a history
snapshot.
"""

from __future__ import annotations

import hashlib
import json
import types
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

#: Section 9.1 / Appendix A.6.
COCKPIT_SCHEMA_VERSION = "1"

#: CockpitFinding.status (Appendix A.6).
FINDING_STATUSES = frozenset({"ok", "info", "warn", "blocked"})

#: CockpitFinding.area (Appendix A.6).
FINDING_AREAS = frozenset(
    {"program", "source", "intelligence", "economics", "value", "reliability", "platform", "schedule", "trust"}
)


class ValueConfidence(str, Enum):
    """Section 9.2. INV-ADF-11: a proxy value may never present as measured."""

    MEASURED = "measured"
    CALIBRATED = "calibrated"
    PROXY = "proxy"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ValueMetric:
    metric_id: str
    program_id: str
    edition_id: str | None
    scope: str  # edition | program_aggregate | fleet
    label: str
    value: float | int | None
    unit: str
    confidence: ValueConfidence
    baseline_value: float | int | None
    delta_value: float | int | None
    formula_version: str
    evidence_refs: tuple[str, ...]
    period_start: datetime
    period_end: datetime


@dataclass(frozen=True, slots=True)
class TimeSavingsCertification:
    schema_version: str
    program_id: str
    edition_id: str
    workflow: str
    manual_sample_count: int
    vertex_sample_count: int
    manual_median_active_seconds: float
    vertex_median_active_seconds: float
    savings_ratio: float
    confidence_interval_low: float | None


@dataclass(frozen=True, slots=True)
class CockpitFinding:
    finding_id: str
    area: str
    status: str
    summary: str
    detail: str
    owner: str | None
    next_command: str | None
    evidence_refs: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.area not in FINDING_AREAS:
            raise ValueError(f"CockpitFinding.area={self.area!r} not in {sorted(FINDING_AREAS)}")
        if self.status not in FINDING_STATUSES:
            raise ValueError(f"CockpitFinding.status={self.status!r} not in {sorted(FINDING_STATUSES)}")


@dataclass(frozen=True, slots=True)
class ProgramCockpitSummary:
    overall_risk: str
    readiness_percent: int | None
    blocker_count: int
    top_three_candidates: tuple[str, ...]
    next_action: str | None


@dataclass(frozen=True, slots=True)
class SourceCockpitSummary:
    required_healthy: int
    required_total: int
    stale_sources: tuple[str, ...]
    degraded_sources: tuple[str, ...]
    manual_sources: tuple[str, ...]
    #: Sole cockpit freshness-watermark field (QG-38). No duplicate top-level
    #: watermark map is permitted (Section 9.1).
    newest_watermarks: dict[str, str]


@dataclass(frozen=True, slots=True)
class IntelligenceCockpitSummary:
    lineage_coverage: float | None
    verification_coverage: float | None
    extraction_quality: tuple[ValueMetric, ...]
    contradiction_count: int
    #: ADF-W2.9: the through-line of the most recent QG-29-*released*
    #: ProgramSynthesis (Section 8.10.5), or None if none has been
    #: generated/released yet. Never an unreleased draft -- see
    #: ``src.core.program_synthesis.load_latest_released_program_synthesis``.
    program_synthesis_through_line: str | None = None
    program_synthesis_generated_at: str | None = None
    program_synthesis_ai_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class EconomicsCockpitSummary:
    frontier_avoidance: float | None
    frontier_cost_usd: float
    cache_hit_rate: float | None
    context_tokens_in: int


@dataclass(frozen=True, slots=True)
class ValueCockpitSummary:
    metrics: tuple[ValueMetric, ...]
    time_savings_certification: TimeSavingsCertification | None


@dataclass(frozen=True, slots=True)
class ReliabilityCockpitSummary:
    outbox_pending: int
    uncertain_remote_state: int
    dead_letter_count: int
    duplicate_preventions: int
    audit_coverage: float | None


@dataclass(frozen=True, slots=True)
class ProposalClassTrustSummary:
    """ADF-W5.12 (Section 8.15.4): one proposal class's trust-cockpit row."""

    proposal_class: str
    level: str  # l0..l4
    permitted_action: str
    ceiling: str
    acceptance_rate: float | None
    reject_rate: float | None
    reversal_rate: float | None  # always None today -- no reversal telemetry exists yet
    review_count: int
    current_sample_rate: float
    last_change_reason: str
    remaining_evidence: str


@dataclass(frozen=True, slots=True)
class TrustCockpitSummary:
    """ADF-W5.12 (Section 8.15.4): the trust cockpit -- one row per
    proposal class governed by the autonomy ladder."""

    classes: tuple[ProposalClassTrustSummary, ...]


@dataclass(frozen=True, slots=True)
class CockpitSnapshot:
    schema_version: str
    program_id: str
    edition_id: str | None
    generated_at: datetime
    as_of: datetime
    program_summary: ProgramCockpitSummary
    source_summary: SourceCockpitSummary
    intelligence_summary: IntelligenceCockpitSummary
    economics_summary: EconomicsCockpitSummary
    value_summary: ValueCockpitSummary
    reliability_summary: ReliabilityCockpitSummary
    findings: tuple[CockpitFinding, ...]
    input_hash: str
    #: ADF-W5.12, added additively with a default so every pre-existing
    #: construction call site (tests, other builders) is unaffected.
    trust_summary: TrustCockpitSummary | None = None


# --------------------------------------------------------------------------------------
# Serialization (Appendix A.6)
# --------------------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        as_utc = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return as_utc.astimezone(timezone.utc).isoformat()
    if isinstance(value, (tuple, list)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


def cockpit_snapshot_to_json_dict(snapshot: CockpitSnapshot) -> dict[str, Any]:
    """The JSON serialization of *snapshot* per Appendix A.6."""
    return _to_jsonable(snapshot)


def _from_jsonable(value: Any, target_type: Any) -> Any:
    """Generic inverse of ``_to_jsonable``, driven by the target dataclass's
    own type hints -- ADF-W5.5's ``cockpit compare``/history reading needs
    a real deserializer (only a serializer existed before). Explicit
    per-type parsing was considered and rejected: ten nested dataclasses
    would mean ten near-identical hand-written parsers to keep in sync with
    ``_to_jsonable``'s own generic (reflective) shape -- a real
    maintenance/drift risk `_to_jsonable` itself does not have.
    """
    origin = get_origin(target_type)

    # PEP 604 `X | None` (used throughout this codebase) produces
    # `types.UnionType`, not `typing.Union` -- `get_origin` distinguishes
    # them, so both must be checked or every `SomeDataclass | None` field
    # silently falls through to the raw-dict fallback below instead of
    # being reconstructed.
    if origin is Union or origin is types.UnionType:
        args = [arg for arg in get_args(target_type) if arg is not type(None)]
        if value is None:
            return None
        return _from_jsonable(value, args[0])

    if origin in (tuple, list):
        item_type = get_args(target_type)[0]
        return tuple(_from_jsonable(item, item_type) for item in (value or ()))

    if origin is dict:
        return dict(value or {})

    if isinstance(target_type, type) and is_dataclass(target_type):
        hints = get_type_hints(target_type)
        kwargs = {f.name: _from_jsonable(value.get(f.name), hints[f.name]) for f in fields(target_type)}
        return target_type(**kwargs)

    if isinstance(target_type, type) and issubclass(target_type, Enum):
        return target_type(value)

    if target_type is datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    return value


def cockpit_snapshot_from_json_dict(payload: dict[str, Any]) -> CockpitSnapshot:
    """Inverse of ``cockpit_snapshot_to_json_dict``. Raises on structurally
    invalid input (missing/mistyped fields) rather than guessing -- a
    corrupt or foreign-schema history file must fail loudly, not silently
    produce a wrong snapshot."""
    return _from_jsonable(payload, CockpitSnapshot)


def cockpit_snapshot_to_json(snapshot: CockpitSnapshot) -> str:
    return json.dumps(cockpit_snapshot_to_json_dict(snapshot), indent=2, sort_keys=True) + "\n"


def compute_cockpit_input_hash(snapshot: CockpitSnapshot) -> str:
    """sha256 of the canonical JSON of *snapshot* minus ``generated_at``/``input_hash`` (A.6)."""
    payload = cockpit_snapshot_to_json_dict(snapshot)
    payload.pop("generated_at", None)
    payload.pop("input_hash", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def finalize_cockpit_snapshot(snapshot: CockpitSnapshot) -> CockpitSnapshot:
    """Return *snapshot* with ``input_hash`` populated from its own content."""
    return replace(snapshot, input_hash=compute_cockpit_input_hash(snapshot))


def cockpit_history_filename(snapshot: CockpitSnapshot) -> str:
    """History snapshot filename: ``generated_at`` as ``YYYYMMDDTHHMMSSZ`` (A.6)."""
    generated_at = snapshot.generated_at
    as_utc = generated_at if generated_at.tzinfo is not None else generated_at.replace(tzinfo=timezone.utc)
    return as_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"


__all__ = [
    "COCKPIT_SCHEMA_VERSION",
    "FINDING_AREAS",
    "FINDING_STATUSES",
    "ValueConfidence",
    "ValueMetric",
    "TimeSavingsCertification",
    "CockpitFinding",
    "ProgramCockpitSummary",
    "SourceCockpitSummary",
    "IntelligenceCockpitSummary",
    "EconomicsCockpitSummary",
    "ValueCockpitSummary",
    "ReliabilityCockpitSummary",
    "CockpitSnapshot",
    "cockpit_snapshot_to_json_dict",
    "cockpit_snapshot_to_json",
    "compute_cockpit_input_hash",
    "finalize_cockpit_snapshot",
    "cockpit_history_filename",
]
