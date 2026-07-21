from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import time
from typing import Any, Iterator, TypedDict
from uuid import uuid4

from src.core.archive_store import ARCHIVE_ROOT, load_skipped_issues_for_program
from src.core.exceptions import ConfigError
from src.core.assumption_tracker import load_assumptions
from src.core.claim_tracker import load_claim_status_updates, load_open_claims, load_open_decision_asks
from src.core.decision_register import load_decisions
from src.core.fact_sor_state import FactSorState, load_fact_sor_state
from src.core.dependency_graph import load_dependencies
from src.core.milestone_engine import load_milestones
from src.core.models_v2 import (
    ActionItem,
    ActionSourceType,
    ActionStatus,
    ADOCoverageRequirement,
    Assumption,
    AssumptionStatus,
    ClaimEntry,
    ClaimStatusUpdate,
    DecisionAsk,
    DecisionEntry,
    DecisionStatus,
    DeliverableEntry,
    IncidentFactEntry,
    ResurfacingPolicy,
    SkippedIssueEntry,
    Dependency,
    DependencyADOQuery,
    DependencyEvidenceTier,
    DependencyScheduleStatus,
    DependencyStatus,
    DependencyType,
    EmailThreadSource,
    Judgment,
    Milestone,
    MilestoneStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    TeamsChat,
    TeamsMeetingSeries,
    Workstream,
    WorkstreamSignalSources,
)
from src.core.yaml_utils import load_yaml_mapping
from src.core.reality_store import get_program_reality_db_path
from src.core.risk_register_engine import load_risk_register
from src.core.trusted_baseline_store import TrustedBaselineHistoryEntry, load_trusted_baseline_for_program
from src.core.workstream_association_store import WorkstreamAssociationRecord, read_workstream_association_records
from src.core.workstream_documents import _parse_workstreams

PROGRAMS_ROOT = Path(__file__).resolve().parents[2] / "programs"

_LOG = logging.getLogger(__name__)


def _resolve_fact_db_root(programs_root: Path | None) -> Path | None:
    """Resolve the ``db_root`` to pass to ``ProgramFactStore``/``get_program_reality_db_path``.

    Mirrors ``milestone_engine._resolve_fact_db_root``: when ``programs_root``
    is the default production root, defer to ``None`` so
    ``get_program_reality_db_path`` applies its own canonical resolution
    (``programs_root.parent / "vertex-db"``) instead of the bare
    ``programs_root.parent`` used here previously -- a mismatch that caused
    fact-store reads and writes to silently target two different SQLite
    files (see specs/arch-data-fix.md; the PS-14 class of split-brain bug).
    A caller-supplied sandboxed ``programs_root`` (e.g. a test tmp dir ending
    in "programs") still resolves to its parent, preserving prior behavior.
    """
    if programs_root is None:
        return None
    if programs_root == PROGRAMS_ROOT:
        return None
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


class FactReviewState(StrEnum):
    ACCEPTED = "accepted"
    PROPOSED = "proposed"


