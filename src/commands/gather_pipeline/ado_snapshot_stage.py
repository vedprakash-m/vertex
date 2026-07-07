from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import logging
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Callable

import typer

from src.commands.gather_pipeline.ado_pipeline_stage import (
    _parse_datetime,
    _parse_float,
    _parse_int,
    _roll_query_value_history,
)
from src.commands.gather_pipeline.ado_analytics_primitives import (
    parse_date_sk as _parse_date_sk,
)
from src.core.ado_client import ADOClient
from src.core.exceptions import QueryError
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.models import WorkItem
from src.core.models_v2 import Program, Signal, Workstream


log = logging.getLogger(__name__)


def _extract_vs403522_property(error_str: str) -> str | None:
    m = re.search(r"VS403522: The property '(\w+)' is not available", error_str)
    return m.group(1) if m else None


AnalyticsSignalBuilder = Callable[..., tuple[Signal, ...]]
SprintSignalBuilder = Callable[..., tuple[Signal, ...]]
WiqlGoldenSignalLoader = Callable[..., tuple[tuple[Signal, ...], int]]
TeamNameNormalizer = Callable[[str | None], str | None]


def load_analytics_signals(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    programs_root: Path,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
    ado_client_factory: Callable[..., ADOClient],
    date_to_sk_fn: Callable[[date], int],
    analytics_snapshot_fields: tuple[str, ...],
    build_analytics_signals_fn: AnalyticsSignalBuilder,
    load_wiql_golden_query_signals_fn: WiqlGoldenSignalLoader,
    expected_max_age_hours: int,
) -> tuple[tuple[Signal, ...], int]:
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program.id}' is missing ado configuration.")

    client = ado_client_factory(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=max(program.ado.api_timeout_seconds, 120),
    )
    start_date_sk = date_to_sk_fn(as_of.date() - timedelta(days=program.ado.date_window_days))
    end_date_sk = date_to_sk_fn(as_of.date())
    ado_calls = 1
    try:
        rows = client.query_work_item_snapshot(
            filter_expression=build_analytics_snapshot_filter(
                program.ado,
                start_date_sk=start_date_sk,
                end_date_sk=end_date_sk,
            ),
            select_fields=analytics_snapshot_fields,
        )
    except QueryError as error:
        # VS403522: one or more fields unavailable in this ADO Analytics project.
        # Retry once: remove IsLastRevisionOfDay from the filter AND strip any
        # field name called out in the error from the select list.
        if "VS403522" in str(error):
            unavailable = _extract_vs403522_property(str(error))
            trimmed_fields = (
                tuple(f for f in analytics_snapshot_fields if f != unavailable)
                if unavailable and unavailable in analytics_snapshot_fields
                else analytics_snapshot_fields
            )
            log.warning(
                "ADO analytics snapshot: VS403522 for %s (field=%r) — retrying without IsLastRevisionOfDay%s",
                program.id,
                unavailable,
                f" and {unavailable}" if unavailable and trimmed_fields != analytics_snapshot_fields else "",
            )
            ado_calls += 1
            rows = client.query_work_item_snapshot(
                filter_expression=build_analytics_snapshot_filter(
                    program.ado,
                    start_date_sk=start_date_sk,
                    end_date_sk=end_date_sk,
                    include_last_revision_of_day=False,
                ),
                select_fields=trimmed_fields,
            )
        else:
            log.warning(
                "ADO analytics snapshot query failed for %s: %s — skipping analytics stage",
                program.id,
                error,
            )
            raise
    analytics_signals = build_analytics_signals_fn(
        rows=rows,
        program_id=program.id,
        workstreams=workstreams,
        start_date_sk=start_date_sk,
        end_date_sk=end_date_sk,
        as_of=as_of,
    )
    for signal in analytics_signals:
        record_ado_analytics_query_state(
            query_state_sink,
            signal,
            as_of=as_of,
            previous_state=(previous_query_states or {}).get(ado_analytics_query_state_id(signal)),
            expected_max_age_hours=expected_max_age_hours,
        )
    wiql_signals, wiql_ado_calls = load_wiql_golden_query_signals_fn(
        program,
        workstreams,
        as_of,
        programs_root=programs_root,
        query_state_sink=query_state_sink,
        previous_query_states=previous_query_states,
    )
    return ((*analytics_signals, *wiql_signals), ado_calls + wiql_ado_calls)


