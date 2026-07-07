from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Iterable, TypeVar

from src.core.ledger.event_log import ConfidenceTier, EventEnvelope
from src.core.ledger.source_refs import source_ref_priority


ValueT = TypeVar("ValueT")


_CONFIDENCE_RANK = {
    ConfidenceTier.INFERRED: 1,
    ConfidenceTier.AI_EXTRACTED: 2,
    ConfidenceTier.SOURCE_AUTHORITATIVE: 3,
    ConfidenceTier.OPERATOR_CONFIRMED: 4,
}


@dataclass(frozen=True, slots=True)
class FieldCandidate(Generic[ValueT]):
    event: EventEnvelope
    value: ValueT


def event_sort_key(event: EventEnvelope) -> tuple[int, datetime, int, str]:
    return (
        _CONFIDENCE_RANK[event.confidence],
        event.occurred_at,
        -source_ref_priority(event.source_ref),
        event.event_id,
    )


def choose_field_winner(candidates: Iterable[FieldCandidate[ValueT]]) -> FieldCandidate[ValueT] | None:
    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        return None
    return max(candidate_tuple, key=lambda candidate: event_sort_key(candidate.event))