class FactLifecycleState(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class FactPrecedence(StrEnum):
    RAW_TELEMETRY = "raw_telemetry"
    VERIFIED_SYSTEM_SIGNAL = "verified_system_signal"
    CONFIRMED_GOVERNANCE_DECISION = "confirmed_governance_decision"
    ACTIVE_PM_JUDGMENT = "active_pm_judgment"


_PRECEDENCE_ORDER = {
    FactPrecedence.RAW_TELEMETRY: 1,
    FactPrecedence.VERIFIED_SYSTEM_SIGNAL: 2,
    FactPrecedence.CONFIRMED_GOVERNANCE_DECISION: 3,
    FactPrecedence.ACTIVE_PM_JUDGMENT: 4,
}


@dataclass(frozen=True, slots=True)
class ProgramEvent:
    """Phase 6 §22 Step 7: an event-log entry written to the Fact Store.

    Events are append-only log records of things that happened
    (skip-issue, baseline lock, etc.) — distinct from domain facts
    (`ProgramFactInput`) which are projections of program state
    (workstream, action, claim, risk). The event's `fact_type` should
    follow the `event.<noun>.<verb>` convention (e.g. `"event.issue.skip"`)
    so the read projection can filter events out of domain-state queries
    by namespace.

    Why:** a single dataclass surface for all "thing happened" log
    entries keeps the write API uniform as new event types are added
    (rollback, baseline trust advance, etc.). Wrapping it as a
    `ProgramFactInput` at the store boundary means the existing
    SQLite append path, dual-read shadow, and parity-check tooling all
    work without per-event-type special-casing.
    **How to apply:** construct a `ProgramEvent(fact_type, natural_key,
    metadata)` and pass to `append_program_event(...)`. Read back with
    `load_program_events(...)` or filter `load_program_facts(fact_types=
    ("event.issue.skip",))`.
    """
    fact_type: str
    natural_key: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FactLineage:
    """Typed 12-field lineage envelope for a program fact revision (S-3 / §5.2).

    Carries the complete audit trail from source EML through to the rendered
    citation, enabling E2E reverse lookup: citation → source_document_key →
    source EML + approval_event_id → operator approval.  Reverse lookup
    degrades to "hash only" after the retention window expires.

    Provenance fields (9)
    ---------------------
    candidate_id:        Discovery candidate from which this fact was derived.
    source_document_key: SHA-256 hash of the source document identifier
                         (e.g. hashed RFC-2822 Message-ID).  **Not** the raw
                         Message-ID — see privacy note in §5.2.
    source_hash:         SHA-256 hash of the canonical source text excerpt.
    evidence_ref:        Vault hash of the evidence excerpt (content-addressed).
                         Set to None on redaction.
    domain_event_id:     Ledger event that produced this fact revision
                         (idempotency key for outbox / S-1).
    approval_event_id:   Audit event ID for the operator approval action
                         (helper `_write_candidate_audit_event` returns this).
    source_event_id:     Idempotency key for the acceptance-time outbox (S-1 /
                         S-4 keystone): the event that triggered projection.
    projector_version:   Projector version at the time this revision was written
                         (for replay migration — S-5).
    extractor_version:   Extractor/prompt version (for tuning the γ-Read→β
                         quality-feedback loop — S-3/S-5/§27 coupling point 4).

    Privacy / retention fields (3, non-nullable with defaults — S-3 / §5.2)
    -----------------------------------------------------------------------
    redaction_status:       "active" | "redacted" | "retention_expired".
                            Vault content purged → evidence_ref=None + this field.
    retention_class:        "pilot_local" | "production_internal" | "production_confidential".
                            Controls how long content is retained and who may access it.
    privacy_classification: "public" | "internal" | "confidential".
                            Set at extraction time from the source document class.
    """

    # Provenance fields
    candidate_id: str | None = None
    source_document_key: str | None = None  # SHA-256(Message-ID), not raw
    source_hash: str | None = None
    evidence_ref: str | None = None
    domain_event_id: str | None = None
    approval_event_id: str | None = None
    source_event_id: str | None = None       # S-1 outbox idempotency key
    projector_version: str | None = None
    extractor_version: str | None = None
    # Privacy / retention fields (non-nullable — ALTER TABLE DEFAULT migration safe)
    redaction_status: str = "active"
    retention_class: str = "pilot_local"
    privacy_classification: str = "internal"

    def as_redacted(self) -> "FactLineage":
        """Return a copy with evidence_ref cleared and status set to 'redacted'."""
        from dataclasses import replace as _replace
        return _replace(self, evidence_ref=None, redaction_status="redacted")

    def as_retention_expired(self) -> "FactLineage":
        """Return a copy with evidence_ref cleared and status set to 'retention_expired'."""
        from dataclasses import replace as _replace
        return _replace(
            self,
            evidence_ref=None,
            source_document_key=self.source_document_key,  # hash survives rotation
            source_hash=self.source_hash,                  # hash survives rotation
            redaction_status="retention_expired",
        )

    @staticmethod
    def unavailable(reason: str) -> "FactLineageUnavailable":
        """Return a structured unavailability marker (never silent None).

        Used by reverse-lookup when source is purged, redacted, or access-denied.
        """
        return FactLineageUnavailable(reason=reason)


@dataclass(frozen=True, slots=True)
class FactLineageUnavailable:
    """Structured marker for reverse-lookup failure (§5.2 — never silent null)."""

    reason: str  # "retention_expired" | "redacted" | "access_denied" | "not_found"


# S-0i: Evidence-vault TTL per (security_profile, retention_class) for pilot-local.
# Keys: (security_profile, retention_class)  →  days (None = indefinite).
# Retention expires → FactLineage.as_retention_expired() + FactLineageUnavailable("retention_expired").
EVIDENCE_VAULT_TTL_DAYS: dict[tuple[str, str], int | None] = {
    ("pilot_local", "pilot_local"):              365,    # 1 year default for pilot deployments
    ("pilot_local", "production_internal"):      365,    # same envelope for pilot
    ("pilot_local", "production_confidential"):  90,     # shorter window for confidential
    ("production", "production_internal"):       365,
    ("production", "production_confidential"):   180,
    ("production", "public"):                    None,   # indefinite for non-sensitive facts
}


@dataclass(frozen=True, slots=True)
class ProgramFactInput:
    """Input for creating a new fact revision in the fact store."""

    fact_type: str
    entity_refs: tuple[str, ...]
    payload: dict[str, Any]
    scope: str = "program"
    source_signal_ids: tuple[str, ...] = ()
    confidence: str | None = None
    precedence: FactPrecedence = FactPrecedence.VERIFIED_SYSTEM_SIGNAL
    review_state: FactReviewState = FactReviewState.ACCEPTED
    lifecycle_state: FactLifecycleState = FactLifecycleState.ACTIVE
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    projection_history: tuple[dict[str, Any], ...] = ()
    natural_key: str | None = None
    created_by: str = "vertex"
    # FR-SG-56: data classification; FR-SG-66: write authority identity
    privacy_classification: str = "internal"
    accepted_by: str | None = None
    write_authority: str = "human"
    # S-3 / G-lineage: full 12-field lineage envelope fields
    # (domain_event_id + candidate_id existed pre-S-3; 7 new fields added for S-3)
    domain_event_id: str | None = None      # ledger event (idempotency key)
    candidate_id: str | None = None         # discovery candidate
    source_document_key: str | None = None  # SHA-256(Message-ID) — S-3
    source_hash: str | None = None          # canonical text hash — S-3
    evidence_ref: str | None = None         # vault content hash — S-3
    approval_event_id: str | None = None    # audit event id — S-3
    source_event_id: str | None = None      # outbox idempotency key — S-1/S-3
    projector_version: str | None = None    # for replay migration — S-5
    extractor_version: str | None = None    # for quality feedback — S-3
    # Privacy / retention (non-nullable with defaults — safe for ALTER TABLE ADD COLUMN)
    redaction_status: str = "active"
    retention_class: str = "pilot_local"
    # D-13 rule 4 / D-15 (specs/armada.md): the gather-run.v1 manifest run_id
    # that produced this fact, threaded down from the gather pipeline so
    # readers can eventually filter to facts from committed runs only.
    # None for facts predating this field or written outside a
    # lifecycle-managed gather run (e.g. reviewer/manual writes).
    gather_run_id: str | None = None

    def build_lineage(self) -> FactLineage:
        """Construct a typed FactLineage from this input's lineage fields."""
        return FactLineage(
            candidate_id=self.candidate_id,
            source_document_key=self.source_document_key,
            source_hash=self.source_hash,
            evidence_ref=self.evidence_ref,
            domain_event_id=self.domain_event_id,
            approval_event_id=self.approval_event_id,
            source_event_id=self.source_event_id,
            projector_version=self.projector_version,
            extractor_version=self.extractor_version,
            redaction_status=self.redaction_status,
            retention_class=self.retention_class,
            privacy_classification=self.privacy_classification,
        )



@dataclass(frozen=True, slots=True)
class ProgramFactRevision:
    revision_id: str
    fact_id: str
    program_id: str
    natural_key: str
    fact_type: str
    scope: str
    entity_refs: tuple[str, ...]
    payload: dict[str, Any]
    source_signal_ids: tuple[str, ...]
    confidence: str | None
    precedence: FactPrecedence
    review_state: FactReviewState
    lifecycle_state: FactLifecycleState
    valid_from: datetime | None
    valid_until: datetime | None
    recorded_at: datetime
    superseded_at: datetime | None
    projection_history: tuple[dict[str, Any], ...]
    proposed_against_revision_id: str | None
    created_by: str
    # FR-SG-56: data classification; FR-SG-66: write authority identity
    privacy_classification: str = "internal"
    accepted_by: str | None = None
    write_authority: str = "human"
    # S-3 / G-lineage: full 12-field lineage envelope
    domain_event_id: str | None = None
    candidate_id: str | None = None
    source_document_key: str | None = None   # SHA-256(Message-ID) — S-3
    source_hash: str | None = None            # canonical text hash — S-3
    evidence_ref: str | None = None           # vault content hash — S-3
    approval_event_id: str | None = None      # audit event id — S-3
    source_event_id: str | None = None        # outbox idempotency key — S-1/S-3
    projector_version: str | None = None      # for replay migration — S-5
    extractor_version: str | None = None      # for quality feedback — S-3
    # Privacy / retention (non-nullable with defaults — safe for migration)
    redaction_status: str = "active"
    retention_class: str = "pilot_local"
    # D-13 rule 4 / D-15 (specs/armada.md): see ProgramFactInput.gather_run_id.
    gather_run_id: str | None = None

    def build_lineage(self) -> FactLineage:
        """Construct a typed FactLineage from this revision's lineage fields."""
        return FactLineage(
            candidate_id=self.candidate_id,
            source_document_key=self.source_document_key,
            source_hash=self.source_hash,
            evidence_ref=self.evidence_ref,
            domain_event_id=self.domain_event_id,
            approval_event_id=self.approval_event_id,
            source_event_id=self.source_event_id,
            projector_version=self.projector_version,
            extractor_version=self.extractor_version,
            redaction_status=self.redaction_status,
            retention_class=self.retention_class,
            privacy_classification=self.privacy_classification,
        )



@dataclass(frozen=True, slots=True)
class ProgramFactWriteResult:
    revision: ProgramFactRevision
    action: str


@dataclass(frozen=True, slots=True)
class ProgramFactSnapshot:
    program_id: str
    as_of: datetime
    facts: tuple[ProgramFactRevision, ...]


class ProgramFactEnvelope(TypedDict, total=False):
    """Wire envelope for serialising a ProgramFactSnapshot (FR-SG-64)."""

    program_id: str
    as_of: str
    fact_count: int
    facts: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class FactSnapshotPin:
    snapshot_id: str
    program_id: str
    created_at: datetime
    pinned_recorded_at: datetime | None
    pinned_revision_count: int
    metadata: dict[str, Any]


def resolve_fact_sor_mode(
    *,
    program_id: str | None = None,
    programs_root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    source = environ or os.environ
    raw_value = source.get("VERTEX_FACT_SOR", "")
    normalized = raw_value.strip().lower()
    if normalized in {"shadow", "primary"}:
        return normalized
    if normalized == "legacy":
        return "legacy"
    if normalized:
        return "legacy"
    if program_id is not None:
        state = load_fact_sor_state(program_id, programs_root=programs_root or PROGRAMS_ROOT)
        if state is not None:
            return state.mode
    return "legacy"


# S-5a: static fact_type → authority_family mapping (mirrors source_authority.yaml family_map).
# Used by resolve_fact_mode_for_type() to avoid a YAML parse per call.
_FACT_TYPE_TO_FAMILY: dict[str, str] = {
    "action.item": "workitem.state",
    "dependency.link": "workitem.state",
    "milestone.entry": "workitem.state",
    "workstream.entry": "workitem.state",
    "risk.entry": "judgment",
    "decision.entry": "judgment",
    "assumption.entry": "judgment",
    "commitment.entry": "commitment",
    "claim.entry": "narrative",
}


def resolve_fact_mode_for_type(
    fact_type: str,
    sor_state: "FactSorState | None",
) -> str:
    """Return the resolved SoR mode for a specific fact type (S-5a).

    Maps ``fact_type`` → ``authority_family`` using the static fact_type→family
    table, then returns the per-family mode override if one exists in
    ``sor_state.family_modes``, falling back to the program-level mode.
    Returns ``"legacy"`` when ``sor_state`` is ``None``.
    """
    if sor_state is None:
        return "legacy"
    family = _FACT_TYPE_TO_FAMILY.get(fact_type.strip().lower())
    if family is None:
        return sor_state.mode
    return sor_state.family_modes.get(family, sor_state.mode)


def build_natural_key(
    fact_type: str,
    *,
    entity_refs: tuple[str, ...],
    scope: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(fact_type.strip().lower().encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(scope.strip().lower().encode("utf-8"))
    digest.update(b"\x1f")
    for entity_ref in sorted(ref.strip() for ref in entity_refs):
        digest.update(entity_ref.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def build_fact_id(program_id: str, natural_key: str) -> str:
    """Return the stable, program-scoped ID for a logical Program Fact.

    A fact's natural key already identifies its semantic identity within a
    program.  Hashing the program ID and natural key gives replays and a clean
    restore the same fact ID without conflating equal keys in different
    programs.  Revisions remain independently unique and are still handled by
    the revision ledger.
    """
    normalized_program_id = program_id.strip().lower()
    normalized_natural_key = natural_key.strip().lower()
    if not normalized_program_id:
        raise ValueError("program_id must not be empty")
    if not normalized_natural_key:
        raise ValueError("natural_key must not be empty")
    digest = hashlib.sha256(
        f"{normalized_program_id}\x1f{normalized_natural_key}".encode("utf-8")
    ).hexdigest()
    return f"pf_{digest}"


def load_program_facts(
    program_id: str,
    *,
    as_of: datetime | None = None,
    fact_types: tuple[str, ...] | None = None,
    home_root: Path | None = None,
    db_root: Path | None = None,
    programs_root: Path | None = None,
    editions_root: Path | None = None,
    archive_root: Path | None = None,
    sor_state: FactSorState | None = None,
    require_committed_gather_run: bool = False,
) -> ProgramFactSnapshot:
    resolved_db_root = db_root
    if resolved_db_root is None:
        resolved_db_root = _resolve_fact_db_root(programs_root)
    snapshot = ProgramFactStore(program_id, home_root=home_root, db_root=resolved_db_root).snapshot(as_of=as_of)
    if require_committed_gather_run:
        from src.core.gather_run_manifest import get_verified_committed_run_ids

        committed_run_ids = get_verified_committed_run_ids(
            program_id,
            programs_root=programs_root or PROGRAMS_ROOT,
        )
        snapshot = ProgramFactSnapshot(
            program_id=snapshot.program_id,
            as_of=snapshot.as_of,
            # Legacy records intentionally remain visible until the explicit
            # §4.17 bootstrap/cutover migration.  A stamped fact is visible
            # only once its manifest is committed and hash-valid.
            facts=tuple(
                fact
                for fact in snapshot.facts
                if fact.gather_run_id is None or fact.gather_run_id in committed_run_ids
            ),
        )
    if as_of is not None:
        return snapshot

    # Resolve program-level mode (env-var override takes precedence)
    program_mode = resolve_fact_sor_mode(program_id=program_id, programs_root=programs_root)
    if program_mode == "primary":
        # Program is fully in primary mode — no shim facts needed at all.
        return snapshot

    # S-5a: if a sor_state with family_modes is provided, exclude shim facts
    # for families that have been individually flipped to "primary".
    primary_families: frozenset[str] = frozenset()
    if sor_state is not None and sor_state.family_modes:
        primary_families = frozenset(
            family for family, fmode in sor_state.family_modes.items() if fmode == "primary"
        )

    shim_facts = _load_current_state_shim_facts(
        program_id,
        recorded_at=snapshot.as_of,
        fact_types=fact_types,
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
    )
    if not shim_facts:
        return snapshot

    merged_facts = {fact.natural_key: fact for fact in snapshot.facts}
    for fact in shim_facts:
        # S-5a: skip shim facts whose authority family is in primary mode
        if primary_families:
            fact_family = _FACT_TYPE_TO_FAMILY.get(fact.fact_type.strip().lower())
            if fact_family in primary_families:
                continue
        merged_facts.setdefault(fact.natural_key, fact)
    return ProgramFactSnapshot(
        program_id=snapshot.program_id,
        as_of=snapshot.as_of,
        facts=tuple(sorted(merged_facts.values(), key=lambda fact: (fact.fact_type, fact.natural_key))),
    )


def build_legacy_program_fact_snapshot(
    program_id: str,
    *,
    recorded_at: datetime | None = None,
    fact_types: tuple[str, ...] | None = None,
    programs_root: Path | None = None,
    editions_root: Path | None = None,
    archive_root: Path | None = None,
) -> ProgramFactSnapshot:
    snapshot_at = recorded_at or datetime.now(timezone.utc)
    shim_facts = _load_current_state_shim_facts(
        program_id,
        recorded_at=snapshot_at,
        fact_types=fact_types,
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
    )
    return ProgramFactSnapshot(
        program_id=program_id,
        as_of=snapshot_at,
        facts=shim_facts,
    )


def project_action_items(snapshot: ProgramFactSnapshot) -> tuple[ActionItem, ...]:
    return tuple(
        _action_item_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "action.item" and fact.review_state == FactReviewState.ACCEPTED
    )


def project_claim_entries(snapshot: ProgramFactSnapshot) -> tuple[ClaimEntry, ...]:
    latest_statuses = _latest_claim_status_updates(snapshot)
    return tuple(
        entry
        for entry in (
            _claim_entry_from_fact(fact)
            for fact in snapshot.facts
            if fact.fact_type == "claim.entry" and fact.review_state == FactReviewState.ACCEPTED
        )
        if latest_statuses.get(entry.id, entry.status) == "open"
    )


def project_claim_status_updates(snapshot: ProgramFactSnapshot) -> tuple[ClaimStatusUpdate, ...]:
    return tuple(
        sorted(
            (
                _claim_status_update_from_fact(fact)
                for fact in snapshot.facts
                if fact.fact_type == "claim.status_update"
                and fact.review_state == FactReviewState.ACCEPTED
            ),
            key=lambda update: (update.updated_at, update.claim_id, update.updated_by, update.new_status),
        )
    )


def project_decision_asks(snapshot: ProgramFactSnapshot) -> tuple[DecisionAsk, ...]:
    latest_updates = {
        update.claim_id: update
        for update in project_claim_status_updates(snapshot)
    }
    projected: list[DecisionAsk] = []
    for fact in snapshot.facts:
        if fact.fact_type != "decision.ask":
            continue
        entry = _decision_ask_from_fact(fact)
        update = latest_updates.get(entry.id)
        effective_status = update.new_status if update is not None else entry.status
        if effective_status != "open":
            continue
        projected.append(_project_decision_ask_last_touch_from_update(entry, update))
    return tuple(projected)


def project_baseline_trust_events(snapshot: ProgramFactSnapshot) -> tuple[TrustedBaselineHistoryEntry, ...]:
    return tuple(
        _baseline_trust_event_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "baseline.trust_event" and fact.review_state == FactReviewState.ACCEPTED
    )


def project_skip_issues(snapshot: ProgramFactSnapshot) -> tuple[SkippedIssueEntry, ...]:
    """Phase 6 §22 Step 7: project skip-issue entries from BOTH the
    legacy `skip.issue` fact type (back-compat for data written before
    rev. 331) and the new `event.issue.skip` event fact type (current
    write path). Both share the same payload schema and natural_key
    formula, so a single `_skip_issue_from_fact` projection works.

    During the migration window, a re-run of `admin_fact_store_migrate`
    after rev. 331 may write both fact_types for the same skip — we
    dedupe by `(edition_id, issue_number)` here so callers see each
    skip exactly once.
    """
    seen: set[tuple[str, int]] = set()
    result: list[SkippedIssueEntry] = []
    for fact in snapshot.facts:
        if fact.fact_type not in ("skip.issue", "event.issue.skip"):
            continue
        if fact.review_state != FactReviewState.ACCEPTED:
            continue
        entry = _skip_issue_from_fact(fact)
        key = (entry.edition_id, entry.issue_number)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return tuple(result)


def project_risk_entries(snapshot: ProgramFactSnapshot) -> tuple[RiskEntry, ...]:
    return tuple(
        _risk_entry_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "risk.entry"
        and fact.lifecycle_state == FactLifecycleState.ACTIVE
        and fact.review_state == FactReviewState.ACCEPTED
    )


def project_dependencies(snapshot: ProgramFactSnapshot) -> tuple[Dependency, ...]:
    return tuple(
        _dependency_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "dependency.link"
        and fact.lifecycle_state == FactLifecycleState.ACTIVE
        and fact.review_state == FactReviewState.ACCEPTED
    )


def project_decision_entries(snapshot: ProgramFactSnapshot) -> tuple[DecisionEntry, ...]:
    return tuple(
        _decision_entry_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "decision.entry"
        and fact.lifecycle_state == FactLifecycleState.ACTIVE
        and fact.review_state == FactReviewState.ACCEPTED
    )


def project_assumptions(snapshot: ProgramFactSnapshot) -> tuple[Assumption, ...]:
    return tuple(
        _assumption_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "assumption.entry" and fact.review_state == FactReviewState.ACCEPTED
    )


def project_milestones(snapshot: ProgramFactSnapshot) -> tuple[Milestone, ...]:
    return tuple(
        _milestone_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "milestone.entry"
        and fact.lifecycle_state == FactLifecycleState.ACTIVE
        and fact.review_state == FactReviewState.ACCEPTED
    )


def project_workstreams(snapshot: ProgramFactSnapshot) -> tuple[Workstream, ...]:
    return tuple(
        _workstream_from_fact(fact)
        for fact in snapshot.facts
        if fact.fact_type == "workstream.entry"
        and fact.lifecycle_state == FactLifecycleState.ACTIVE
        and fact.review_state == FactReviewState.ACCEPTED
    )


def project_workstream_associations(snapshot: ProgramFactSnapshot) -> tuple[WorkstreamAssociationRecord, ...]:
    """Project workstream-association records from a fact snapshot.

    Spec §22 Step 6: ``workstream.association`` is the SoR projection of
    the workstream-association ledger rows.  Each accepted fact revision
    corresponds to exactly one ``WorkstreamAssociationRecord``; re-running
    confirm on the same issue (with a distinct ``recorded_at``) emits a fresh
    fact (the natural key includes ``recorded_at``), so the projector
    preserves the append-only semantics of the legacy ledger.
    """
    result: list[WorkstreamAssociationRecord] = []
    for fact in snapshot.facts:
        if fact.fact_type != "workstream.association":
            continue
        if fact.review_state != FactReviewState.ACCEPTED:
            continue
        try:
            result.append(_workstream_association_from_fact(fact))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)


# ── Current-state convenience readers ────────────────────────────────────────
# One-call wrappers over ``load_program_facts(...)`` + the matching ``project_*``
# helper so command/god-module call sites stay a single line (FR-SG-51: keep
# fact-layer logic in this module, not inlined into gather.py/confirm.py/etc.).
# Each loads only the fact type it projects, preserving the bounded-shim seam.


def load_current_action_items(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[ActionItem, ...]:
    return project_action_items(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("action.item",))
    )


def load_current_claim_entries(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[ClaimEntry, ...]:
    return project_claim_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("claim.entry", "claim.status_update"),
        )
    )


def load_current_claim_status_updates(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[ClaimStatusUpdate, ...]:
    return project_claim_status_updates(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("claim.status_update",))
    )


def load_current_decision_asks(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[DecisionAsk, ...]:
    return project_decision_asks(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("decision.ask", "claim.status_update"),
        )
    )


def load_current_baseline_trust_events(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[TrustedBaselineHistoryEntry, ...]:
    return project_baseline_trust_events(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("baseline.trust_event",))
    )


def load_current_skip_issues(
    program_id: str,
    *,
    programs_root: Path | None = None,
    editions_root: Path | None = None,
    archive_root: Path | None = None,
) -> tuple[SkippedIssueEntry, ...]:
    return project_skip_issues(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            editions_root=editions_root,
            archive_root=archive_root,
            # Phase 6 §22 Step 7: request both the legacy `skip.issue`
            # fact_type (data written before rev. 331) and the new
            # `event.issue.skip` event fact_type (current write path).
            # `project_skip_issues` dedupes by natural_key.
            fact_types=("skip.issue", "event.issue.skip"),
        )
    )


def append_skip_issue_fact(
    program_id: str,
    *,
    edition_id: str,
    issue_number: int,
    generated_at: datetime,
    reason: str | None,
    db_root: Path | None = None,
    home_root: Path | None = None,
) -> ProgramFactWriteResult:
    """Backwards-compat shim: wraps `append_program_event` for the legacy
    `skip.issue` fact type. New code should use `append_program_event`
    directly with `fact_type="event.issue.skip"`.

    Kept so the admin_fact_store_migrate legacy ETL can backfill the
    old fact_type without an import cycle. Reads via
    `load_current_skip_issues` continue to recognize both fact types
    during the migration window."""
    return append_program_event(
        program_id,
        ProgramEvent(
            fact_type="skip.issue",
            natural_key=f"skip:{edition_id}:{issue_number}",
            metadata={
                "edition_id": edition_id,
                "issue_number": issue_number,
                "generated_at": _serialize_datetime(generated_at),
                "reason": reason,
            },
        ),
        recorded_at=generated_at,
        db_root=db_root,
        home_root=home_root,
    )


def append_program_event(
    program_id: str,
    event: ProgramEvent,
    *,
    precedence: FactPrecedence = FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
    recorded_at: datetime | None = None,
    db_root: Path | None = None,
    home_root: Path | None = None,
) -> ProgramFactWriteResult:
    """Phase 6 §22 Step 7: append a `ProgramEvent` to the Fact Store.

    Translates the event into a `ProgramFactInput` with:
      - `scope="program"`
      - `entity_refs=("event:{natural_key}",)`
      - `payload=event.metadata` (1:1 mapping, the event's metadata
        bag is the fact's payload)
      - `precedence=CONFIRMED_GOVERNANCE_DECISION` by default (events represent
        decided actions, not raw telemetry); specialised event seams may pass
        their explicitly classified precedence.
      - `natural_key=event.natural_key` (so re-running the same event
        is idempotent and surfaces as a no-op in the dual-read window)

    The recorded_at defaults to `datetime.now(timezone.utc)` if not
    supplied; callers usually want to pass the event's own timestamp
    (e.g. `generated_at` for skip-issue, `locked_at` for baseline-lock)
    so the event ordering matches the actual sequence of events.

    Why:** a single helper for all event writes keeps the
    `entity_refs`/`scope`/`precedence` ceremony in one place and means
    adding a new event type is just a call site, not a new schema
    carve-out.
    **How to apply:** use this for any new "thing happened" log entry.
    For reads, use `load_program_facts(fact_types=(event.fact_type,))`
    and project via the same projection helper as the domain fact type
    (the shape of `event.metadata` and the projection's payload
    schema should match).
    """
    resolved_recorded_at = recorded_at or datetime.now(timezone.utc)
    return ProgramFactStore(program_id, home_root=home_root, db_root=db_root).append_fact(
        ProgramFactInput(
            fact_type=event.fact_type,
            entity_refs=(f"event:{event.natural_key}",),
            payload=event.metadata,
            scope="program",
            precedence=precedence,
            natural_key=event.natural_key,
        ),
        recorded_at=resolved_recorded_at,
    )


def append_nudge_event(
    program_id: str,
    fact_type: str,
    payload: dict[str, Any],
    *,
    precedence: FactPrecedence | None = None,
    recorded_at: datetime | None = None,
    db_root: Path | None = None,
    home_root: Path | None = None,
) -> ProgramFactWriteResult:
    """Phase 0 §6.7: single-seam nudge fact writer (NQD-10 contract).

    Delegates to the existing append_program_event so the sanctioned write
    path (INV-SG-*) is preserved.  Fact types follow event.<noun>.<verb>;
    precedence defaults are per D-6:
      event.nudge.generated        → SYSTEM_OBSERVATION (telemetry, low)
      event.nudge.sent_attested    → HUMAN_ATTESTATION  (human/governance)
      event.nudge.sent_imported    → HUMAN_ATTESTATION  (reconstructed human/governance)
      event.nudge.draft_approved   → HUMAN_ATTESTATION  (human/governance)
      event.nudge.evaluated        → VERIFIED_SIGNAL    (system, mid)
      event.nudge.waiver_created   → HUMAN_ATTESTATION  (human judgment)
    """
    _NUDGE_PRECEDENCE: dict[str, FactPrecedence] = {
        "event.nudge.generated": FactPrecedence.RAW_TELEMETRY,
        "event.nudge.sent_attested": FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        "event.nudge.sent_imported": FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        "event.nudge.draft_approved": FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        "event.nudge.evaluated": FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        "event.nudge.waiver_created": FactPrecedence.ACTIVE_PM_JUDGMENT,
    }
    resolved_precedence = precedence or _NUDGE_PRECEDENCE.get(
        fact_type, FactPrecedence.RAW_TELEMETRY
    )
    natural_key = payload.get("run_id") or str(uuid4())
    return append_program_event(
        program_id,
        ProgramEvent(
            fact_type=fact_type,
            natural_key=natural_key,
            metadata={**payload, "fact_type": fact_type},
        ),
        precedence=resolved_precedence,
        recorded_at=recorded_at,
        db_root=db_root,
        home_root=home_root,
    )


def load_current_risk_entries(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[RiskEntry, ...]:
    return project_risk_entries(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("risk.entry",))
    )


def load_current_dependencies(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[Dependency, ...]:
    return project_dependencies(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("dependency.link",))
    )


def load_current_decision_entries(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[DecisionEntry, ...]:
    return project_decision_entries(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("decision.entry",))
    )


def load_current_assumptions(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[Assumption, ...]:
    return project_assumptions(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("assumption.entry",))
    )


def load_current_milestones(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[Milestone, ...]:
    return project_milestones(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("milestone.entry",))
    )


def load_current_workstreams(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[Workstream, ...]:
    return _load_current_workstreams(program_id, programs_root=programs_root)


def _load_current_workstreams_legacy(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[Workstream, ...]:
    return project_workstreams(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("workstream.entry",))
    )


def _load_workstreams_via_reality(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[Workstream, ...]:
    """S-8d: project workstreams from ``ProgramReality.workstreams()``.

    Active only when the ``workitem.state`` family SoR mode is non-legacy
    (resolved by the caller). Mirrors ``MilestoneStage._load_milestones_via_reality``
    and the S-8c commitment overlay: reads the FactAssessments exposed by the
    read facade and returns their underlying ``Workstream`` records. Raises on
    failure so the caller can apply the graceful legacy fallback. Carries
    ``ownership.changed`` (a v1-authoritative claim type) into the read path.
    """
    from src.core.program_reality import ProgramReality  # noqa: PLC0415

    reality = ProgramReality.load(
        program_id, programs_root=programs_root or PROGRAMS_ROOT
    )
    return tuple(fa.record for fa in reality.workstreams())


def _load_current_workstreams(
    program_id: str, *, programs_root: Path | None = None
) -> tuple[Workstream, ...]:
    """S-8d: workstream read path with a ProgramReality overlay for the workitem.state family.

    When the ``workitem.state`` family SoR mode is non-legacy (shadow/primary),
    workstreams are projected from ``ProgramReality.workstreams()`` instead of
    the legacy Plane 1 shim — extending the S-8a read-path slice to the
    ``ownership.changed`` v1-authoritative family. A ProgramReality failure
    degrades gracefully to the legacy path with a WARNING (never silent, never
    breaks the read path). In ``legacy`` mode the overlay is never consulted.
    """
    from src.core.fact_sor_state import resolve_family_sor_mode  # noqa: PLC0415

    resolved_root = programs_root or PROGRAMS_ROOT
    workitem_mode = resolve_family_sor_mode(
        program_id, "workitem.state", programs_root=resolved_root
    )
    if workitem_mode == "legacy":
        return _load_current_workstreams_legacy(program_id, programs_root=programs_root)
    try:
        return _load_workstreams_via_reality(program_id, programs_root=programs_root)
    except Exception as exc:  # noqa: BLE001
        # Graceful fallback — S-8d must not break the workstream read path.
        legacy = _load_current_workstreams_legacy(program_id, programs_root=programs_root)
        _LOG.warning("[S-8d] workstream ProgramReality fallback for %s: %s", program_id, exc)
        return legacy


def load_current_workstream_associations(
    program_id: str,
    *,
    programs_root: Path | None = None,
    home_root: Path | None = None,
    db_root: Path | None = None,
) -> tuple[WorkstreamAssociationRecord, ...]:
    """Load all ``workstream.association`` facts for a program (spec §22 Step 6)."""
    return project_workstream_associations(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            home_root=home_root,
            db_root=db_root,
            fact_types=("workstream.association",),
        )
    )


def append_workstream_association_fact(
    program_id: str,
    record: WorkstreamAssociationRecord,
    *,
    programs_root: Path | None = None,
) -> ProgramFactWriteResult:
    """Append a ``workstream.association`` fact revision mirroring one
    ``WorkstreamAssociationRecord`` row from the JSONL ledger.

    The natural key includes ``recorded_at`` so re-running confirm on the same
    issue (with a fresh ``datetime.now()``) is a new fact rather than a
    dedupe-noop, matching the JSONL append-only contract.  Two confirms on
    the same issue with the same ``recorded_at`` (e.g. crash-recovery retry
    of the same transaction) WILL dedupe to ``noop`` and that's the intended
    idempotency guarantee for the fact-store side.
    """
    resolved_db_root = _resolve_fact_db_root(programs_root)
    store = ProgramFactStore(program_id, db_root=resolved_db_root)
    natural_key = (
        f"workstream_assoc:{record.edition}:{record.issue_number}:"
        f"{record.workstream_id}:{record.work_item_id if record.work_item_id is not None else 'none'}:"
        f"{record.source_type}:{record.recorded_at.isoformat()}"
    )
    return store.append_fact(
        ProgramFactInput(
            fact_type="workstream.association",
            scope="program",
            entity_refs=(
                f"WORKSTREAM_ASSOC:{record.edition}:{record.issue_number}:"
                f"{record.workstream_id}:{record.work_item_id if record.work_item_id is not None else '-'}:"
                f"{record.source_type}",
            ),
            payload={
                "recorded_at": _serialize_datetime(record.recorded_at),
                "edition": record.edition,
                "issue_number": record.issue_number,
                "workstream_id": record.workstream_id,
                "source_type": record.source_type,
                "source_slice_id": record.source_slice_id,
                "section_id": record.section_id,
                "work_item_id": record.work_item_id,
                "note": record.note,
            },
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            natural_key=natural_key,
            created_by="vertex.workstream_association_store",
        ),
        recorded_at=record.recorded_at,
    )


def persist_program_fact_snapshot(
    snapshot: ProgramFactSnapshot,
    *,
    recorded_at: datetime | None = None,
    accepted_by: str | None = None,
    home_root: Path | None = None,
    db_root: Path | None = None,
) -> tuple[ProgramFactWriteResult, ...]:
    store = ProgramFactStore(snapshot.program_id, home_root=home_root, db_root=db_root)
    write_recorded_at = recorded_at or snapshot.as_of
    return tuple(
        store.append_fact(
            ProgramFactInput(
                fact_type=fact.fact_type,
                entity_refs=fact.entity_refs,
                payload=fact.payload,
                scope=fact.scope,
                source_signal_ids=fact.source_signal_ids,
                confidence=fact.confidence,
                precedence=fact.precedence,
                review_state=fact.review_state,
                lifecycle_state=fact.lifecycle_state,
                valid_from=fact.valid_from,
                valid_until=fact.valid_until,
                projection_history=fact.projection_history,
                natural_key=fact.natural_key,
                created_by=fact.created_by,
                privacy_classification=fact.privacy_classification,
                accepted_by=accepted_by if accepted_by is not None else fact.accepted_by,
                write_authority=fact.write_authority,
            ),
            recorded_at=write_recorded_at,
        )
        for fact in snapshot.facts
    )


class ProgramFactStore:
    def __init__(
        self,
        program_id: str,
        *,
        home_root: Path | None = None,
        db_root: Path | None = None,
    ) -> None:
        self._program_id = program_id.strip()
        self._db_path = get_program_reality_db_path(self._program_id, home_root=home_root, db_root=db_root)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def program_id(self) -> str:
        return self._program_id

    def initialize(self) -> Path:
        with self._connect() as connection:
            self._initialize_schema(connection)
        return self._db_path

    def append_fact(
        self,
        fact: ProgramFactInput,
        *,
        recorded_at: datetime | None = None,
    ) -> ProgramFactWriteResult:
        if not self._program_id:
            raise ValueError("program_id must not be empty")
        if not fact.fact_type.strip():
            raise ValueError("fact_type must not be empty")
        if not fact.entity_refs:
            raise ValueError("entity_refs must not be empty")

        natural_key = fact.natural_key or build_natural_key(
            fact.fact_type,
            entity_refs=fact.entity_refs,
            scope=fact.scope,
        )
        if not natural_key.strip():
            raise ValueError("natural_key must not be empty")

        recorded_at_value = recorded_at or datetime.now(timezone.utc)
        with self._connect() as connection:
            # W2-6 / G-lineage: idempotency gate — if a non-superseded revision
            # for this domain_event_id already exists, return noop to prevent
            # duplicate facts on replay or at-least-once bridge re-delivery.
            if fact.domain_event_id:
                existing_for_event = connection.execute(
                    """
                    SELECT * FROM program_fact_revisions
                    WHERE program_id = ? AND domain_event_id = ? AND superseded_at IS NULL
                    LIMIT 1
                    """,
                    (self._program_id, fact.domain_event_id),
                ).fetchone()
                if existing_for_event is not None:
                    return ProgramFactWriteResult(
                        revision=self._row_to_revision(existing_for_event), action="noop"
                    )
            active_revision = self._load_active_revision(connection, natural_key)
            if active_revision is None:
                revision = self._insert_revision(
                    connection,
                    fact_id=build_fact_id(self._program_id, natural_key),
                    natural_key=natural_key,
                    fact=fact,
                    recorded_at=recorded_at_value,
                    proposed_against_revision_id=None,
                )
                return ProgramFactWriteResult(revision=revision, action="created")

            if self._revisions_equivalent(active_revision, fact):
                return ProgramFactWriteResult(revision=active_revision, action="noop")

            if _PRECEDENCE_ORDER[fact.precedence] < _PRECEDENCE_ORDER[active_revision.precedence]:
                proposed_revision = self._insert_revision(
                    connection,
                    fact_id=active_revision.fact_id,
                    natural_key=natural_key,
                    fact=ProgramFactInput(
                        fact_type=fact.fact_type,
                        entity_refs=fact.entity_refs,
                        payload=fact.payload,
                        scope=fact.scope,
                        source_signal_ids=fact.source_signal_ids,
                        confidence=fact.confidence,
                        precedence=fact.precedence,
                        review_state=FactReviewState.PROPOSED,
                        lifecycle_state=fact.lifecycle_state,
                        valid_from=fact.valid_from,
                        valid_until=fact.valid_until,
                        projection_history=fact.projection_history,
                        natural_key=natural_key,
                        created_by=fact.created_by,
                        privacy_classification=fact.privacy_classification,
                        accepted_by=fact.accepted_by,
                        write_authority=fact.write_authority,
                        gather_run_id=fact.gather_run_id,
                    ),
                    recorded_at=recorded_at_value,
                    proposed_against_revision_id=active_revision.revision_id,
                )
                return ProgramFactWriteResult(revision=proposed_revision, action="proposed_revision")

            connection.execute(
                """
                UPDATE program_fact_revisions
                SET superseded_at = ?
                WHERE revision_id = ?
                """,
                (_serialize_datetime(recorded_at_value), active_revision.revision_id),
            )
            revision = self._insert_revision(
                connection,
                fact_id=active_revision.fact_id,
                natural_key=natural_key,
                fact=fact,
                recorded_at=recorded_at_value,
                proposed_against_revision_id=None,
            )
            return ProgramFactWriteResult(revision=revision, action="superseded")

    def snapshot(self, *, as_of: datetime | None = None) -> ProgramFactSnapshot:
        snapshot_at = as_of or datetime.now(timezone.utc)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision_id, fact_id, program_id, natural_key, fact_type, scope,
                       entity_refs_json, payload_json, source_signal_ids_json, confidence,
                       precedence, review_state, lifecycle_state, valid_from, valid_until,
                       recorded_at, superseded_at, projection_history_json,
                       proposed_against_revision_id, created_by,
                       privacy_classification, accepted_by, write_authority,
                       domain_event_id, candidate_id,
                       source_document_key, source_hash, evidence_ref,
                       approval_event_id, source_event_id, projector_version, extractor_version,
                       redaction_status, retention_class, gather_run_id
                FROM program_fact_revisions
                WHERE program_id = ?
                  AND review_state = ?
                  AND recorded_at <= ?
                  AND (superseded_at IS NULL OR superseded_at > ?)
                  AND (valid_from IS NULL OR valid_from <= ?)
                  AND (valid_until IS NULL OR valid_until > ?)
                ORDER BY fact_type, natural_key
                """,
                (
                    self._program_id,
                    FactReviewState.ACCEPTED.value,
                    _serialize_datetime(snapshot_at),
                    _serialize_datetime(snapshot_at),
                    _serialize_datetime(snapshot_at),
                    _serialize_datetime(snapshot_at),
                ),
            ).fetchall()
        return ProgramFactSnapshot(
            program_id=self._program_id,
            as_of=snapshot_at,
            facts=tuple(self._row_to_revision(row) for row in rows),
        )

    def pin_snapshot(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> FactSnapshotPin:
        pin_created_at = created_at or datetime.now(timezone.utc)
        # Pin the live accepted snapshot at confirm/report time even when the
        # caller supplies a synthetic created_at for deterministic artifacts.
        snapshot = self.snapshot()
        pinned_recorded_at = max((fact.recorded_at for fact in snapshot.facts), default=None)
        pin = FactSnapshotPin(
            snapshot_id=f"pfs_{uuid4().hex}",
            program_id=self._program_id,
            created_at=pin_created_at,
            pinned_recorded_at=pinned_recorded_at,
            pinned_revision_count=len(snapshot.facts),
            metadata=dict(metadata or {}),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO program_fact_snapshot_pins (
                    snapshot_id, program_id, created_at, pinned_recorded_at,
                    pinned_revision_count, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pin.snapshot_id,
                    pin.program_id,
                    _serialize_datetime(pin.created_at),
                    _serialize_datetime(pin.pinned_recorded_at),
                    pin.pinned_revision_count,
                    json.dumps(pin.metadata, sort_keys=True),
                ),
            )
        return pin

    def load_snapshot_pin(self, snapshot_id: str) -> FactSnapshotPin | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, program_id, created_at, pinned_recorded_at,
                       pinned_revision_count, metadata_json
                FROM program_fact_snapshot_pins
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return FactSnapshotPin(
            snapshot_id=row["snapshot_id"],
            program_id=row["program_id"],
            created_at=_deserialize_datetime(row["created_at"]) or datetime.now(timezone.utc),
            pinned_recorded_at=_deserialize_datetime(row["pinned_recorded_at"]),
            pinned_revision_count=int(row["pinned_revision_count"]),
            metadata=_loads_json_object(row["metadata_json"]),
        )

    def detect_drift(self, snapshot_id: str) -> tuple[ProgramFactRevision, ...]:
        pin = self.load_snapshot_pin(snapshot_id)
        if pin is None:
            raise KeyError(f"Unknown snapshot pin: {snapshot_id}")
        if pin.pinned_recorded_at is None:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision_id, fact_id, program_id, natural_key, fact_type, scope,
                       entity_refs_json, payload_json, source_signal_ids_json, confidence,
                       precedence, review_state, lifecycle_state, valid_from, valid_until,
                       recorded_at, superseded_at, projection_history_json,
                       proposed_against_revision_id, created_by,
                       privacy_classification, accepted_by, write_authority,
                       domain_event_id, candidate_id,
                       source_document_key, source_hash, evidence_ref,
                       approval_event_id, source_event_id, projector_version, extractor_version,
                       redaction_status, retention_class, gather_run_id
                FROM program_fact_revisions
                WHERE program_id = ?
                  AND review_state = ?
                  AND recorded_at > ?
                ORDER BY recorded_at, natural_key
                """,
                (
                    self._program_id,
                    FactReviewState.ACCEPTED.value,
                    _serialize_datetime(pin.pinned_recorded_at),
                ),
            ).fetchall()
        return tuple(self._row_to_revision(row) for row in rows)

    def list_proposed_revisions(self) -> tuple[ProgramFactRevision, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision_id, fact_id, program_id, natural_key, fact_type, scope,
                       entity_refs_json, payload_json, source_signal_ids_json, confidence,
                       precedence, review_state, lifecycle_state, valid_from, valid_until,
                       recorded_at, superseded_at, projection_history_json,
                       proposed_against_revision_id, created_by,
                       privacy_classification, accepted_by, write_authority,
                       domain_event_id, candidate_id,
                       source_document_key, source_hash, evidence_ref,
                       approval_event_id, source_event_id, projector_version, extractor_version,
                       redaction_status, retention_class, gather_run_id
                FROM program_fact_revisions
                WHERE program_id = ?
                  AND review_state = ?
                ORDER BY recorded_at, natural_key
                """,
                (self._program_id, FactReviewState.PROPOSED.value),
            ).fetchall()
        return tuple(self._row_to_revision(row) for row in rows)

    def _load_active_revision(
        self,
        connection: sqlite3.Connection,
        natural_key: str,
    ) -> ProgramFactRevision | None:
        row = connection.execute(
            """
            SELECT revision_id, fact_id, program_id, natural_key, fact_type, scope,
                   entity_refs_json, payload_json, source_signal_ids_json, confidence,
                   precedence, review_state, lifecycle_state, valid_from, valid_until,
                   recorded_at, superseded_at, projection_history_json,
                   proposed_against_revision_id, created_by,
                   privacy_classification, accepted_by, write_authority,
                   domain_event_id, candidate_id,
                   source_document_key, source_hash, evidence_ref,
                   approval_event_id, source_event_id, projector_version, extractor_version,
                   redaction_status, retention_class, gather_run_id
            FROM program_fact_revisions
            WHERE program_id = ?
              AND natural_key = ?
              AND review_state = ?
              AND superseded_at IS NULL
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            (self._program_id, natural_key, FactReviewState.ACCEPTED.value),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_revision(row)

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        *,
        fact_id: str,
        natural_key: str,
        fact: ProgramFactInput,
        recorded_at: datetime,
        proposed_against_revision_id: str | None,
    ) -> ProgramFactRevision:
        revision_id = f"pfr_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO program_fact_revisions (
                revision_id, fact_id, program_id, natural_key, fact_type, scope,
                entity_refs_json, payload_json, source_signal_ids_json, confidence,
                precedence, review_state, lifecycle_state, valid_from, valid_until,
                recorded_at, superseded_at, projection_history_json,
                proposed_against_revision_id, created_by,
                privacy_classification, accepted_by, write_authority,
                domain_event_id, candidate_id,
                source_document_key, source_hash, evidence_ref,
                approval_event_id, source_event_id, projector_version, extractor_version,
                redaction_status, retention_class, gather_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                fact_id,
                self._program_id,
                natural_key,
                fact.fact_type,
                fact.scope,
                json.dumps(list(fact.entity_refs), sort_keys=True),
                json.dumps(fact.payload, sort_keys=True),
                json.dumps(list(fact.source_signal_ids), sort_keys=True),
                fact.confidence,
                fact.precedence.value,
                fact.review_state.value,
                fact.lifecycle_state.value,
                _serialize_datetime(fact.valid_from),
                _serialize_datetime(fact.valid_until),
                _serialize_datetime(recorded_at),
                None,
                json.dumps(list(fact.projection_history), sort_keys=True),
                proposed_against_revision_id,
                fact.created_by,
                fact.privacy_classification,
                fact.accepted_by,
                fact.write_authority,
                fact.domain_event_id,
                fact.candidate_id,
                fact.source_document_key,
                fact.source_hash,
                fact.evidence_ref,
                fact.approval_event_id,
                fact.source_event_id,
                fact.projector_version,
                fact.extractor_version,
                fact.redaction_status,
                fact.retention_class,
                fact.gather_run_id,
            ),
        )
        row = connection.execute(
            """
            SELECT revision_id, fact_id, program_id, natural_key, fact_type, scope,
                   entity_refs_json, payload_json, source_signal_ids_json, confidence,
                   precedence, review_state, lifecycle_state, valid_from, valid_until,
                   recorded_at, superseded_at, projection_history_json,
                   proposed_against_revision_id, created_by,
                   privacy_classification, accepted_by, write_authority,
                   domain_event_id, candidate_id,
                   source_document_key, source_hash, evidence_ref,
                   approval_event_id, source_event_id, projector_version, extractor_version,
                   redaction_status, retention_class, gather_run_id
            FROM program_fact_revisions
            WHERE revision_id = ?
            """,
            (revision_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Inserted revision {revision_id} was not found")
        return self._row_to_revision(row)

    def _revisions_equivalent(self, active_revision: ProgramFactRevision, fact: ProgramFactInput) -> bool:
        return (
            active_revision.fact_type == fact.fact_type
            and active_revision.scope == fact.scope
            and active_revision.entity_refs == fact.entity_refs
            and active_revision.payload == fact.payload
            and active_revision.source_signal_ids == fact.source_signal_ids
            and active_revision.confidence == fact.confidence
            and active_revision.precedence == fact.precedence
            and active_revision.lifecycle_state == fact.lifecycle_state
            and active_revision.valid_from == fact.valid_from
            and active_revision.valid_until == fact.valid_until
            and active_revision.projection_history == fact.projection_history
            and active_revision.write_authority == fact.write_authority
        )

    def _row_to_revision(self, row: sqlite3.Row) -> ProgramFactRevision:
        row_dict = dict(row)
        return ProgramFactRevision(
            revision_id=row_dict["revision_id"],
            fact_id=row_dict["fact_id"],
            program_id=row_dict["program_id"],
            natural_key=row_dict["natural_key"],
            fact_type=row_dict["fact_type"],
            scope=row_dict["scope"],
            entity_refs=tuple(_loads_json_list(row_dict["entity_refs_json"])),
            payload=_loads_json_object(row_dict["payload_json"]),
            source_signal_ids=tuple(_loads_json_list(row_dict["source_signal_ids_json"])),
            confidence=row_dict["confidence"],
            precedence=FactPrecedence(row_dict["precedence"]),
            review_state=FactReviewState(row_dict["review_state"]),
            lifecycle_state=FactLifecycleState(row_dict["lifecycle_state"]),
            valid_from=_deserialize_datetime(row_dict["valid_from"]),
            valid_until=_deserialize_datetime(row_dict["valid_until"]),
            recorded_at=_deserialize_datetime(row_dict["recorded_at"]) or datetime.now(timezone.utc),
            superseded_at=_deserialize_datetime(row_dict["superseded_at"]),
            projection_history=tuple(_loads_json_list_of_objects(row_dict["projection_history_json"])),
            proposed_against_revision_id=row_dict["proposed_against_revision_id"],
            created_by=row_dict["created_by"],
            privacy_classification=row_dict.get("privacy_classification") or "internal",
            accepted_by=row_dict.get("accepted_by"),
            write_authority=row_dict.get("write_authority") or "human",
            domain_event_id=row_dict.get("domain_event_id"),
            candidate_id=row_dict.get("candidate_id"),
            source_document_key=row_dict.get("source_document_key"),
            source_hash=row_dict.get("source_hash"),
            evidence_ref=row_dict.get("evidence_ref"),
            approval_event_id=row_dict.get("approval_event_id"),
            source_event_id=row_dict.get("source_event_id"),
            projector_version=row_dict.get("projector_version"),
            extractor_version=row_dict.get("extractor_version"),
            redaction_status=row_dict.get("redaction_status") or "active",
            retention_class=row_dict.get("retention_class") or "pilot_local",
            gather_run_id=row_dict.get("gather_run_id"),
        )

    def purge_facts_after(self, cutoff: datetime) -> int:
        """Delete all program_fact_revisions rows recorded after ``cutoff``.

        Used by ``vertex rollback`` (PB-36) to bring the SQLite ProgramFactStore
        back in sync with a restored filesystem checkpoint.  Returns the number
        of rows deleted.

        Only rows whose ``recorded_at`` is strictly after ``cutoff`` are removed;
        rows exactly at the cutoff are retained (consistent with snapshot semantics).
        """
        cutoff_iso = _serialize_datetime(cutoff)
        if cutoff_iso is None:
            return 0
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM program_fact_revisions WHERE program_id = ? AND recorded_at > ?",
                (self._program_id, cutoff_iso),
            )
            return cursor.rowcount

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection_with_retry()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _open_connection_with_retry(
        self, *, max_attempts: int = 8, base_delay_s: float = 0.02
    ) -> sqlite3.Connection:
        """Open a connection and initialize schema, retrying the whole
        connect+pragma+schema-init sequence on ``sqlite3.OperationalError:
        database is locked``/``database is busy``.

        ADF-W5.9 (multi-program concurrency testing): ``PRAGMA busy_timeout``
        only covers lock contention on statements issued *after* it takes
        effect. Converting a brand-new database file to WAL mode (the very
        next statement) needs a brief exclusive lock, so multiple
        threads/processes racing to open the SAME not-yet-existing
        fact-store db for the first time (reproduced by
        ``tests/contracts/test_multi_program_concurrency.py``'s concurrent
        fact-store write stress test) can hit "database is locked" on that
        one statement even with a generous busy_timeout -- the same failure
        mode ``src.core._db.open_program_db_with_retry`` was built to
        absorb for ``workspace_lease.py``. Bounded, jittered exponential
        backoff; re-raises any non-lock/busy ``OperationalError``
        immediately, and the last error after ``max_attempts`` is exhausted.
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(max_attempts):
            connection = sqlite3.connect(self._db_path)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                self._initialize_schema(connection)
                return connection
            except sqlite3.OperationalError as error:
                connection.close()
                message = str(error).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = error
                time.sleep(base_delay_s * (2**attempt) + random.uniform(0, base_delay_s))
        assert last_error is not None
        raise last_error

    def _initialize_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS program_fact_revisions (
                revision_id TEXT PRIMARY KEY,
                fact_id TEXT NOT NULL,
                program_id TEXT NOT NULL,
                natural_key TEXT NOT NULL,
                fact_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                entity_refs_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source_signal_ids_json TEXT NOT NULL,
                confidence TEXT,
                precedence TEXT NOT NULL,
                review_state TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                valid_from TEXT,
                valid_until TEXT,
                recorded_at TEXT NOT NULL,
                superseded_at TEXT,
                projection_history_json TEXT NOT NULL,
                proposed_against_revision_id TEXT,
                created_by TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_program_fact_current
            ON program_fact_revisions(program_id, natural_key, review_state, superseded_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_program_fact_recorded_at
            ON program_fact_revisions(program_id, recorded_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS program_fact_snapshot_pins (
                snapshot_id TEXT PRIMARY KEY,
                program_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                pinned_recorded_at TEXT,
                pinned_revision_count INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        # FR-SG-56: idempotent column migrations for fields added after initial schema
        try:
            connection.execute(
                "ALTER TABLE program_fact_revisions ADD COLUMN privacy_classification TEXT DEFAULT 'internal'"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            connection.execute(
                "ALTER TABLE program_fact_revisions ADD COLUMN accepted_by TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            connection.execute(
                "ALTER TABLE program_fact_revisions ADD COLUMN write_authority TEXT DEFAULT 'human'"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # W2-6 / G-lineage: domain_event_id + candidate_id for fact traceability
        try:
            connection.execute(
                "ALTER TABLE program_fact_revisions ADD COLUMN domain_event_id TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            connection.execute(
                "ALTER TABLE program_fact_revisions ADD COLUMN candidate_id TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_program_fact_domain_event
            ON program_fact_revisions(domain_event_id)
            WHERE domain_event_id IS NOT NULL
            """
        )
        # S-3 / G-lineage: 7 new provenance + 2 privacy/retention columns (12-field envelope)
        # All use DEFAULT values so ALTER TABLE ADD COLUMN succeeds on existing DBs.
        _s3_columns: list[tuple[str, str]] = [
            ("source_document_key",  "TEXT"),          # SHA-256(Message-ID)
            ("source_hash",          "TEXT"),          # canonical text hash
            ("evidence_ref",         "TEXT"),          # vault content hash
            ("approval_event_id",    "TEXT"),          # audit event id
            ("source_event_id",      "TEXT"),          # outbox idempotency key (S-1)
            ("projector_version",    "TEXT"),          # replay migration (S-5)
            ("extractor_version",    "TEXT"),          # quality feedback (S-3)
            ("redaction_status",     "TEXT DEFAULT 'active'"),
            ("retention_class",      "TEXT DEFAULT 'pilot_local'"),
        ]
        for col_name, col_def in _s3_columns:
            try:
                connection.execute(
                    f"ALTER TABLE program_fact_revisions ADD COLUMN {col_name} {col_def}"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
        # Index for source_event_id lookups (outbox idempotency, S-1/S-4)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_program_fact_source_event
            ON program_fact_revisions(source_event_id)
            WHERE source_event_id IS NOT NULL
            """
        )
        # D-13 rule 4 (specs/armada.md): gather-run.v1 run_id lineage, so a
        # future activated reader can filter facts to committed runs only.
        try:
            connection.execute(
                "ALTER TABLE program_fact_revisions ADD COLUMN gather_run_id TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_program_fact_gather_run
            ON program_fact_revisions(gather_run_id)
            WHERE gather_run_id IS NOT NULL
            """
        )
        connection.commit()



