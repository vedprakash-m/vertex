from __future__ import annotations

from dataclasses import dataclass

from src.core.models import Confidence, FreshnessReport, WorkItem
from src.core.models_v2 import ActionItem, ClaimEntry, DecisionAsk, RaidChainLink, RiskEntry, RiskStatus, Signal
from src.core.signal_ranking import signal_source_family


_SEVERITY_ORDER = {"block": 0, "warn": 1, "info": 2}
_SOURCE_ORDER = {
    "ado_blocked": 0,
    "freshness_block": 1,
    "icm_incident": 2,
    "decision_ask": 3,
    "overdue_action": 4,
}


@dataclass(frozen=True, slots=True)
class IssueProjection:
    work_item_id: int | None
    source_type: str
    severity: str
    summary: str
    owner_alias: str | None
    workstream_id: str | None
    ado_url: str | None
    linked_entity_ids: tuple[str, ...]
    confidence: Confidence = Confidence.NONE
    raid_chain: tuple[RaidChainLink, ...] | None = None


def issue_projection_source_label(entry: IssueProjection) -> str:
    return entry.source_type.replace("_", " ")


def issue_projection_confidence_label(entry: IssueProjection) -> str:
    return f"{entry.confidence.value.lower()} confidence"


def build_issue_projection(
    items: tuple[WorkItem, ...],
    freshness_report: FreshnessReport,
    icm_signals: tuple[Signal, ...],
    open_asks: tuple[DecisionAsk, ...],
    overdue_actions: tuple[ActionItem, ...],
    open_claims: tuple[ClaimEntry, ...] = (),
    risk_entries: tuple[RiskEntry, ...] = (),
    ado_item_base_url: str | None = None,
) -> tuple[IssueProjection, ...]:
    item_lookup = {item.id: item for item in items}
    asks_by_work_item = _build_ask_index(open_asks)
    overdue_actions_by_work_item = _build_action_index(overdue_actions)
    claims_by_work_item = _build_claim_index(open_claims)
    risks_by_work_item = _build_risk_index(risk_entries)

    projections: list[IssueProjection] = []
    projections.extend(
        IssueProjection(
            work_item_id=item.id,
            source_type="ado_blocked",
            severity="block",
            summary=_format_blocked_item_summary(item),
            owner_alias=_work_item_owner(item),
            workstream_id=None,
            ado_url=_ado_item_url(item.id, ado_item_base_url),
            linked_entity_ids=_linked_entity_ids_for_work_item(
                item.id,
                asks_by_work_item=asks_by_work_item,
                overdue_actions_by_work_item=overdue_actions_by_work_item,
                claims_by_work_item=claims_by_work_item,
                risks_by_work_item=risks_by_work_item,
            ),
            confidence=Confidence.HIGH,
        )
        for item in items
        if _is_blocked_item(item)
    )
    projections.extend(
        _build_freshness_projections(
            item_lookup=item_lookup,
            freshness_report=freshness_report,
            asks_by_work_item=asks_by_work_item,
            overdue_actions_by_work_item=overdue_actions_by_work_item,
            claims_by_work_item=claims_by_work_item,
            risks_by_work_item=risks_by_work_item,
            ado_item_base_url=ado_item_base_url,
        )
    )
    projections.extend(
        _build_icm_projections(
            icm_signals,
            asks_by_work_item=asks_by_work_item,
            overdue_actions_by_work_item=overdue_actions_by_work_item,
            claims_by_work_item=claims_by_work_item,
            risks_by_work_item=risks_by_work_item,
            ado_item_base_url=ado_item_base_url,
        )
    )
    projections.extend(_build_decision_ask_projections(open_asks, ado_item_base_url=ado_item_base_url))
    projections.extend(_build_overdue_action_projections(overdue_actions, ado_item_base_url=ado_item_base_url))

    return tuple(
        sorted(
            projections,
            key=lambda entry: (
                _SEVERITY_ORDER.get(entry.severity, 99),
                _SOURCE_ORDER.get(entry.source_type, 99),
                entry.work_item_id if entry.work_item_id is not None else 1_000_000_000,
                entry.summary.lower(),
            ),
        )
    )


