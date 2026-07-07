from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Callable, TYPE_CHECKING
from urllib.parse import quote

from src.core.config_loader import ScorecardDimensionSettings
from src.core.models import RiskLevel, ScorecardEvidencePacket, Snapshot, WorkItem
from src.core.slice_contract_loader import SliceAdoSourceContract, SliceContract, SliceFilterDefinition, SlicePredicateDefinition
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES

if TYPE_CHECKING:
    from src.core.overrides_store import GovernanceState


_DFD_PROXIMITY_DAYS = 14


def _build_dfd_annotation(governance: "GovernanceState | None", today: date) -> str:
    """Return DFD proximity annotation text for a proximity-sensitive dimension.

    Overdue DFD takes precedence; otherwise annotate when within the proximity window.
    """
    if governance is None or governance.dfd_date is None:
        return ""
    dfd_date = governance.dfd_date
    if dfd_date < today:
        return "⚠️ DFD Overdue"
    if (dfd_date - today).days <= _DFD_PROXIMITY_DAYS:
        return f"DFD: {dfd_date.isoformat()}"
    return ""


_ALLOWED_FIELDS = {"area_path", "tag", "title", "type", "state", "assigned_to", "risk_level"}
_ALLOWED_OPERATORS = {"contains", "eq", "ne"}
_SAVED_QUERY_IDS_FIELD = "saved_query_ids"
_RISK_PRIORITY = {
    RiskLevel.HIGH: 4,
    RiskLevel.MEDIUM: 3,
    RiskLevel.LOW: 2,
    RiskLevel.DONE: 1,
    RiskLevel.UNKNOWN: 0,
}


@dataclass(frozen=True, slots=True)
class _Predicate:
    field_name: str
    operator: str
    value: str


@dataclass(frozen=True, slots=True)
class DimensionAssignment:
    items: tuple[WorkItem, ...]
    ado_query_url: str


def build_scorecard(
    items: tuple[WorkItem, ...] | list[WorkItem],
    dimensions: tuple[ScorecardDimensionSettings, ...] | list[ScorecardDimensionSettings],
    prev_confirmed: Snapshot | None,
    scorecard_name: str | None = None,
    slice_contracts: dict[tuple[str, str], SliceContract] | None = None,
    stale_warn_days: int = 14,
    ado_query_base_url: str = "https://dev.azure.com/query",
    ado_item_base_url: str = "https://dev.azure.com/workitems",
    governance: "GovernanceState | None" = None,
    today: date | None = None,
) -> list[ScorecardEvidencePacket]:
    packets: list[ScorecardEvidencePacket] = []
    resolved_today = today or date.today()
    escalation_badge = (
        "⚠️ LT Escalation Active"
        if governance is not None and governance.escalation_active
        else ""
    )
    for dimension in dimensions:
        slice_contract = None
        if slice_contracts is not None and scorecard_name is not None:
            slice_contract = slice_contracts.get((scorecard_name, dimension.name))
        assignment = assign_dimension_items(
            items,
            dimension,
            slice_contract=slice_contract,
            ado_query_base_url=ado_query_base_url,
        )
        matching = assignment.items
        items_by_risk = dict(Counter(item.risk_level.value for item in matching))
        derived_risk = _derive_dimension_risk(matching)
        stale_items = tuple(
            item.id for item in matching if _is_stale(item, threshold_days=stale_warn_days)
        )
        overdue_items = tuple(item.id for item in matching if _is_overdue(item))
        blocked_items = tuple(item.id for item in matching if _is_blocked(item))
        unowned_items = tuple(item.id for item in matching if not item.assigned_to)
        high_activity_items = tuple(item.id for item in matching if _has_high_activity(item))
        next_target_date, latest_target_date = _derive_scorecard_target_dates(matching)
        dfd_annotation = (
            _build_dfd_annotation(governance, resolved_today)
            if dimension.dfd_proximity_sensitive
            else ""
        )
        packets.append(
            ScorecardEvidencePacket(
                dimension_name=dimension.name,
                dimension_description=dimension.description or "",
                total_items=len(matching),
                items_by_risk=items_by_risk,
                stale_items=stale_items,
                stale_count=len(stale_items),
                overdue_items=overdue_items,
                overdue_count=len(overdue_items),
                blocked_items=blocked_items,
                blocked_count=len(blocked_items),
                unowned_items=unowned_items,
                unowned_count=len(unowned_items),
                high_activity_items=high_activity_items,
                prior_confirmed_risk=_get_prior_dimension_risk(
                    dimension.name,
                    prev_confirmed,
                    scorecard_name,
                ),
                author_risk=None,
                derived_risk=derived_risk,
                ado_query_url=assignment.ado_query_url,
                item_links=tuple(f"{ado_item_base_url}/{item.id}" for item in matching),
                item_ids=tuple(item.id for item in matching),
                next_target_date=next_target_date,
                latest_target_date=latest_target_date,
                dfd_annotation=dfd_annotation,
                escalation_badge=escalation_badge,
            )
        )
    return packets