def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _deserialize_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _loads_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object, got {type(loaded)!r}")
    return loaded


def _loads_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    loaded = json.loads(raw)
    if not isinstance(loaded, list):
        raise ValueError(f"Expected JSON list, got {type(loaded)!r}")
    return loaded


def _loads_json_list_of_objects(raw: str | None) -> list[dict[str, Any]]:
    values = _loads_json_list(raw)
    objects: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object list, got element {type(value)!r}")
        objects.append(value)
    return objects


def _load_current_state_shim_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    fact_types: tuple[str, ...] | None = None,
    programs_root: Path | None,
    editions_root: Path | None,
    archive_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    requested_fact_types = {
        fact_type.strip().lower()
        for fact_type in (
            fact_types
            or (
                "action.item",
                "claim.entry",
                "claim.status_update",
                "decision.ask",
                "baseline.trust_event",
                "skip.issue",
                "event.issue.skip",
                "assumption.entry",
                "decision.entry",
                "risk.entry",
                "dependency.link",
                "milestone.entry",
                "workstream.entry",
                "workstream.association",
            )
        )
    }
    shim_facts = [
        *(
            _build_action_item_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "action.item" in requested_fact_types
            else ()
        ),
        *(
            _build_claim_entry_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "claim.entry" in requested_fact_types
            else ()
        ),
        *(
            _build_claim_status_update_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "claim.status_update" in requested_fact_types
            else ()
        ),
        *(
            _build_decision_ask_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "decision.ask" in requested_fact_types
            else ()
        ),
        *(
            _build_baseline_trust_event_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "baseline.trust_event" in requested_fact_types
            else ()
        ),
        *(
            _build_skip_issue_facts(
                program_id,
                recorded_at=recorded_at,
                programs_root=programs_root,
                editions_root=editions_root,
                archive_root=archive_root,
            )
            # Phase 6 §22 Step 7: the shim emits the legacy `skip.issue`
            # fact_type. Trigger it on either legacy or new fact_type
            # requests so callers asking for `event.issue.skip` still
            # see the archive-seeded shim data (and the dedupe in
            # `project_skip_issues` keeps the result unique).
            if "skip.issue" in requested_fact_types or "event.issue.skip" in requested_fact_types
            else ()
        ),
        *(
            _build_assumption_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "assumption.entry" in requested_fact_types
            else ()
        ),
        *(
            _build_decision_entry_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "decision.entry" in requested_fact_types
            else ()
        ),
        *(
            _build_risk_entry_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "risk.entry" in requested_fact_types
            else ()
        ),
        *(
            _build_dependency_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "dependency.link" in requested_fact_types
            else ()
        ),
        *(
            _build_workstream_entry_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "workstream.entry" in requested_fact_types
            else ()
        ),
        *(
            _build_workstream_association_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            if "workstream.association" in requested_fact_types
            else ()
        ),
    ]
    # milestone.entry: propagate ConfigError in targeted-load mode so callers
    # (e.g. MilestoneStage) can detect and surface a warning; soft-fail in
    # full-load mode (fact_types=None) so ProgramReality.load() stays healthy
    # even when milestones.yaml is absent or malformed.
    if "milestone.entry" in requested_fact_types:
        try:
            shim_facts.extend(
                _build_milestone_entry_facts(program_id, recorded_at=recorded_at, programs_root=programs_root)
            )
        except ConfigError:
            if fact_types is not None:
                raise
    return tuple(sorted(shim_facts, key=lambda fact: (fact.fact_type, fact.natural_key)))


