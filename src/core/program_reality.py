"""WI-1.1: ProgramReality — the single read facade for program state (§6.1).

This is the G-1 goal implementation: one read interface for every projection.
Source systems remain authoritative for their native records; this facade is
authoritative for the cross-source program reality picture.

**Zone A module — INV-1 applies.** Must not import from src.ai or src.m365.

**Single I/O rule:** `load()` is the ONLY disk-touching point. Once loaded,
the object is immutable; all accessors are pure functions over in-memory state.

**Phase-1 note:** Truth levels are statically derived (management families
→ HUMAN_CONFIRMED; everything else → RAW_OBSERVED). This is replaced by the
full derivation engine in WI-3.0. The contract test
`test_truth_level_static_pre_phase3` asserts this and is DELETED by WI-3.0.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.entity_registry import EntityRegistry
    from src.core.truth_model import TruthContext

from src.core.truth_levels import TruthLevel
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.knowledge_claim_store import KnowledgeContext, load_program_knowledge_claims, load_program_knowledge_scopes, resolve_knowledge_context, summarize_knowledge_status
from src.core.knowledge.vault_integrity import summarize_knowledge_vault_integrity
from src.core.ledger.candidate_store import CandidateDecisionRecord, load_triage_decisions
from src.core.ledger.event_index import load_entity_event_ids, load_indexed_events
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, read_events
from src.core.ledger.program_views import collapse_orphan_links, collapse_shadow_links, project_events_to_memory
from src.core.ledger.source_refs import source_document_key, source_ref_priority
from src.core.section_proposal_store import load_archived_stale_claim_ids, load_stale_claim_ids
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root
from src.core.program_fact_store import (
    PROGRAMS_ROOT,
    FactLineage,
    ProgramFactRevision,
    ProgramFactSnapshot,
    FactLifecycleState,
    is_fact_stale,
    load_program_facts,
    project_action_items,
    project_assumptions,
    project_claim_entries,
    project_decision_entries,
    project_dependencies,
    project_milestones,
    project_risk_entries,
    project_workstreams,
)
from src.core.models_v2 import (
    ActionItem,
    Assumption,
    ClaimEntry,
    DecisionEntry,
    Dependency,
    MetricObservation,
    Milestone,
    RiskEntry,
    Signal,
    Workstream,
)

# Chronicle ProgramEvent (the fact-store event type)
# NOTE: chronicle.ProgramEvent is for program-level narrative events; we use
# a local FactStoreEvent for the fact-store feed.


@dataclass(frozen=True, slots=True)
class FactStoreEvent:
    """A single fact-store event as emitted by events_since()."""
    fact_type: str
    natural_key: str
    fact_id: str | None
    payload: dict[str, Any]

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

_MANAGEMENT_FACT_TYPES: frozenset[str] = frozenset({
    "action.item",
    "risk.entry",
    "decision.entry",
    "dependency.link",
    "milestone.entry",
    "assumption.entry",
    "workstream.entry",
    "claim.entry",
})

_REALITY_SCHEMA_VERSION = "1"
_TEMPORAL_CONFIDENCE_STRENGTH = {
    "exact": 4,
    "approximate": 3,
    "estimated": 2,
    "reconstructed": 1,
}

@dataclass(frozen=True, slots=True)
class FactAssessment:
    """A domain record wrapped with its validation context.

    ``record`` is the domain-typed view model (ActionItem, RiskEntry, …).
    In Phase 1, truth_level is statically derived; Phase 3 replaces this with
    the full TruthContext-based derivation engine.
    ``lineage`` carries provenance/privacy metadata (S-3 surface — §5.2/§5.7).
    """
    record: Any
    fact_id: str | None
    truth_level: TruthLevel
    disputed: bool
    stale: bool
    provisional_inputs: bool
    evidence: tuple[str, ...]
    lineage: FactLineage | None = None


@dataclass(frozen=True, slots=True)
class RealityDomainFreshness:
    domain: str
    fact_count: int
    stale_count: int
    latest_recorded_at: datetime | None
    sor_mode: str


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    signal_id: str | None
    entity_ref: str | None
    source: str | None


@dataclass(frozen=True, slots=True)
class FactExplanation:
    program_id: str
    fact_id: str
    fact_type: str
    natural_key: str
    truth_level: TruthLevel
    disputed: bool
    stale: bool
    provisional_inputs: bool
    evidence: tuple[EvidenceRef, ...]
    open_conflicts: tuple["RealityConflict", ...]
    source_signal_ids: tuple[str, ...]
    entity_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RealityConflict:
    conflict_id: str
    entity_refs: tuple[str, ...]
    family: str
    open: bool
    description: str


@dataclass(frozen=True, slots=True)
class GapRecord:
    event_id: str
    pipeline: str
    gap_kind: str
    detail: str
    window_start: datetime | None
    window_end: datetime | None
    acknowledged: bool


@dataclass(frozen=True, slots=True)
class LedgerFieldLockRecord:
    entity_id: str
    field: str
    valid_until: datetime


@dataclass(frozen=True, slots=True)
class LedgerShadowReviewRecord:
    event_id: str
    shadowed_by: str
    field_name: str


@dataclass(frozen=True, slots=True)
class LedgerStaleOperatorAssertionRecord:
    event_id: str
    shadowed_by: str
    field_name: str
    asserted_at: datetime


@dataclass(frozen=True, slots=True)
class LedgerTemporalConfidenceReviewRecord:
    event_id: str
    shadowed_by: str
    field_name: str
    winner_temporal_confidence: str
    loser_temporal_confidence: str


@dataclass(frozen=True, slots=True)
class KnowledgeClaimFreshnessRecord:
    issue_number: int
    claim_ids: tuple[str, ...]
    evidence_source: str = "live_proposals"


@dataclass(frozen=True, slots=True)
class LedgerTimelineEntry:
    event_id: str
    event_type: str
    occurred_at: datetime
    recorded_at: datetime
    actor: str
    confidence: str
    temporal_confidence: str
    source_document_key: str
    orphaned_by: str | None = None
    shadowed_by: str | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class ProgramProjection:
    as_of: datetime
    knowledge_as_of: datetime | None
    tables: dict[str, tuple[dict[str, Any], ...]]

    def table(self, name: str) -> tuple[dict[str, Any], ...]:
        return self.tables.get(name, ())

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "knowledge_as_of": self.knowledge_as_of.isoformat() if self.knowledge_as_of is not None else None,
            "tables": {
                name: [dict(row) for row in rows]
                for name, rows in self.tables.items()
            },
        }


@dataclass(frozen=True, slots=True)
class RealityDelta:
    added: tuple[FactAssessment, ...]
    changed: tuple[tuple[FactAssessment, FactAssessment], ...]
    retired: tuple[FactAssessment, ...]
    dispute_opened: tuple[FactAssessment, ...]
    dispute_resolved: tuple[FactAssessment, ...]
    non_replayable_families: tuple[str, ...]


class AttentionKind(str):
    """Closed-by-design enumeration of attention item types.

    (v3.1) Adding a kind is a deliberate core change — unlike YAML extension
    points, attention semantics are part of the platform contract.
    """
    DISPUTED_FACT = "disputed_fact"
    STALE_HIGH_SEVERITY = "stale_high_severity"
    UNANSWERED_DECISION = "unanswered_decision"
    PENDING_ACTUATION = "pending_actuation"
    CORROBORATED_RISK_AWAITING_REVIEW = "corroborated_risk_awaiting_review"
    COMMITMENT_SLIPPED = "commitment_slipped"
    STRUCTURAL_GAP = "structural_gap"
    DECISION_OUTCOME_DRIFT = "decision_outcome_drift"
    OVERRIDE_RECERTIFICATION_DUE = "override_recertification_due"
    LEDGER_CONFLICT_REVIEW = "ledger_conflict_review"
    OPERATOR_ASSERTION_STALE = "operator_assertion_stale"
    TEMPORAL_CONFIDENCE_REVIEW = "temporal_confidence_review"
    CLAIM_FRESHNESS = "claim_freshness"
    KNOWLEDGE_VAULT_INTEGRITY = "knowledge_vault_integrity"


@dataclass(frozen=True, slots=True)
class AttentionItem:
    kind: str
    priority: int
    record: FactAssessment | None
    description: str
    action_hint: str
    provisional_inputs: bool = False


@dataclass(frozen=True, slots=True)
class ActuationProposal:
    proposal_id: str
    rule_id: str
    adapter: str
    operation: str
    entity_ref: str
    payload: dict[str, Any]
    proposed_at: datetime
    approved: bool = False
    gap_reason: str = ""  # non-empty → proposal blocked at derivation (e.g. "missing_area_path")


@dataclass(frozen=True, slots=True)
class FleetAttentionItem:
    program_id: str
    item: AttentionItem


@dataclass(frozen=True, slots=True)
class FleetConflict:
    program_id: str
    conflict: RealityConflict


@dataclass(frozen=True, slots=True)
class FleetActuationProposal:
    program_id: str
    proposal: ActuationProposal


@dataclass(frozen=True, slots=True)
class FleetFreshnessRecord:
    program_id: str
    freshness: RealityDomainFreshness


@dataclass(frozen=True, slots=True)
class CanonicalEntity:
    entity_id: str
    entity_type: str
    canonical_name: str
    aliases: tuple[str, ...]
    scope: str


# ---------------------------------------------------------------------------
# Helper: provisional propagation (§6.3 rule 9)
# ---------------------------------------------------------------------------

def any_provisional(*assessments: FactAssessment) -> bool:
    """The ONE sanctioned way derived computations propagate the provisional flag.

    Derived computations (risk scoring, nudge triggers, attention priority) MUST
    use this helper to propagate the flag — never inspect provisional_inputs
    directly at call sites.
    """
    return any(a.provisional_inputs for a in assessments)


# ---------------------------------------------------------------------------
# Phase-1 static truth derivation
# ---------------------------------------------------------------------------

def _derive_truth_level_phase1(fact_type: str) -> TruthLevel:
    """Phase-1 static truth derivation (§6.1.1). Replaced by WI-3.0."""
    if fact_type in _MANAGEMENT_FACT_TYPES:
        return TruthLevel.HUMAN_CONFIRMED
    return TruthLevel.RAW_OBSERVED


# ---------------------------------------------------------------------------
# Per-family _to_assessment adapters (§6.1.1)
# ---------------------------------------------------------------------------

def _make_assessment(
    record: Any,
    *,
    fact: ProgramFactRevision | None,
    fact_type: str,
    as_of: datetime,
    truth_ctx: TruthContext | None = None,
    disputed_natural_keys: frozenset[str] | None = None,
    provisional_signal_ids: frozenset[str] | None = None,
) -> FactAssessment:
    fact_id = fact.fact_id if fact is not None else None
    if fact is not None and truth_ctx is not None:
        from src.core.truth_model import derive_truth_level
        truth_level = derive_truth_level(fact, truth_ctx)
    else:
        truth_level = _derive_truth_level_phase1(fact_type)
    stale = is_fact_stale(fact, as_of) if fact is not None else False
    evidence: tuple[str, ...] = (
        tuple(fact.source_signal_ids) + tuple(fact.entity_refs)
        if fact is not None
        else ()
    )
    # Disputed: an unresolved fact.conflict references this fact's natural_key (WI-3.2b).
    disputed = bool(
        fact is not None
        and disputed_natural_keys
        and getattr(fact, "natural_key", None)
        and fact.natural_key in disputed_natural_keys
    )
    # Provisional inputs: any source signal is pending review, not yet accepted (WI-3.2a).
    provisional_inputs = bool(
        fact is not None
        and provisional_signal_ids
        and frozenset(getattr(fact, "source_signal_ids", ()) or ()) & provisional_signal_ids
    )
    # S-3: attach provenance/privacy lineage from the ProgramFactRevision (§5.2/§5.7)
    lineage: FactLineage | None = fact.build_lineage() if fact is not None else None
    return FactAssessment(
        record=record,
        fact_id=fact_id,
        truth_level=truth_level,
        disputed=disputed,
        stale=stale,
        provisional_inputs=provisional_inputs,
        evidence=evidence,
        lineage=lineage,
    )


def _to_assessment_action(item: ActionItem, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(item, fact=fact, fact_type="action.item", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_risk(entry: RiskEntry, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(entry, fact=fact, fact_type="risk.entry", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_decision(entry: DecisionEntry, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(entry, fact=fact, fact_type="decision.entry", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_dependency(dep: Dependency, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(dep, fact=fact, fact_type="dependency.link", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_milestone(m: Milestone, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(m, fact=fact, fact_type="milestone.entry", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_assumption(a: Assumption, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(a, fact=fact, fact_type="assumption.entry", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_workstream(ws: Workstream, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(ws, fact=fact, fact_type="workstream.entry", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_claim(c: ClaimEntry, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(c, fact=fact, fact_type="claim.entry", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _to_assessment_commitment(entry: Any, *, fact: ProgramFactRevision | None, as_of: datetime, truth_ctx: TruthContext | None = None, disputed_natural_keys: frozenset[str] | None = None, provisional_signal_ids: frozenset[str] | None = None) -> FactAssessment:
    return _make_assessment(entry, fact=fact, fact_type="commitment.entry", as_of=as_of, truth_ctx=truth_ctx, disputed_natural_keys=disputed_natural_keys, provisional_signal_ids=provisional_signal_ids)


def _find_fact_for_record(
    fact_lookup: dict[tuple[str, str], ProgramFactRevision],
    *,
    fact_type: str,
    record: Any,
) -> ProgramFactRevision | None:
    record_id = getattr(record, "id", None)
    if isinstance(record_id, str) and record_id:
        matched = fact_lookup.get((fact_type, record_id))
        if matched is not None:
            return matched
    record_fact_id = getattr(record, "fact_id", None)
    if isinstance(record_fact_id, str) and record_fact_id:
        matched = fact_lookup.get((fact_type, record_fact_id))
        if matched is not None:
            return matched
    return None


def _find_assessment_by_fact_id(
    assessments: tuple[FactAssessment, ...],
    fact_id: str,
) -> FactAssessment | None:
    for assessment in assessments:
        if assessment.fact_id == fact_id:
            return assessment
    return None


# ---------------------------------------------------------------------------
# Structural-gap attention rules (§6.1.3)
# ---------------------------------------------------------------------------

_STRUCTURAL_GAP_RULES: dict[str, str] = {
    "critical_risk_no_mitigation": "A critical/high risk has no open mitigation action.",
    "commitment_due_untracked": "A commitment approaching its due date has no associated tracked item.",
    "decision_stuck_proposed": "A decision has been in 'proposed' state for an extended period.",
    "workstream_no_fresh_state": "A workstream has no fresh state update.",
}


def _check_structural_gaps(
    index: dict[str, list[FactAssessment]],
    as_of: datetime,
) -> list[AttentionItem]:
    items: list[AttentionItem] = []

    # critical_risk_no_mitigation: risks with no corresponding open action
    risks_with_no_action: list[FactAssessment] = []
    for assessments in index.values():
        for a in assessments:
            if not isinstance(a.record, RiskEntry):
                continue
            risk: RiskEntry = a.record
            risk_impact = getattr(risk, "impact", None)
            risk_status = getattr(risk, "status", None)
            impact_val = getattr(risk_impact, "value", str(risk_impact or "")).lower()
            status_val = getattr(risk_status, "value", str(risk_status or "")).lower()
            if impact_val in ("high", "critical") and status_val not in ("closed", "mitigated", "accepted"):
                risks_with_no_action.append(a)
    if risks_with_no_action:
        items.append(AttentionItem(
            kind=AttentionKind.STRUCTURAL_GAP,
            priority=2,
            record=risks_with_no_action[0],
            description=f"critical_risk_no_mitigation: {len(risks_with_no_action)} critical/high risk(s) without open mitigation action.",
            action_hint="Add a mitigation action or mark the risk as accepted.",
            provisional_inputs=any_provisional(*risks_with_no_action),
        ))

    # workstream_no_fresh_state: workstreams that are stale
    stale_workstreams = [
        a for assessments in index.values()
        for a in assessments
        if isinstance(a.record, Workstream) and a.stale
    ]
    if stale_workstreams:
        items.append(AttentionItem(
            kind=AttentionKind.STRUCTURAL_GAP,
            priority=3,
            record=stale_workstreams[0],
            description=f"workstream_no_fresh_state: {len(stale_workstreams)} workstream(s) with no fresh state update.",
            action_hint="Run a gather cycle or manually update workstream status.",
            provisional_inputs=any_provisional(*stale_workstreams),
        ))

    # commitment_due_untracked: commitments approaching due date with no tracked item
    from src.core.commitment_store import CommitmentEntry as _CommitmentEntry
    from datetime import timedelta, date as _date
    horizon = (as_of + timedelta(days=14)).date()
    due_untracked: list[FactAssessment] = []
    for assessments in index.values():
        for a in assessments:
            if not isinstance(a.record, _CommitmentEntry):
                continue
            commitment: _CommitmentEntry = a.record
            if commitment.status in ("closed", "cancelled", "delivered"):
                continue
            try:
                due = _date.fromisoformat(commitment.due_date) if isinstance(commitment.due_date, str) else commitment.due_date
            except (ValueError, TypeError):
                continue
            if due <= horizon:
                due_untracked.append(a)
    if due_untracked:
        items.append(AttentionItem(
            kind=AttentionKind.STRUCTURAL_GAP,
            priority=2,
            record=due_untracked[0],
            description=f"commitment_due_untracked: {len(due_untracked)} commitment(s) due within 14 days with no associated tracked item.",
            action_hint="Link a work item or update the commitment status.",
            provisional_inputs=any_provisional(*due_untracked),
        ))

    # decision_stuck_proposed: decisions stuck in 'proposed' for >30 days
    from src.core.models_v2 import DecisionStatus as _DecisionStatus
    stuck_decisions: list[FactAssessment] = []
    stuck_threshold = (as_of - timedelta(days=30)).date()
    for assessments in index.values():
        for a in assessments:
            if not isinstance(a.record, DecisionEntry):
                continue
            decision: DecisionEntry = a.record
            if decision.status != _DecisionStatus.PROPOSED:
                continue
            if decision.decision_date <= stuck_threshold:  # type: ignore[operator]
                stuck_decisions.append(a)
    if stuck_decisions:
        items.append(AttentionItem(
            kind=AttentionKind.STRUCTURAL_GAP,
            priority=2,
            record=stuck_decisions[0],
            description=f"decision_stuck_proposed: {len(stuck_decisions)} decision(s) stuck in 'proposed' state for >30 days.",
            action_hint="Review and advance the decision or mark it as superseded.",
            provisional_inputs=any_provisional(*stuck_decisions),
        ))

    return items


def _check_decision_outcome_drift(
    *,
    decisions: tuple,  # tuple[FactAssessment, ...]
    assumptions: tuple,  # tuple[FactAssessment, ...]
    snapshot: "ProgramFactSnapshot",
    as_of: "datetime",
) -> list["AttentionItem"]:
    """WI-3.11: Detect decision-outcome drift (§6.2.8).

    Fires DECISION_OUTCOME_DRIFT when a decision's linked assumption is:
    - `disputed` (a fact.conflict exists for the assumption's natural_key), OR
    - `stale` (the assumption fact has exceeded its TTL, making premise unverifiable)
    """
    items: list[AttentionItem] = []

    # Build set of disputed natural keys from snapshot
    disputed_keys: set[str] = set()
    for fact in snapshot.facts:
        if fact.fact_type == "fact.conflict" and not fact.payload.get("resolved", False):
            # Conflicts reference their target natural_key in the payload
            target = fact.payload.get("target_natural_key")
            if target:
                disputed_keys.add(str(target))

    # Build map of assumption natural_key → stale state
    assumption_stale: dict[str, bool] = {a.record.natural_key if hasattr(a.record, "natural_key") else "": a.stale for a in assumptions}
    assumption_disputed: dict[str, bool] = {a.record.natural_key if hasattr(a.record, "natural_key") else "": a.disputed for a in assumptions}

    for d in decisions:
        if d.record is None:
            continue
        decision_entry = d.record
        expected_outcome_refs: tuple[str, ...] = tuple(
            getattr(decision_entry, "expected_outcome_refs", ()) or ()
        )
        if not expected_outcome_refs:
            # Check payload for expected_outcome_refs
            payload = getattr(decision_entry, "payload", {}) if hasattr(decision_entry, "payload") else {}
            if isinstance(payload, dict):
                expected_outcome_refs = tuple(payload.get("expected_outcome_refs", []))

        if not expected_outcome_refs:
            continue

        for assumption_key in expected_outcome_refs:
            is_stale = assumption_stale.get(assumption_key, False)
            is_disputed = assumption_disputed.get(assumption_key, False) or assumption_key in disputed_keys
            if is_stale or is_disputed:
                reason = "stale (premise unverifiable)" if is_stale else "disputed"
                title = getattr(decision_entry, "title", str(getattr(decision_entry, "decision_id", "?")))
                items.append(AttentionItem(
                    kind=AttentionKind.DECISION_OUTCOME_DRIFT,
                    priority=2,
                    record=d,
                    description=f"Decision '{title}': linked assumption '{assumption_key}' is {reason}.",
                    action_hint="Revisit the decision — its premise may no longer hold.",
                    provisional_inputs=d.provisional_inputs or is_stale,
                ))

    return items


# ---------------------------------------------------------------------------
# ProgramReality facade
# ---------------------------------------------------------------------------

class ProgramReality:
    """The single read interface for program state (G-1, §6.1).

    All projections (report, risk board, triage, etc.) must read ONLY through
    this interface. It is the authority for cross-source program reality.

    Phase-1: legacy accessors wrap the existing project_* family functions.
    Phase-3: full truth model with TruthContext, conflicts, promotions.
    Phase-5: SoR flip awareness (primary reads from fact-store only).
    """

    def __init__(
        self,
        *,
        program_id: str,
        snapshot: ProgramFactSnapshot,
        sor_mode: str,
        as_of: datetime,
        _entity_fact_index: dict[str, list[FactAssessment]],
        _actions: tuple[FactAssessment, ...],
        _risks: tuple[FactAssessment, ...],
        _decisions: tuple[FactAssessment, ...],
        _dependencies: tuple[FactAssessment, ...],
        _milestones: tuple[FactAssessment, ...],
        _assumptions: tuple[FactAssessment, ...],
        _workstreams: tuple[FactAssessment, ...],
        _claims: tuple[FactAssessment, ...],
        _commitments: tuple[FactAssessment, ...] = (),
        _family_sor_modes: dict[str, str] | None = None,
        _ledger_gaps: tuple[GapRecord, ...] = (),
        _ledger_expiring_locks: tuple[LedgerFieldLockRecord, ...] = (),
        _ledger_shadow_reviews: tuple[LedgerShadowReviewRecord, ...] = (),
        _ledger_stale_operator_assertions: tuple[LedgerStaleOperatorAssertionRecord, ...] = (),
        _ledger_temporal_reviews: tuple[LedgerTemporalConfidenceReviewRecord, ...] = (),
        _archived_knowledge_claim_freshness: KnowledgeClaimFreshnessRecord | None = None,
        _knowledge_claim_freshness: KnowledgeClaimFreshnessRecord | None = None,
        _knowledge_vault_integrity_issues: tuple[dict[str, object], ...] = (),
        _knowledge_vault_hash_mismatch_count: int = 0,
        _ledger_events: tuple[LedgerTimelineEntry, ...] = (),
        _ledger_entity_event_ids: dict[str, tuple[str, ...]] | None = None,
        _ledger_event_log: tuple[EventEnvelope, ...] = (),
        _ledger_triage_decisions: tuple[CandidateDecisionRecord, ...] = (),
        _knowledge_scope_chain: tuple[str, ...] = (),
        _knowledge_claim_revisions: tuple[Any, ...] = (),
    ) -> None:
        self._program_id = program_id
        self._snapshot = snapshot
        self._sor_mode = sor_mode
        self._as_of = as_of
        self._family_sor_modes: dict[str, str] = dict(_family_sor_modes or {})
        self.__entity_fact_index = _entity_fact_index
        self.__actions = _actions
        self.__risks = _risks
        self.__decisions = _decisions
        self.__dependencies = _dependencies
        self.__milestones = _milestones
        self.__assumptions = _assumptions
        self.__workstreams = _workstreams
        self.__claims = _claims
        self.__commitments = _commitments
        self.__ledger_gaps = _ledger_gaps
        self.__ledger_expiring_locks = _ledger_expiring_locks
        self.__ledger_shadow_reviews = _ledger_shadow_reviews
        self.__ledger_stale_operator_assertions = _ledger_stale_operator_assertions
        self.__ledger_temporal_reviews = _ledger_temporal_reviews
        self.__archived_knowledge_claim_freshness = _archived_knowledge_claim_freshness
        self.__knowledge_claim_freshness = _knowledge_claim_freshness
        self.__knowledge_vault_integrity_issues = _knowledge_vault_integrity_issues
        self.__knowledge_vault_hash_mismatch_count = _knowledge_vault_hash_mismatch_count
        self.__ledger_events = _ledger_events
        self.__ledger_entity_event_ids = dict(_ledger_entity_event_ids or {})
        self.__ledger_event_log = _ledger_event_log
        self.__ledger_triage_decisions = _ledger_triage_decisions
        self.__knowledge_scope_chain = _knowledge_scope_chain
        self.__knowledge_claim_revisions = _knowledge_claim_revisions

    @classmethod
    def load(
        cls,
        program_id: str,
        *,
        programs_root: Path = PROGRAMS_ROOT,
        as_of: datetime | None = None,
        edition_name: str | None = None,
        archive_root: Path = ARCHIVE_ROOT,
        domains: tuple[str, ...] | None = None,
        entity_registry: EntityRegistry | None = None,
    ) -> "ProgramReality":
        """Load program reality from disk. The ONLY disk-touching point.

        Pass already-loaded objects down; never reload from disk inside a
        request handler or loop (Q:-drive rule).

        Args:
            program_id: The program identifier.
            programs_root: Root directory for all programs.
            as_of: If provided, loads historical facts up to this timestamp.
            domains: If provided, restricts loaded fact types to these families.
        """
        resolved_as_of = as_of or datetime.now(timezone.utc)

        # Resolve SoR mode (single point, per rule 3)
        from src.core.program_fact_store import resolve_fact_sor_mode
        sor_mode = resolve_fact_sor_mode(
            program_id=program_id,
            programs_root=programs_root,
        )

        # S-5a: load full FactSorState for per-family mode resolution
        from src.core.fact_sor_state import load_fact_sor_state as _load_sor_state
        _sor_state = _load_sor_state(program_id, programs_root=programs_root)
        _family_sor_modes: dict[str, str] = dict(_sor_state.family_modes) if _sor_state else {}

        # Load the fact snapshot (single I/O point) — pass sor_state for S-5a per-family shim filtering
        snapshot = load_program_facts(
            program_id,
            as_of=as_of,
            programs_root=programs_root,
            sor_state=_sor_state,
        )

        # Project domain views from snapshot
        action_items = project_action_items(snapshot)
        risk_entries = project_risk_entries(snapshot)
        decision_entries = project_decision_entries(snapshot)
        dependencies = project_dependencies(snapshot)
        milestones = project_milestones(snapshot)
        assumptions = project_assumptions(snapshot)
        workstreams = project_workstreams(snapshot)
        claim_entries = project_claim_entries(snapshot)
        # S-2a: load commitment entries alongside other families so truth threading and lineage apply
        from src.core.commitment_store import project_commitment_entries as _project_commitment_entries
        commitment_entries = _project_commitment_entries(snapshot)

        # Build FactAssessment tuples for each family
        # Build a fact_id → fact lookup for efficient joining
        fact_by_type_and_ref: dict[tuple[str, str], ProgramFactRevision] = {}
        for fact in snapshot.facts:
            for ref in fact.entity_refs:
                fact_by_type_and_ref[(fact.fact_type, ref)] = fact
                # REV bridge appenders key entity_refs with a family prefix
                # (e.g. "MILESTONE:milestone:abc", "RISK:risk:abc") that the
                # projected record's bare `id` field does not carry (it is
                # just the payload id). Index the unprefixed suffix too so
                # _find_fact_for_record can join bridge-appended facts to
                # their records and attach fact_id/lineage (S-3). setdefault
                # avoids clobbering an existing exact-match entry.
                if ":" in ref:
                    fact_by_type_and_ref.setdefault((fact.fact_type, ref.split(":", 1)[1]), fact)

        # Build the trust context once for the whole facade load (WI-3.0 / GAP-5).
        # Lazy import avoids a module-level circular dependency; the try/except
        # gracefully degrades to the Phase-1 static stub if the truth model is
        # unavailable (e.g. empty snapshot in unit tests).
        _truth_ctx: TruthContext | None = None
        try:
            from src.core.truth_model import build_trust_context_from_snapshot
            _truth_ctx = build_trust_context_from_snapshot(snapshot)
        except Exception:
            pass

        # Build disputed-key and provisional-signal indexes once for the whole load (GAP-5).
        # disputed_natural_keys: natural keys referenced by unresolved fact.conflict facts.
        # provisional_signal_ids: fact_ids of signal.observation facts pending review.
        _disputed_natural_keys: frozenset[str] = frozenset(
            nk
            for fact in snapshot.facts
            if fact.fact_type == "fact.conflict" and not fact.payload.get("resolved", False)
            if (nk := fact.payload.get("target_natural_key"))
        )
        _provisional_signal_ids: frozenset[str] = frozenset(
            fact.fact_id
            for fact in snapshot.facts
            if fact.fact_type == "signal.observation"
            and str(getattr(fact, "review_state", "") or "") not in ("accepted", "rejected")
            and fact.fact_id
        )

        actions = tuple(
            _to_assessment_action(
                item,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="action.item", record=item),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for item in action_items
        )
        risks = tuple(
            _to_assessment_risk(
                entry,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="risk.entry", record=entry),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for entry in risk_entries
        )
        decisions = tuple(
            _to_assessment_decision(
                entry,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="decision.entry", record=entry),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for entry in decision_entries
        )
        deps = tuple(
            _to_assessment_dependency(
                dep,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="dependency.link", record=dep),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for dep in dependencies
        )
        mstones = tuple(
            _to_assessment_milestone(
                m,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="milestone.entry", record=m),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for m in milestones
        )
        assum = tuple(
            _to_assessment_assumption(
                a,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="assumption.entry", record=a),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for a in assumptions
        )
        wstreams = tuple(
            _to_assessment_workstream(
                ws,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="workstream.entry", record=ws),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for ws in workstreams
        )
        claims = tuple(
            _to_assessment_claim(
                c,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="claim.entry", record=c),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for c in claim_entries
        )
        # S-2a: wire commitment truth threading (review_state → TruthLevel, lineage attached)
        commitments_assessed = tuple(
            _to_assessment_commitment(
                entry,
                fact=_find_fact_for_record(fact_by_type_and_ref, fact_type="commitment.entry", record=entry),
                as_of=resolved_as_of,
                truth_ctx=_truth_ctx,
                disputed_natural_keys=_disputed_natural_keys,
                provisional_signal_ids=_provisional_signal_ids,
            )
            for entry in commitment_entries
        )

        # Build _entity_fact_index (WI-2.5: canonical entity_id keying)
        # Join domain objects to their snapshot facts via entity_refs, then
        # optionally resolve to canonical IDs through the EntityRegistry.
        all_assessments: list[FactAssessment] = [
            *actions, *risks, *decisions, *deps, *mstones, *assum, *wstreams, *claims
        ]

        # AG-6 (activation.md v1.26/v1.28): resolve approval_event_id for
        # REV-bridge-appended facts. The fact bridge fires synchronously while
        # persisting the *resulting* domain event (e.g. milestone.completed.v1),
        # before the separate discovery.candidate_approved.v1 audit event exists
        # — so build_bridge_fact_input can never know the approval event id at
        # write time. Close the loop here, at read time: every triage decision
        # already links resulting_event_id -> approval_event_id
        # (CandidateDecisionRecord, written by `ledger triage approve`), and
        # every bridge-appended fact carries lineage.domain_event_id == the
        # resulting event's id. Join the two so the reverse-lookup citation can
        # resolve "source EML + approval event", not just the source EML.
        # Covers commitments_assessed too — it is assembled separately (S-2a)
        # and is not part of all_assessments.
        if any(
            a.lineage is not None and a.lineage.approval_event_id is None and a.lineage.domain_event_id
            for a in (*all_assessments, *commitments_assessed)
        ):
            try:
                _decisions_for_lineage = load_triage_decisions(program_id, programs_root=programs_root)
                _resulting_to_approval: dict[str, str] = {
                    d.resulting_event_id: d.approval_event_id
                    for d in _decisions_for_lineage
                    if d.resulting_event_id and d.approval_event_id
                }
                if _resulting_to_approval:
                    def _patch_approval_event_id(_a: FactAssessment) -> FactAssessment:
                        if (
                            _a.lineage is not None
                            and _a.lineage.approval_event_id is None
                            and _a.lineage.domain_event_id
                            and _a.lineage.domain_event_id in _resulting_to_approval
                        ):
                            return replace(
                                _a,
                                lineage=replace(
                                    _a.lineage,
                                    approval_event_id=_resulting_to_approval[_a.lineage.domain_event_id],
                                ),
                            )
                        return _a

                    for _idx, _a in enumerate(all_assessments):
                        all_assessments[_idx] = _patch_approval_event_id(_a)
                    commitments_assessed = tuple(_patch_approval_event_id(_a) for _a in commitments_assessed)
            except Exception:
                # Never let a lineage-enrichment failure break ProgramReality.load()
                # (AG-12 graceful-degradation parity) — worst case, approval_event_id
                # stays unresolved and the citation still reverse-resolves via
                # source_document_key alone.
                pass
                # stays unresolved and the citation still reverse-resolves via
                # source_document_key alone.
                pass

        # Index assessments by their record's ID so snapshot facts can join to them
        _assessment_by_record_id: dict[str, FactAssessment] = {}
        for _a in all_assessments:
            _rec_id = getattr(_a.record, "id", None)
            if _rec_id:
                _assessment_by_record_id[str(_rec_id)] = _a
        entity_fact_index: dict[str, list[FactAssessment]] = {}
        for _fact in snapshot.facts:
            if not _fact.fact_id:
                continue
            for _ref in _fact.entity_refs:
                _assessment = _assessment_by_record_id.get(_ref)
                if _assessment is None:
                    continue
                # WI-2.5: resolve entity_ref to canonical ID when registry available
                _key: str | None = None
                if entity_registry is not None:
                    _resolved = entity_registry.resolve(_ref)
                    if _resolved is not None:
                        _key = _resolved.entity_id
                # Fallback: key by fact_id
                if _key is None:
                    _key = _fact.fact_id
                entity_fact_index.setdefault(_key, []).append(_assessment)

        ledger_triage_decisions = load_triage_decisions(program_id, programs_root=programs_root)
        ledger_event_log = read_events(program_id, programs_root=programs_root)
        knowledge_scope_chain = load_program_knowledge_scopes(program_id, programs_root=programs_root)
        knowledge_claim_revisions = load_program_knowledge_claims(program_id, programs_root=programs_root)
        knowledge_status = summarize_knowledge_status(knowledge_root=programs_root / "knowledge")
        knowledge_vault_integrity = summarize_knowledge_vault_integrity(programs_root=programs_root)
        ledger_projection = project_events_to_memory(
            program_id,
            ledger_event_log,
            triage_decisions=ledger_triage_decisions,
        )
        ledger_orphaned_by = collapse_orphan_links(ledger_projection.get("event_orphan_links", []))
        ledger_shadowed_by = collapse_shadow_links(ledger_projection.get("event_shadow_links", []))

        return cls(
            program_id=program_id,
            snapshot=snapshot,
            sor_mode=sor_mode,
            as_of=resolved_as_of,
            _entity_fact_index=entity_fact_index,
            _actions=actions,
            _risks=risks,
            _decisions=decisions,
            _dependencies=deps,
            _milestones=mstones,
            _assumptions=assum,
            _workstreams=wstreams,
            _claims=claims,
            _commitments=commitments_assessed,
            _family_sor_modes=_family_sor_modes,
            _ledger_gaps=_load_ledger_gaps(ledger_event_log, ledger_triage_decisions),
            _ledger_expiring_locks=_load_expiring_ledger_field_locks(ledger_projection.get("field_locks", []), as_of=resolved_as_of),
            _ledger_shadow_reviews=_load_ledger_shadow_reviews(ledger_projection.get("event_shadow_links", []), ledger_event_log),
            _ledger_stale_operator_assertions=_load_stale_operator_assertion_reviews(
                ledger_projection.get("event_shadow_links", []),
                ledger_event_log,
                as_of=resolved_as_of,
            ),
            _ledger_temporal_reviews=_load_temporal_confidence_reviews(
                ledger_projection.get("event_shadow_links", []),
                ledger_event_log,
            ),
            _archived_knowledge_claim_freshness=_load_latest_archived_claim_freshness(
                edition_name,
                archive_root=archive_root,
            ),
            _knowledge_claim_freshness=_load_latest_live_claim_freshness(program_id, programs_root=programs_root),
            _knowledge_vault_integrity_issues=tuple(knowledge_vault_integrity.issue_records()),
            _knowledge_vault_hash_mismatch_count=knowledge_status.vault.hash_mismatch_count,
            _ledger_events=_load_ledger_timeline_entries(
                ledger_event_log,
                program_id=program_id,
                programs_root=programs_root,
                orphaned_by=ledger_orphaned_by,
                shadowed_by=ledger_shadowed_by,
            ),
            _ledger_entity_event_ids=load_entity_event_ids(program_id, programs_root=programs_root),
            _ledger_event_log=ledger_event_log,
            _ledger_triage_decisions=ledger_triage_decisions,
            _knowledge_scope_chain=knowledge_scope_chain,
            _knowledge_claim_revisions=knowledge_claim_revisions,
        )

    # -------------------------------------------------------------------------
    # Domain accessors
    # -------------------------------------------------------------------------

    def actions(self) -> tuple[FactAssessment, ...]:
        return self.__actions

    def risks(self) -> tuple[FactAssessment, ...]:
        return self.__risks

    def decisions(self) -> tuple[FactAssessment, ...]:
        return self.__decisions

    def dependencies(self) -> tuple[FactAssessment, ...]:
        return self.__dependencies

    def milestones(self) -> tuple[FactAssessment, ...]:
        return self.__milestones

    def assumptions(self) -> tuple[FactAssessment, ...]:
        return self.__assumptions

    def workstreams(self) -> tuple[FactAssessment, ...]:
        return self.__workstreams

    def claims(self) -> tuple[FactAssessment, ...]:
        return self.__claims

    def ledger_gaps(self, *, unacknowledged_only: bool = True) -> tuple[GapRecord, ...]:
        if not unacknowledged_only:
            return self.__ledger_gaps
        return tuple(gap for gap in self.__ledger_gaps if not gap.acknowledged)

    def ledger_timeline(self, entity_id: str, *, as_of: datetime | None = None) -> tuple[LedgerTimelineEntry, ...]:
        event_ids = self.__ledger_entity_event_ids.get(entity_id, ())
        events_by_id = {entry.event_id: entry for entry in self.__ledger_events}
        timeline = [events_by_id[event_id] for event_id in event_ids if event_id in events_by_id]
        if as_of is not None:
            timeline = [entry for entry in timeline if entry.occurred_at <= as_of]
        timeline.sort(key=lambda entry: (entry.occurred_at, entry.event_id))
        return tuple(timeline)

    def ledger_as_of(self, as_of: datetime, *, knowledge_as_of: datetime | None = None) -> ProgramProjection:
        projection_dump = project_events_to_memory(
            self._program_id,
            self.__ledger_event_log,
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
            triage_decisions=self.__ledger_triage_decisions,
        )
        return ProgramProjection(
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
            tables={name: tuple(rows) for name, rows in projection_dump.items()},
        )

    def knowledge_context(
        self,
        entity_ids: tuple[str, ...] | list[str],
        *,
        as_of: datetime | None = None,
        knowledge_as_of: datetime | None = None,
    ) -> KnowledgeContext:
        requested_entity_ids = tuple(dict.fromkeys(str(entity_id) for entity_id in entity_ids if str(entity_id)))
        if not requested_entity_ids:
            return resolve_knowledge_context(
                (),
                scope_chain=self.__knowledge_scope_chain,
                revisions=self.__knowledge_claim_revisions,
                projection_coverage={},
                as_of=as_of,
                knowledge_as_of=knowledge_as_of,
            )

        projection = self.ledger_as_of(as_of or self._as_of, knowledge_as_of=knowledge_as_of)
        projection_coverage = _projection_coverage_for_entities(requested_entity_ids, projection)
        return resolve_knowledge_context(
            requested_entity_ids,
            scope_chain=self.__knowledge_scope_chain,
            revisions=self.__knowledge_claim_revisions,
            projection_coverage=projection_coverage,
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
        )

    def commitments(self) -> tuple[FactAssessment, ...]:
        """Return commitment entries from the fact store.

        S-2a: truth level is derived from review_state (same pattern as milestones/risks);
        lineage is attached from the underlying ProgramFactRevision.
        """
        return self.__commitments

    def metric_observations(self) -> tuple[FactAssessment, ...]:
        """Phase-3 placeholder: metric observations from reality_store arrive in WI-3.x."""
        return ()

    def hypotheses(self) -> tuple[FactAssessment, ...]:
        """Phase-3 placeholder: hypotheses from reality_store arrive in WI-3.x."""
        return ()

    def approved_signals(self) -> tuple[FactAssessment, ...]:
        """WI-3.2a: Return facts from approved (non-provisional) signals.

        Returns FactAssessments for signal.observation facts with
        review_state=ACCEPTED (promoted via signal_promotion.py).
        """
        from src.core.truth_model import build_trust_context_from_snapshot, derive_truth_level
        truth_ctx = build_trust_context_from_snapshot(self._snapshot)
        results: list[FactAssessment] = []
        for fact in self._snapshot.facts:
            if fact.fact_type != "signal.observation":
                continue
            if str(fact.review_state) != "accepted":
                continue
            tl = derive_truth_level(fact, truth_ctx)
            results.append(FactAssessment(
                record=fact,
                fact_id=fact.fact_id,
                truth_level=tl,
                disputed=False,
                stale=is_fact_stale(fact, self._as_of, snapshot=self._snapshot),
                provisional_inputs=False,
                evidence=tuple(fact.source_signal_ids) + tuple(fact.entity_refs),
            ))
        return tuple(results)

    def observation_facts(
        self,
        fact_types: tuple[str, ...] | None = None,
        include_stale: bool = False,
        min_truth_level: TruthLevel = TruthLevel.RAW_OBSERVED,
    ) -> tuple[FactAssessment, ...]:
        """Phase-1: all non-management facts from snapshot at RAW_OBSERVED level."""
        results: list[FactAssessment] = []
        for fact in self._snapshot.facts:
            if fact.fact_type in _MANAGEMENT_FACT_TYPES:
                continue
            if fact_types is not None and fact.fact_type not in fact_types:
                continue
            a = FactAssessment(
                record=fact,
                fact_id=fact.fact_id,
                truth_level=TruthLevel.RAW_OBSERVED,
                disputed=False,
                stale=is_fact_stale(fact, self._as_of),
                provisional_inputs=False,
                evidence=tuple(fact.source_signal_ids) + tuple(fact.entity_refs),
            )
            if not include_stale and a.stale:
                continue
            # min_truth_level filter: Phase 1 everything is RAW_OBSERVED,
            # so only RAW_OBSERVED passes unless specifically overridden.
            if TruthLevel.RAW_OBSERVED != min_truth_level:
                continue
            results.append(a)
        return tuple(results)

    # -------------------------------------------------------------------------
    # Reality API
    # -------------------------------------------------------------------------

    def evidence_for(self, fact_id: str) -> tuple[EvidenceRef, ...]:
        """Return evidence refs for a given fact_id."""
        for fact in self._snapshot.facts:
            if fact.fact_id == fact_id:
                refs: list[EvidenceRef] = []
                for sid in fact.source_signal_ids:
                    refs.append(EvidenceRef(signal_id=sid, entity_ref=None, source=None))
                for eref in fact.entity_refs:
                    refs.append(EvidenceRef(signal_id=None, entity_ref=eref, source=None))
                return tuple(refs)
        return ()

    def explain(self, fact_id: str) -> FactExplanation | None:
        """Return an operator-facing explanation bundle for one fact."""
        fact = next((entry for entry in self._snapshot.facts if entry.fact_id == fact_id), None)
        if fact is None:
            return None
        assessment = _find_assessment_by_fact_id(self._all_assessments(), fact_id)
        if assessment is None:
            return None
        open_conflicts = tuple(
            conflict
            for conflict in self.conflicts(open_only=True)
            if fact.natural_key == conflict.conflict_id
            or any(entity_ref in fact.entity_refs for entity_ref in conflict.entity_refs)
        )
        return FactExplanation(
            program_id=self._program_id,
            fact_id=fact.fact_id,
            fact_type=fact.fact_type,
            natural_key=fact.natural_key,
            truth_level=assessment.truth_level,
            disputed=assessment.disputed,
            stale=assessment.stale,
            provisional_inputs=assessment.provisional_inputs,
            evidence=self.evidence_for(fact_id),
            open_conflicts=open_conflicts,
            source_signal_ids=tuple(fact.source_signal_ids),
            entity_refs=tuple(fact.entity_refs),
        )

    def conflicts(self, *, open_only: bool = True) -> tuple[RealityConflict, ...]:
        """WI-3.2b: Return conflicts from fact.conflict facts in the snapshot.

        A fact.conflict fact records a discovered disagreement between two
        data sources. open_only=True (default) returns only unresolved conflicts.
        """
        results: list[RealityConflict] = []
        for fact in self._snapshot.facts:
            if fact.fact_type != "fact.conflict":
                continue
            payload = fact.payload
            is_resolved = bool(payload.get("resolved", False))
            if open_only and is_resolved:
                continue
            results.append(RealityConflict(
                conflict_id=fact.fact_id,
                entity_refs=fact.entity_refs,
                family=str(payload.get("family", "unknown")),
                open=not is_resolved,
                description=str(payload.get("description", "")),
            ))
        return tuple(results)

    def stale_facts(self) -> tuple[FactAssessment, ...]:
        all_families = (*self.__actions, *self.__risks, *self.__decisions,
                        *self.__dependencies, *self.__milestones, *self.__assumptions,
                        *self.__workstreams, *self.__claims)
        return tuple(a for a in all_families if a.stale)

    def attention(self, *, owner: str | None = None) -> tuple[AttentionItem, ...]:
        """Compute attention items using the entity_fact_index (§6.1.3).

        Phase-1 fires: UNANSWERED_DECISION (from stale open decisions),
        STALE_HIGH_SEVERITY (from stale high/critical risks),
        STRUCTURAL_GAP (from structural gap rules).
        """
        items: list[AttentionItem] = []

        # UNANSWERED_DECISION: open decisions that are stale
        for a in self.__decisions:
            entry: DecisionEntry = a.record
            status = getattr(entry, "status", None)
            if str(status or "").lower() in ("proposed", "open") and a.stale:
                items.append(AttentionItem(
                    kind=AttentionKind.UNANSWERED_DECISION,
                    priority=2,
                    record=a,
                    description=f"Decision '{getattr(entry, 'title', '?')}' is stale and unanswered.",
                    action_hint="Review and resolve or defer this decision.",
                    provisional_inputs=a.provisional_inputs,
                ))

        # STALE_HIGH_SEVERITY: stale high/critical risks
        for a in self.__risks:
            risk: RiskEntry = a.record
            severity = str(getattr(risk, "risk_impact", None) or getattr(risk, "impact", None) or "").lower()
            if severity in ("high", "critical") and a.stale:
                items.append(AttentionItem(
                    kind=AttentionKind.STALE_HIGH_SEVERITY,
                    priority=1,
                    record=a,
                    description=f"High/critical risk '{getattr(risk, 'title', '?')}' has not been updated recently.",
                    action_hint="Update the risk status or mitigation plan.",
                    provisional_inputs=a.provisional_inputs,
                ))

        # COMMITMENT_SLIPPED: commitments with slip history (WI-2.7)
        for a in self.commitments():
            from src.core.commitment_store import CommitmentEntry as _CommitmentEntry
            commitment: _CommitmentEntry = a.record
            if commitment.is_slipped and commitment.status not in ("closed", "cancelled", "delivered"):
                items.append(AttentionItem(
                    kind=AttentionKind.COMMITMENT_SLIPPED,
                    priority=2,
                    record=a,
                    description=f"Commitment '{commitment.title}' has slipped {commitment.slip_count} time(s). Current due: {commitment.due_date}.",
                    action_hint="Review commitment status, escalate if inbound, negotiate if outbound.",
                    provisional_inputs=a.provisional_inputs,
                ))

        # STRUCTURAL_GAP rules (§6.1.3 — uses _entity_fact_index)
        items.extend(_check_structural_gaps(self.__entity_fact_index, self._as_of))

        # Ledger gaps: current unacknowledged pipeline gaps should stay visible in the same facade.
        for gap in self.ledger_gaps(unacknowledged_only=True):
            items.append(AttentionItem(
                kind=AttentionKind.STRUCTURAL_GAP,
                priority=2,
                record=None,
                description=f"Pipeline '{gap.pipeline}' gap '{gap.gap_kind}': {gap.detail}",
                action_hint="Review ledger gaps and acknowledge or remediate the source issue.",
                provisional_inputs=False,
            ))

        if self.__ledger_expiring_locks:
            first_lock = self.__ledger_expiring_locks[0]
            items.append(AttentionItem(
                kind=AttentionKind.OVERRIDE_RECERTIFICATION_DUE,
                priority=2,
                record=None,
                description=(
                    f"{len(self.__ledger_expiring_locks)} assertion lock(s) expire within 7 days; "
                    f"first expiry is {first_lock.entity_id}.{first_lock.field} at {first_lock.valid_until.isoformat()}."
                ),
                action_hint="Re-confirm the expiring lock or release it before expiry.",
                provisional_inputs=False,
            ))

        if self.__ledger_shadow_reviews:
            first_review = self.__ledger_shadow_reviews[0]
            items.append(AttentionItem(
                kind=AttentionKind.LEDGER_CONFLICT_REVIEW,
                priority=2,
                record=None,
                description=(
                    f"{len(self.__ledger_shadow_reviews)} ledger field resolution(s) relied on write-order tiebreaks; "
                    f"first review is field '{first_review.field_name}' between {first_review.event_id} and {first_review.shadowed_by}."
                ),
                action_hint="Review the tied ledger evidence and confirm the winning value.",
                provisional_inputs=False,
            ))

        if self.__ledger_stale_operator_assertions:
            first_assertion = self.__ledger_stale_operator_assertions[0]
            items.append(AttentionItem(
                kind=AttentionKind.OPERATOR_ASSERTION_STALE,
                priority=2,
                record=None,
                description=(
                    f"{len(self.__ledger_stale_operator_assertions)} operator assertion(s) older than 30 days still win over "
                    f"source-authoritative evidence; first review is field '{first_assertion.field_name}' between "
                    f"{first_assertion.event_id} and {first_assertion.shadowed_by}."
                ),
                action_hint="Confirm the operator assertion or let it expire.",
                provisional_inputs=False,
            ))

        if self.__ledger_temporal_reviews:
            first_temporal_review = self.__ledger_temporal_reviews[0]
            items.append(AttentionItem(
                kind=AttentionKind.TEMPORAL_CONFIDENCE_REVIEW,
                priority=2,
                record=None,
                description=(
                    f"{len(self.__ledger_temporal_reviews)} ledger field resolution(s) rely on newer but weaker temporal confidence; "
                    f"first review is field '{first_temporal_review.field_name}' where {first_temporal_review.shadowed_by} "
                    f"({first_temporal_review.winner_temporal_confidence}) beat {first_temporal_review.event_id} ({first_temporal_review.loser_temporal_confidence})."
                ),
                action_hint="Review the date certainty and confirm the winning value ordering.",
                provisional_inputs=False,
            ))

        for claim_freshness in filter(None, (self.__archived_knowledge_claim_freshness, self.__knowledge_claim_freshness)):
            preview = ", ".join(claim_freshness.claim_ids[:3])
            suffix = "" if len(claim_freshness.claim_ids) <= 3 else f" (+{len(claim_freshness.claim_ids) - 3} more)"
            if claim_freshness.evidence_source == "archive_accepted_proposals":
                description = (
                    f"Latest confirmed issue {claim_freshness.issue_number:03d} still cites "
                    f"{len(claim_freshness.claim_ids)} expired/stale claim(s): {preview}{suffix}."
                )
                action_hint = "Refresh or remove stale claim citations from the archived accepted proposal evidence."
            else:
                description = (
                    f"Latest live proposal-backed issue {claim_freshness.issue_number:03d} still cites "
                    f"{len(claim_freshness.claim_ids)} expired/stale claim(s): {preview}{suffix}."
                )
                action_hint = "Refresh or remove stale claim citations from the persisted proposal evidence."
            items.append(AttentionItem(
                kind=AttentionKind.CLAIM_FRESHNESS,
                priority=2,
                record=None,
                description=description,
                action_hint=action_hint,
                provisional_inputs=False,
            ))

        integrity_issues = self.__knowledge_vault_integrity_issues
        if not integrity_issues and self.__knowledge_vault_hash_mismatch_count > 0:
            integrity_issues = ({"kind": "hash_mismatch", "count": self.__knowledge_vault_hash_mismatch_count},)
        if integrity_issues:
            detail_parts: list[str] = []
            for issue in integrity_issues:
                kind = issue.get("kind")
                count = issue.get("count")
                if not isinstance(kind, str) or not isinstance(count, int) or count <= 0:
                    continue
                if kind == "hash_mismatch":
                    detail_parts.append(f"{count} file(s) with content hash mismatches")
                elif kind == "missing_metadata":
                    detail_parts.append(f"{count} file(s) missing metadata")
                elif kind == "missing_source_record":
                    detail_parts.append(f"{count} dangling source registry record(s)")
                elif kind == "missing_claim_ref":
                    detail_parts.append(f"{count} claim reference(s) to missing vault entries")
                elif kind == "missing_candidate_ref":
                    detail_parts.append(f"{count} active candidate reference(s) to missing vault entries")
            detail = "; ".join(detail_parts) if detail_parts else "shared knowledge vault integrity issues"
            items.append(AttentionItem(
                kind=AttentionKind.KNOWLEDGE_VAULT_INTEGRITY,
                priority=1,
                record=None,
                description=f"Shared knowledge vault has integrity issues: {detail}.",
                action_hint="Repair or re-ingest the affected knowledge-vault entries before relying on knowledge claims.",
                provisional_inputs=False,
            ))

        orphaned_events = tuple(entry for entry in self.__ledger_events if entry.orphaned_by is not None)
        if orphaned_events:
            first_orphan = orphaned_events[0]
            items.append(AttentionItem(
                kind=AttentionKind.STRUCTURAL_GAP,
                priority=2,
                record=None,
                description=(
                    f"Ledger has {len(orphaned_events)} orphaned event(s) after tombstone correction(s); "
                    f"first orphaned event '{first_orphan.event_type}' is orphaned by {first_orphan.orphaned_by}."
                ),
                action_hint="Review orphaned ledger events and repoint or supersede them if the entity should remain active.",
                provisional_inputs=False,
            ))

        # DECISION_OUTCOME_DRIFT: WI-3.11 — linked assumption is disputed or stale
        items.extend(_check_decision_outcome_drift(
            decisions=self.__decisions,
            assumptions=self.__assumptions,
            snapshot=self._snapshot,
            as_of=self._as_of,
        ))

        # Owner filter
        if owner is not None:
            items = [i for i in items if _attention_matches_owner(i, owner)]

        return tuple(sorted(items, key=lambda i: i.priority))

    def pending_actuations(self) -> tuple[ActuationProposal, ...]:
        """Return pending (unapproved, non-executed, non-expired) actuation proposals.

        Proposals are stored as action.proposal facts during gather/triage by
        actuation_engine.derive_proposals(). This method reads them from the
        loaded snapshot (no disk I/O — snapshot is loaded at load() time).

        Expired proposals (past approval_ttl_hours) and proposals that have
        already been executed or terminally failed are excluded.
        """
        now = datetime.now(timezone.utc)

        # Collect executed and terminal-failed proposal_ids
        executed_ids: set[str] = set()
        terminal_failed_ids: set[str] = set()
        for fact in self._snapshot.facts:
            if fact.fact_type == "action.executed":
                pid = fact.payload.get("proposal_id", "")
                if pid:
                    executed_ids.add(str(pid))
            elif fact.fact_type == "action.failed":
                if fact.payload.get("terminal", False):
                    pid = fact.payload.get("proposal_id", "")
                    if pid:
                        terminal_failed_ids.add(str(pid))

        results: list[ActuationProposal] = []
        for fact in self._snapshot.facts:
            if fact.fact_type != "action.proposal":
                continue
            payload = fact.payload
            proposal_id = str(payload.get("proposal_id", fact.fact_id or ""))

            # Skip executed or terminally failed
            if proposal_id in executed_ids or proposal_id in terminal_failed_ids:
                continue

            # Check TTL expiry
            proposed_at_str = payload.get("proposed_at", "")
            proposed_at: datetime | None = None
            if proposed_at_str:
                try:
                    proposed_at = datetime.fromisoformat(str(proposed_at_str))
                    if proposed_at.tzinfo is None:
                        proposed_at = proposed_at.replace(tzinfo=timezone.utc)
                    ttl_hours = int(payload.get("approval_ttl_hours", 24))
                    if now > proposed_at + timedelta(hours=ttl_hours):
                        continue  # expired
                except (ValueError, TypeError):
                    pass

            entity_refs = fact.entity_refs
            entity_ref = entity_refs[0] if entity_refs else ""
            results.append(ActuationProposal(
                proposal_id=proposal_id,
                rule_id=str(payload.get("rule_id", "")),
                adapter=str(payload.get("adapter", "")),
                operation=str(payload.get("operation", "")),
                entity_ref=entity_ref,
                payload=dict(payload),
                proposed_at=proposed_at or now,
                approved=bool(payload.get("approved", False)),
                gap_reason=str(payload.get("gap_reason", "")),
            ))

        return tuple(results)

    def diff(self, other: "ProgramReality") -> RealityDelta:
        """Compute the delta between two ProgramReality snapshots.

        In legacy/mixed SoR mode, all families are listed in
        ``non_replayable_families`` (§6.1.2 rule 9). Phase 5 makes
        fact-store families replayable.
        """
        is_legacy = self._sor_mode != "primary" or other._sor_mode != "primary"
        if is_legacy:
            return RealityDelta(
                added=(),
                changed=(),
                retired=(),
                dispute_opened=(),
                dispute_resolved=(),
                non_replayable_families=tuple(sorted(_MANAGEMENT_FACT_TYPES)),
            )
        # Primary mode: compute actual delta by natural_key
        self_by_key = {a.fact_id: a for a in self._all_assessments() if a.fact_id}
        other_by_key = {a.fact_id: a for a in other._all_assessments() if a.fact_id}
        added = tuple(a for k, a in other_by_key.items() if k not in self_by_key)
        retired = tuple(a for k, a in self_by_key.items() if k not in other_by_key)
        changed: list[tuple[FactAssessment, FactAssessment]] = []
        for k in set(self_by_key) & set(other_by_key):
            before = self_by_key[k]
            after = other_by_key[k]
            if before != after:
                changed.append((before, after))
        return RealityDelta(
            added=added,
            changed=tuple(changed),
            retired=retired,
            dispute_opened=(),
            dispute_resolved=(),
            non_replayable_families=(),
        )

    # -------------------------------------------------------------------------
    # Platform contracts
    # -------------------------------------------------------------------------

    def to_dict(self, *, max_classification: str = "internal") -> dict[str, Any]:
        """Serialize to a versioned envelope dict (§6.12.1).

        Envelope carries ``reality_schema_version: "1"``. Major bumps ONLY
        on breaking change; additive fields never bump.
        Facts with classification > max_classification are omitted (WI-3.7).
        """
        from src.core.privacy_filter import load_privacy_policy, is_fact_visible
        policy = load_privacy_policy()

        def _filter_domain(assessments: tuple) -> list[dict]:
            return [
                _assessment_to_dict(a)
                for a in assessments
                if is_fact_visible(
                    a.fact_id and next(
                        (f.fact_type for f in self._snapshot.facts if f.fact_id == a.fact_id),
                        "action.item",
                    ) or "action.item",
                    max_classification=max_classification,
                    policy=policy,
                )
            ]

        return {
            "reality_schema_version": _REALITY_SCHEMA_VERSION,
            "program_id": self._program_id,
            "as_of": self._as_of.isoformat(),
            "sor_mode": self._sor_mode,
            "max_classification": max_classification,
            "domains": {
                "actions": [_assessment_to_dict(a) for a in self.__actions],
                "risks": [_assessment_to_dict(a) for a in self.__risks],
                "decisions": [_assessment_to_dict(a) for a in self.__decisions],
                "dependencies": [_assessment_to_dict(a) for a in self.__dependencies],
                "milestones": [_assessment_to_dict(a) for a in self.__milestones],
                "assumptions": [_assessment_to_dict(a) for a in self.__assumptions],
                "workstreams": [_assessment_to_dict(a) for a in self.__workstreams],
                "claims": [_assessment_to_dict(a) for a in self.__claims],
            },
        }

    def events_since(self, cursor: str | None) -> tuple[tuple[FactStoreEvent, ...], str]:
        """Return events from the fact store since the given cursor.

        Returns (events, new_cursor). Covers the fact-store event table;
        reality_store hypothesis mutations are NOT in the feed (documented
        limitation per §6.1.2 rule 8).
        """
        all_facts = self._snapshot.facts
        if cursor is not None:
            found_cursor = False
            events: list[FactStoreEvent] = []
            for fact in all_facts:
                if found_cursor:
                    events.append(FactStoreEvent(
                        fact_type=fact.fact_type,
                        natural_key=fact.natural_key,
                        fact_id=fact.fact_id,
                        payload=fact.payload,
                    ))
                if fact.revision_id == cursor:
                    found_cursor = True
        else:
            events = [
                FactStoreEvent(
                    fact_type=fact.fact_type,
                    natural_key=fact.natural_key,
                    fact_id=fact.fact_id,
                    payload=fact.payload,
                )
                for fact in all_facts
            ]
        new_cursor = all_facts[-1].revision_id if all_facts else cursor or ""
        return tuple(events), new_cursor

    # -------------------------------------------------------------------------
    # Meta
    # -------------------------------------------------------------------------

    def freshness(self) -> tuple[RealityDomainFreshness, ...]:
        """Return per-domain freshness summary."""
        result: list[RealityDomainFreshness] = []
        domain_map: dict[str, tuple[FactAssessment, ...]] = {
            "actions": self.__actions,
            "risks": self.__risks,
            "decisions": self.__decisions,
            "dependencies": self.__dependencies,
            "milestones": self.__milestones,
            "assumptions": self.__assumptions,
            "workstreams": self.__workstreams,
            "claims": self.__claims,
        }
        for domain, assessments in domain_map.items():
            stale_count = sum(1 for a in assessments if a.stale)
            latest: datetime | None = None
            # For legacy mode, we can't easily get recorded_at from domain objects;
            # this is improved in Phase 3 when we have fact-store reads.
            result.append(RealityDomainFreshness(
                domain=domain,
                fact_count=len(assessments),
                stale_count=stale_count,
                latest_recorded_at=latest,
                sor_mode=self._sor_mode,
            ))
        return tuple(result)

    def entity(self, ref: str) -> CanonicalEntity | None:
        """Entity lookup via EntityRegistry (WI-2.0).

        Resolves exact and casefold matches. WI-2.1 adds fuzzy tier.
        Returns None when below resolution threshold.
        """
        from src.core.entity_registry import EntityRegistry
        registry = EntityRegistry.load(
            self._program_id,
            programs_root=PROGRAMS_ROOT,
        )
        return registry.resolve(ref)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _all_assessments(self) -> tuple[FactAssessment, ...]:
        return (
            *self.__actions, *self.__risks, *self.__decisions,
            *self.__dependencies, *self.__milestones, *self.__assumptions,
            *self.__workstreams, *self.__claims,
        )

    @property
    def _entity_fact_index(self) -> dict[str, list[FactAssessment]]:
        return self.__entity_fact_index

    @property
    def program_id(self) -> str:
        return self._program_id

    @property
    def sor_mode(self) -> str:
        return self._sor_mode

    def family_sor_mode(self, family: str) -> str:
        """Return the resolved SoR mode for a specific authority family (S-5a).

        Checks per-family overrides first; falls back to the program-level
        ``sor_mode``.  Returns ``"legacy"`` if the family has no override and
        the program-level mode is also ``"legacy"``.
        """
        return self._family_sor_modes.get(family, self._sor_mode)

    @property
    def as_of(self) -> datetime:
        return self._as_of


def _load_ledger_gaps(
    events: tuple[EventEnvelope, ...],
    triage_decisions: tuple[CandidateDecisionRecord, ...],
) -> tuple[GapRecord, ...]:
    acknowledged_ids = {
        decision.gap_event_id
        for decision in triage_decisions
        if decision.kind == "gap_acknowledged" and decision.gap_event_id is not None
    }
    gaps: list[GapRecord] = []
    for event in events:
        if event.event_type != "pipeline.gap_detected.v1":
            continue
        window_start_raw = event.payload.get("window_start")
        window_end_raw = event.payload.get("window_end")
        gaps.append(
            GapRecord(
                event_id=event.event_id,
                pipeline=str(event.payload["pipeline"]),
                gap_kind=str(event.payload["gap_kind"]),
                detail=str(event.payload["detail"]),
                window_start=_parse_gap_datetime(window_start_raw),
                window_end=_parse_gap_datetime(window_end_raw),
                acknowledged=event.event_id in acknowledged_ids,
            )
        )
    return tuple(gaps)


def _load_expiring_ledger_field_locks(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    as_of: datetime,
) -> tuple[LedgerFieldLockRecord, ...]:
    expiring: list[LedgerFieldLockRecord] = []
    horizon = as_of + timedelta(days=7)
    for row in rows:
        valid_until = row.get("valid_until")
        entity_id = row.get("entity_id")
        field_name = row.get("field")
        if not isinstance(valid_until, str) or not isinstance(entity_id, str) or not isinstance(field_name, str):
            continue
        try:
            expiry = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        except ValueError:
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        else:
            expiry = expiry.astimezone(timezone.utc)
        if as_of <= expiry <= horizon:
            expiring.append(LedgerFieldLockRecord(entity_id=entity_id, field=field_name, valid_until=expiry))
    expiring.sort(key=lambda row: (row.valid_until, row.entity_id, row.field))
    return tuple(expiring)


def _load_ledger_shadow_reviews(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    events: tuple[EventEnvelope, ...],
) -> tuple[LedgerShadowReviewRecord, ...]:
    events_by_id = {event.event_id: event for event in events}
    reviews: list[LedgerShadowReviewRecord] = []
    for row in rows:
        event_id = row.get("event_id")
        shadowed_by = row.get("shadowed_by")
        field_name = row.get("field_name")
        if not isinstance(event_id, str) or not isinstance(shadowed_by, str) or not isinstance(field_name, str):
            continue
        loser = events_by_id.get(event_id)
        winner = events_by_id.get(shadowed_by)
        if loser is None or winner is None:
            continue
        if loser.confidence != winner.confidence:
            continue
        if loser.occurred_at != winner.occurred_at:
            continue
        if source_ref_priority(loser.source_ref) != source_ref_priority(winner.source_ref):
            continue
        reviews.append(LedgerShadowReviewRecord(event_id=event_id, shadowed_by=shadowed_by, field_name=field_name))
    reviews.sort(key=lambda review: (review.field_name, review.event_id, review.shadowed_by))
    return tuple(reviews)


def _load_stale_operator_assertion_reviews(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    events: tuple[EventEnvelope, ...],
    *,
    as_of: datetime,
) -> tuple[LedgerStaleOperatorAssertionRecord, ...]:
    cutoff = as_of - timedelta(days=30)
    events_by_id = {event.event_id: event for event in events}
    reviews: list[LedgerStaleOperatorAssertionRecord] = []
    for row in rows:
        event_id = row.get("event_id")
        shadowed_by = row.get("shadowed_by")
        field_name = row.get("field_name")
        if not isinstance(event_id, str) or not isinstance(shadowed_by, str) or not isinstance(field_name, str):
            continue
        loser = events_by_id.get(event_id)
        winner = events_by_id.get(shadowed_by)
        if loser is None or winner is None:
            continue
        if loser.confidence != ConfidenceTier.SOURCE_AUTHORITATIVE:
            continue
        if winner.confidence != ConfidenceTier.OPERATOR_CONFIRMED:
            continue
        if getattr(winner.source_ref, "ref_type", None) != "operator_assertion":
            continue
        asserted_at = getattr(winner.source_ref, "asserted_at", None)
        if not isinstance(asserted_at, datetime):
            asserted_at = winner.recorded_at
        if asserted_at.tzinfo is None:
            asserted_at = asserted_at.replace(tzinfo=timezone.utc)
        else:
            asserted_at = asserted_at.astimezone(timezone.utc)
        if asserted_at > cutoff:
            continue
        reviews.append(LedgerStaleOperatorAssertionRecord(
            event_id=event_id,
            shadowed_by=shadowed_by,
            field_name=field_name,
            asserted_at=asserted_at,
        ))
    reviews.sort(key=lambda review: (review.asserted_at, review.field_name, review.event_id, review.shadowed_by))
    return tuple(reviews)


def _load_temporal_confidence_reviews(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    events: tuple[EventEnvelope, ...],
) -> tuple[LedgerTemporalConfidenceReviewRecord, ...]:
    events_by_id = {event.event_id: event for event in events}
    reviews: list[LedgerTemporalConfidenceReviewRecord] = []
    for row in rows:
        event_id = row.get("event_id")
        shadowed_by = row.get("shadowed_by")
        field_name = row.get("field_name")
        if not isinstance(event_id, str) or not isinstance(shadowed_by, str) or not isinstance(field_name, str):
            continue
        loser = events_by_id.get(event_id)
        winner = events_by_id.get(shadowed_by)
        if loser is None or winner is None:
            continue
        if winner.occurred_at <= loser.occurred_at:
            continue
        winner_strength = _TEMPORAL_CONFIDENCE_STRENGTH.get(winner.temporal_confidence.value, 0)
        loser_strength = _TEMPORAL_CONFIDENCE_STRENGTH.get(loser.temporal_confidence.value, 0)
        if winner_strength >= loser_strength:
            continue
        reviews.append(LedgerTemporalConfidenceReviewRecord(
            event_id=event_id,
            shadowed_by=shadowed_by,
            field_name=field_name,
            winner_temporal_confidence=winner.temporal_confidence.value,
            loser_temporal_confidence=loser.temporal_confidence.value,
        ))
    reviews.sort(key=lambda review: (review.field_name, review.event_id, review.shadowed_by))
    return tuple(reviews)


def _load_latest_live_claim_freshness(
    program_id: str,
    *,
    programs_root: Path,
) -> KnowledgeClaimFreshnessRecord | None:
    narratives_root = programs_root / program_id / "narratives"
    if not narratives_root.exists():
        return None
    latest_issue_with_proposals: int | None = None
    for path in narratives_root.glob("issue_*/proposals.jsonl"):
        try:
            discovered_issue = int(path.parent.name.removeprefix("issue_"))
        except ValueError:
            continue
        latest_issue_with_proposals = discovered_issue if latest_issue_with_proposals is None else max(latest_issue_with_proposals, discovered_issue)
    if latest_issue_with_proposals is None:
        return None
    stale_claim_ids = load_stale_claim_ids(program_id, latest_issue_with_proposals, programs_root=programs_root)
    if not stale_claim_ids:
        return None
    return KnowledgeClaimFreshnessRecord(issue_number=latest_issue_with_proposals, claim_ids=stale_claim_ids)


def _load_latest_archived_claim_freshness(
    edition_name: str | None,
    *,
    archive_root: Path,
) -> KnowledgeClaimFreshnessRecord | None:
    if not edition_name:
        return None
    latest_confirmed_entry = find_latest_confirmed_entry(read_archive_index(edition_name, archive_root=archive_root))
    if latest_confirmed_entry is None:
        return None
    archive_narratives_dir = get_archive_root(edition_name, archive_root) / "narratives" / f"issue_{latest_confirmed_entry.issue_number:03d}"
    stale_claim_ids = load_archived_stale_claim_ids(archive_narratives_dir)
    if not stale_claim_ids:
        return None
    return KnowledgeClaimFreshnessRecord(
        issue_number=latest_confirmed_entry.issue_number,
        claim_ids=stale_claim_ids,
        evidence_source="archive_accepted_proposals",
    )


def _load_ledger_timeline_entries(
    events: tuple[EventEnvelope, ...],
    *,
    program_id: str,
    programs_root: Path,
    orphaned_by: dict[str, str | None] | None = None,
    shadowed_by: dict[str, str | None] | None = None,
) -> tuple[LedgerTimelineEntry, ...]:
    superseded_by = {record.event_id: record.superseded_by for record in load_indexed_events(program_id, programs_root=programs_root)}
    orphaned_by_map = dict(orphaned_by or {})
    shadowed_by_map = dict(shadowed_by or {})
    entries: list[LedgerTimelineEntry] = []
    for event in events:
        entries.append(
            LedgerTimelineEntry(
                event_id=event.event_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                recorded_at=event.recorded_at,
                actor=event.actor,
                confidence=event.confidence.value,
                temporal_confidence=event.temporal_confidence.value,
                source_document_key=source_document_key(event.source_ref),
                orphaned_by=orphaned_by_map.get(event.event_id),
                shadowed_by=shadowed_by_map.get(event.event_id),
                superseded_by=superseded_by.get(event.event_id),
            )
        )
    return tuple(entries)


def _parse_gap_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _projection_coverage_for_entities(
    entity_ids: tuple[str, ...],
    projection: ProgramProjection,
) -> dict[str, str]:
    table_keys = {
        "risk": ("proj_risk", "risk_id"),
        "milestone": ("proj_milestone", "milestone_id"),
        "decision": ("proj_decision", "decision_id"),
        "assumption": ("proj_assumption", "assumption_id"),
        "dependency": ("proj_dependency", "dependency_id"),
        "workstream": ("proj_workstream", "workstream_id"),
        "deliverable": ("proj_deliverable", "deliverable_id"),
        "commitment": ("proj_commitment", "commitment_id"),
        "incident": ("proj_incident", "incident_id"),
        "article": ("proj_knowledge_article", "article_id"),
        "sku_generation": ("proj_sku_generation", "sku_generation_id"),
    }
    coverage: dict[str, str] = {}
    for entity_id in entity_ids:
        family = entity_id.split(":", maxsplit=1)[0] if ":" in entity_id else ""
        table_info = table_keys.get(family)
        if table_info is None:
            coverage[entity_id] = "absent"
            continue
        table_name, key_name = table_info
        rows = projection.table(table_name)
        matching = next((row for row in rows if row.get(key_name) == entity_id), None)
        if matching is None:
            coverage[entity_id] = "absent"
            continue
        coverage[entity_id] = "stub" if matching.get("status") == "stub" else "present"
    return coverage


# ---------------------------------------------------------------------------
# FleetReality (Phase 5 skeleton — §6.12.3)
# ---------------------------------------------------------------------------

class FleetReality:
    """Multi-program view over per-program ProgramReality snapshots."""

    def __init__(self, programs: tuple[ProgramReality, ...]) -> None:
        self._programs = programs

    @classmethod
    def load(
        cls,
        program_ids: tuple[str, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
        domains: tuple[str, ...] | None = None,
    ) -> "FleetReality":
        """Lazily fan out ProgramReality.load() per program."""
        programs = tuple(
            ProgramReality.load(pid, programs_root=programs_root, domains=domains)
            for pid in program_ids
        )
        return cls(programs)

    def programs(self) -> tuple[ProgramReality, ...]:
        return self._programs

    def program_ids(self) -> tuple[str, ...]:
        return tuple(program.program_id for program in self._programs)

    def attention(self, *, owner: str | None = None) -> tuple[FleetAttentionItem, ...]:
        items: list[FleetAttentionItem] = []
        for program in self._programs:
            for item in program.attention(owner=owner):
                items.append(FleetAttentionItem(program_id=program.program_id, item=item))
        return tuple(sorted(items, key=lambda entry: (entry.item.priority, entry.program_id, entry.item.kind)))

    def conflicts(self, *, open_only: bool = True) -> tuple[FleetConflict, ...]:
        results: list[FleetConflict] = []
        for program in self._programs:
            for conflict in program.conflicts(open_only=open_only):
                results.append(FleetConflict(program_id=program.program_id, conflict=conflict))
        return tuple(results)

    def pending_actuations(self) -> tuple[FleetActuationProposal, ...]:
        results: list[FleetActuationProposal] = []
        for program in self._programs:
            for proposal in program.pending_actuations():
                results.append(FleetActuationProposal(program_id=program.program_id, proposal=proposal))
        return tuple(results)

    def freshness(self) -> tuple[FleetFreshnessRecord, ...]:
        rows: list[FleetFreshnessRecord] = []
        for program in self._programs:
            for freshness in program.freshness():
                rows.append(FleetFreshnessRecord(program_id=program.program_id, freshness=freshness))
        return tuple(rows)

    def to_dict(self, *, max_classification: str = "internal") -> dict[str, Any]:
        return {
            "scope": "fleet",
            "program_count": len(self._programs),
            "program_ids": list(self.program_ids()),
            "open_conflict_count": len(self.conflicts(open_only=True)),
            "pending_actuation_count": len(self.pending_actuations()),
            "attention_count": len(self.attention()),
            "programs": [
                program.to_dict(max_classification=max_classification)
                for program in self._programs
            ],
        }


# ---------------------------------------------------------------------------
# Internal serialization helpers
# ---------------------------------------------------------------------------

def _assessment_to_dict(a: FactAssessment) -> dict[str, Any]:
    return {
        "fact_id": a.fact_id,
        "truth_level": a.truth_level.value if isinstance(a.truth_level, TruthLevel) else str(a.truth_level),
        "disputed": a.disputed,
        "stale": a.stale,
        "provisional_inputs": a.provisional_inputs,
        "evidence": list(a.evidence),
    }


def _attention_matches_owner(item: AttentionItem, owner: str) -> bool:
    """Return True if the attention item is relevant to the given owner."""
    if item.record is None:
        return False
    record = item.record.record
    # Check common owner fields
    for attr in ("owner_alias", "owner", "dri", "assigned_to"):
        val = getattr(record, attr, None)
        if val is not None and str(val).lower() == owner.lower():
            return True
    return False