def assign_dimension_items(
    items: tuple[WorkItem, ...] | list[WorkItem],
    dimension: ScorecardDimensionSettings,
    *,
    slice_contract: SliceContract | None,
    ado_query_base_url: str = "https://dev.azure.com/query",
) -> DimensionAssignment:
    if slice_contract is None:
        matching = tuple(item for item in items if matches_filter(item, dimension.ado_filter))
        query_url = f"{ado_query_base_url}?filter={quote(dimension.ado_filter)}" if dimension.ado_filter else ""
        return DimensionAssignment(items=matching, ado_query_url=query_url)

    if slice_contract.assignment_mode == "manual_only":
        explicit_work_item_ids = _explicit_work_item_ids(slice_contract)
        matching = tuple(item for item in items if item.id in explicit_work_item_ids)
        return DimensionAssignment(items=matching, ado_query_url="")

    ado_contract = slice_contract.source_contract.ado
    if ado_contract is None:
        raise ValueError(f"Slice '{slice_contract.id}' is missing an ADO source contract")
    compiled_filter = compile_filter_definition(ado_contract.filters)
    explicit_work_item_ids = set(ado_contract.explicit_work_item_ids)
    if not compiled_filter and not explicit_work_item_ids:
        if slice_contract.degradation.blank_filter_is_error:
            raise ValueError(f"Slice '{slice_contract.id}' has blank assignment rules")
        return DimensionAssignment(items=(), ado_query_url="")

    scoped_items = _restrict_items_to_saved_query_scope(items, ado_contract.saved_queries)
    scoped_item_ids = {item.id for item in scoped_items}
    candidate_items = tuple(
        item
        for item in items
        if item.id in scoped_item_ids or item.id in explicit_work_item_ids
    )
    matching = tuple(
        item
        for item in candidate_items
        if item.id in explicit_work_item_ids
        or (
            _matches_dimension_filter(item, dimension.ado_filter)
            and (not compiled_filter or matches_filter(item, compiled_filter))
        )
    )
    query_url = _resolve_ado_query_url(ado_contract=ado_contract, ado_query_base_url=ado_query_base_url)
    return DimensionAssignment(items=matching, ado_query_url=query_url)


def _restrict_items_to_saved_query_scope(
    items: tuple[WorkItem, ...] | list[WorkItem],
    saved_queries: tuple[str, ...],
) -> tuple[WorkItem, ...]:
    if not saved_queries:
        return tuple(items)

    scope = set(saved_queries)
    scoped_items = tuple(item for item in items if _item_saved_query_scope(item) & scope)
    if scoped_items:
        return scoped_items
    if any(_item_saved_query_scope(item) for item in items):
        return ()
    return tuple(items)


def _item_saved_query_scope(item: WorkItem) -> set[str]:
    raw_value = item.custom_fields.get(_SAVED_QUERY_IDS_FIELD)
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        return {normalized} if normalized else set()
    if isinstance(raw_value, (list, tuple, set)):
        return {str(value).strip() for value in raw_value if str(value).strip()}
    return set()


def _matches_dimension_filter(item: WorkItem, ado_filter: str | None) -> bool:
    if ado_filter is None or not ado_filter.strip():
        return True
    return matches_filter(item, ado_filter)


def matches_filter(item: WorkItem, ado_filter: str | tuple[tuple[_Predicate, ...], ...]) -> bool:
    groups = parse_ado_filter(ado_filter) if isinstance(ado_filter, str) else ado_filter
    if not groups:
        return True
    return any(all(_predicate_matches(item, predicate) for predicate in group) for group in groups)


