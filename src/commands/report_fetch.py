from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

from src.core.ado_enrichment import (
    ADO_RISK_ASSESSMENT_COMMENT_FIELD,
    ADO_RISK_ASSESSMENT_FIELD,
    ADO_ANALYTICS_HISTORY_FIELDS,
    ADO_CHILD_BATCH_FIELDS,
    build_analytics_history,
    build_child_work_items,
    build_significant_findings,
    extract_child_ids_by_parent,
    infer_ado_risk_level,
    normalize_risk_assessment,
    serialize_trajectory_points,
)
from src.core.ado_saved_query_helpers import (
    bound_saved_query_wiql as _bound_saved_query_wiql,
    extract_saved_query_wiql as _extract_saved_query_wiql,
    merge_item_ids as _merge_item_ids,
    query_work_item_batch_rows as _query_work_item_batch_rows,
    query_work_item_snapshot_history_rows as _query_work_item_snapshot_history_rows,
)
from src.core.config_loader import ReportBundle
from src.core.exceptions import QueryError
from src.core.models import RiskLevel, WorkItem
from src.core.query_builder import build_odata_filter
from src.commands.gather_pipeline.slice_contract_helpers import (
    render_saved_query_filter_clause as _shared_render_saved_query_filter_clause,
    slice_contract_saved_query_clauses as _shared_slice_contract_saved_query_clauses,
)

DEFAULT_ADO_TOP = 1000
_WORK_ITEM_BATCH_SIZE = 200
_ADO_WIQL_TOP_CAP = 2000
_BATCH_FIELDS = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AssignedTo",
    "System.AreaPath",
    "System.IterationPath",
    "System.ChangedDate",
    "Microsoft.VSTS.Scheduling.TargetDate",
    "System.Tags",
)
_TRAJECTORY_BACKFILL_WINDOW_DAYS = 180


def _load_saved_query_item_ids(
    client: Any,
    query_ids: tuple[str, ...],
    *,
    since: datetime | None,
    query_clauses: dict[str, str] | None = None,
) -> tuple[list[int], dict[int, tuple[str, ...]], int]:
    from src.core.ado_saved_query_helpers import load_saved_query_item_ids

    return load_saved_query_item_ids(
        client,
        query_ids,
        since=since,
        query_clauses=query_clauses,
        top_cap=_ADO_WIQL_TOP_CAP,
        logger=log,
    )


