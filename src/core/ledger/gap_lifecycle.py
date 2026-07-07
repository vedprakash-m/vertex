"""ContextGapRecord gap lifecycle + retrieval-coverage maturity (FR-PCI-13 / REV-16).

.archive/specs/program-context-intelligence.md §5.14 / .archive/specs/still-gaps.md W2-11. Introduces:

1. **Gap lifecycle** on ``ContextGapRecord``: ``status: open | filling | resolved | reopened``
   with ``resolution_evidence_ref``, ``last_evaluated_at``, and reopen semantics.

2. **Structured match criteria** (``GapMatchCriteria``): a gap can now match on
   ``program_id + entity_id + fact_family + fact_field + expected_value`` in addition to
   the legacy ``metadata["event_types"]`` approach.  Both coexist; callers can use either.

3. **Contradiction reopening**: ``ContextGapRecord.check_contradiction(fact_family,
   entity_id, payload)`` returns ``True`` when a new accepted fact contradicts the
   expected value for a resolved gap's match criteria.  Callers should then call
   ``gap.reopen(reason=...)`` to transition the gap back to ``reopened``.

4. **Staleness reopening**: ``ContextGapRecord.is_stale(reference_dt)`` returns ``True``
   when ``stale_after_days`` is set and the gap has been resolved for longer than the
   configured window.  ``GapLifecycleStore.evaluate_stale_gaps(reference_dt)`` collects
   all resolved gaps that have become stale.

5. **Fact-driven lookup**: ``GapLifecycleStore.find_by_criteria(entity_id, fact_family)``
   returns all gaps whose ``match_criteria`` match the given entity and family.

6. **Retrieval-coverage maturity** — a metric **separate from** the existing Plane-1
   ``ProgramContext.maturity_level`` (authored; not movable by discovered facts). Coverage
   maturity measures the REV pipeline's observed coverage breadth, not editorial completeness.

7. Serialized as ``programs/<prog>/rev_gap_lifecycle.json`` (one JSON file per program).
   Append-only in spirit; gap status transitions are logged inline.

Zone A — no AI or M365 imports.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.ledger.candidate_store import PROGRAMS_ROOT, get_candidate_dir

log = logging.getLogger(__name__)

GAP_LIFECYCLE_SCHEMA_VERSION = "gap_lifecycle.v2"


# ---------------------------------------------------------------------------
# Structured match criteria (W2-11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapMatchCriteria:
    """Structured criteria for gap matching, resolution, and reopening (W2-11 / PS-6).

    A gap is considered *resolved* when a fact satisfying all non-None criteria
    exists in the fact store (entity_id + fact_family + payload[fact_field] ==
    expected_value).

    A gap is automatically *reopened* when:
    - A newly accepted fact for the same entity+family has ``fact_field`` set to a
      value other than ``expected_value`` (contradiction).
    - The resolving evidence was recorded more than ``stale_after_days`` days ago.
    """

    program_id: str
    entity_id: str | None = None        # canonical entity key, e.g. "MILESTONE:ms-001"
    fact_family: str | None = None      # e.g. "milestone", "risk", "commitment"
    fact_field: str | None = None       # payload field checked, e.g. "status"
    expected_value: str | None = None   # expected payload value, e.g. "complete"
    stale_after_days: int | None = None # reopen if resolved fact is older than N days

    def matches_fact(self, fact_family: str, entity_id: str) -> bool:
        """Return True if this criteria applies to the given fact_family + entity_id."""
        if self.fact_family is not None and self.fact_family != fact_family:
            return False
        if self.entity_id is not None and self.entity_id != entity_id:
            return False
        return True

    def is_contradicted_by(self, payload: dict[str, Any]) -> bool:
        """Return True if the payload contradicts the expected value.

        Only meaningful when all of (fact_field, expected_value) are set.
        """
        if self.fact_field is None or self.expected_value is None:
            return False
        actual = str(payload.get(self.fact_field, "")) if payload else ""
        return bool(actual) and actual != self.expected_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "entity_id": self.entity_id,
            "fact_family": self.fact_family,
            "fact_field": self.fact_field,
            "expected_value": self.expected_value,
            "stale_after_days": self.stale_after_days,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GapMatchCriteria":
        return cls(
            program_id=str(d["program_id"]),
            entity_id=d.get("entity_id"),
            fact_family=d.get("fact_family"),
            fact_field=d.get("fact_field"),
            expected_value=d.get("expected_value"),
            stale_after_days=int(d["stale_after_days"]) if d.get("stale_after_days") is not None else None,
        )


# ---------------------------------------------------------------------------
# Gap status enum
# ---------------------------------------------------------------------------


class GapStatus(str, Enum):
    OPEN = "open"           # gap identified; no evidence ingested yet
    FILLING = "filling"     # REV candidates exist for this gap; awaiting triage
    RESOLVED = "resolved"   # at least one accepted event addresses this gap
    REOPENED = "reopened"   # a resolved gap became relevant again (new evidence or date change)


# ---------------------------------------------------------------------------
# ContextGapRecord
# ---------------------------------------------------------------------------


@dataclass
class ContextGapRecord:
    """A tracked context gap with a lifecycle state.

    ``gap_id`` is a stable identifier (e.g., ``gap:<program>:<hash>``).
    ``description`` is the human-readable gap (e.g., "Unknown status of XFN dependency").
    ``source_workstream_id`` links the gap to its originating workstream.
    ``status`` follows ``GapStatus`` transitions.
    ``resolution_evidence_ref`` points to the vault hash of the evidence that resolved
    the gap (set at transition to ``resolved``).
    ``last_evaluated_at`` records when the gap was last checked by the pipeline.
    ``reopen_reason`` records the reason for transition back to ``reopened``.
    """

    gap_id: str
    description: str
    source_workstream_id: str | None = None
    status: str = GapStatus.OPEN.value
    resolution_evidence_ref: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_evaluated_at: datetime | None = None
    resolved_at: datetime | None = None
    reopened_at: datetime | None = None
    reopen_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    match_criteria: GapMatchCriteria | None = None  # W2-11: structured match + reopening criteria

    # Status-transition log (append-only entries inside this record).
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def transition_to(self, new_status: GapStatus, *, reason: str = "", evidence_ref: str | None = None) -> None:
        """Record a status transition (append-only history inside this record)."""
        now = datetime.now(timezone.utc)
        entry: dict[str, Any] = {
            "from_status": self.status,
            "to_status": new_status.value,
            "at": now.isoformat(),
            "reason": reason,
        }
        if evidence_ref:
            entry["evidence_ref"] = evidence_ref
        self.transitions.append(entry)
        self.status = new_status.value
        self.last_evaluated_at = now
        if new_status is GapStatus.RESOLVED:
            self.resolved_at = now
            self.resolution_evidence_ref = evidence_ref
        if new_status is GapStatus.REOPENED:
            self.reopened_at = now
            self.reopen_reason = reason

    def mark_filling(self, *, reason: str = "") -> None:
        if self.status in (GapStatus.OPEN.value, GapStatus.REOPENED.value):
            self.transition_to(GapStatus.FILLING, reason=reason)

    def mark_resolved(self, *, evidence_ref: str, reason: str = "") -> None:
        self.transition_to(GapStatus.RESOLVED, reason=reason, evidence_ref=evidence_ref)

    def reopen(self, *, reason: str) -> None:
        """Reopen a resolved gap when new context makes it relevant again."""
        if self.status in (GapStatus.RESOLVED.value, GapStatus.FILLING.value):
            self.transition_to(GapStatus.REOPENED, reason=reason)

    # ------------------------------------------------------------------
    # W2-11: structured contradiction + staleness checks
    # ------------------------------------------------------------------

    def check_contradiction(
        self,
        fact_family: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Return True if a newly accepted fact contradicts this gap's expected value.

        Only meaningful for resolved gaps with structured match_criteria.  The
        caller is responsible for calling ``gap.reopen(reason=...)`` when this
        returns True — this method is pure (no side-effects).
        """
        if self.status != GapStatus.RESOLVED.value:
            return False
        if self.match_criteria is None:
            return False
        if not self.match_criteria.matches_fact(fact_family, entity_id):
            return False
        return self.match_criteria.is_contradicted_by(payload)

    def is_stale(self, reference_dt: datetime | None = None) -> bool:
        """Return True if the resolving evidence has exceeded ``stale_after_days``.

        Only meaningful for resolved gaps with ``match_criteria.stale_after_days``
        set.  ``reference_dt`` defaults to UTC now.
        """
        if self.status != GapStatus.RESOLVED.value:
            return False
        if self.match_criteria is None or self.match_criteria.stale_after_days is None:
            return False
        if self.resolved_at is None:
            return False
        cutoff = reference_dt or datetime.now(timezone.utc)
        age = cutoff - self.resolved_at
        return age > timedelta(days=self.match_criteria.stale_after_days)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "gap_id": self.gap_id,
            "description": self.description,
            "source_workstream_id": self.source_workstream_id,
            "status": self.status,
            "resolution_evidence_ref": self.resolution_evidence_ref,
            "created_at": self.created_at.isoformat(),
            "last_evaluated_at": self.last_evaluated_at.isoformat() if self.last_evaluated_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "reopened_at": self.reopened_at.isoformat() if self.reopened_at else None,
            "reopen_reason": self.reopen_reason,
            "metadata": self.metadata,
            "transitions": self.transitions,
        }
        if self.match_criteria is not None:
            d["match_criteria"] = self.match_criteria.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContextGapRecord":
        mc_raw = d.get("match_criteria")
        rec = cls(
            gap_id=str(d["gap_id"]),
            description=str(d.get("description", "")),
            source_workstream_id=d.get("source_workstream_id"),
            status=str(d.get("status", GapStatus.OPEN.value)),
            resolution_evidence_ref=d.get("resolution_evidence_ref"),
            created_at=datetime.fromisoformat(str(d["created_at"])).astimezone(timezone.utc),
            last_evaluated_at=(
                datetime.fromisoformat(str(d["last_evaluated_at"])).astimezone(timezone.utc)
                if d.get("last_evaluated_at")
                else None
            ),
            resolved_at=(
                datetime.fromisoformat(str(d["resolved_at"])).astimezone(timezone.utc)
                if d.get("resolved_at")
                else None
            ),
            reopened_at=(
                datetime.fromisoformat(str(d["reopened_at"])).astimezone(timezone.utc)
                if d.get("reopened_at")
                else None
            ),
            reopen_reason=d.get("reopen_reason"),
            metadata=dict(d.get("metadata", {})),
            match_criteria=GapMatchCriteria.from_dict(mc_raw) if isinstance(mc_raw, dict) else None,
            transitions=list(d.get("transitions", [])),
        )
        return rec