def parse_ado_filter(filter_expression: str) -> tuple[tuple[_Predicate, ...], ...]:
    if not filter_expression.strip():
        return ()

    groups: list[list[_Predicate]] = []
    current_group: list[_Predicate] = []
    position = _skip_whitespace(filter_expression, 0)
    while position < len(filter_expression):
        predicate, position = _parse_predicate(filter_expression, position)
        current_group.append(predicate)
        position = _skip_whitespace(filter_expression, position)
        if position >= len(filter_expression):
            break
        connector_match = re.match(r"(AND|OR)\b", filter_expression[position:], flags=re.IGNORECASE)
        if connector_match is None:
            raise ValueError(f"Malformed filter at position {position}: expected AND or OR")
        connector = connector_match.group(1).upper()
        position += connector_match.end()
        position = _skip_whitespace(filter_expression, position)
        if connector == "OR":
            groups.append(current_group)
            current_group = []
    groups.append(current_group)
    return tuple(tuple(group) for group in groups)


def compile_filter_definition(
    filter_definition: SliceFilterDefinition | None,
) -> tuple[tuple[_Predicate, ...], ...]:
    if filter_definition is None or filter_definition.is_empty():
        return ()
    all_of = tuple(_predicate_from_definition(predicate) for predicate in filter_definition.all_of)
    if filter_definition.any_of:
        return tuple(
            all_of + (_predicate_from_definition(predicate),)
            for predicate in filter_definition.any_of
        )
    return (all_of,)


def render_filter_definition(filter_definition: SliceFilterDefinition | None) -> str:
    groups = compile_filter_definition(filter_definition)
    if not groups:
        return ""
    rendered_groups = []
    for group in groups:
        rendered_groups.append(
            " AND ".join(
                f"{predicate.field_name} {predicate.operator} '{predicate.value}'"
                for predicate in group
            )
        )
    return " OR ".join(rendered_groups)


def _resolve_ado_query_url(*, ado_contract: SliceAdoSourceContract, ado_query_base_url: str) -> str:
    if ado_contract.saved_queries:
        return f"{ado_query_base_url.rstrip('/')}/{ado_contract.saved_queries[0]}"
    query_text = render_filter_definition(ado_contract.filters)
    return f"{ado_query_base_url}?filter={quote(query_text)}" if query_text else ""


def _parse_predicate(filter_expression: str, position: int) -> tuple[_Predicate, int]:
    field_match = re.match(r"([a-z_]+)\b", filter_expression[position:], flags=re.IGNORECASE)
    if field_match is None:
        raise ValueError(f"Malformed filter at position {position}: expected field name")
    field_name = field_match.group(1).lower()
    if field_name not in _ALLOWED_FIELDS:
        raise ValueError(f"Unknown field name at position {position}: {field_name}")
    position += field_match.end()
    position = _skip_whitespace(filter_expression, position)

    operator_match = re.match(r"(contains|eq|ne)\b", filter_expression[position:], flags=re.IGNORECASE)
    if operator_match is None:
        raise ValueError(f"Malformed filter at position {position}: expected operator")
    operator = operator_match.group(1).lower()
    if operator not in _ALLOWED_OPERATORS:
        raise ValueError(f"Malformed filter at position {position}: unsupported operator")
    position += operator_match.end()
    position = _skip_whitespace(filter_expression, position)

    value_match = re.match(r"'([^']*)'", filter_expression[position:])
    if value_match is None:
        raise ValueError(f"Malformed filter at position {position}: expected quoted value")
    value = value_match.group(1)
    position += value_match.end()
    position = _skip_whitespace(filter_expression, position)
    return _Predicate(field_name=field_name, operator=operator, value=value), position


def _skip_whitespace(filter_expression: str, position: int) -> int:
    while position < len(filter_expression) and filter_expression[position].isspace():
        position += 1
    return position


def _predicate_matches(item: WorkItem, predicate: _Predicate) -> bool:
    values = _field_values(item, predicate.field_name)
    expected = predicate.value.lower()
    if predicate.operator == "contains":
        return any(expected in value.lower() for value in values)
    if predicate.operator == "eq":
        return any(value.lower() == expected for value in values)
    if predicate.operator == "ne":
        return all(value.lower() != expected for value in values)
    raise ValueError(f"Unsupported operator: {predicate.operator}")