def load_sprint_signals(
    program: Program,
    workstreams: tuple[Workstream, ...],
    items: tuple[WorkItem, ...],
    as_of: datetime,
    *,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
    ado_client_factory: Callable[..., ADOClient],
    normalize_ado_team_name_fn: TeamNameNormalizer,
    date_to_sk_fn: Callable[[date], int],
    sprint_snapshot_fields: tuple[str, ...],
    build_sprint_signals_fn: SprintSignalBuilder,
    snapshot_item_filter_batch_size: int,
    expected_max_age_hours: int,
) -> tuple[tuple[Signal, ...], int]:
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program.id}' is missing ado configuration.")

    client = ado_client_factory(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=max(program.ado.api_timeout_seconds, 120),
    )
    team_names: set[str | None] = {
        normalized_team
        for workstream in workstreams
        if (normalized_team := normalize_ado_team_name_fn(workstream.ado_team)) is not None
    }
    has_default_team_workstreams = not workstreams or any(
        normalize_ado_team_name_fn(workstream.ado_team) is None for workstream in workstreams
    )
    if not team_names or has_default_team_workstreams:
        team_names.add(None)

    iterations_by_team: dict[str | None, tuple[dict[str, Any], ...]] = {}
    capacities_by_team_iteration: dict[tuple[str | None, str | None], tuple[dict[str, Any], ...]] = {}
    ado_calls = 0
    ordered_team_names = sorted(team_names, key=lambda value: "" if value is None else value.casefold())
    for team_name in ordered_team_names:
        if team_name is None:
            iterations = tuple(client.list_team_iterations(timeframe="current"))
        else:
            iterations = tuple(client.list_team_iterations(timeframe="current", team=team_name))
        iterations_by_team[team_name] = iterations
        ado_calls += 1
        for iteration in iterations:
            iteration_id = _optional_string(iteration.get("id"))
            if iteration_id is None:
                continue
            try:
                if team_name is None:
                    capacities_by_team_iteration[(team_name, iteration_id)] = tuple(
                        client.list_iteration_capacities(iteration_id)
                    )
                else:
                    capacities_by_team_iteration[(team_name, iteration_id)] = tuple(
                        client.list_iteration_capacities(iteration_id, team=team_name)
                    )
            except QueryError as error:
                log.warning(
                    "ADO sprint capacity fetch skipped for %s team %s iteration %s: %s",
                    program.id,
                    team_name or "<default>",
                    iteration_id,
                    error,
                )
                capacities_by_team_iteration[(team_name, iteration_id)] = ()
            finally:
                ado_calls += 1

    start_date_sk = date_to_sk_fn(as_of.date() - timedelta(days=program.ado.date_window_days))
    end_date_sk = date_to_sk_fn(as_of.date())
    try:
        sprint_snapshot_rows = client.query_work_item_snapshot(
            filter_expression=build_analytics_snapshot_filter(
                program.ado,
                start_date_sk=start_date_sk,
                end_date_sk=end_date_sk,
            ),
            select_fields=sprint_snapshot_fields,
        )
        ado_calls += 1
    except QueryError as error:
        log.warning(
            "ADO sprint snapshot filter fallback for %s: %s",
            program.id,
            error,
        )
        fallback_client = ado_client_factory(
            organization=program.ado.organization,
            project=program.ado.project,
            timeout=max(program.ado.api_timeout_seconds, 120),
        )
        sprint_snapshot_rows, snapshot_ado_calls = query_snapshot_rows_by_item_ids(
            fallback_client,
            items,
            ado=program.ado,
            start_date_sk=start_date_sk,
            end_date_sk=end_date_sk,
            select_fields=sprint_snapshot_fields,
            expand_fields=("Iteration",),
            snapshot_item_filter_batch_size=snapshot_item_filter_batch_size,
        )
        ado_calls += 1 + snapshot_ado_calls
    signals = build_sprint_signals_fn(
        iterations_by_team=iterations_by_team,
        capacities_by_team_iteration=capacities_by_team_iteration,
        sprint_snapshot_rows=sprint_snapshot_rows,
        items=items,
        program_id=program.id,
        workstreams=workstreams,
        as_of=as_of,
    )
    for signal in signals:
        record_ado_sprint_query_state(
            query_state_sink,
            signal,
            as_of=as_of,
            previous_state=(previous_query_states or {}).get(ado_sprint_query_state_id(signal)),
            expected_max_age_hours=expected_max_age_hours,
        )
    return signals, ado_calls


def build_analytics_snapshot_filter(
    ado: Any, *, start_date_sk: int, end_date_sk: int, include_last_revision_of_day: bool = True
) -> str:
    area_conditions = [f"startswith(Area/AreaPath, '{p}')" for p in [x.replace("'", "''") for x in ado.area_paths]]
    type_conditions = [f"WorkItemType eq '{t}'" for t in [x.replace("'", "''") for x in ado.work_item_types]]
    state_conditions = [f"State eq '{s}'" for s in [x.replace("'", "''") for x in ado.excluded_states]]
    clauses = [
        f"( {' or '.join(area_conditions)} )",
        f"( {' or '.join(type_conditions)} )",
        f"DateSK ge {start_date_sk}",
        f"DateSK le {end_date_sk}",
    ]
    if include_last_revision_of_day:
        clauses.append("IsLastRevisionOfDay eq true")
    if state_conditions:
        clauses.append(f"not ( {' or '.join(state_conditions)} )")
    return " and ".join(clauses)