# ---------------------------------------------------------------------------
# Gap lifecycle store
# ---------------------------------------------------------------------------


def _gap_store_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidate_dir(program_id, programs_root=programs_root) / "rev_gap_lifecycle.json"


class GapLifecycleStore:
    """Persistent store for a program's ContextGapRecords."""

    def __init__(self, gaps: dict[str, ContextGapRecord] | None = None) -> None:
        self._gaps: dict[str, ContextGapRecord] = gaps or {}

    @classmethod
    def load(
        cls,
        program_id: str,
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> "GapLifecycleStore":
        path = _gap_store_path(program_id, programs_root=programs_root)
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("gap_lifecycle.load failed for program %s: %s", program_id, exc)
            return cls()
        gaps: dict[str, ContextGapRecord] = {}
        for entry in raw.get("gaps", []):
            try:
                rec = ContextGapRecord.from_dict(entry)
                gaps[rec.gap_id] = rec
            except (KeyError, ValueError) as exc:
                log.warning("gap_lifecycle.load: skipping malformed entry: %s", exc)
        return cls(gaps)

    def save(
        self,
        program_id: str,
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> None:
        path = _gap_store_path(program_id, programs_root=programs_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": GAP_LIFECYCLE_SCHEMA_VERSION,
            "gaps": [g.to_dict() for g in self._gaps.values()],
        }
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, gap_id: str) -> ContextGapRecord | None:
        return self._gaps.get(gap_id)

    def upsert(self, gap: ContextGapRecord) -> None:
        self._gaps[gap.gap_id] = gap

    def all_gaps(self) -> tuple[ContextGapRecord, ...]:
        return tuple(self._gaps.values())

    def by_status(self, status: GapStatus) -> tuple[ContextGapRecord, ...]:
        return tuple(g for g in self._gaps.values() if g.status == status.value)

    def status_distribution(self) -> dict[str, int]:
        dist: dict[str, int] = {}
        for g in self._gaps.values():
            dist[g.status] = dist.get(g.status, 0) + 1
        return dist

    # ------------------------------------------------------------------
    # W2-11: structured lookup + automated reopening
    # ------------------------------------------------------------------

    def find_by_criteria(
        self,
        entity_id: str,
        fact_family: str,
    ) -> tuple[ContextGapRecord, ...]:
        """Return all gaps whose match_criteria cover (entity_id, fact_family).

        Gaps without match_criteria are excluded from this lookup (they rely on
        the legacy ``metadata["event_types"]`` matching path).
        """
        return tuple(
            g for g in self._gaps.values()
            if g.match_criteria is not None
            and g.match_criteria.matches_fact(fact_family, entity_id)
        )

    def evaluate_stale_gaps(
        self,
        reference_dt: datetime | None = None,
    ) -> list[tuple[ContextGapRecord, str]]:
        """Return resolved gaps that have exceeded their stale_after_days window.

        Returns a list of (gap, reason) tuples.  Callers should call
        ``gap.reopen(reason=reason)`` and then ``store.save(...)`` for each.
        """
        now = reference_dt or datetime.now(timezone.utc)
        results: list[tuple[ContextGapRecord, str]] = []
        for g in self._gaps.values():
            if g.is_stale(now):
                days = g.match_criteria.stale_after_days if g.match_criteria else None
                reason = f"resolving evidence exceeded stale_after_days={days} (reference={now.date().isoformat()})"
                results.append((g, reason))
        return results

    def evaluate_contradictions(
        self,
        fact_family: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> list[tuple[ContextGapRecord, str]]:
        """Return resolved gaps that are contradicted by a new accepted fact.

        Returns a list of (gap, reason) tuples.  Callers should call
        ``gap.reopen(reason=reason)`` and then ``store.save(...)`` for each.
        """
        results: list[tuple[ContextGapRecord, str]] = []
        for g in self._gaps.values():
            if g.check_contradiction(fact_family, entity_id, payload):
                crit = g.match_criteria
                field_val = payload.get(crit.fact_field, "") if crit and crit.fact_field else ""
                reason = (
                    f"contradiction: fact {fact_family}/{entity_id} "
                    f"has {crit.fact_field}={field_val!r} "
                    f"(expected {crit.expected_value!r})"
                    if crit else "contradiction detected"
                )
                results.append((g, reason))
        return results


# ---------------------------------------------------------------------------
# Retrieval-coverage maturity (separate from Plane-1 authored maturity)
# ---------------------------------------------------------------------------


class CoverageMaturiyLevel(str, Enum):
    """REV retrieval-coverage maturity levels (§5.14).

    Separate from ``ProgramContext.maturity_level`` (authored; not movable by
    discovered facts). Coverage maturity measures the pipeline's observed
    coverage breadth.
    """

    NONE = "none"             # no REV cycles run
    BOOTSTRAPPING = "bootstrapping"   # ≥1 cycle, <3 accepted events
    PARTIAL = "partial"       # ≥3 accepted events, not all sources covered
    ESTABLISHED = "established"   # ≥10 accepted events across ≥2 source types


@dataclass(frozen=True, slots=True)
class CoverageMaturity:
    """REV retrieval-coverage maturity snapshot for one program."""

    level: str
    accepted_event_count: int
    source_type_count: int
    open_gap_count: int
    filling_gap_count: int
    resolved_gap_count: int


def compute_coverage_maturity(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> CoverageMaturity:
    """Compute REV retrieval-coverage maturity (read-only, §5.14).

    The active gap-fill loop is deferred (OS-7); this function computes the
    metric from the current ledger state without triggering any retrieval.
    """
    from src.core.ledger.candidate_store import load_triage_decisions, load_pending_candidates
    from src.core.rev.run_state import state_distribution

    run_dist = state_distribution(program_id, programs_root=programs_root)
    accepted_count = run_dist.get("accepted", 0)

    # Count distinct source types among accepted candidates.
    candidates = load_pending_candidates(program_id, programs_root=programs_root)
    decisions = load_triage_decisions(program_id, programs_root=programs_root)
    approved_ids = {d.candidate_id for d in decisions if d.kind == "approved"}
    source_types: set[str] = set()
    for c in candidates:
        if c.candidate_id in approved_ids:
            if hasattr(c, "source_ref") and c.source_ref is not None:
                source_types.add(type(c.source_ref).__name__)

    gap_store = GapLifecycleStore.load(program_id, programs_root=programs_root)
    gap_dist = gap_store.status_distribution()

    total_runs = sum(run_dist.values())
    if total_runs == 0:
        level = CoverageMaturiyLevel.NONE.value
    elif accepted_count < 3:
        level = CoverageMaturiyLevel.BOOTSTRAPPING.value
    elif accepted_count >= 10 and len(source_types) >= 2:
        level = CoverageMaturiyLevel.ESTABLISHED.value
    else:
        level = CoverageMaturiyLevel.PARTIAL.value

    return CoverageMaturity(
        level=level,
        accepted_event_count=accepted_count,
        source_type_count=len(source_types),
        open_gap_count=gap_dist.get(GapStatus.OPEN.value, 0),
        filling_gap_count=gap_dist.get(GapStatus.FILLING.value, 0),
        resolved_gap_count=gap_dist.get(GapStatus.RESOLVED.value, 0),
    )


__all__ = [
    "GapMatchCriteria",
    "GapStatus",
    "ContextGapRecord",
    "GapLifecycleStore",
    "CoverageMaturiyLevel",
    "CoverageMaturity",
    "compute_coverage_maturity",
    "GAP_LIFECYCLE_SCHEMA_VERSION",
]
