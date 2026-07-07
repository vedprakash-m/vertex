from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Collection
from uuid import NAMESPACE_URL, uuid5

from src.commands.gather_pipeline.support import coerce_datetime_or_none, roll_query_value_history
from src.commands.gather_workiq_helpers import _truncate_signal_text
from src.core.ado_saved_query_helpers import merge_item_ids as _merge_item_ids
from src.core.ado_saved_query_helpers import query_work_item_batch_rows as _query_work_item_batch_rows
from src.core.freshness_engine import build_freshness_report
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import DependencyADOQuery, Program, Signal, Workstream
from src.core.signal_ref_utils import merge_entity_refs


@dataclass(frozen=True, slots=True)
class _DependencyQueryItems:
    workstream_id: str
    label: str
    resolution_path: str
    items: tuple[WorkItem, ...]


WorkItemFromSources = Callable[..., WorkItem]


def load_dependency_program_items(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    ado_client_factory: Callable[..., Any],
    batch_fields: tuple[str, ...],
    work_item_from_sources_fn: WorkItemFromSources,
) -> tuple[tuple[_DependencyQueryItems, ...], int]:
    if program.ado is None:
        return (), 0

    configured_queries = _configured_dependency_queries(workstreams)
    if not configured_queries:
        return (), 0

    client = ado_client_factory(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )
    since = as_of - timedelta(days=program.ado.date_window_days)
    groups: list[_DependencyQueryItems] = []
    ado_calls = 0

    for workstream_id, query in configured_queries:
        item_ids: list[int] = list(query.work_item_ids)
        if query.area_path is not None:
            rows = client.query_all(
                filter_expression=_build_dependency_odata_filter(program.ado, query.area_path, since),
                select_fields=(
                    "WorkItemId",
                    "WorkItemType",
                    "Title",
                    "State",
                    "ChangedDate",
                ),
            )
            ado_calls += 1
            item_ids = _merge_item_ids(
                item_ids,
                [
                    int(row.get("WorkItemId") or row.get("id") or 0)
                    for row in rows
                    if int(row.get("WorkItemId") or row.get("id") or 0) > 0
                ],
            )
        if not item_ids:
            groups.append(
                _DependencyQueryItems(
                    workstream_id=workstream_id,
                    label=query.label,
                    resolution_path=query.resolution_path,
                    items=(),
                )
            )
            continue

        batch_rows, batch_ado_calls = _query_work_item_batch_rows(client, item_ids, batch_fields)
        ado_calls += batch_ado_calls
        batch_by_id = {int(row.get("id") or row.get("fields", {}).get("System.Id") or 0): row for row in batch_rows}
        items = tuple(
            work_item_from_sources_fn(
                raw={},
                batch_row=batch_by_id.get(work_item_id, {}),
                revision_rows=[],
                comment_rows=[],
                fetched_at=as_of,
            )
            for work_item_id in item_ids
            if work_item_id in batch_by_id
        )
        for item in items:
            batch_row = batch_by_id.get(item.id, {})
            fields = batch_row.get("fields", {}) if isinstance(batch_row, dict) else {}
            item.custom_fields["changed_date"] = fields.get("System.ChangedDate")
        groups.append(
            _DependencyQueryItems(
                workstream_id=workstream_id,
                label=query.label,
                resolution_path=query.resolution_path,
                items=items,
            )
        )
    return tuple(groups), ado_calls


