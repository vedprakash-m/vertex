from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

import typer

from src.commands.gather_pipeline.ado_pipeline_stage import _parse_datetime, _roll_query_value_history
from src.commands.gather_workiq_helpers import _truncate_signal_text
from src.core.ado_client import ADO_WIQL_DEFAULT_TOP, ADOClient
from src.core.ado_discovery import expand_with_linked_items as _expand_with_linked_items
from src.core.exceptions import AuthError, QueryError
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.models import Confidence
from src.core.models_v2 import KustoQuery, Program, Signal, Workstream
from src.core.signal_ref_utils import merge_entity_refs


TeamNameNormalizer = Callable[[str | None], str | None]
GraphExpander = Callable[[ADOClient, frozenset[int]], frozenset[int] | set[int]]


def load_wiql_golden_query_signals(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    *,
    queries: tuple[KustoQuery, ...],
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
    ado_client_factory: Callable[..., ADOClient],
    normalize_ado_team_name_fn: TeamNameNormalizer,
    expand_with_linked_items_fn: GraphExpander = _expand_with_linked_items,
) -> tuple[tuple[Signal, ...], int]:
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program.id}' is missing ado configuration.")
    if not queries:
        return (), 0

    client = ado_client_factory(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )
    valid_workstream_ids = {workstream.id for workstream in workstreams}
    capture_date = as_of.date().isoformat()
    signals: list[Signal] = []
    ado_calls = 0
    current_iteration_path_by_team: dict[str | None, str | None] = {}
    all_seed_ids: set[int] = set()

    for query in queries:
        wiql = _optional_string(query.wiql)
        if wiql is None:
            continue
        started_at = perf_counter()
        resolved_wiql, iteration_ado_calls = resolve_wiql_query_text(
            query,
            program=program,
            workstreams=workstreams,
            client=client,
            current_iteration_path_by_team=current_iteration_path_by_team,
            normalize_ado_team_name_fn=normalize_ado_team_name_fn,
        )
        ado_calls += iteration_ado_calls
        try:
            work_item_ids = tuple(client.execute_wiql(resolved_wiql))
        except (AuthError, QueryError) as exc:
            record_ado_wiql_query_state(
                query_state_sink,
                query,
                work_item_count=0,
                as_of=as_of,
                duration_ms=int(round((perf_counter() - started_at) * 1000)),
                error=str(exc),
                previous_state=(previous_query_states or {}).get(query.id),
            )
            raise
        ado_calls += 1
        # ADF-W2.1 (Section 8.4.2): a capped WIQL result is a completeness
        # finding, not just a log line -- is_degraded/cap_reached ride the
        # existing query-state sink already consumed by report/doctor/scorecard.
        # Compares against ADO_WIQL_DEFAULT_TOP (the cap this call site
        # relies on client.execute_wiql's default for) rather than passing a
        # new on_pagination callback, so every pre-existing fake ADO client
        # in the test suite keeps working unchanged.
        cap_reached = len(work_item_ids) >= ADO_WIQL_DEFAULT_TOP
        record_ado_wiql_query_state(
            query_state_sink,
            query,
            work_item_count=len(work_item_ids),
            as_of=as_of,
            duration_ms=int(round((perf_counter() - started_at) * 1000)),
            previous_state=(previous_query_states or {}).get(query.id),
            cap_reached=cap_reached,
        )
        all_seed_ids.update(work_item_ids)
        preview_refs = tuple(f"WI:{work_item_id}" for work_item_id in work_item_ids[:10])
        workstream_id = (
            query.workstream_ids[0]
            if len(query.workstream_ids) == 1 and query.workstream_ids[0] in valid_workstream_ids
            else None
        )
        summary = f"{query.section}: {len(work_item_ids)} item(s) matched WIQL query {query.id}"
        if preview_refs:
            summary = f"{summary}; top {', '.join(preview_refs[:3])}"
        signals.append(
            Signal(
                id=str(uuid5(NAMESPACE_URL, f"{program.id}|ado_wiql|{query.id}|{capture_date}|{','.join(str(item_id) for item_id in work_item_ids)}")),
                timestamp=as_of,
                source="ado/wiql",
                program_id=program.id,
                workstream_id=workstream_id,
                entity_refs=merge_entity_refs(
                    provider_refs=preview_refs,
                    workstream_id=workstream_id,
                ),
                text=_truncate_signal_text(summary),
                raw_ref=f"ado_wiql:{query.id}:{capture_date}",
                confidence=Confidence.HIGH if query.validated else Confidence.MEDIUM,
                metadata={
                    "query_id": query.id,
                    "engine": "wiql",
                    "work_item_count": len(work_item_ids),
                    "work_item_ids": tuple(str(item_id) for item_id in work_item_ids[:25]),
                    "date": capture_date,
                },
            )
        )
    if all_seed_ids:
        linked_ids = expand_with_linked_items_fn(client, frozenset(all_seed_ids))
        if linked_ids:
            linked_refs = tuple(f"WI:{lid}" for lid in sorted(linked_ids)[:25])
            signals.append(
                Signal(
                    id=str(uuid5(NAMESPACE_URL, f"{program.id}|ado_graph_expansion|{capture_date}|{','.join(str(i) for i in sorted(linked_ids))}")),
                    timestamp=as_of,
                    source="ado/graph",
                    program_id=program.id,
                    workstream_id=None,
                    entity_refs=linked_refs,
                    text=_truncate_signal_text(
                        f"Graph expansion: {len(linked_ids)} linked item(s) discovered from {len(all_seed_ids)} seed item(s)"
                    ),
                    raw_ref=f"ado_graph:{capture_date}",
                    confidence=Confidence.MEDIUM,
                    metadata={
                        "engine": "graph",
                        "seed_count": len(all_seed_ids),
                        "linked_count": len(linked_ids),
                        "linked_work_item_ids": tuple(str(i) for i in sorted(linked_ids)[:25]),
                        "date": capture_date,
                    },
                )
            )
    return tuple(signals), ado_calls