def _load_live_work_items(
    bundle: ReportBundle,
    as_of: datetime,
    *,
    ado_client_factory: Callable[..., Any],
    slice_contract_full_scope_query_ids: Callable[[ReportBundle], tuple[str, ...]],
    slice_contract_activity_delta_query_ids: Callable[[ReportBundle], tuple[str, ...]],
    slice_contract_saved_query_clauses: Callable[[ReportBundle], dict[str, str]],
    slice_contract_explicit_work_item_ids: Callable[[ReportBundle], list[int]],
    query_work_item_batch_rows: Callable[[Any, list[int], tuple[str, ...]], tuple[list[dict[str, Any]], int]],
    work_item_from_sources: Callable[[dict[str, Any], dict[str, Any], datetime], WorkItem],
) -> tuple[tuple[WorkItem, ...], int]:
    since = as_of - timedelta(days=bundle.config.ado.date_window_days)
    client = ado_client_factory(
        organization=bundle.config.ado.organization,
        project=bundle.config.ado.project,
        timeout=bundle.config.ado_fetch_timeout_seconds,
    )
    rows = client.query_all(
        filter_expression=build_odata_filter(
            area_paths=bundle.config.ado.area_paths,
            work_item_types=bundle.config.ado.work_item_types,
            since=since,
            states_excluded=bundle.config.ado.excluded_states,
        ),
        select_fields=(
            "WorkItemId",
            "WorkItemType",
            "Title",
            "State",
            "ChangedDate",
        ),
        top=DEFAULT_ADO_TOP,
    )
    row_by_id = {
        int(row.get("WorkItemId") or row.get("id") or 0): row
        for row in rows
        if int(row.get("WorkItemId") or row.get("id") or 0) > 0
    }
    ids = list(row_by_id)
    saved_query_clauses = slice_contract_saved_query_clauses(bundle)
    # Armada spec D-2: `full_scope` bindings are never date-bounded (since=None,
    # matching gather's undated membership); `activity_delta` bindings keep the
    # existing recent-activity date bound. `analytics_history` bindings are
    # audit-only and are intentionally excluded from live-item membership.
    full_scope_ids, full_scope_membership, full_scope_ado_calls = _load_saved_query_item_ids(
        client,
        slice_contract_full_scope_query_ids(bundle),
        since=None,
        query_clauses=saved_query_clauses,
    )
    activity_delta_ids, activity_delta_membership, activity_delta_ado_calls = _load_saved_query_item_ids(
        client,
        slice_contract_activity_delta_query_ids(bundle),
        since=since,
        query_clauses=saved_query_clauses,
    )
    saved_query_item_ids = _merge_item_ids(full_scope_ids, activity_delta_ids)
    saved_query_membership: dict[int, tuple[str, ...]] = {}
    for membership in (full_scope_membership, activity_delta_membership):
        for work_item_id, query_ids in membership.items():
            existing = saved_query_membership.get(work_item_id, ())
            saved_query_membership[work_item_id] = existing + tuple(
                query_id for query_id in query_ids if query_id not in existing
            )
    saved_query_ado_calls = full_scope_ado_calls + activity_delta_ado_calls
    ids = _merge_item_ids(ids, saved_query_item_ids)
    ids = _merge_item_ids(ids, slice_contract_explicit_work_item_ids(bundle))
    batch_rows, batch_ado_calls = query_work_item_batch_rows(client, ids, _BATCH_FIELDS)
    batch_by_id = {int(row.get("id") or row.get("fields", {}).get("System.Id") or 0): row for row in batch_rows}
    work_items: list[WorkItem] = []
    for work_item_id in ids:
        item = work_item_from_sources(
            row_by_id.get(work_item_id, {}),
            batch_by_id.get(work_item_id, {}),
            as_of,
        )
        saved_query_ids = saved_query_membership.get(work_item_id, ())
        if saved_query_ids:
            item.custom_fields["saved_query_ids"] = saved_query_ids
        work_items.append(item)

    work_item_lookup = {item.id: item for item in work_items}
    child_ids_by_parent: dict[int, tuple[int, ...]] = {}
    child_batch_ado_calls = 0
    child_lookup: dict[int, Any] = {}
    relation_loader = getattr(client, "get_work_item_relations", None)
    if callable(relation_loader):
        try:
            relation_rows = relation_loader(ids)
            child_ids_by_parent = extract_child_ids_by_parent(relation_rows)
            child_ids = sorted({child_id for child_ids in child_ids_by_parent.values() for child_id in child_ids})
            child_rows, child_batch_ado_calls = query_work_item_batch_rows(client, child_ids, ADO_CHILD_BATCH_FIELDS)
            child_lookup = {child.id: child for child in build_child_work_items(child_rows)}
        except QueryError:
            child_ids_by_parent = {}
            child_batch_ado_calls = 0
            child_lookup = {}
    analytics_history: dict[int, tuple] = {}
    history_loader = getattr(client, "query_work_item_snapshot_history", None)
    if callable(history_loader):
        try:
            history_rows, history_ado_calls = _query_work_item_snapshot_history_rows(
                client,
                ids,
                select_fields=ADO_ANALYTICS_HISTORY_FIELDS,
                start_date=(as_of - timedelta(days=_TRAJECTORY_BACKFILL_WINDOW_DAYS)).date().isoformat(),
                batch_size=_WORK_ITEM_BATCH_SIZE,
            )
            analytics_history = build_analytics_history(history_rows, work_item_lookup)
        except QueryError:
            analytics_history = {}
            history_ado_calls = 0
    else:
        history_ado_calls = 0
    for item in work_items:
        item.child_items = tuple(
            child_lookup[child_id]
            for child_id in child_ids_by_parent.get(item.id, ())
            if child_id in child_lookup
        )
        history_points = analytics_history.get(item.id, ())
        if history_points:
            item.custom_fields["analytics_history"] = list(serialize_trajectory_points(history_points))
        significant_findings = build_significant_findings(item, history_points, as_of=as_of.date())
        if significant_findings:
            item.custom_fields["significant_findings"] = list(significant_findings)
    return (
        tuple(work_items),
        1 + saved_query_ado_calls + batch_ado_calls + child_batch_ado_calls + history_ado_calls + (len(ids) if child_ids_by_parent else 0),
    )


def _slice_contract_saved_query_ids(bundle: ReportBundle) -> tuple[str, ...]:
    if not bundle.slice_contracts:
        return ()

    ordered_query_ids: list[str] = []
    seen_query_ids: set[str] = set()
    for contract in bundle.slice_contracts:
        ado_contract = contract.source_contract.ado
        if ado_contract is None:
            continue
        for query_id in ado_contract.saved_queries:
            if query_id in seen_query_ids:
                continue
            seen_query_ids.add(query_id)
            ordered_query_ids.append(query_id)
    return tuple(ordered_query_ids)


def _slice_contract_saved_query_ids_by_mode(bundle: ReportBundle, mode: str) -> tuple[str, ...]:
    if not bundle.slice_contracts:
        return ()

    ordered_query_ids: list[str] = []
    seen_query_ids: set[str] = set()
    for contract in bundle.slice_contracts:
        ado_contract = contract.source_contract.ado
        if ado_contract is None:
            continue
        for binding in ado_contract.saved_query_bindings:
            if binding.mode != mode:
                continue
            if binding.query_id in seen_query_ids:
                continue
            seen_query_ids.add(binding.query_id)
            ordered_query_ids.append(binding.query_id)
    return tuple(ordered_query_ids)


