from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from collections.abc import Collection
from typing import Iterable, Mapping

from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Signal


_INITIAL_STATES = {"new", "proposed"}
_ITEM_REF_RE = re.compile(r"\bWI[:#\s-]?(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CoverageGap:
    work_item_id: int
    title: str
    state: str
    assigned_to: str | None
    confidence: Confidence = Confidence.NONE


def coverage_gap_confidence_label(gap: CoverageGap) -> str:
    return f"{gap.confidence.value.lower()} confidence"


def build_coverage_gaps(
    items: tuple[WorkItem, ...],
    *,
    approved_signals: tuple[Signal, ...],
    narratives: Mapping[str, str] | Iterable[str],
    as_of: datetime,
    min_age_days: int = 7,
    covered_item_ids: Collection[int] = (),
) -> tuple[CoverageGap, ...]:
    resolved_covered_item_ids = set(int(item_id) for item_id in covered_item_ids)
    resolved_covered_item_ids.update(_item_refs_from_signals(approved_signals))
    resolved_covered_item_ids.update(_item_refs_from_narratives(narratives))
    gaps: list[CoverageGap] = []
    for item in items:
        if not _is_gap_candidate(item, as_of=as_of, min_age_days=min_age_days):
            continue
        if item.id in resolved_covered_item_ids:
            continue
        gaps.append(
            CoverageGap(
                work_item_id=item.id,
                title=item.title,
                state=item.state,
                assigned_to=item.assigned_to,
                confidence=Confidence.HIGH,
            )
        )
    return tuple(sorted(gaps, key=lambda gap: gap.work_item_id))


def _is_gap_candidate(item: WorkItem, *, as_of: datetime, min_age_days: int) -> bool:
    if item.state.strip().lower() in _INITIAL_STATES:
        return False
    changed_date = item.custom_fields.get("changed_date")
    if isinstance(changed_date, str):
        parsed = datetime.fromisoformat(changed_date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = max(0, (as_of.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).days)
        if age_days < min_age_days:
            return False
    return True


def _item_refs_from_signals(signals: tuple[Signal, ...]) -> set[int]:
    item_ids: set[int] = set()
    for signal in signals:
        for ref in signal.entity_refs:
            match = _ITEM_REF_RE.search(ref)
            if match is not None:
                item_ids.add(int(match.group(1)))
    return item_ids


def _item_refs_from_narratives(narratives: Mapping[str, str] | Iterable[str]) -> set[int]:
    values = narratives.values() if isinstance(narratives, Mapping) else narratives
    item_ids: set[int] = set()
    for content in values:
        for match in _ITEM_REF_RE.finditer(content):
            item_ids.add(int(match.group(1)))
    return item_ids