def _field_values(item: WorkItem, field_name: str) -> tuple[str, ...]:
    field_getters: dict[str, Callable[[WorkItem], tuple[str, ...]]] = {
        "area_path": lambda work_item: (work_item.area_path,),
        "tag": lambda work_item: tuple(work_item.tags),
        "title": lambda work_item: (work_item.title,),
        "type": lambda work_item: (work_item.type,),
        "state": lambda work_item: (work_item.state,),
        "assigned_to": lambda work_item: ((work_item.assigned_to or ""),),
        "risk_level": lambda work_item: (work_item.risk_level.value,),
    }
    return field_getters[field_name](item)


def _predicate_from_definition(predicate: SlicePredicateDefinition) -> _Predicate:
    field_name = predicate.field.strip().lower()
    if field_name not in _ALLOWED_FIELDS:
        raise ValueError(f"Unknown field name in slice contract: {field_name}")
    operator = predicate.op.strip().lower()
    if operator not in _ALLOWED_OPERATORS:
        raise ValueError(f"Unsupported operator in slice contract: {operator}")
    return _Predicate(field_name=field_name, operator=operator, value=predicate.value.strip())


def _explicit_work_item_ids(slice_contract: SliceContract) -> set[int]:
    ado_contract = slice_contract.source_contract.ado
    if ado_contract is None:
        return set()
    return set(ado_contract.explicit_work_item_ids)


def _get_prior_dimension_risk(
    dimension_name: str,
    prev_confirmed: Snapshot | None,
    scorecard_name: str | None,
) -> RiskLevel | None:
    if prev_confirmed is None:
        return None
    for dimension in prev_confirmed.scorecards:
        if dimension.name != dimension_name:
            continue
        if scorecard_name is not None and dimension.scorecard_name != scorecard_name:
            continue
        return dimension.risk
    return None


def _derive_dimension_risk(items: tuple[WorkItem, ...]) -> RiskLevel:
    if not items:
        return RiskLevel.UNKNOWN
    return max((item.risk_level for item in items), key=lambda risk: _RISK_PRIORITY.get(risk, 0))


def _derive_scorecard_target_dates(
    items: tuple[WorkItem, ...],
) -> tuple[date | None, date | None]:
    target_dates = sorted(
        item.target_date
        for item in items
        if item.target_date is not None and item.state.lower() not in TERMINAL_WORK_ITEM_STATES
    )
    if not target_dates:
        return None, None
    as_of_date = max(item.fetched_at for item in items).date() if items else None
    future_dates = [target_date for target_date in target_dates if as_of_date is not None and target_date >= as_of_date]
    next_target_date = future_dates[0] if future_dates else None
    latest_target_date = target_dates[-1]
    return next_target_date, latest_target_date


def _is_stale(item: WorkItem, threshold_days: int) -> bool:
    if item.state.lower() in TERMINAL_WORK_ITEM_STATES:
        return False
    activity_timestamps = [revision.changed_date for revision in item.revisions]
    activity_timestamps.extend(comment.created_date for comment in item.comments)
    if not activity_timestamps:
        return False
    latest_activity = max(activity_timestamps)
    return (item.fetched_at - latest_activity).days >= threshold_days


def _is_overdue(item: WorkItem) -> bool:
    if item.target_date is None:
        return False
    if item.state.lower() in TERMINAL_WORK_ITEM_STATES:
        return False
    return item.target_date < item.fetched_at.date()


def _is_blocked(item: WorkItem) -> bool:
    if any("blocked" in tag.lower() for tag in item.tags):
        return True
    if item.custom_fields.get("blocked") is True:
        return True
    return "blocked" in item.state.lower()


def _has_high_activity(item: WorkItem) -> bool:
    recent_window_start = item.fetched_at - timedelta(days=7)
    recent_changes = sum(1 for revision in item.revisions if revision.changed_date >= recent_window_start)
    recent_changes += sum(1 for comment in item.comments if comment.created_date >= recent_window_start)
    return recent_changes >= 3