def _build_action_item_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    try:
        from src.core.action_tracker import load_actions  # lazy to break import cycle
        actions = load_actions(program_id, programs_root=root)
    except ConfigError:
        return ()
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="action.item",
            entity_refs=(f"ACTION:{action.id}",),
            payload={
                "id": action.id,
                "program_id": action.program_id,
                "text": action.text,
                "owner_alias": action.owner_alias,
                "due_date": _serialize_date(action.due_date),
                "status": action.status.value,
                "source_signal_id": action.source_signal_id,
                "source_type": action.source_type.value,
                "linked_work_item_ids": list(action.linked_work_item_ids),
                "linked_claim_id": action.linked_claim_id,
                "linked_risk_id": action.linked_risk_id,
                "workstream_id": action.workstream_id,
                "created_at": _serialize_datetime(action.created_at),
                "resolved_at": _serialize_datetime(action.resolved_at),
                "resolution_note": action.resolution_note,
            },
            recorded_at=recorded_at,
        )
        for action in actions
    )


def _build_claim_entry_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="claim.entry",
            entity_refs=tuple(entry.entity_refs) or (f"CLAIM:{entry.id}",),
            payload={
                "id": entry.id,
                "program_id": entry.program_id,
                "edition_id": entry.edition_id,
                "issue_number": entry.issue_number,
                "workstream_id": entry.workstream_id,
                "text": entry.text,
                "entity_refs": list(entry.entity_refs),
                "claim_date": _serialize_date(entry.claim_date),
                "owner_alias": entry.owner_alias,
                "due_date": _serialize_date(entry.due_date),
                "status": entry.status,
                "contradiction_status": entry.contradiction_status,
                "source_confidence_tier": entry.source_confidence_tier,
                "last_validated_date": _serialize_date(entry.last_validated_date),
            },
            recorded_at=recorded_at,
        )
        for entry in load_open_claims(program_id, programs_root=root)
    )


