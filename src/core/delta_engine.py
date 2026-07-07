from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import AttributionTier, Confidence, DeltaKind, DeltaSet, EvidencePacket, ItemDelta
from src.core.models import Snapshot, WorkItem, RiskLevel
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


def build_deltas(
    current_items: tuple[WorkItem, ...] | list[WorkItem],
    previous_snapshot: Snapshot | None,
    issue_number: int,
    previous_issue_number: int | None,
    evidence_by_item: dict[int, EvidencePacket] | None = None,
    terminal_states: tuple[str, ...] = tuple(sorted(TERMINAL_WORK_ITEM_STATES)),
) -> DeltaSet:
    previous_items = {item.id: item for item in previous_snapshot.items} if previous_snapshot is not None else {}
    normalized_terminal_states = {state.lower() for state in terminal_states}
    evidence_lookup = evidence_by_item or {}

    new_items: list[ItemDelta] = []
    closed_items: list[ItemDelta] = []
    risk_changes: list[ItemDelta] = []
    eta_changes: list[ItemDelta] = []
    owner_changes: list[ItemDelta] = []
    unchanged_count = 0

    for item in current_items:
        previous = previous_items.get(item.id)
        evidence = evidence_lookup.get(item.id, _empty_evidence(item.id))
        if previous is None:
            new_items.append(
                ItemDelta(
                    work_item_id=item.id,
                    kind=DeltaKind.NEW,
                    field_changes={"id": (None, str(item.id))},
                    old_risk=None,
                    new_risk=item.risk_level,
                    old_eta=None,
                    new_eta=item.target_date,
                    evidence=evidence,
                )
            )
            continue

        emitted_change = False
        if previous.state.lower() not in normalized_terminal_states and item.state.lower() in normalized_terminal_states:
            closed_items.append(
                ItemDelta(
                    work_item_id=item.id,
                    kind=DeltaKind.CLOSED,
                    field_changes={"state": (previous.state, item.state)},
                    old_risk=previous.risk_level,
                    new_risk=item.risk_level,
                    old_eta=previous.target_date,
                    new_eta=item.target_date,
                    evidence=evidence,
                )
            )
            emitted_change = True

        risk_delta_kind = _risk_delta_kind(previous.risk_level, item.risk_level)
        if risk_delta_kind is not None:
            risk_changes.append(
                ItemDelta(
                    work_item_id=item.id,
                    kind=risk_delta_kind,
                    field_changes={
                        "risk_level": (
                            previous.risk_level.value,
                            item.risk_level.value,
                        )
                    },
                    old_risk=previous.risk_level,
                    new_risk=item.risk_level,
                    old_eta=previous.target_date,
                    new_eta=item.target_date,
                    evidence=evidence,
                )
            )
            emitted_change = True

        if previous.target_date is not None and item.target_date is not None and previous.target_date != item.target_date:
            eta_changes.append(
                ItemDelta(
                    work_item_id=item.id,
                    kind=DeltaKind.ETA_CHANGED,
                    field_changes={
                        "target_date": (
                            previous.target_date.isoformat(),
                            item.target_date.isoformat(),
                        )
                    },
                    old_risk=previous.risk_level,
                    new_risk=item.risk_level,
                    old_eta=previous.target_date,
                    new_eta=item.target_date,
                    evidence=evidence,
                )
            )
            emitted_change = True

        previous_owner = previous.assigned_to or ""
        current_owner_name = item.assigned_to or ""
        current_owner_email = item.assigned_to_email or ""
        if previous_owner not in {current_owner_name, current_owner_email}:
            owner_changes.append(
                ItemDelta(
                    work_item_id=item.id,
                    kind=DeltaKind.OWNER_CHANGED,
                    field_changes={
                        "assigned_to": (
                            previous.assigned_to,
                            item.assigned_to_email or item.assigned_to,
                        )
                    },
                    old_risk=previous.risk_level,
                    new_risk=item.risk_level,
                    old_eta=previous.target_date,
                    new_eta=item.target_date,
                    evidence=evidence,
                )
            )
            emitted_change = True

        if not emitted_change:
            unchanged_count += 1

    return DeltaSet(
        issue_number=issue_number,
        previous_issue_number=previous_issue_number,
        new_items=tuple(new_items),
        closed_items=tuple(closed_items),
        risk_changes=tuple(risk_changes),
        eta_changes=tuple(eta_changes),
        unchanged_count=unchanged_count,
        owner_changes=tuple(owner_changes),
    )


def _risk_delta_kind(old_risk: RiskLevel, new_risk: RiskLevel) -> DeltaKind | None:
    if old_risk == new_risk:
        return None
    if RiskLevel.UNKNOWN in (old_risk, new_risk):
        return None
    if new_risk == RiskLevel.DONE:
        return DeltaKind.RISK_DOWN
    if old_risk == RiskLevel.DONE:
        return DeltaKind.RISK_UP

    ordering = {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
    }
    old_value = ordering.get(old_risk)
    new_value = ordering.get(new_risk)
    if old_value is None or new_value is None:
        return None
    if new_value > old_value:
        return DeltaKind.RISK_UP
    if new_value < old_value:
        return DeltaKind.RISK_DOWN
    return None


def _empty_evidence(work_item_id: int) -> EvidencePacket:
    return EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.NONE,
        tier=AttributionTier.TIER3,
        summary_for_reviewer="No evidence provided for delta computation.",
    )