def resolve_wiql_query_text(
    query: KustoQuery,
    *,
    program: Program,
    workstreams: tuple[Workstream, ...],
    client: ADOClient,
    current_iteration_path_by_team: dict[str | None, str | None],
    normalize_ado_team_name_fn: TeamNameNormalizer,
) -> tuple[str, int]:
    del program
    wiql = _optional_string(query.wiql)
    if wiql is None:
        return "", 0
    if "{current_iteration_path}" not in wiql:
        return wiql, 0

    target_team_name: str | None = None
    if len(query.workstream_ids) == 1:
        matching_workstream = next(
            (workstream for workstream in workstreams if workstream.id == query.workstream_ids[0]),
            None,
        )
        if matching_workstream is not None:
            target_team_name = normalize_ado_team_name_fn(matching_workstream.ado_team)

    if target_team_name not in current_iteration_path_by_team:
        if target_team_name is None:
            iterations = client.list_team_iterations(timeframe="current")
        else:
            iterations = client.list_team_iterations(timeframe="current", team=target_team_name)
        current_iteration_path_by_team[target_team_name] = next(
            (
                _optional_string(iteration.get("path"))
                for iteration in iterations
                if _optional_string(iteration.get("path"))
            ),
            None,
        )
        ado_calls = 1
    else:
        ado_calls = 0

    current_iteration_path = current_iteration_path_by_team.get(target_team_name)
    if current_iteration_path is None:
        raise QueryError(f"Current iteration path unavailable for WIQL query '{query.id}'.")

    return wiql.replace("{current_iteration_path}", current_iteration_path), ado_calls


def record_ado_wiql_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    query: KustoQuery,
    *,
    work_item_count: int,
    as_of: datetime,
    duration_ms: int,
    error: str | None = None,
    previous_state: dict[str, Any] | None = None,
    cap_reached: bool = False,
) -> None:
    if query_state_sink is None:
        return
    timestamp = as_of.astimezone(timezone.utc)
    numeric_value = None if error is not None else float(work_item_count)
    value_last_4 = _roll_query_value_history(previous_state, numeric_value)
    state: dict[str, Any] = {
        "last_attempted_at": timestamp,
        "last_succeeded_at": timestamp if error is None else _coerce_datetime_or_none((previous_state or {}).get("last_succeeded_at")),
        "row_count": work_item_count,
        "duration_ms": duration_ms,
        "last_cycle_succeeded": error is None,
        "zero_rows_ok": query.expected_cardinality == "zero_ok",
        "last_error": error,
        # WS-1 PB-4: a query-state sink that records an error MUST also
        # set is_degraded=True so downstream readers (report, doctor,
        # scorecard) can render the data as degraded rather than as
        # `work_item_count=0` (which is misleadingly normal-looking).
        # ADF-W2.1: a capped WIQL result is the same kind of "don't trust
        # this number at face value" situation, so it degrades the query
        # state too, with its own explicit reason distinct from an error.
        "is_degraded": error is not None or cap_reached,
        "cap_reached": cap_reached,
    }
    if value_last_4:
        state["value_last_4"] = value_last_4
        state["value_frozen_warning"] = bool(
            numeric_value is not None
            and len(value_last_4) == 4
            and len({float(value) for value in value_last_4}) == 1
        )
    query_state_sink[query.id] = state


def _coerce_datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else _parse_datetime(value)