def _build_claim_status_update_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="claim.status_update",
            entity_refs=(f"CLAIM_STATUS:{update.claim_id}:{update.updated_at.isoformat()}",),
            payload={
                "claim_id": update.claim_id,
                "new_status": update.new_status,
                "updated_at": _serialize_datetime(update.updated_at),
                "updated_by": update.updated_by,
                "note": update.note,
                "record_type": update.record_type,
            },
            recorded_at=recorded_at,
        )
        for update in load_claim_status_updates(program_id, programs_root=root)
    )


def _build_decision_ask_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="decision.ask",
            entity_refs=tuple(entry.entity_refs) or (f"ASK:{entry.id}",),
            payload={
                "id": entry.id,
                "program_id": entry.program_id,
                "edition_id": entry.edition_id,
                "issue_number": entry.issue_number,
                "text": entry.text,
                "entity_refs": list(entry.entity_refs),
                "ask_date": _serialize_date(entry.ask_date),
                "owner_alias": entry.owner_alias,
                "status": entry.status,
                "resolution": entry.resolution,
                "expiry_date": _serialize_date(entry.expiry_date),
                "resurfacing_policy": _serialize_resurfacing_policy(entry.resurfacing_policy),
                "affected_milestone_ids": list(entry.affected_milestone_ids),
                "last_touched_at": _serialize_datetime(entry.last_touched_at),
            },
            recorded_at=recorded_at,
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
        )
        for entry in load_open_decision_asks(program_id, programs_root=root)
    )


