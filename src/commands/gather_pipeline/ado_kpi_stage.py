from __future__ import annotations

from datetime import datetime, timezone
import logging
from time import perf_counter
from typing import Any, Callable

from src.commands.gather_pipeline.ado_pipeline_stage import _parse_datetime, _parse_int
from src.commands.gather_pipeline.ado_wiql_stage import TeamNameNormalizer, record_ado_wiql_query_state, resolve_wiql_query_text
from src.core.ado_client import ADO_WIQL_DEFAULT_TOP, ADOClient
from src.core.exceptions import QueryError
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.models_v2 import KustoQuery, Program, Signal, Workstream

KustoQueryExecutor = Callable[[KustoQuery], list[dict[str, Any]]]
KustoQueryStateRecorder = Callable[..., None]
KpiSignalBuilder = Callable[..., Signal | None]
PullRequestSummarizer = Callable[..., dict[str, Any] | None]
PullRequestRefBuilder = Callable[[dict[str, Any], str], str | None]
PullRequestEntityRefBuilder = Callable[[dict[str, Any], str], tuple[str, ...]]

log = logging.getLogger(__name__)

_KPI_BATCH_FIELDS: tuple[str, ...] = (
    "System.Id",
    "System.Title",
    "System.State",
    "System.AreaPath",
    "System.IterationPath",
    "Microsoft.VSTS.Scheduling.TargetDate",
    "System.ChangedDate",
    "System.AssignedTo",
    "System.Tags",
)