def build_dependency_signals(
    dependency_items: tuple[_DependencyQueryItems, ...],
    *,
    program_id: str,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    stale_warn_days: int,
    stale_block_days: int,
    freshness_signal_rule_ids: Collection[str],
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[Signal, ...]:
    signals: list[Signal] = []
    capture_date = as_of.date().isoformat()
    workstream_ids = {workstream.id for workstream in workstreams}
    for group in dependency_items:
        report = build_freshness_report(
            group.items,
            issue_number=0,
            as_of=as_of,
            stale_warn_days=stale_warn_days,
            stale_block_days=stale_block_days,
        )
        workstream_id = group.workstream_id if group.workstream_id in workstream_ids else None
        for finding in report.items:
            if finding.rule_id not in freshness_signal_rule_ids:
                continue
            item = next((entry for entry in group.items if entry.id == finding.work_item_id), None)
            if item is None:
                continue
            raw_ref = f"dependency:{group.label}:{item.id}:{finding.rule_id}:{capture_date}"
            signals.append(
                Signal(
                    id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{capture_date}")),
                    timestamp=as_of,
                    source="ado/dependency",
                    program_id=program_id,
                    workstream_id=workstream_id,
                    entity_refs=merge_entity_refs(
                        provider_refs=(f"WI:{item.id}",),
                        workstream_id=workstream_id,
                    ),
                    text=_truncate_signal_text(f"Dependency {group.label}: {finding.message}"),
                    raw_ref=raw_ref,
                    confidence=Confidence.HIGH,
                    metadata={
                        "dependency_label": group.label,
                        "resolution_path": group.resolution_path,
                        "work_item_id": item.id,
                        "finding_type": finding.rule_id,
                        "severity": finding.severity,
                        "date": capture_date,
                    },
                )
            )
        record_ado_dependency_query_state(
            query_state_sink,
            group,
            as_of=as_of,
            signal_count=sum(
                1
                for signal in signals
                if signal.source == "ado/dependency"
                and signal.metadata is not None
                and signal.metadata.get("dependency_label") == group.label
                and signal.workstream_id == workstream_id
            ),
            expected_max_age_hours=stale_warn_days * 24,
            previous_state=(previous_query_states or {}).get(ado_dependency_query_state_id(group)),
        )
    return tuple(signals)


def record_ado_dependency_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    group: _DependencyQueryItems,
    *,
    as_of: datetime,
    signal_count: int,
    expected_max_age_hours: int,
    previous_state: dict[str, Any] | None = None,
) -> None:
    if query_state_sink is None:
        return
    timestamp = as_of.astimezone(timezone.utc)
    latest_changed_at = latest_dependency_item_changed_at(group.items)
    value_last_4 = roll_query_value_history(previous_state, float(len(group.items)))
    state: dict[str, Any] = {
        "last_attempted_at": timestamp,
        "last_succeeded_at": timestamp,
        "row_count": len(group.items),
        "duration_ms": 0,
        "last_cycle_succeeded": True,
        "zero_rows_ok": True,
        "last_error": None,
        "signal_count": signal_count,
        "dependency_label": group.label,
        "resolution_path": group.resolution_path,
    }
    if latest_changed_at is not None:
        data_age_hours = round((timestamp - latest_changed_at).total_seconds() / 3600.0, 2)
        state["max_data_timestamp"] = latest_changed_at
        state["data_age_hours"] = data_age_hours
        state["expected_max_age_hours"] = expected_max_age_hours
        state["data_freshness_ok"] = data_age_hours <= expected_max_age_hours
    if value_last_4:
        state["value_last_4"] = value_last_4
        state["value_frozen_warning"] = bool(
            len(value_last_4) == 4
            and len({float(value) for value in value_last_4}) == 1
        )
    query_state_sink[ado_dependency_query_state_id(group)] = state


def ado_dependency_query_state_id(group: _DependencyQueryItems) -> str:
    return f"ado-dependency:{group.workstream_id}:{group.label}"


def latest_dependency_item_changed_at(items: tuple[WorkItem, ...]) -> datetime | None:
    timestamps = [
        changed_at
        for item in items
        for changed_at in [coerce_datetime_or_none(item.custom_fields.get("changed_date"))]
        if changed_at is not None
    ]
    if not timestamps:
        return None
    return max(timestamps)


def _configured_dependency_queries(
    workstreams: tuple[Workstream, ...],
) -> list[tuple[str, DependencyADOQuery]]:
    configured_queries: list[tuple[str, DependencyADOQuery]] = []
    for workstream in workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        for query in signal_sources.dependency_ado_queries:
            configured_queries.append((workstream.id, query))
    return configured_queries


def _build_dependency_odata_filter(ado: Any, area_path: str, since: datetime | None) -> str:
    escaped_area_path = area_path.replace("'", "''")
    clauses = [f"startswith(Area/AreaPath, '{escaped_area_path}')"]
    if getattr(ado, "work_item_types", ()):
        type_clauses = [f"WorkItemType eq '{work_item_type}'" for work_item_type in ado.work_item_types]
        clauses.append("(" + " or ".join(type_clauses) + ")")
    if since is not None:
        since_text = since.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        clauses.append(f"ChangedDate ge {since_text}")
    excluded_states = tuple(getattr(ado, "excluded_states", ()) or ())
    if excluded_states:
        excluded = " and ".join(f"not ( State eq '{state}' )" for state in excluded_states)
        clauses.append(excluded)
    return " and ".join(f"( {clause} )" for clause in clauses)