def _build_freshness_projections(
    *,
    item_lookup: dict[int, WorkItem],
    freshness_report: FreshnessReport,
    asks_by_work_item: dict[int, tuple[DecisionAsk, ...]],
    overdue_actions_by_work_item: dict[int, tuple[ActionItem, ...]],
    claims_by_work_item: dict[int, tuple[ClaimEntry, ...]],
    risks_by_work_item: dict[int, tuple[RiskEntry, ...]],
    ado_item_base_url: str | None,
) -> tuple[IssueProjection, ...]:
    grouped_messages: dict[int, list[str]] = {}
    for finding in freshness_report.items:
        if finding.severity != "block":
            continue
        grouped_messages.setdefault(finding.work_item_id, []).append(finding.message)

    projections: list[IssueProjection] = []
    for work_item_id, messages in sorted(grouped_messages.items()):
        item = item_lookup.get(work_item_id)
        summary = "; ".join(messages)
        if item is not None:
            summary = f'WI:{work_item_id} "{item.title}" — {summary}'
        projections.append(
            IssueProjection(
                work_item_id=work_item_id,
                source_type="freshness_block",
                severity="block",
                summary=summary,
                owner_alias=_work_item_owner(item),
                workstream_id=None,
                ado_url=_ado_item_url(work_item_id, ado_item_base_url),
                linked_entity_ids=_linked_entity_ids_for_work_item(
                    work_item_id,
                    asks_by_work_item=asks_by_work_item,
                    overdue_actions_by_work_item=overdue_actions_by_work_item,
                    claims_by_work_item=claims_by_work_item,
                    risks_by_work_item=risks_by_work_item,
                ),
                confidence=Confidence.HIGH,
            )
        )
    return tuple(projections)


def _build_icm_projections(
    icm_signals: tuple[Signal, ...],
    *,
    asks_by_work_item: dict[int, tuple[DecisionAsk, ...]],
    overdue_actions_by_work_item: dict[int, tuple[ActionItem, ...]],
    claims_by_work_item: dict[int, tuple[ClaimEntry, ...]],
    risks_by_work_item: dict[int, tuple[RiskEntry, ...]],
    ado_item_base_url: str | None,
) -> tuple[IssueProjection, ...]:
    projections: list[IssueProjection] = []
    for signal in icm_signals:
        if signal_source_family(signal.source) != "icm":
            continue
        work_item_id = _extract_first_work_item_id(signal.entity_refs)
        projections.append(
            IssueProjection(
                work_item_id=work_item_id,
                source_type="icm_incident",
                severity=_icm_signal_severity(signal),
                summary=signal.text.strip(),
                owner_alias=None,
                workstream_id=signal.workstream_id,
                ado_url=_ado_item_url(work_item_id, ado_item_base_url),
                linked_entity_ids=(
                    _linked_entity_ids_for_work_item(
                        work_item_id,
                        asks_by_work_item=asks_by_work_item,
                        overdue_actions_by_work_item=overdue_actions_by_work_item,
                        claims_by_work_item=claims_by_work_item,
                        risks_by_work_item=risks_by_work_item,
                    )
                    if work_item_id is not None
                    else ()
                ),
                confidence=signal.confidence,
            )
        )
    return tuple(projections)


def _build_decision_ask_projections(
    open_asks: tuple[DecisionAsk, ...],
    *,
    ado_item_base_url: str | None,
) -> tuple[IssueProjection, ...]:
    projections: list[IssueProjection] = []
    for ask in open_asks:
        work_item_id = _extract_first_work_item_id(ask.entity_refs)
        owner_suffix = f" (owner {ask.owner_alias})" if ask.owner_alias else ""
        projections.append(
            IssueProjection(
                work_item_id=work_item_id,
                source_type="decision_ask",
                severity="warn",
                summary=f"Issue #{ask.issue_number:03d} ask: {ask.text}{owner_suffix}",
                owner_alias=ask.owner_alias,
                workstream_id=None,
                ado_url=_ado_item_url(work_item_id, ado_item_base_url),
                linked_entity_ids=(ask.id,),
                confidence=Confidence.HIGH,
            )
        )
    return tuple(projections)


def _build_overdue_action_projections(
    overdue_actions: tuple[ActionItem, ...],
    *,
    ado_item_base_url: str | None,
) -> tuple[IssueProjection, ...]:
    projections: list[IssueProjection] = []
    for action in overdue_actions:
        work_item_id = action.linked_work_item_ids[0] if action.linked_work_item_ids else None
        due_label = action.due_date.isoformat() if action.due_date is not None else "no due date"
        projections.append(
            IssueProjection(
                work_item_id=work_item_id,
                source_type="overdue_action",
                severity="warn",
                summary=f"Overdue action: {action.text} (due {due_label})",
                owner_alias=action.owner_alias,
                workstream_id=action.workstream_id,
                ado_url=_ado_item_url(work_item_id, ado_item_base_url),
                linked_entity_ids=_linked_entity_ids_for_action(action),
                confidence=Confidence.HIGH,
            )
        )
    return tuple(projections)


def _ado_item_url(work_item_id: int | None, ado_item_base_url: str | None) -> str | None:
    if work_item_id is None or ado_item_base_url is None:
        return None
    return f"{ado_item_base_url}/{work_item_id}"