def query_snapshot_rows_by_item_ids(
    client: ADOClient,
    items: tuple[WorkItem, ...],
    *,
    ado: Any,
    start_date_sk: int,
    end_date_sk: int,
    select_fields: tuple[str, ...],
    expand_fields: tuple[str, ...],
    snapshot_item_filter_batch_size: int,
) -> tuple[list[dict[str, Any]], int]:
    if not items:
        return [], 0

    item_by_id = {item.id: item for item in items}
    query_select_fields = tuple(
        dict.fromkeys(
            field
            for field in (*select_fields, "Revision")
            if field not in {"AreaPath", "IterationPath"}
        )
    )
    rows_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    ado_calls = 0
    item_ids = list(item_by_id)

    for start in range(0, len(item_ids), snapshot_item_filter_batch_size):
        batch_ids = item_ids[start:start + snapshot_item_filter_batch_size]
        params = {
            "$filter": _build_snapshot_item_filter(
                ado,
                batch_ids,
                start_date_sk=start_date_sk,
                end_date_sk=end_date_sk,
            ),
            "$select": ",".join(query_select_fields),
        }
        if expand_fields:
            params["$expand"] = ",".join(expand_fields)
        batch_rows = client.query_odata_all("WorkItemSnapshot", params)
        ado_calls += 1

        for row in batch_rows:
            work_item_id = _parse_int(row.get("WorkItemId"))
            date_sk = _parse_date_sk(row.get("DateSK"))
            if work_item_id is None or date_sk is None:
                continue

            normalized_row = dict(row)
            item = item_by_id.get(work_item_id)
            if item is not None:
                area_payload = row.get("Area")
                area_path = None
                if isinstance(area_payload, dict):
                    area_path = _optional_string(area_payload.get("AreaPath"))
                normalized_row["AreaPath"] = item.area_path or area_path or ""

                iteration_payload = row.get("Iteration")
                iteration_path = None
                if isinstance(iteration_payload, dict):
                    iteration_path = _optional_string(iteration_payload.get("IterationPath"))
                normalized_row["IterationPath"] = iteration_path or item.iteration_path

            key = (work_item_id, date_sk)
            existing = rows_by_key.get(key)
            if existing is None or (_parse_int(normalized_row.get("Revision")) or 0) >= (_parse_int(existing.get("Revision")) or 0):
                rows_by_key[key] = normalized_row

    ordered_rows = list(rows_by_key.values())
    ordered_rows.sort(
        key=lambda row: (
            _parse_date_sk(row.get("DateSK")) or 0,
            _parse_int(row.get("WorkItemId")) or 0,
        )
    )
    return ordered_rows, ado_calls


def record_ado_analytics_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    signal: Signal,
    *,
    as_of: datetime,
    previous_state: dict[str, Any] | None = None,
    expected_max_age_hours: int,
) -> None:
    if query_state_sink is None:
        return
    metadata = signal.metadata or {}
    query_id = ado_analytics_query_state_id(signal)
    latest_snapshot_date = _parse_iso_date(_optional_string(metadata.get("latest_snapshot_date")))
    average_cycle_time_days = _parse_float(metadata.get("average_cycle_time_days"))
    completed_item_count = _parse_int(metadata.get("completed_item_count"))
    snapshot_item_count = _parse_int(metadata.get("snapshot_item_count"))
    numeric_value = average_cycle_time_days
    value_metric = "average_cycle_time_days"
    if numeric_value is None:
        numeric_value = float(completed_item_count) if completed_item_count is not None else None
        value_metric = "completed_item_count"
    value_last_4 = _roll_query_value_history(previous_state, numeric_value)
    timestamp = as_of.astimezone(timezone.utc)
    state: dict[str, Any] = {
        "last_attempted_at": timestamp,
        "last_succeeded_at": timestamp,
        "row_count": snapshot_item_count or 0,
        "duration_ms": 0,
        "last_cycle_succeeded": True,
        "zero_rows_ok": False,
        "last_error": None,
        "expected_max_age_hours": expected_max_age_hours,
        "value_metric": value_metric,
    }
    if completed_item_count is not None:
        state["completed_item_count"] = completed_item_count
    if latest_snapshot_date is not None:
        max_data_timestamp = datetime.combine(latest_snapshot_date, time.min, tzinfo=timezone.utc)
        data_age_hours = round((timestamp - max_data_timestamp).total_seconds() / 3600.0, 2)
        state["max_data_timestamp"] = max_data_timestamp
        state["data_age_hours"] = data_age_hours
        state["data_freshness_ok"] = data_age_hours <= expected_max_age_hours
    if value_last_4:
        state["value_last_4"] = value_last_4
        state["value_frozen_warning"] = bool(
            numeric_value is not None
            and len(value_last_4) == 4
            and len({float(value) for value in value_last_4}) == 1
        )
    query_state_sink[query_id] = state