def build_kusto_kpi_signals(
    *,
    queries: tuple[KustoQuery, ...],
    program: Program,
    program_id: str,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    executor: KustoQueryExecutor,
    ado_client_factory: Callable[..., ADOClient],
    normalize_ado_team_name_fn: TeamNameNormalizer,
    record_kusto_query_state_fn: KustoQueryStateRecorder,
    build_kusto_kpi_signal_fn: KpiSignalBuilder,
    summarize_pull_requests_fn: PullRequestSummarizer,
    pull_request_provider_ref_fn: PullRequestRefBuilder,
    pull_request_entity_refs_fn: PullRequestEntityRefBuilder,
    batch_fields: tuple[str, ...] = _KPI_BATCH_FIELDS,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[Signal, ...]:
    signals: list[Signal] = []
    wiql_client: ADOClient | None = None
    ado_pr_client: ADOClient | None = None
    current_iteration_path_by_team: dict[str | None, str | None] = {}
    ado_cfg = program.ado
    previous_states = previous_query_states or {}
    for query in queries:
        started_at = perf_counter()
        try:
            if query.engine == "wiql":
                if wiql_client is None:
                    if ado_cfg is None:
                        continue
                    wiql_client = ado_client_factory(
                        ado_cfg.organization,
                        ado_cfg.project,
                        timeout=ado_cfg.api_timeout_seconds,
                    )
                rows = execute_wiql_kpi_query(
                    query,
                    program=program,
                    workstreams=workstreams,
                    client=wiql_client,
                    as_of=as_of,
                    current_iteration_path_by_team=current_iteration_path_by_team,
                    normalize_ado_team_name_fn=normalize_ado_team_name_fn,
                    batch_fields=batch_fields,
                )
            elif query.engine == "ado_pr":
                if ado_pr_client is None:
                    if ado_cfg is None:
                        continue
                    ado_pr_client = ado_client_factory(
                        ado_cfg.organization,
                        ado_cfg.project,
                        timeout=ado_cfg.api_timeout_seconds,
                    )
                rows = execute_ado_pr_kpi_query(
                    query,
                    workstreams=workstreams,
                    client=ado_pr_client,
                    as_of=as_of,
                    summarize_pull_requests_fn=summarize_pull_requests_fn,
                    pull_request_provider_ref_fn=pull_request_provider_ref_fn,
                    pull_request_entity_refs_fn=pull_request_entity_refs_fn,
                )
            else:
                rows = executor(query)
        except Exception as exc:
            duration_ms = int(round((perf_counter() - started_at) * 1000))
            log.warning("Kusto KPI query %s failed — skipping. Error: %s", query.id, exc)
            if query.engine == "wiql":
                record_ado_wiql_query_state(
                    query_state_sink,
                    query,
                    work_item_count=0,
                    as_of=as_of,
                    duration_ms=duration_ms,
                    error=str(exc),
                    previous_state=previous_states.get(query.id),
                )
            else:
                record_kusto_query_state_fn(
                    query_state_sink,
                    query,
                    rows=[],
                    as_of=as_of,
                    duration_ms=duration_ms,
                    error=str(exc),
                    previous_state=previous_states.get(query.id),
                )
            continue
        duration_ms = int(round((perf_counter() - started_at) * 1000))
        if query.engine == "wiql":
            wiql_count = wiql_query_work_item_count(rows)
            # ADF-W2.1 (Section 8.4.2): same cap_reached treatment the production
            # gather WIQL path (ado_wiql_stage.py) already applies. A WIQL result
            # at the top cap is likely truncated and must surface as a structured
            # completeness finding (is_degraded/cap_reached), not just a log line.
            cap_reached = wiql_count >= ADO_WIQL_DEFAULT_TOP
            record_ado_wiql_query_state(
                query_state_sink,
                query,
                work_item_count=wiql_count,
                as_of=as_of,
                duration_ms=duration_ms,
                cap_reached=cap_reached,
                previous_state=previous_states.get(query.id),
            )
            entity_refs = entity_refs_from_wiql_kpi_rows(rows)
        elif query.engine == "ado_pr":
            record_kusto_query_state_fn(
                query_state_sink,
                query,
                rows=rows,
                as_of=as_of,
                duration_ms=duration_ms,
                previous_state=previous_states.get(query.id),
            )
            entity_refs = entity_refs_from_ado_pr_kpi_rows(rows)
        else:
            record_kusto_query_state_fn(
                query_state_sink,
                query,
                rows=rows,
                as_of=as_of,
                duration_ms=duration_ms,
                previous_state=previous_states.get(query.id),
            )
            entity_refs = None
        signal = build_kusto_kpi_signal_fn(
            query=query,
            rows=rows,
            program_id=program_id,
            as_of=as_of,
            entity_refs=entity_refs,
        )
        if signal is not None:
            signals.append(signal)
    return tuple(signals)


def execute_wiql_kpi_query(
    query: KustoQuery,
    *,
    program: Program,
    workstreams: tuple[Workstream, ...],
    client: ADOClient,
    as_of: datetime,
    current_iteration_path_by_team: dict[str | None, str | None],
    normalize_ado_team_name_fn: TeamNameNormalizer,
    batch_fields: tuple[str, ...] = _KPI_BATCH_FIELDS,
) -> list[dict[str, Any]]:
    resolved_wiql, _ = resolve_wiql_query_text(
        query,
        program=program,
        workstreams=workstreams,
        client=client,
        current_iteration_path_by_team=current_iteration_path_by_team,
        normalize_ado_team_name_fn=normalize_ado_team_name_fn,
    )
    if not resolved_wiql:
        raise QueryError(f"WIQL-backed KPI query '{query.id}' has no WIQL text.")
    work_item_ids = tuple(client.execute_wiql(resolved_wiql))
    if query.render_as == "table":
        if not work_item_ids:
            return []
        batch_rows = client.query_work_items_batch(list(work_item_ids), batch_fields)
        rows_by_id: dict[int, dict[str, Any]] = {}
        for raw_row in batch_rows:
            if not isinstance(raw_row, dict):
                continue
            raw_fields = raw_row.get("fields")
            fields = raw_fields if isinstance(raw_fields, dict) else {}
            work_item_id = _parse_int(fields.get("System.Id") or raw_row.get("id"))
            if work_item_id is None:
                continue
            assigned_to = fields.get("System.AssignedTo")
            if isinstance(assigned_to, dict):
                assigned_label = _optional_string(assigned_to.get("displayName")) or _optional_string(
                    assigned_to.get("uniqueName")
                )
            else:
                assigned_label = _optional_string(assigned_to)
            rows_by_id[work_item_id] = {
                "WorkItemId": work_item_id,
                "Title": _optional_string(fields.get("System.Title")) or f"Work Item {work_item_id}",
                "State": _optional_string(fields.get("System.State")),
                "AreaPath": _optional_string(fields.get("System.AreaPath")),
                "IterationPath": _optional_string(fields.get("System.IterationPath")),
                "TargetDate": fields.get("Microsoft.VSTS.Scheduling.TargetDate"),
                "ChangedDate": fields.get("System.ChangedDate"),
                "AssignedTo": assigned_label,
                "Tags": _optional_string(fields.get("System.Tags")),
                "WorkItemIds": [str(work_item_id)],
            }
        return [rows_by_id[work_item_id] for work_item_id in work_item_ids if work_item_id in rows_by_id]
    count_column = query.result_column or "Count"
    return [
        {
            count_column: len(work_item_ids),
            "Count": len(work_item_ids),
            "Timestamp": as_of.astimezone(timezone.utc).isoformat(),
            "WorkItemIds": [str(work_item_id) for work_item_id in work_item_ids],
        }
    ]


def execute_ado_pr_kpi_query(
    query: KustoQuery,
    *,
    workstreams: tuple[Workstream, ...],
    client: ADOClient,
    as_of: datetime,
    summarize_pull_requests_fn: PullRequestSummarizer,
    pull_request_provider_ref_fn: PullRequestRefBuilder,
    pull_request_entity_refs_fn: PullRequestEntityRefBuilder,
) -> list[dict[str, Any]]:
    scoped_workstreams = tuple(
        workstream
        for workstream in workstreams
        if not query.workstream_ids or workstream.id in query.workstream_ids
    )
    if not scoped_workstreams:
        raise QueryError(f"ADO PR-backed KPI query '{query.id}' does not match any configured workstreams.")

    repository_ids = tuple(
        dict.fromkeys(
            repository_id
            for workstream in scoped_workstreams
            for repository_id in workstream.ado_repository_ids
            if repository_id.strip()
        )
    )
    if not repository_ids:
        raise QueryError(
            f"ADO PR-backed KPI query '{query.id}' requires at least one ado_repository_ids entry on its target workstreams."
        )

    age_entries: list[tuple[str, int, float]] = []
    entity_refs: list[str] = []
    repository_rows: list[dict[str, Any]] = []
    for repository_id in repository_ids:
        pull_requests = tuple(client.list_pull_requests(repository_id, status="active", top=100))
        summary = summarize_pull_requests_fn(
            repository_id=repository_id,
            pull_requests=pull_requests,
            as_of=as_of,
        )
        if summary is None:
            continue
        metadata = dict(summary["metadata"])
        repository_rows.append(metadata)
        repository_name = _optional_string(metadata.get("repository_name")) or repository_id
        for pull_request in pull_requests:
            entity_ref = pull_request_provider_ref_fn(pull_request, repository_name)
            if entity_ref is None:
                continue
            pull_request_id = _parse_int(pull_request.get("pullRequestId"))
            created_at = _parse_datetime(pull_request.get("creationDate"))
            if pull_request_id is None or created_at is None:
                continue
            age_days = max((as_of - created_at).total_seconds() / 86400.0, 0.0)
            age_entries.append((entity_ref, pull_request_id, age_days))
            entity_refs.extend(pull_request_entity_refs_fn(pull_request, repository_name))

    if not age_entries:
        return []

    sorted_by_age = tuple(sorted(age_entries, key=lambda entry: entry[2]))
    p90_index = max(int(len(sorted_by_age) * 0.9 + 0.999999) - 1, 0)
    oldest_entry = max(age_entries, key=lambda entry: entry[2])
    return [
        {
            (query.result_column or "P90AgeDays"): round(sorted_by_age[p90_index][2], 1),
            "P90AgeDays": round(sorted_by_age[p90_index][2], 1),
            "OpenPrCount": len(age_entries),
            "RepositoryCount": len(repository_rows),
            "OldestPrId": oldest_entry[1],
            "OldestPrAgeDays": round(oldest_entry[2], 1),
            "Timestamp": as_of.astimezone(timezone.utc).isoformat(),
            "PullRequestRefs": [entry[0] for entry in sorted(age_entries, key=lambda entry: entry[2], reverse=True)],
            "EntityRefs": list(dict.fromkeys(entity_refs)),
            "Repositories": repository_rows,
        }
    ]


def wiql_query_work_item_count(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    first_row = rows[0]
    raw_work_item_ids = first_row.get("WorkItemIds") if isinstance(first_row, dict) else None
    if len(rows) == 1 and isinstance(raw_work_item_ids, list):
        return len(raw_work_item_ids)
    return len(rows)


def entity_refs_from_wiql_kpi_rows(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    refs: list[str] = []
    for row in rows:
        work_item_id = _parse_int(row.get("WorkItemId"))
        if work_item_id is not None:
            refs.append(f"WI:{work_item_id}")
        raw_work_item_ids = row.get("WorkItemIds")
        if not isinstance(raw_work_item_ids, list):
            continue
        for work_item_id in raw_work_item_ids:
            work_item_text = _optional_string(work_item_id)
            if work_item_text is None:
                continue
            refs.append(f"WI:{work_item_text}")
    return tuple(dict.fromkeys(refs))


def entity_refs_from_ado_pr_kpi_rows(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    refs: list[str] = []
    for row in rows:
        raw_refs = row.get("EntityRefs")
        if not isinstance(raw_refs, list):
            raw_refs = row.get("PullRequestRefs")
        if not isinstance(raw_refs, list):
            continue
        for entity_ref in raw_refs:
            ref_text = _optional_string(entity_ref)
            if ref_text is not None:
                refs.append(ref_text)
    return tuple(dict.fromkeys(refs))