def _slice_contract_full_scope_query_ids(bundle: ReportBundle) -> tuple[str, ...]:
    return _slice_contract_saved_query_ids_by_mode(bundle, "full_scope")


def _slice_contract_activity_delta_query_ids(bundle: ReportBundle) -> tuple[str, ...]:
    """Armada spec D-2: if a query id is bound `full_scope` anywhere it is never
    date-bounded, even when the same GUID is also separately bound `activity_delta`
    (safety-first — full-scope classification always wins for that query id).
    """
    full_scope_ids = set(_slice_contract_full_scope_query_ids(bundle))
    return tuple(
        query_id
        for query_id in _slice_contract_saved_query_ids_by_mode(bundle, "activity_delta")
        if query_id not in full_scope_ids
    )


def _slice_contract_explicit_work_item_ids(bundle: ReportBundle) -> list[int]:
    if not bundle.slice_contracts:
        return []

    ordered_item_ids: list[int] = []
    seen_item_ids: set[int] = set()
    for contract in bundle.slice_contracts:
        ado_contract = contract.source_contract.ado
        if ado_contract is None:
            continue
        for work_item_id in ado_contract.explicit_work_item_ids:
            if work_item_id in seen_item_ids:
                continue
            seen_item_ids.add(work_item_id)
            ordered_item_ids.append(work_item_id)
    return ordered_item_ids


def _slice_contract_saved_query_clauses(bundle: ReportBundle) -> dict[str, str]:
    return _shared_slice_contract_saved_query_clauses(bundle.slice_contracts)


def _render_saved_query_filter_clause(filter_definition: Any) -> str:
    """Compatibility export for existing report callers and tests.

    Gather and report must render the exact same WIQL predicates (D-2/D-4),
    so the implementation lives in the shared slice-contract helper.
    """
    return _shared_render_saved_query_filter_clause(filter_definition)


def _work_item_from_sources(raw: dict[str, Any], batch_row: dict[str, Any], fetched_at: datetime) -> WorkItem:
    fields = batch_row.get("fields", {}) if isinstance(batch_row, dict) else {}
    work_item_id = int(raw.get("WorkItemId") or raw.get("id") or fields.get("System.Id") or 0)
    assigned_to, assigned_to_email = _parse_identity(
        fields.get("System.AssignedTo")
        or _raw_identity(raw)
    )
    tags = _parse_tags(fields.get("System.Tags") or raw.get("Tags"))
    state = str(fields.get("System.State") or raw.get("State") or "Active")
    changed_date = _parse_datetime(fields.get("System.ChangedDate") or raw.get("ChangedDate"))
    risk_assessment = normalize_risk_assessment(fields.get(ADO_RISK_ASSESSMENT_FIELD))
    custom_fields: dict[str, object] = {}
    if changed_date is not None:
        custom_fields["changed_date"] = changed_date.isoformat()
    return WorkItem(
        id=work_item_id,
        type=str(fields.get("System.WorkItemType") or raw.get("WorkItemType") or "WorkItem"),
        title=str(fields.get("System.Title") or raw.get("Title") or f"Work Item {work_item_id}"),
        state=state,
        assigned_to=assigned_to,
        assigned_to_email=assigned_to_email,
        area_path=str(fields.get("System.AreaPath") or raw.get("AreaPath") or raw.get("Area", {}).get("AreaPath") or ""),
        iteration_path=str(fields.get("System.IterationPath") or raw.get("IterationPath") or ""),
        target_date=_parse_date(fields.get("Microsoft.VSTS.Scheduling.TargetDate") or raw.get("TargetDate")),
        risk_level=_infer_risk_level(state, tags, risk_assessment),
        tags=tags,
        custom_fields=custom_fields,
        revisions=[],
        comments=[],
        fetched_at=fetched_at,
        risk_assessment=risk_assessment,
        risk_assessment_comment=_optional_string(fields.get(ADO_RISK_ASSESSMENT_COMMENT_FIELD)),
    )


def _work_item_from_raw(raw: dict[str, Any], fetched_at: datetime) -> WorkItem:
    return _work_item_from_sources(raw, {}, fetched_at)


def _raw_identity(raw: dict[str, Any]) -> dict[str, Any] | None:
    assigned_to = raw.get("AssignedTo")
    assigned_to_email = raw.get("AssignedToEmail")
    if assigned_to is None and assigned_to_email is None:
        return None
    return {
        "displayName": assigned_to,
        "uniqueName": assigned_to_email,
    }


def _parse_identity(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        display_name = value.get("displayName") or value.get("name")
        email = value.get("uniqueName") or value.get("mailAddress")
        return (_optional_string(display_name), _optional_string(email))
    if isinstance(value, str):
        return (value, None)
    return (None, None)


def _parse_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(";") if tag.strip()]
    if isinstance(value, (list, tuple)):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    return [str(value)]


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _infer_risk_level(state: str, tags: list[str], risk_assessment: str | None = None) -> RiskLevel:
    return infer_ado_risk_level(state, tags, risk_assessment)


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