def ado_analytics_query_state_id(signal: Signal) -> str:
    workstream_id = signal.workstream_id or "program"
    return f"ado-analytics:{workstream_id}"


def record_ado_sprint_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    signal: Signal,
    *,
    as_of: datetime,
    previous_state: dict[str, Any] | None = None,
    expected_max_age_hours: int,
) -> None:
    if query_state_sink is None:
        return
    metadata = signal.metadata or {}
    query_id = ado_sprint_query_state_id(signal)
    committed_item_count = _parse_int(metadata.get("committed_item_count"))
    open_item_count = _parse_int(metadata.get("open_item_count"))
    completion_pct = _parse_float(metadata.get("completion_pct"))
    numeric_value = float(open_item_count) if open_item_count is not None else None
    value_metric = "open_item_count"
    if numeric_value is None and completion_pct is not None:
        numeric_value = completion_pct
        value_metric = "completion_pct"
    latest_snapshot_date = _latest_sprint_snapshot_date(metadata)
    value_last_4 = _roll_query_value_history(previous_state, numeric_value)
    timestamp = as_of.astimezone(timezone.utc)
    state: dict[str, Any] = {
        "last_attempted_at": timestamp,
        "last_succeeded_at": timestamp,
        "row_count": committed_item_count or 0,
        "duration_ms": 0,
        "last_cycle_succeeded": True,
        "zero_rows_ok": False,
        "last_error": None,
        "expected_max_age_hours": expected_max_age_hours,
        "value_metric": value_metric,
        "iteration_name": _optional_string(metadata.get("iteration_name")),
        "iteration_path": _optional_string(metadata.get("iteration_path")),
        "pace_status": _optional_string(metadata.get("pace_status")),
        "projection_status": _optional_string(metadata.get("projection_status")),
    }
    if committed_item_count is not None:
        state["committed_item_count"] = committed_item_count
    if open_item_count is not None:
        state["open_item_count"] = open_item_count
    if completion_pct is not None:
        state["completion_pct"] = completion_pct
    if latest_snapshot_date is not None:
        max_data_timestamp = datetime.combine(latest_snapshot_date, time.min, tzinfo=timezone.utc)
        data_age_hours = round((timestamp - max_data_timestamp).total_seconds() / 3600.0, 2)
        state["max_data_timestamp"] = max_data_timestamp
        state["data_age_hours"] = data_age_hours
        state["data_freshness_ok"] = data_age_hours <= expected_max_age_hours
    if value_last_4:
        state["value_last_4"] = value_last_4
        state["value_frozen_warning"] = bool(
            numeric_value is not None
            and len(value_last_4) == 4
            and len({float(value) for value in value_last_4}) == 1
        )
    query_state_sink[query_id] = state


def ado_sprint_query_state_id(signal: Signal) -> str:
    metadata = signal.metadata or {}
    workstream_id = signal.workstream_id or "program"
    iteration_id = _optional_string(metadata.get("iteration_id"))
    iteration_path = _optional_string(metadata.get("iteration_path"))
    return f"ado-sprint:{workstream_id}:{iteration_id or iteration_path or 'current'}"


def _build_snapshot_item_filter(
    ado: Any,
    work_item_ids: list[int],
    *,
    start_date_sk: int,
    end_date_sk: int,
) -> str:
    id_conditions = [f"WorkItemId eq {work_item_id}" for work_item_id in work_item_ids]
    type_conditions = [f"WorkItemType eq '{t}'" for t in [x.replace("'", "''") for x in ado.work_item_types]]
    state_conditions = [f"State eq '{s}'" for s in [x.replace("'", "''") for x in ado.excluded_states]]
    clauses = [
        f"( {' or '.join(id_conditions)} )",
        f"( {' or '.join(type_conditions)} )",
        f"DateSK ge {start_date_sk}",
        f"DateSK le {end_date_sk}",
    ]
    if state_conditions:
        clauses.append(f"not ( {' or '.join(state_conditions)} )")
    return " and ".join(clauses)


def _latest_sprint_snapshot_date(metadata: dict[str, Any]) -> date | None:
    latest: date | None = None
    for key in ("open_history", "completed_history"):
        history = metadata.get(key)
        if not isinstance(history, dict):
            continue
        for raw_date in history:
            parsed = _parse_iso_date(raw_date if isinstance(raw_date, str) else None)
            if parsed is not None and (latest is None or parsed > latest):
                latest = parsed
    return latest


def _parse_iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None