def _build_baseline_trust_event_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    baseline = load_trusted_baseline_for_program(program_id, programs_root=root)
    if baseline is None:
        return ()
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="baseline.trust_event",
            entity_refs=(f"BASELINE_EVENT:{entry.issue}:{entry.action}:{entry.at.isoformat()}",),
            payload={
                "issue": entry.issue,
                "at": _serialize_datetime(entry.at),
                "by": entry.by,
                "action": entry.action,
                "reason": entry.reason,
            },
            recorded_at=recorded_at,
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        )
        for entry in baseline.history
    )


def _build_skip_issue_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
    editions_root: Path | None,
    archive_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="skip.issue",
            entity_refs=(f"SKIP:{entry.edition_id}:{entry.issue_number}",),
            payload={
                "edition_id": entry.edition_id,
                "issue_number": entry.issue_number,
                "generated_at": _serialize_datetime(entry.generated_at),
                "reason": entry.reason,
            },
            recorded_at=recorded_at,
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        )
        for entry in load_skipped_issues_for_program(
            program_id,
            editions_root=editions_root,
            programs_root=root,
            archive_root=resolved_archive_root,
        )
    )


def _build_assumption_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="assumption.entry",
            entity_refs=(f"ASSUMPTION:{entry.id}",),
            payload={
                "id": entry.id,
                "program_id": entry.program_id,
                "text": entry.text,
                "validation_method": entry.validation_method,
                "validation_due": _serialize_date(entry.validation_due),
                "status": entry.status.value,
                "category": entry.category,
                "linked_risk_id": entry.linked_risk_id,
                "linked_workstream_ids": list(entry.linked_workstream_ids),
                "linked_milestone_id": entry.linked_milestone_id,
                "owner_alias": entry.owner_alias,
                "identified_date": _serialize_date(entry.identified_date),
                "entity_refs": list(entry.entity_refs),
                "resolved_date": _serialize_date(entry.resolved_date),
                "linked_milestone_ids": list(entry.linked_milestone_ids),
                "last_reviewed_date": _serialize_date(entry.last_reviewed_date),
            },
            recorded_at=recorded_at,
        )
        for entry in load_assumptions(program_id, programs_root=root)
    )


def _build_risk_entry_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="risk.entry",
            entity_refs=(f"RISK:{entry.id}",),
            payload={
                "id": entry.id,
                "program_id": entry.program_id,
                "title": entry.title,
                "description": entry.description,
                "probability": entry.probability.value,
                "impact": entry.impact.value,
                "category": entry.category.value,
                "owner_alias": entry.owner_alias,
                "mitigation_plan": entry.mitigation_plan,
                "mitigation_due_date": _serialize_date(entry.mitigation_due_date),
                "linked_workstream_ids": list(entry.linked_workstream_ids),
                "linked_work_item_ids": list(entry.linked_work_item_ids),
                "linked_milestone_ids": list(entry.linked_milestone_ids),
                "linked_claim_ids": list(entry.linked_claim_ids),
                "linked_action_ids": list(entry.linked_action_ids),
                "status": entry.status.value,
                "identified_date": _serialize_date(entry.identified_date),
                "identified_in_vertex_issue": entry.identified_in_vertex_issue,
                "last_reviewed_date": _serialize_date(entry.last_reviewed_date),
                "entity_refs": list(entry.entity_refs),
                "source_signal_ids": list(entry.source_signal_ids),
            },
            recorded_at=recorded_at,
        )
        for entry in load_risk_register(program_id, programs_root=root)
    )


def _build_decision_entry_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="decision.entry",
            entity_refs=tuple(entry.entity_refs) or (f"DECISION:{entry.id}",),
            payload={
                "id": entry.id,
                "program_id": entry.program_id,
                "title": entry.title,
                "context": entry.context,
                "decision": entry.decision,
                "rationale": entry.rationale,
                "alternatives_considered": list(entry.alternatives_considered),
                "decided_by": entry.decided_by,
                "decision_date": _serialize_date(entry.decision_date),
                "status": entry.status.value,
                "superseded_by": entry.superseded_by,
                "linked_claim_id": entry.linked_claim_id,
                "linked_risk_id": entry.linked_risk_id,
                "linked_action_ids": list(entry.linked_action_ids),
                "workstream_id": entry.workstream_id,
                "entity_refs": list(entry.entity_refs),
                "review_by": _serialize_date(entry.review_by),
                "linked_milestone_ids": list(entry.linked_milestone_ids),
                "last_reviewed_date": _serialize_date(entry.last_reviewed_date),
            },
            recorded_at=recorded_at,
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        )
        for entry in load_decisions(program_id, programs_root=root)
    )


def _build_dependency_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="dependency.link",
            entity_refs=(f"DEPENDENCY:{dependency.id}",),
            payload={
                "id": dependency.id,
                "from_program_id": dependency.from_program_id,
                "from_workstream_id": dependency.from_workstream_id,
                "from_item_id": dependency.from_item_id,
                "from_milestone_id": dependency.from_milestone_id,
                "to_program_id": dependency.to_program_id,
                "to_workstream_id": dependency.to_workstream_id,
                "to_item_id": dependency.to_item_id,
                "to_milestone_id": dependency.to_milestone_id,
                "dependency_type": dependency.dependency_type.value,
                "risk_if_broken": dependency.risk_if_broken,
                "mitigation": dependency.mitigation,
                "status": dependency.status.value,
                "owner_alias": dependency.owner_alias,
                "resolution_path": dependency.resolution_path,
                "planned_resolution_date": _serialize_date(dependency.planned_resolution_date),
                "schedule_status": (
                    dependency.schedule_status.value if dependency.schedule_status is not None else None
                ),
                "linked_risk_ids": list(dependency.linked_risk_ids),
            },
            recorded_at=recorded_at,
        )
        for dependency in load_dependencies(program_id, programs_root=root)
    )


def _build_milestone_entry_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="milestone.entry",
            entity_refs=(f"MILESTONE:{entry.id}",),
            payload={
                "id": entry.id,
                "program_id": entry.program_id,
                "name": entry.name,
                "target_date": _serialize_date(entry.target_date),
                "owner_alias": entry.owner_alias,
                "status": entry.status.value,
                "exit_criteria": list(entry.exit_criteria),
                "linked_workstream_ids": list(entry.linked_workstream_ids),
                "linked_work_item_ids": list(entry.linked_work_item_ids),
                "notes": entry.notes,
                "last_reviewed_date": _serialize_date(entry.last_reviewed_date),
            },
            recorded_at=recorded_at,
        )
        for entry in load_milestones(program_id, programs_root=root)
    )


def _build_workstream_entry_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    root = programs_root or PROGRAMS_ROOT
    workstreams_path = root / program_id / "workstreams.yaml"
    if not workstreams_path.exists():
        return ()
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="workstream.entry",
            entity_refs=(f"WS:{entry.id}",),
            payload={
                "id": entry.id,
                "name": entry.name,
                "owner_person_id": entry.owner_person_id,
                "status": entry.status,
                "aliases": list(entry.aliases),
                "area_paths": list(entry.area_paths),
                "ado_team": entry.ado_team,
                "ado_pipeline_ids": list(entry.ado_pipeline_ids),
                "ado_repository_ids": list(entry.ado_repository_ids),
                "pm_owner": entry.pm_owner,
                "eng_owner": entry.eng_owner,
                "accountable_owner": entry.accountable_owner,
                "accountable_email": entry.accountable_email,
                "responsible_owners": list(entry.responsible_owners),
                "consulted_owners": list(entry.consulted_owners),
                "informed_owners": list(entry.informed_owners),
                "dri_email": entry.dri_email,
                "alternate_owner": entry.alternate_owner,
                "always_notify": list(entry.always_notify),
                "description": entry.description,
                "why_it_matters": entry.why_it_matters,
                "history_summary": entry.history_summary,
                "leadership_sensitivity": entry.leadership_sensitivity,
                "current_blocker": entry.current_blocker,
                "ado_saved_query_ids": list(entry.ado_saved_query_ids),
                "last_reviewed_date": _serialize_date(entry.last_reviewed_date),
                "signal_sources": _serialize_workstream_signal_sources(entry.signal_sources),
            },
            recorded_at=recorded_at,
        )
        for entry in _parse_workstreams(load_yaml_mapping(workstreams_path), workstreams_path)
    )


def _build_workstream_association_facts(
    program_id: str,
    *,
    recorded_at: datetime,
    programs_root: Path | None,
) -> tuple[ProgramFactRevision, ...]:
    """Build ``workstream.association`` shim facts from the workstream-association ledger.

    Spec §22 Step 6: this is the legacy-read shim projection of the
    workstream-association ledger (the file-backed sidecar under
    ``programs/<prog>/journal/``).  When a program has flipped to
    ``primary`` SoR mode the live writer (``workstream_association_store``)
    emits a fact revision in the same call, so the shim and the live
    fact store agree on the natural key and dedup transparently.

    The shim uses the *same* natural key as
    ``append_workstream_association_fact`` (``workstream_assoc:{edition}:{issue}:{ws}:{wi_or_none}:{src}:{recorded_at}``)
    so a program that has dual-written finds the live fact already on disk
    and the SQL-level dedup collapses shim + live to one revision; a
    program still in ``legacy`` SoR mode (no live writes) still gets
    exactly one shim fact per ledger row.
    """
    root = programs_root or PROGRAMS_ROOT
    records = read_workstream_association_records(program_id, programs_root=root)
    return tuple(
        _shim_fact_revision(
            program_id=program_id,
            fact_type="workstream.association",
            entity_refs=(
                f"WORKSTREAM_ASSOC:{record.edition}:{record.issue_number}:"
                f"{record.workstream_id}:{record.work_item_id if record.work_item_id is not None else '-'}:"
                f"{record.source_type}",
            ),
            payload={
                "recorded_at": _serialize_datetime(record.recorded_at),
                "edition": record.edition,
                "issue_number": record.issue_number,
                "workstream_id": record.workstream_id,
                "source_type": record.source_type,
                "source_slice_id": record.source_slice_id,
                "section_id": record.section_id,
                "work_item_id": record.work_item_id,
                "note": record.note,
            },
            # Use the same natural key as the live writer so shim + live
            # collapse at the SQL level (dedup by natural_key in the
            # program_fact_revisions table).  See
            # ``append_workstream_association_fact`` for the formula.
            natural_key_override=(
                f"workstream_assoc:{record.edition}:{record.issue_number}:"
                f"{record.workstream_id}:{record.work_item_id if record.work_item_id is not None else 'none'}:"
                f"{record.source_type}:{record.recorded_at.isoformat()}"
            ),
            recorded_at=recorded_at,
        )
        for record in records
    )


def _shim_fact_revision(
    *,
    program_id: str,
    fact_type: str,
    entity_refs: tuple[str, ...],
    payload: dict[str, Any],
    recorded_at: datetime,
    precedence: FactPrecedence = FactPrecedence.ACTIVE_PM_JUDGMENT,
    natural_key_override: str | None = None,
) -> ProgramFactRevision:
    natural_key = natural_key_override or build_natural_key(
        fact_type, entity_refs=entity_refs, scope="program"
    )
    digest = natural_key[:16]
    return ProgramFactRevision(
        revision_id=f"shimr_{digest}",
        fact_id=f"shimf_{digest}",
        program_id=program_id,
        natural_key=natural_key,
        fact_type=fact_type,
        scope="program",
        entity_refs=entity_refs,
        payload=payload,
        source_signal_ids=(),
        confidence=None,
        precedence=precedence,
        review_state=FactReviewState.ACCEPTED,
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=recorded_at,
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="vertex.load_program_facts",
    )


def _action_item_from_fact(fact: ProgramFactRevision) -> ActionItem:
    payload = fact.payload
    created_at = _deserialize_datetime(str(payload["created_at"]))
    if created_at is None:
        raise ValueError("missing created_at")
    return ActionItem(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        text=str(payload["text"]),
        owner_alias=str(payload["owner_alias"]),
        due_date=_deserialize_date(payload.get("due_date")),
        status=ActionStatus.from_string(str(payload["status"])),
        source_signal_id=_deserialize_optional_string(payload.get("source_signal_id")),
        source_type=ActionSourceType.from_string(str(payload["source_type"])),
        linked_work_item_ids=tuple(int(value) for value in payload.get("linked_work_item_ids") or ()),
        linked_claim_id=_deserialize_optional_string(payload.get("linked_claim_id")),
        linked_risk_id=_deserialize_optional_string(payload.get("linked_risk_id")),
        workstream_id=_deserialize_optional_string(payload.get("workstream_id")),
        created_at=created_at,
        resolved_at=_deserialize_datetime(_deserialize_optional_string(payload.get("resolved_at"))),
        resolution_note=_deserialize_optional_string(payload.get("resolution_note")),
        fact_id=fact.fact_id,
        last_validated_at=fact.recorded_at,
    )


