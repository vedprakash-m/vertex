"""RELO-W4.10 (Tech Spec Sec 8.4.4): deterministic long-horizon relevance
selection -- "bounded active state plus retrieved prior outcomes selected by
explicit relevance rules." Zone A owns this module; it is pure (no I/O).

Selection rules, applied in order:

0. Knowledge cutoff: when ``as_of`` is given, records whose transaction time
   (``PriorOutcomeRecord.as_of``) is later than it are not candidates, so a
   replay never sees outcomes recorded after its point in time.
1. Tier ``same_entity``: the record's stable ``entity_id`` is in the active
   entity set.
2. Tier ``linked_entity``: otherwise, at least one ``linked_entity_ids``
   value is in the active entity set.
3. Tier ``recency``: every remaining candidate.

Within a tier, records are ordered by ``valid_at`` (world-state time) newest
first, then by ``record_id`` ascending. The first ``max_items`` records are
selected; each carries its reason code and the active entity ids it matched.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

DEFAULT_MAX_ITEMS = 10


class RelevanceReason(str, Enum):
    SAME_ENTITY = "same_entity"
    LINKED_ENTITY = "linked_entity"
    RECENCY = "recency"


_TIER_ORDER = {
    RelevanceReason.SAME_ENTITY: 0,
    RelevanceReason.LINKED_ENTITY: 1,
    RelevanceReason.RECENCY: 2,
}


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")


@dataclass(frozen=True, slots=True)
class PriorOutcomeRecord:
    """A prior outcome on the bitemporal axes of ``ProgramFactSnapshot``:
    ``as_of`` is transaction time, ``valid_at`` is world-state time."""

    record_id: str
    entity_id: str
    kind: str
    as_of: datetime
    valid_at: datetime
    outcome_summary: str
    evidence_refs: tuple[str, ...]
    linked_entity_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("record_id", "entity_id", "kind"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        _require_aware("as_of", self.as_of)
        _require_aware("valid_at", self.valid_at)


@dataclass(frozen=True, slots=True)
class SelectedPriorOutcome:
    record: PriorOutcomeRecord
    reason: RelevanceReason
    matched_entity_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PriorOutcomeSelection:
    selected: tuple[SelectedPriorOutcome, ...]
    candidate_count: int
    omitted_count: int


def _classify(
    record: PriorOutcomeRecord, active: frozenset[str]
) -> SelectedPriorOutcome:
    if record.entity_id in active:
        return SelectedPriorOutcome(record, RelevanceReason.SAME_ENTITY, (record.entity_id,))
    linked = tuple(sorted(active.intersection(record.linked_entity_ids)))
    if linked:
        return SelectedPriorOutcome(record, RelevanceReason.LINKED_ENTITY, linked)
    return SelectedPriorOutcome(record, RelevanceReason.RECENCY, ())


def select_prior_outcomes(
    records: Iterable[PriorOutcomeRecord],
    active_entity_ids: Iterable[str],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    as_of: datetime | None = None,
) -> PriorOutcomeSelection:
    if max_items < 1:
        raise ValueError("max_items must be >= 1")
    if as_of is not None:
        _require_aware("as_of", as_of)
    records = tuple(records)
    seen: set[str] = set()
    for record in records:
        if record.record_id in seen:
            raise ValueError(f"duplicate record_id: {record.record_id}")
        seen.add(record.record_id)

    active = frozenset(active_entity_ids)
    candidates = [
        _classify(record, active)
        for record in records
        if as_of is None or record.as_of <= as_of
    ]
    # Two-pass stable sort: record_id ascending, then tier and newest valid_at.
    candidates.sort(key=lambda item: item.record.record_id)
    candidates.sort(
        key=lambda item: (_TIER_ORDER[item.reason], -item.record.valid_at.timestamp())
    )
    selected = tuple(candidates[:max_items])
    return PriorOutcomeSelection(
        selected=selected,
        candidate_count=len(candidates),
        omitted_count=len(candidates) - len(selected),
    )
