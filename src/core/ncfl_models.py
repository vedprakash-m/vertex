"""
Newsletter → Context Feedback Loop (NCFL) — Core Data Models.

Implements §23.4 (ContextUpdateProposal), §23.1.3 (dedup/supersession),
and the v1.2 immutable-extraction / mutable-decision-state split.

Zone A only — no AI imports, no external clients.

Key invariants:
  INV-6: Single write path for program stores.
  INV-7: Event-sourced corrections (append-only decision_history).
  INV-8: All dates UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Constants — extractor version and confidence matrix
# ---------------------------------------------------------------------------

NCFL_EXTRACTOR_VERSION = "1.0.0"

# Confidence assignment matrix (§23.4 table).
# Keys are extraction_method values; values are the assigned confidence tier.
# This lookup is authoritative — callers must not apply per-call judgment.
EXTRACTION_METHOD_CONFIDENCE: dict[str, str] = {
    "overrides_yaml": "high",            # operator-confirmed at publish time
    "scorecard_data": "high",            # direct deterministic score→RiskStatus mapping
    "context_snapshot_diff": "medium",   # stale forensic record; see §23.4 note on corroboration
    "ado_snapshot": "medium",            # ADO state-change; milestone inference is indirect
    "narrative_markdown_dri": "low",     # free-text; name variants; entity resolution needed
    "narrative_markdown_date": "low",    # free-text; date semantics ambiguous without corroboration
    "field_update_email": "medium",      # semi-structured email; operator-reviewed; never auto-batch
    "knowledge_synthesis": "low",        # Zone B AI synthesis; always proposal only
}

# Target stores recognized by the NCFL system (§23.3 taxonomy).
TARGET_STORES: frozenset[str] = frozenset(
    {"milestones", "risk_register", "decisions", "workstreams", "assumptions", "knowledge_doc"}
)

# Statuses for the proposal lifecycle state machine.
PROPOSAL_STATUSES: frozenset[str] = frozenset(
    {"pending", "accepted", "dismissed", "superseded"}
)


# ---------------------------------------------------------------------------
# DecisionRecord — one append-only lifecycle transition entry (§23.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One append-only entry per lifecycle transition on a proposal.

    Satisfies INV-7 (event-sourced corrections): each status change is
    recorded as an immutable append, never in-place mutation.
    """

    timestamp: datetime
    actor: str           # operator alias or "system"
    from_status: str
    to_status: str
    note: str | None     # e.g. dismiss reason, supersession link, reconciliation marker

    def to_json(self) -> dict[str, Any]:
        return {
            "timestamp": _normalize_datetime(self.timestamp).isoformat().replace("+00:00", "Z"),
            "actor": self.actor,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "note": self.note,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> DecisionRecord:
        return DecisionRecord(
            timestamp=_parse_datetime(str(d["timestamp"])),
            actor=str(d.get("actor", "system")),
            from_status=str(d.get("from_status", "pending")),
            to_status=str(d.get("to_status", "pending")),
            note=d.get("note") or None,
        )


# ---------------------------------------------------------------------------
# ContextUpdateProposal — the core NCFL unit (§23.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextUpdateProposal:
    """A proposed update to a Plane 1 context store.

    Implements the v1.2 immutable-extraction / mutable-decision-state split:
      - Extraction core (identity + source + target + governance): frozen
        at extraction time; never mutated.
      - Decision state (status, history, rationale): expressed as a new
        frozen instance via ``dataclasses.replace()``, appending one
        ``DecisionRecord`` to ``decision_history``.

    Dedup/supersession coordinate:
      ``proposal_id`` — unique per extraction (includes source+value);
      ``conflict_key`` — (target_store, target_key, target_field) used for
                          dedup/supersession across proposals.

    Storage: JSON array per issue at
      ``programs/<prog>/context_proposals/issue_NNN.proposals.json``

    Spec references: §23.4, §23.1.3, §23.5, §24.2.
    """

    # ------------------------------------------------------------------
    # Identity (immutable — unique per extraction run + source)
    # ------------------------------------------------------------------

    proposal_id: str          # ULID — stable identifier unique per extraction
    program_id: str
    issue_number: int
    edition_id: str
    source_type: str          # "confirmed_overrides" | "published_narrative" |
                              # "context_snapshot" | "field_update_email"
    extracted_at: datetime    # INV-8: UTC
    extractor_version: str    # semver string; used for tuning-feedback attribution

    # ------------------------------------------------------------------
    # Source evidence (immutable)
    # ------------------------------------------------------------------

    source_artifact: str      # relative path to source file (e.g. overrides/issue_079.yaml)
    source_field: str         # JSON path within source artifact
    extraction_method: str    # key in EXTRACTION_METHOD_CONFIDENCE

    # ------------------------------------------------------------------
    # Target update (immutable)
    # ------------------------------------------------------------------

    target_store: str         # member of TARGET_STORES
    target_key: str           # natural key of the record to update/create
    target_field: str         # field path within the target record
    source_value: str         # the extracted value (what to write)
    current_value: str | None # value in the live YAML at extraction time
                              # (None = new record)
    current_value_hash: str | None  # SHA-256 of current_value for optimistic
                                    # concurrency check at apply time (§23.1.9)

    # ------------------------------------------------------------------
    # Governance (immutable)
    # ------------------------------------------------------------------

    confidence: str           # "high" | "medium" | "low"
    batch_eligible: bool      # True only when: high-confidence AND existing
                              # record AND no L4 (ACTIVE_PM_JUDGMENT) conflict
                              # AND current_value_hash still matches live YAML.
                              # confidence ≠ batch_eligible (§23.4).
    extraction_method_rationale: str  # human-readable extraction rationale

    # ------------------------------------------------------------------
    # Dedup key (derived; immutable)
    # ------------------------------------------------------------------

    conflict_key: str         # f"{target_store}:{target_key}:{target_field}"
                              # This is the supersession coordinate, NOT the id.

    # ------------------------------------------------------------------
    # Decision state (mutable via dataclasses.replace(); see INV-7)
    # ------------------------------------------------------------------

    status: str = "pending"             # member of PROPOSAL_STATUSES
    superseded_by: str | None = None    # proposal_id of the superseding proposal
    decision_history: tuple[DecisionRecord, ...] = ()  # append-only; INV-7

    rationale: str | None = None        # operator-provided dismiss reason
    applied_at: datetime | None = None  # INV-8: UTC
    applied_by: str | None = None
    dismissed_at: datetime | None = None
    dismissed_by: str | None = None

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "program_id": self.program_id,
            "issue_number": self.issue_number,
            "edition_id": self.edition_id,
            "source_type": self.source_type,
            "extracted_at": _normalize_datetime(self.extracted_at).isoformat().replace("+00:00", "Z"),
            "extractor_version": self.extractor_version,
            "source_artifact": self.source_artifact,
            "source_field": self.source_field,
            "extraction_method": self.extraction_method,
            "target_store": self.target_store,
            "target_key": self.target_key,
            "target_field": self.target_field,
            "source_value": self.source_value,
            "current_value": self.current_value,
            "current_value_hash": self.current_value_hash,
            "confidence": self.confidence,
            "batch_eligible": self.batch_eligible,
            "extraction_method_rationale": self.extraction_method_rationale,
            "conflict_key": self.conflict_key,
            "status": self.status,
            "superseded_by": self.superseded_by,
            "decision_history": [r.to_json() for r in self.decision_history],
            "rationale": self.rationale,
            "applied_at": (
                _normalize_datetime(self.applied_at).isoformat().replace("+00:00", "Z")
                if self.applied_at is not None
                else None
            ),
            "applied_by": self.applied_by,
            "dismissed_at": (
                _normalize_datetime(self.dismissed_at).isoformat().replace("+00:00", "Z")
                if self.dismissed_at is not None
                else None
            ),
            "dismissed_by": self.dismissed_by,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> ContextUpdateProposal:
        decision_history = tuple(
            DecisionRecord.from_json(r)
            for r in (d.get("decision_history") or [])
        )
        return ContextUpdateProposal(
            proposal_id=str(d["proposal_id"]),
            program_id=str(d["program_id"]),
            issue_number=int(d["issue_number"]),
            edition_id=str(d["edition_id"]),
            source_type=str(d["source_type"]),
            extracted_at=_parse_datetime(str(d["extracted_at"])),
            extractor_version=str(d.get("extractor_version", "1.0.0")),
            source_artifact=str(d.get("source_artifact", "")),
            source_field=str(d.get("source_field", "")),
            extraction_method=str(d.get("extraction_method", "")),
            target_store=str(d["target_store"]),
            target_key=str(d["target_key"]),
            target_field=str(d["target_field"]),
            source_value=str(d["source_value"]),
            current_value=d.get("current_value"),
            current_value_hash=d.get("current_value_hash"),
            confidence=str(d.get("confidence", "medium")),
            batch_eligible=bool(d.get("batch_eligible", False)),
            extraction_method_rationale=str(d.get("extraction_method_rationale", "")),
            conflict_key=str(d.get("conflict_key", "")),
            status=str(d.get("status", "pending")),
            superseded_by=d.get("superseded_by"),
            decision_history=decision_history,
            rationale=d.get("rationale"),
            applied_at=_parse_datetime(str(d["applied_at"])) if d.get("applied_at") else None,
            applied_by=d.get("applied_by"),
            dismissed_at=_parse_datetime(str(d["dismissed_at"])) if d.get("dismissed_at") else None,
            dismissed_by=d.get("dismissed_by"),
        )


# ---------------------------------------------------------------------------
# ProposalBatch — a collection of proposals from one issue cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalBatch:
    """A collection of proposals extracted for one issue cycle.

    Produced by ``NcflExtractor.extract_from_confirmed_issue()`` and
    written to ``context_proposals/issue_NNN.proposals.json`` by the
    post-confirm hook or the ``vertex context extract`` command.
    """

    program_id: str
    issue_number: int
    edition_id: str
    extracted_at: datetime            # INV-8: UTC
    proposals: tuple[ContextUpdateProposal, ...]
    extractor_version: str

    @property
    def pending(self) -> tuple[ContextUpdateProposal, ...]:
        """All proposals still in pending status."""
        return tuple(p for p in self.proposals if p.status == "pending")

    @property
    def high_confidence(self) -> tuple[ContextUpdateProposal, ...]:
        """Pending proposals with confidence='high'."""
        return tuple(p for p in self.pending if p.confidence == "high")

    @property
    def batch_eligible_proposals(self) -> tuple[ContextUpdateProposal, ...]:
        """Pending proposals eligible for apply-batch (high + batch_eligible)."""
        return tuple(p for p in self.pending if p.confidence == "high" and p.batch_eligible)


# ---------------------------------------------------------------------------
# Internal helpers (shared with proposal_store)
# ---------------------------------------------------------------------------


def _normalize_datetime(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware (INV-8)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime string to a UTC-aware datetime."""
    return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