def _claim_entry_from_fact(fact: ProgramFactRevision) -> ClaimEntry:
    payload = fact.payload
    return ClaimEntry(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        edition_id=str(payload["edition_id"]),
        issue_number=int(payload["issue_number"]),
        workstream_id=_deserialize_optional_string(payload.get("workstream_id")),
        text=str(payload["text"]),
        entity_refs=tuple(str(value) for value in payload.get("entity_refs") or ()),
        claim_date=_deserialize_required_date(payload.get("claim_date"), field_name="claim_date"),
        owner_alias=_deserialize_optional_string(payload.get("owner_alias")),
        due_date=_deserialize_date(payload.get("due_date")),
        status="open",
        contradiction_status=str(payload.get("contradiction_status") or "ok"),  # type: ignore[arg-type]
        source_confidence_tier=str(payload.get("source_confidence_tier") or "low"),  # type: ignore[arg-type]
        last_validated_date=_deserialize_date(payload.get("last_validated_date")),
    )


def _claim_status_update_from_fact(fact: ProgramFactRevision) -> ClaimStatusUpdate:
    payload = fact.payload
    updated_at = _deserialize_datetime(_deserialize_optional_string(payload.get("updated_at")))
    if updated_at is None:
        raise ValueError("missing claim status update timestamp")
    new_status = str(payload.get("new_status") or "open")
    if new_status not in {"open", "met", "contradicted", "stale", "deferred", "resolved"}:
        raise ValueError(f"invalid claim status update status: {new_status}")
    return ClaimStatusUpdate(
        claim_id=str(payload["claim_id"]),
        new_status=new_status,  # type: ignore[arg-type]
        updated_at=updated_at,
        updated_by=str(payload["updated_by"]),
        note=_deserialize_optional_string(payload.get("note")),
        record_type="status_update",
    )


def _latest_claim_status_updates(snapshot: ProgramFactSnapshot) -> dict[str, str]:
    latest: dict[str, ClaimStatusUpdate] = {}
    for update in project_claim_status_updates(snapshot):
        latest[update.claim_id] = update
    return {claim_id: update.new_status for claim_id, update in latest.items()}


def _project_decision_ask_last_touch_from_update(
    entry: DecisionAsk,
    update: ClaimStatusUpdate | None,
) -> DecisionAsk:
    if update is None or update.new_status != "open":
        return entry
    if entry.last_touched_at is not None and entry.last_touched_at >= update.updated_at:
        return entry
    return replace(entry, last_touched_at=update.updated_at)


def _decision_ask_from_fact(fact: ProgramFactRevision) -> DecisionAsk:
    payload = fact.payload
    status = str(payload.get("status") or "open")
    if status not in {"open", "resolved", "deferred"}:
        status = "open"
    return DecisionAsk(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        edition_id=str(payload["edition_id"]),
        issue_number=int(payload["issue_number"]),
        text=str(payload["text"]),
        entity_refs=tuple(str(value) for value in payload.get("entity_refs") or ()),
        ask_date=_deserialize_required_date(payload.get("ask_date"), field_name="ask_date"),
        owner_alias=_deserialize_optional_string(payload.get("owner_alias")),
        status=status,  # type: ignore[arg-type]
        resolution=_deserialize_optional_string(payload.get("resolution")),
        expiry_date=_deserialize_date(payload.get("expiry_date")),
        resurfacing_policy=_deserialize_resurfacing_policy(payload.get("resurfacing_policy")),
        affected_milestone_ids=tuple(str(value) for value in payload.get("affected_milestone_ids") or ()),
        last_touched_at=_deserialize_datetime(_deserialize_optional_string(payload.get("last_touched_at"))),
    )


def _baseline_trust_event_from_fact(fact: ProgramFactRevision) -> TrustedBaselineHistoryEntry:
    payload = fact.payload
    event_at = _deserialize_datetime(_deserialize_optional_string(payload.get("at")))
    if event_at is None:
        raise ValueError("missing baseline trust event timestamp")
    return TrustedBaselineHistoryEntry(
        issue=int(payload["issue"]),
        at=event_at,
        by=_deserialize_optional_string(payload.get("by")),
        action=str(payload["action"]),
        reason=_deserialize_optional_string(payload.get("reason")),
    )


def _skip_issue_from_fact(fact: ProgramFactRevision) -> SkippedIssueEntry:
    payload = fact.payload
    generated_at = _deserialize_datetime(_deserialize_optional_string(payload.get("generated_at")))
    if generated_at is None:
        raise ValueError("missing skipped issue generated_at")
    return SkippedIssueEntry(
        edition_id=str(payload["edition_id"]),
        issue_number=int(payload["issue_number"]),
        generated_at=generated_at,
        reason=_deserialize_optional_string(payload.get("reason")),
    )


def _risk_entry_from_fact(fact: ProgramFactRevision) -> RiskEntry:
    payload = fact.payload
    return RiskEntry(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        title=str(payload["title"]),
        description=str(payload["description"]),
        probability=RiskProbability.from_string(str(payload["probability"])),
        impact=RiskImpact.from_string(str(payload["impact"])),
        category=RiskCategory.from_string(str(payload["category"])),
        owner_alias=str(payload["owner_alias"]),
        mitigation_plan=_deserialize_optional_string(payload.get("mitigation_plan")),
        mitigation_due_date=_deserialize_date(payload.get("mitigation_due_date")),
        linked_workstream_ids=tuple(str(value) for value in payload.get("linked_workstream_ids") or ()),
        linked_work_item_ids=tuple(int(value) for value in payload.get("linked_work_item_ids") or ()),
        linked_milestone_ids=tuple(str(value) for value in payload.get("linked_milestone_ids") or ()),
        linked_claim_ids=tuple(str(value) for value in payload.get("linked_claim_ids") or ()),
        linked_action_ids=tuple(str(value) for value in payload.get("linked_action_ids") or ()),
        status=RiskStatus.from_string(str(payload["status"])),
        identified_date=_deserialize_required_date(payload.get("identified_date"), field_name="identified_date"),
        identified_in_vertex_issue=_deserialize_optional_int(payload.get("identified_in_vertex_issue")),
        last_reviewed_date=_deserialize_date(payload.get("last_reviewed_date")),
        entity_refs=tuple(str(value) for value in payload.get("entity_refs") or ()),
        source_signal_ids=tuple(str(v) for v in payload.get("source_signal_ids") or ()),
        kind=str(payload.get("kind") or "strategic"),
        dimension_id=_deserialize_optional_string(payload.get("dimension_id")),
        fact_id=fact.fact_id,
        last_validated_at=fact.recorded_at,
    )


def _dependency_evidence_tier_from_payload(payload: dict[str, object]) -> DependencyEvidenceTier:
    """Read ``evidence_tier`` off a dependency fact payload, defaulting to
    AUTHORED for legacy facts persisted before ADF-W4.4 added the field.

    An unrecognized value also falls back to AUTHORED rather than raising --
    a forward-compat tolerant read matching every other optional enum on this
    projection (schedule_status, DependencyStatus.from_string in the parser).
    """
    raw_tier = payload.get("evidence_tier")
    if not isinstance(raw_tier, str) or not raw_tier:
        return DependencyEvidenceTier.AUTHORED
    try:
        return DependencyEvidenceTier.from_string(raw_tier)
    except ValueError:
        return DependencyEvidenceTier.AUTHORED


def _dependency_from_fact(fact: ProgramFactRevision) -> Dependency:
    payload = fact.payload
    schedule_status = _deserialize_optional_string(payload.get("schedule_status"))
    return Dependency(
        id=str(payload["id"]),
        from_program_id=str(payload["from_program_id"]),
        from_workstream_id=_deserialize_optional_string(payload.get("from_workstream_id")),
        from_item_id=_deserialize_optional_int(payload.get("from_item_id")),
        from_milestone_id=_deserialize_optional_string(payload.get("from_milestone_id")),
        to_program_id=str(payload["to_program_id"]),
        to_workstream_id=_deserialize_optional_string(payload.get("to_workstream_id")),
        to_item_id=_deserialize_optional_int(payload.get("to_item_id")),
        to_milestone_id=_deserialize_optional_string(payload.get("to_milestone_id")),
        dependency_type=DependencyType.from_string(str(payload["dependency_type"])),
        risk_if_broken=str(payload["risk_if_broken"]),
        mitigation=_deserialize_optional_string(payload.get("mitigation")),
        status=DependencyStatus.from_string(str(payload["status"])),
        owner_alias=_deserialize_optional_string(payload.get("owner_alias")),
        resolution_path=_deserialize_optional_string(payload.get("resolution_path")),
        planned_resolution_date=_deserialize_date(payload.get("planned_resolution_date")),
        schedule_status=(
            DependencyScheduleStatus.from_string(schedule_status)
            if schedule_status is not None
            else None
        ),
        linked_risk_ids=tuple(str(value) for value in payload.get("linked_risk_ids") or ()),
        evidence_tier=_dependency_evidence_tier_from_payload(payload),
        evidence_refs=tuple(str(value) for value in payload.get("evidence_refs") or ()),
    )


def _decision_entry_from_fact(fact: ProgramFactRevision) -> DecisionEntry:
    payload = fact.payload
    return DecisionEntry(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        title=str(payload["title"]),
        context=str(payload["context"]),
        decision=str(payload["decision"]),
        rationale=_deserialize_optional_string(payload.get("rationale")),
        alternatives_considered=tuple(str(value) for value in payload.get("alternatives_considered") or ()),
        decided_by=_deserialize_optional_string(payload.get("decided_by")),
        decision_date=_deserialize_date(payload.get("decision_date")),
        status=DecisionStatus.from_string(str(payload["status"])),
        superseded_by=_deserialize_optional_string(payload.get("superseded_by")),
        linked_claim_id=_deserialize_optional_string(payload.get("linked_claim_id")),
        linked_risk_id=_deserialize_optional_string(payload.get("linked_risk_id")),
        linked_action_ids=tuple(str(value) for value in payload.get("linked_action_ids") or ()),
        workstream_id=_deserialize_optional_string(payload.get("workstream_id")),
        entity_refs=tuple(str(value) for value in payload.get("entity_refs") or ()),
        review_by=_deserialize_date(payload.get("review_by")),
        linked_milestone_ids=tuple(str(value) for value in payload.get("linked_milestone_ids") or ()),
        last_reviewed_date=_deserialize_date(payload.get("last_reviewed_date")),
        fact_id=fact.fact_id,
        last_validated_at=fact.recorded_at,
    )


def _assumption_from_fact(fact: ProgramFactRevision) -> Assumption:
    payload = fact.payload
    return Assumption(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        text=str(payload["text"]),
        validation_method=_deserialize_optional_string(payload.get("validation_method")),
        validation_due=_deserialize_date(payload.get("validation_due")),
        status=AssumptionStatus.from_string(str(payload["status"])),
        category=_deserialize_optional_string(payload.get("category")),
        linked_risk_id=_deserialize_optional_string(payload.get("linked_risk_id")),
        linked_workstream_ids=tuple(str(value) for value in payload.get("linked_workstream_ids") or ()),
        linked_milestone_id=_deserialize_optional_string(payload.get("linked_milestone_id")),
        owner_alias=_deserialize_optional_string(payload.get("owner_alias")),
        identified_date=_deserialize_required_date(payload.get("identified_date"), field_name="identified_date"),
        entity_refs=tuple(str(value) for value in payload.get("entity_refs") or ()),
        resolved_date=_deserialize_date(payload.get("resolved_date")),
        linked_milestone_ids=tuple(str(value) for value in payload.get("linked_milestone_ids") or ()),
        last_reviewed_date=_deserialize_date(payload.get("last_reviewed_date")),
    )


def _milestone_from_fact(fact: ProgramFactRevision) -> Milestone:
    payload = fact.payload
    return Milestone(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        name=str(payload["name"]),
        target_date=_deserialize_required_date(payload.get("target_date"), field_name="target_date"),
        owner_alias=str(payload["owner_alias"]),
        status=MilestoneStatus.from_string(str(payload["status"])),
        exit_criteria=tuple(str(value) for value in payload.get("exit_criteria") or ()),
        linked_workstream_ids=tuple(str(value) for value in payload.get("linked_workstream_ids") or ()),
        linked_work_item_ids=tuple(int(value) for value in payload.get("linked_work_item_ids") or ()),
        notes=_deserialize_optional_string(payload.get("notes")),
        last_reviewed_date=_deserialize_date(payload.get("last_reviewed_date")),
    )


def _workstream_from_fact(fact: ProgramFactRevision) -> Workstream:
    payload = fact.payload
    return Workstream(
        id=str(payload["id"]),
        name=str(payload["name"]),
        aliases=tuple(str(value) for value in payload.get("aliases") or ()),
        area_paths=tuple(str(value) for value in payload.get("area_paths") or ()),
        ado_team=_deserialize_optional_string(payload.get("ado_team")),
        ado_pipeline_ids=tuple(str(value) for value in payload.get("ado_pipeline_ids") or ()),
        ado_repository_ids=tuple(str(value) for value in payload.get("ado_repository_ids") or ()),
        pm_owner=_deserialize_optional_string(payload.get("pm_owner")),
        eng_owner=_deserialize_optional_string(payload.get("eng_owner")),
        accountable_owner=_deserialize_optional_string(payload.get("accountable_owner")),
        accountable_email=_deserialize_optional_string(payload.get("accountable_email")),
        responsible_owners=tuple(str(value) for value in payload.get("responsible_owners") or ()),
        consulted_owners=tuple(str(value) for value in payload.get("consulted_owners") or ()),
        informed_owners=tuple(str(value) for value in payload.get("informed_owners") or ()),
        dri_email=_deserialize_optional_string(payload.get("dri_email")),
        alternate_owner=_deserialize_optional_string(payload.get("alternate_owner")),
        always_notify=tuple(str(value) for value in payload.get("always_notify") or ()),
        description=_deserialize_optional_string(payload.get("description")),
        why_it_matters=_deserialize_optional_string(payload.get("why_it_matters")),
        history_summary=_deserialize_optional_string(payload.get("history_summary")),
        leadership_sensitivity=_deserialize_optional_string(payload.get("leadership_sensitivity")),
        current_blocker=_deserialize_optional_string(payload.get("current_blocker")),
        ado_saved_query_ids=tuple(str(value) for value in payload.get("ado_saved_query_ids") or ()),
        last_reviewed_date=_deserialize_date(payload.get("last_reviewed_date")),
        signal_sources=_deserialize_workstream_signal_sources(payload.get("signal_sources")),
        owner_person_id=_deserialize_optional_string(payload.get("owner_person_id")),
        status=_deserialize_optional_string(payload.get("status")) or "active",
    )