def _build_ask_index(open_asks: tuple[DecisionAsk, ...]) -> dict[int, tuple[DecisionAsk, ...]]:
    index: dict[int, list[DecisionAsk]] = {}
    for ask in open_asks:
        for work_item_id in _extract_work_item_ids(ask.entity_refs):
            index.setdefault(work_item_id, []).append(ask)
    return {work_item_id: tuple(entries) for work_item_id, entries in index.items()}


def _build_action_index(overdue_actions: tuple[ActionItem, ...]) -> dict[int, tuple[ActionItem, ...]]:
    index: dict[int, list[ActionItem]] = {}
    for action in overdue_actions:
        for work_item_id in action.linked_work_item_ids:
            index.setdefault(work_item_id, []).append(action)
    return {work_item_id: tuple(entries) for work_item_id, entries in index.items()}


def _build_claim_index(open_claims: tuple[ClaimEntry, ...]) -> dict[int, tuple[ClaimEntry, ...]]:
    index: dict[int, list[ClaimEntry]] = {}
    for claim in open_claims:
        for work_item_id in _extract_work_item_ids(claim.entity_refs):
            index.setdefault(work_item_id, []).append(claim)
    return {work_item_id: tuple(entries) for work_item_id, entries in index.items()}


def _build_risk_index(risk_entries: tuple[RiskEntry, ...]) -> dict[int, tuple[RiskEntry, ...]]:
    index: dict[int, list[RiskEntry]] = {}
    for risk in risk_entries:
        if risk.status not in {RiskStatus.OPEN, RiskStatus.ESCALATED}:
            continue
        work_item_ids = dict.fromkeys((*risk.linked_work_item_ids, *_extract_work_item_ids(risk.entity_refs)))
        for work_item_id in work_item_ids:
            index.setdefault(work_item_id, []).append(risk)
    return {work_item_id: tuple(entries) for work_item_id, entries in index.items()}


def _linked_entity_ids_for_work_item(
    work_item_id: int,
    *,
    asks_by_work_item: dict[int, tuple[DecisionAsk, ...]],
    overdue_actions_by_work_item: dict[int, tuple[ActionItem, ...]],
    claims_by_work_item: dict[int, tuple[ClaimEntry, ...]],
    risks_by_work_item: dict[int, tuple[RiskEntry, ...]],
) -> tuple[str, ...]:
    linked_ids: list[str] = []
    for ask in asks_by_work_item.get(work_item_id, ()): 
        linked_ids.append(ask.id)
    for action in overdue_actions_by_work_item.get(work_item_id, ()): 
        linked_ids.extend(_linked_entity_ids_for_action(action))
    for claim in claims_by_work_item.get(work_item_id, ()): 
        linked_ids.append(claim.id)
    for risk in risks_by_work_item.get(work_item_id, ()): 
        linked_ids.append(risk.id)
    return tuple(dict.fromkeys(linked_ids))


def _linked_entity_ids_for_action(action: ActionItem) -> tuple[str, ...]:
    linked_ids = [action.id]
    if action.linked_claim_id is not None:
        linked_ids.append(action.linked_claim_id)
    if action.linked_risk_id is not None:
        linked_ids.append(action.linked_risk_id)
    return tuple(dict.fromkeys(linked_ids))


def _extract_work_item_ids(entity_refs: tuple[str, ...]) -> tuple[int, ...]:
    return tuple(
        int(ref.split(":", 1)[1])
        for ref in entity_refs
        if ref.upper().startswith("WI:") and ref.split(":", 1)[1].isdigit()
    )


def _extract_first_work_item_id(entity_refs: tuple[str, ...]) -> int | None:
    work_item_ids = _extract_work_item_ids(entity_refs)
    if not work_item_ids:
        return None
    return work_item_ids[0]


def _format_blocked_item_summary(item: WorkItem) -> str:
    return f'WI:{item.id} "{item.title}" blocked in ADO ({item.state})'


def _work_item_owner(item: WorkItem | None) -> str | None:
    if item is None:
        return None
    return item.assigned_to_email or item.assigned_to


def _is_blocked_item(item: WorkItem) -> bool:
    if item.state.strip().lower() == "blocked":
        return True
    return any(tag.strip().lower() == "blocked" for tag in item.tags)


def _icm_signal_severity(signal: Signal) -> str:
    severity_value = None
    if signal.metadata is not None:
        severity_value = signal.metadata.get("severity")
    if isinstance(severity_value, str) and severity_value.isdigit():
        severity_value = int(severity_value)
    if isinstance(severity_value, int):
        if severity_value <= 2:
            return "block"
        if severity_value == 3:
            return "warn"
    return "info"