def _workstream_association_from_fact(fact: ProgramFactRevision) -> WorkstreamAssociationRecord:
    """Project a ``workstream.association`` fact back to a WorkstreamAssociationRecord."""
    payload = fact.payload
    recorded_at_value = _deserialize_datetime(payload.get("recorded_at"))
    if recorded_at_value is None:
        raise ValueError("workstream.association fact is missing recorded_at")
    issue_number_value = payload.get("issue_number")
    if not isinstance(issue_number_value, int) or isinstance(issue_number_value, bool):
        raise TypeError("workstream.association fact has non-integer issue_number")
    work_item_id_value = payload.get("work_item_id")
    if work_item_id_value is not None and (not isinstance(work_item_id_value, int) or isinstance(work_item_id_value, bool)):
        raise TypeError("workstream.association fact has non-integer work_item_id")
    return WorkstreamAssociationRecord(
        recorded_at=recorded_at_value,
        edition=str(payload["edition"]),
        issue_number=issue_number_value,
        workstream_id=str(payload["workstream_id"]),
        source_type=str(payload["source_type"]),
        source_slice_id=_deserialize_optional_string(payload.get("source_slice_id")),
        section_id=_deserialize_optional_string(payload.get("section_id")),
        work_item_id=work_item_id_value,
        note=_deserialize_optional_string(payload.get("note")),
    )


def _serialize_workstream_signal_sources(value: WorkstreamSignalSources | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return json.loads(json.dumps(asdict(value), sort_keys=True))


def _deserialize_workstream_signal_sources(value: object) -> WorkstreamSignalSources | None:
    if not isinstance(value, dict):
        return None
    ado_coverage = value.get("ado_coverage")
    return WorkstreamSignalSources(
        teams_meeting_series=tuple(
            TeamsMeetingSeries(
                display_name=str(entry["display_name"]),
                series_id=_deserialize_optional_string(entry.get("series_id")),
                include_transcripts=bool(entry.get("include_transcripts", True)),
                work_item_ids=tuple(int(value) for value in entry.get("work_item_ids") or ()),
                calendar_name=_deserialize_optional_string(entry.get("calendar_name")),
                vpn_required=bool(entry.get("vpn_required", False)),
            )
            for entry in value.get("teams_meeting_series") or ()
            if isinstance(entry, dict)
        ),
        teams_chats=tuple(
            TeamsChat(
                display_name=str(entry["display_name"]),
                thread_id=_deserialize_optional_string(entry.get("thread_id")),
                work_item_ids=tuple(int(value) for value in entry.get("work_item_ids") or ()),
            )
            for entry in value.get("teams_chats") or ()
            if isinstance(entry, dict)
        ),
        email_subject_filters=tuple(str(entry) for entry in value.get("email_subject_filters") or ()),
        workiq_keywords=tuple(str(entry) for entry in value.get("workiq_keywords") or ()),
        kusto_query_ids=tuple(str(entry) for entry in value.get("kusto_query_ids") or ()),
        ado_coverage=(
            ADOCoverageRequirement(
                min_ado_count=int(ado_coverage.get("min_ado_count", 3)),
                required_work_item_types=tuple(
                    str(entry) for entry in ado_coverage.get("required_work_item_types") or ()
                ),
                suppress_coverage_alert=bool(ado_coverage.get("suppress_coverage_alert", False)),
            )
            if isinstance(ado_coverage, dict)
            else None
        ),
        workiq_exclude_keywords=tuple(str(entry) for entry in value.get("workiq_exclude_keywords") or ()),
        email_threads=tuple(
            EmailThreadSource(
                display_name=str(entry["display_name"]),
                thread_id=str(entry["thread_id"]),
                work_item_ids=tuple(int(value) for value in entry.get("work_item_ids") or ()),
            )
            for entry in value.get("email_threads") or ()
            if isinstance(entry, dict)
        ),
        dependency_ado_queries=tuple(
            DependencyADOQuery(
                label=str(entry["label"]),
                resolution_path=str(entry["resolution_path"]),
                area_path=_deserialize_optional_string(entry.get("area_path")),
                work_item_ids=tuple(int(item_id) for item_id in entry.get("work_item_ids") or ()),
            )
            for entry in value.get("dependency_ado_queries") or ()
            if isinstance(entry, dict)
        ),
    )


def _serialize_resurfacing_policy(value: ResurfacingPolicy | None) -> dict[str, int] | None:
    if value is None:
        return None
    return {
        "watch_days": int(value.watch_days),
        "nudge_days": int(value.nudge_days),
        "escalate_days": int(value.escalate_days),
    }


def _deserialize_resurfacing_policy(value: object) -> ResurfacingPolicy | None:
    if not isinstance(value, dict):
        return None
    return ResurfacingPolicy(
        watch_days=int(value.get("watch_days", 7)),
        nudge_days=int(value.get("nudge_days", 14)),
        escalate_days=int(value.get("escalate_days", 21)),
    )


def _serialize_date(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _deserialize_date(value: object) -> date | None:
    text = _deserialize_optional_string(value)
    if text is None:
        return None
    return date.fromisoformat(text)


def _deserialize_required_date(value: object, *, field_name: str) -> date:
    parsed = _deserialize_date(value)
    if parsed is None:
        raise ValueError(f"missing {field_name}")
    return parsed


def _deserialize_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, str)):
        return int(value)
    raise ValueError(f"cannot parse int from {value!r}")


def _deserialize_optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# FR-SG-59: Judgment projection
# ---------------------------------------------------------------------------

def _judgment_from_fact(fact: ProgramFactRevision) -> Judgment:
    payload = fact.payload
    decided_at = _deserialize_datetime(str(payload["decided_at"]))
    if decided_at is None:
        raise ValueError("missing decided_at")
    return Judgment(
        id=str(payload["id"]),
        program_id=str(payload["program_id"]),
        dimension=str(payload["dimension"]),
        risk_level=str(payload["risk_level"]),
        edition_id=str(payload["edition_id"]),
        issue_number=int(payload["issue_number"]),
        justification=str(payload["justification"]),
        decided_by=str(payload["decided_by"]),
        decided_at=decided_at,
        review_by=_deserialize_date(payload.get("review_by")),
        status=str(payload.get("status") or "active"),
        superseded_by=_deserialize_optional_string(payload.get("superseded_by")),
        fact_id=fact.fact_id,
    )


def project_judgments(snapshot: ProgramFactSnapshot) -> tuple[Judgment, ...]:
    """Project all active judgment facts from a snapshot."""
    result = []
    for fact in snapshot.facts:
        if (
            fact.fact_type == "judgment.dimension"
            and fact.lifecycle_state == FactLifecycleState.ACTIVE
            and fact.review_state == FactReviewState.ACCEPTED
        ):
            try:
                result.append(_judgment_from_fact(fact))
            except (KeyError, ValueError):
                continue
    return tuple(result)


def load_current_judgments(
    program_id: str,
    *,
    home_root: Path | None = None,
    db_root: Path | None = None,
) -> tuple[Judgment, ...]:
    """Load all active judgment facts for a program."""
    snapshot = ProgramFactStore(program_id, home_root=home_root, db_root=db_root).snapshot()
    return project_judgments(snapshot)


# ---------------------------------------------------------------------------
# FR-SG-71: Fact freshness TTLs
# ---------------------------------------------------------------------------

def _get_fact_type_ttl_days() -> dict[str, int]:
    """Load fact-type TTLs from freshness policy (vertex/policies/freshness_policy.yaml)."""
    from src.core.policy_loader import load_freshness_policy
    return load_freshness_policy().fact_type_ttl_days


def compute_fact_freshness(
    snapshot: ProgramFactSnapshot,
    as_of: datetime,
) -> dict[str, int]:
    """Return a mapping of fact_id -> days_since_recorded for every fact in the snapshot."""
    result: dict[str, int] = {}
    for fact in snapshot.facts:
        delta = as_of - fact.recorded_at.replace(tzinfo=None) if fact.recorded_at.tzinfo is None else as_of - fact.recorded_at
        result[fact.fact_id] = max(0, delta.days)
    return result


def find_last_reconfirmation_at(
    natural_key: str,
    snapshot: ProgramFactSnapshot,
) -> datetime | None:
    """WI-3.6: Return the latest fact.reconfirmation timestamp for a fact.

    Scans the snapshot for fact.reconfirmation facts whose payload contains
    `target_natural_key == natural_key`. Returns the most recent
    `reconfirmed_at` (or `recorded_at` as fallback) across all matching events.

    Returns None when no reconfirmation exists for this fact.
    """
    latest: datetime | None = None
    for fact in snapshot.facts:
        if fact.fact_type != "fact.reconfirmation":
            continue
        if fact.payload.get("target_natural_key") != natural_key:
            continue
        reconfirmed_raw = fact.payload.get("reconfirmed_at")
        try:
            ts = datetime.fromisoformat(reconfirmed_raw) if reconfirmed_raw else fact.recorded_at
        except (ValueError, TypeError):
            ts = fact.recorded_at
        ts_aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        if latest is None or ts_aware > latest:
            latest = ts_aware
    return latest


def effective_freshness_date(
    fact: ProgramFactRevision,
    snapshot: ProgramFactSnapshot,
) -> datetime:
    """WI-3.6: Freshness clock = max(recorded_at, last_reconfirmed_at).

    When a fact.reconfirmation event lands, this resets the staleness clock
    for the target fact without creating a new revision.
    """
    recorded = fact.recorded_at if fact.recorded_at.tzinfo is not None else fact.recorded_at.replace(tzinfo=timezone.utc)
    last_reconfirmed = find_last_reconfirmation_at(fact.natural_key, snapshot)
    if last_reconfirmed is not None:
        return max(recorded, last_reconfirmed)
    return recorded


def is_fact_stale(
    fact: ProgramFactRevision,
    as_of: datetime,
    snapshot: ProgramFactSnapshot | None = None,
) -> bool:
    """Return True if the fact has exceeded its TTL.

    WI-3.6: When a snapshot is provided, uses the reconfirmation-aware
    freshness clock: max(recorded_at, last_reconfirmed_at).
    Without a snapshot, falls back to recorded_at only (backward compat).
    """
    ttl = _get_fact_type_ttl_days().get(fact.fact_type)
    if ttl is None:
        return False
    if snapshot is not None:
        effective_date = effective_freshness_date(fact, snapshot)
    else:
        effective_date = fact.recorded_at if fact.recorded_at.tzinfo is not None else fact.recorded_at.replace(tzinfo=timezone.utc)
    as_of_aware = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
    return (as_of_aware - effective_date).days > ttl


# ---------------------------------------------------------------------------
# FR-SG-72: Privacy enforcement
# ---------------------------------------------------------------------------

SENSITIVE_FACT_TYPES: frozenset[str] = frozenset()


def filter_facts_for_render(
    snapshot: ProgramFactSnapshot,
    function_name: str,
) -> ProgramFactSnapshot:
    """Return a snapshot with sensitive facts removed (FR-SG-72)."""
    visible = tuple(
        fact for fact in snapshot.facts
        if fact.privacy_classification != "sensitive"
        or fact.fact_type not in SENSITIVE_FACT_TYPES
    )
    return ProgramFactSnapshot(
        program_id=snapshot.program_id,
        as_of=snapshot.as_of,
        facts=visible,
    )


# ---------------------------------------------------------------------------
# Phase 2 stubs — deliverable and incident authority (deliv-incident-fu)
# ---------------------------------------------------------------------------
# These functions scaffold the `deliverable.entry` and `incident.entry`
# fact-type projection path for Phase 2 implementation.
# In v1 the event_type_registry disposes both as KNOWN_UNPROJECTEABLE; these
# stubs return empty tuples until the Phase 2 bridge appender is wired.
# See: .archive/specs/consolidated.md §40 (deliv-incident-fu epic, local-only).

def project_deliverable_entries(snapshot: ProgramFactSnapshot) -> tuple[DeliverableEntry, ...]:
    """Phase 2 stub — deliverable.entry not yet projectable in v1 (S-2d / Q9).

    Returns empty until Phase 2 fact-type schema and bridge appender land.
    Once the Phase 2 bridge appender populates `deliverable.entry` facts in the
    store, replace this stub with a typed projector (pattern: project_milestones).
    """
    return ()


def project_incident_entries(snapshot: ProgramFactSnapshot) -> tuple[IncidentFactEntry, ...]:
    """Phase 2 stub — incident.entry not yet projectable in v1 (S-2d / Q9).

    Returns empty until Phase 2 IcM source and bridge appender land.
    Once S-10a/S-10b wire the IcM source and populate `incident.entry` facts,
    replace this stub with a typed projector (pattern: project_risk_entries).
    """
    return ()


def load_current_deliverable_entries(
    program_id: str,
    *,
    programs_root: Path | None = None,
) -> tuple[DeliverableEntry, ...]:
    """Phase 2 stub — convenience reader for deliverable entries. Returns empty in v1."""
    return project_deliverable_entries(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("deliverable.entry",))
    )


def load_current_incident_entries(
    program_id: str,
    *,
    programs_root: Path | None = None,
) -> tuple[IncidentFactEntry, ...]:
    """Phase 2 stub — convenience reader for incident entries. Returns empty in v1."""
    return project_incident_entries(
        load_program_facts(program_id, programs_root=programs_root, fact_types=("incident.entry",))
    )
