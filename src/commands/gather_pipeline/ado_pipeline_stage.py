from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import typer

from src.commands.gather_workiq_helpers import (
    _extract_work_item_refs,
    _truncate_signal_text,
)
from src.core.ado_client import ADOClient
from src.core.m365_payload_support import optional_string as _optional_string
from src.core.models import Confidence
from src.core.models_v2 import Program, Signal, Workstream
from src.core.signal_ref_utils import merge_entity_refs


def load_pipeline_signals(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    query_state_sink: dict[str, dict[str, Any]] | None = None,
    previous_query_states: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[Signal, ...], int]:
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program.id}' is missing ado configuration.")

    configured_workstreams = tuple(
        workstream
        for workstream in workstreams
        if workstream.ado_pipeline_ids or workstream.ado_repository_ids
    )
    if not configured_workstreams:
        return (), 0

    client = ADOClient(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )
    window_start = as_of - timedelta(days=program.ado.date_window_days)
    capture_date = as_of.date().isoformat()
    signals: list[Signal] = []
    ado_calls = 0

    for workstream in configured_workstreams:
        pipeline_summaries: list[str] = []
        pipeline_metadata: list[dict[str, Any]] = []
        pipeline_recent_run_count = 0
        pipeline_failed_run_count = 0
        pipeline_latest_run_timestamp: datetime | None = None
        pull_request_summaries: list[str] = []
        pull_request_metadata: list[dict[str, Any]] = []
        pull_request_refs: list[str] = []
        pull_request_open_count = 0
        pull_request_p90_age_days = 0.0
        for pipeline_id in workstream.ado_pipeline_ids:
            runs = tuple(client.list_pipeline_runs(pipeline_id, top=10))
            ado_calls += 1
            pipeline_query_state = _summarize_pipeline_query_state(
                runs=runs,
                window_start=window_start,
            )
            pipeline_recent_run_count += pipeline_query_state["recent_run_count"]
            pipeline_failed_run_count += pipeline_query_state["failed_run_count"]
            latest_pipeline_run = pipeline_query_state["latest_run_finished_at"]
            if latest_pipeline_run is not None and (
                pipeline_latest_run_timestamp is None or latest_pipeline_run > pipeline_latest_run_timestamp
            ):
                pipeline_latest_run_timestamp = latest_pipeline_run
            summary = _summarize_pipeline_runs(
                pipeline_id=pipeline_id,
                runs=runs,
                window_start=window_start,
                window_days=program.ado.date_window_days,
            )
            if summary is None:
                continue
            pipeline_summaries.append(summary["text"])
            pipeline_metadata.append(summary["metadata"])
        for repository_id in workstream.ado_repository_ids:
            pull_requests = tuple(client.list_pull_requests(repository_id, status="active", top=100))
            ado_calls += 1
            pull_request_query_state = _summarize_pull_request_query_state(
                pull_requests=pull_requests,
                as_of=as_of,
            )
            pull_request_open_count += pull_request_query_state["open_pr_count"]
            pull_request_p90_age_days = max(pull_request_p90_age_days, pull_request_query_state["p90_age_days"])
            summary = summarize_pull_requests(
                repository_id=repository_id,
                pull_requests=pull_requests,
                as_of=as_of,
            )
            if summary is None:
                continue
            pull_request_summaries.append(summary["text"])
            pull_request_metadata.append(summary["metadata"])
            pull_request_refs.extend(summary["entity_refs"])
        if pipeline_summaries:
            raw_ref = f"ado-pipeline:{workstream.id}:{','.join(workstream.ado_pipeline_ids)}:{capture_date}"
            signals.append(
                Signal(
                    id=str(uuid5(NAMESPACE_URL, f"{program.id}|{raw_ref}")),
                    timestamp=as_of,
                    source="ado/pipeline",
                    program_id=program.id,
                    workstream_id=workstream.id,
                    entity_refs=merge_entity_refs(
                        provider_refs=tuple(f"ado/pipeline:{pipeline_id}" for pipeline_id in workstream.ado_pipeline_ids),
                        workstream_id=workstream.id,
                    ),
                    text=_truncate_signal_text(f"{workstream.name}: {'; '.join(pipeline_summaries)}"),
                    raw_ref=raw_ref,
                    confidence=Confidence.HIGH,
                    metadata={
                        "workstream_id": workstream.id,
                        "window_days": program.ado.date_window_days,
                        "pipeline_ids": list(workstream.ado_pipeline_ids),
                        "pipelines": pipeline_metadata,
                    },
                )
            )
            _record_ado_pipeline_source_query_state(
                query_state_sink,
                signals[-1],
                as_of=as_of,
                previous_state=(previous_query_states or {}).get(_ado_pipeline_source_query_state_id(signals[-1])),
            )
        elif workstream.ado_pipeline_ids:
            _record_ado_pipeline_query_state_values(
                query_state_sink,
                source="ado/pipeline",
                workstream_id=workstream.id,
                metadata={"window_days": program.ado.date_window_days},
                as_of=as_of,
                previous_state=(previous_query_states or {}).get(f"ado-pipeline:{workstream.id}"),
                row_count=pipeline_recent_run_count,
                numeric_value=float(pipeline_failed_run_count),
                value_metric="failed_run_count",
                extra_fields={"failed_run_count": pipeline_failed_run_count},
                zero_rows_ok=True,
                max_data_timestamp=pipeline_latest_run_timestamp,
                suppress_zero_frozen_warning=True,
            )
        if pull_request_summaries:
            raw_ref = f"ado-pr:{workstream.id}:{','.join(workstream.ado_repository_ids)}:{capture_date}"
            signals.append(
                Signal(
                    id=str(uuid5(NAMESPACE_URL, f"{program.id}|{raw_ref}")),
                    timestamp=as_of,
                    source="ado/pr",
                    program_id=program.id,
                    workstream_id=workstream.id,
                    entity_refs=merge_entity_refs(
                        provider_refs=tuple(dict.fromkeys(pull_request_refs[:10])),
                        workstream_id=workstream.id,
                    ),
                    text=_truncate_signal_text(f"{workstream.name}: {'; '.join(pull_request_summaries)}"),
                    raw_ref=raw_ref,
                    confidence=Confidence.HIGH,
                    metadata={
                        "workstream_id": workstream.id,
                        "window_days": program.ado.date_window_days,
                        "repository_ids": list(workstream.ado_repository_ids),
                        "repositories": pull_request_metadata,
                        "date": capture_date,
                    },
                )
            )
            _record_ado_pipeline_source_query_state(
                query_state_sink,
                signals[-1],
                as_of=as_of,
                previous_state=(previous_query_states or {}).get(_ado_pipeline_source_query_state_id(signals[-1])),
            )
        elif workstream.ado_repository_ids:
            _record_ado_pipeline_query_state_values(
                query_state_sink,
                source="ado/pr",
                workstream_id=workstream.id,
                metadata={"window_days": program.ado.date_window_days},
                as_of=as_of,
                previous_state=(previous_query_states or {}).get(f"ado-pr:{workstream.id}"),
                row_count=pull_request_open_count,
                numeric_value=float(pull_request_open_count),
                value_metric="open_pr_count",
                extra_fields={"open_pr_count": pull_request_open_count, "p90_age_days": pull_request_p90_age_days},
                zero_rows_ok=True,
                max_data_timestamp=as_of.astimezone(timezone.utc),
                suppress_zero_frozen_warning=True,
            )
    return tuple(signals), ado_calls


def summarize_pull_requests(
    *,
    repository_id: str,
    pull_requests: tuple[dict[str, Any], ...],
    as_of: datetime,
) -> dict[str, Any] | None:
    active_pull_requests = tuple(
        pull_request
        for pull_request in pull_requests
        if str(pull_request.get("status") or "").strip().lower() == "active"
    )
    if not active_pull_requests:
        return None
    age_pairs: list[tuple[dict[str, Any], float]] = []
    for pull_request in active_pull_requests:
        created_at = _parse_pull_request_timestamp(pull_request)
        if created_at is None:
            continue
        age_pairs.append((pull_request, max((as_of - created_at).total_seconds() / 86400.0, 0.0)))
    if not age_pairs:
        return None
    sorted_by_age = tuple(sorted(age_pairs, key=lambda entry: entry[1]))
    oldest_pull_request, oldest_age_days = max(age_pairs, key=lambda entry: entry[1])
    p90_index = max(int(len(sorted_by_age) * 0.9 + 0.999999) - 1, 0)
    p90_age_days = sorted_by_age[p90_index][1]
    repository = active_pull_requests[0].get("repository")
    repository_name = repository_id
    if isinstance(repository, dict):
        candidate_name = _optional_string(repository.get("name"))
        if candidate_name:
            repository_name = candidate_name
    oldest_pr_id = _parse_int(oldest_pull_request.get("pullRequestId"))
    text = f"repo {repository_name} has {len(active_pull_requests)} open PRs; P90 age {p90_age_days:.1f}d"
    if oldest_pr_id is not None:
        text += f"; oldest #{oldest_pr_id} {oldest_age_days:.1f}d"
    sorted_pull_requests = tuple(
        pull_request for pull_request, _age_days in sorted(age_pairs, key=lambda entry: entry[1], reverse=True)
    )
    pr_entity_refs = tuple(
        pr_ref
        for pull_request in sorted_pull_requests
        if (pr_ref := pull_request_provider_ref(pull_request, repository_name)) is not None
    )
    entity_refs = tuple(
        dict.fromkeys(
            ref
            for pull_request in sorted_pull_requests
            for ref in pull_request_entity_refs(pull_request, repository_name)
        )
    )
    return {
        "text": text,
        "entity_refs": entity_refs,
        "pr_entity_refs": pr_entity_refs,
        "metadata": {
            "repository_id": repository_id,
            "repository_name": repository_name,
            "open_pr_count": len(active_pull_requests),
            "p90_age_days": round(p90_age_days, 1),
            "oldest_pr_id": oldest_pr_id,
            "oldest_pr_age_days": round(oldest_age_days, 1),
            "draft_pr_count": sum(1 for pull_request in active_pull_requests if bool(pull_request.get("isDraft"))),
        },
    }


def pull_request_entity_refs(pull_request: dict[str, Any], repository_name: str) -> tuple[str, ...]:
    provider_ref = pull_request_provider_ref(pull_request, repository_name)
    title = _optional_string(pull_request.get("title")) or ""
    work_item_refs = _extract_work_item_refs(title)
    if provider_ref is None:
        return work_item_refs
    return (provider_ref, *work_item_refs)


def pull_request_provider_ref(pull_request: dict[str, Any], repository_name: str) -> str | None:
    pr_id = _parse_int(pull_request.get("pullRequestId"))
    if pr_id is None:
        return None
    return f"PR:{repository_name}/{pr_id}"


def _summarize_pipeline_runs(
    *,
    pipeline_id: str,
    runs: tuple[dict[str, Any], ...],
    window_start: datetime,
    window_days: int,
) -> dict[str, Any] | None:
    ordered_runs = tuple(sorted(runs, key=_pipeline_run_sort_key, reverse=True))
    recent_runs = tuple(
        run
        for run in ordered_runs
        if (timestamp := _pipeline_run_timestamp(run)) is None or timestamp >= window_start
    )
    failed_runs = tuple(run for run in recent_runs if _pipeline_run_result(run) == "failed")
    if not failed_runs:
        return None

    latest_failure = failed_runs[0]
    latest_run = recent_runs[0] if recent_runs else latest_failure
    pipeline_name = _pipeline_display_name(latest_run, pipeline_id)
    latest_failure_id = _optional_string(latest_failure.get("id")) or pipeline_id
    latest_failure_time = _pipeline_run_timestamp(latest_failure)
    latest_run_id = _optional_string(latest_run.get("id"))
    latest_run_result = _pipeline_run_result(latest_run) or _pipeline_run_state(latest_run) or "unknown"

    text = (
        f"pipeline {pipeline_name} failed {len(failed_runs)} of last {len(recent_runs)} runs in {window_days}d; "
        f"latest failure #{latest_failure_id}"
    )
    if latest_failure_time is not None:
        text += f" on {latest_failure_time.date().isoformat()}"
    if latest_run_id is not None and latest_run_id != latest_failure_id:
        text += f"; latest run #{latest_run_id} {latest_run_result}"

    metadata: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "recent_run_count": len(recent_runs),
        "failed_run_count": len(failed_runs),
        "latest_failure_run_id": _parse_int(latest_failure.get("id")),
        "latest_failure_finished_at": latest_failure_time.isoformat() if latest_failure_time is not None else None,
        "latest_failure_url": _pipeline_run_web_url(latest_failure),
        "latest_run_id": _parse_int(latest_run.get("id")),
        "latest_run_finished_at": (
            latest_run_ts.isoformat()
            if (latest_run_ts := _pipeline_run_timestamp(latest_run)) is not None
            else None
        ),
        "latest_run_result": latest_run_result,
    }
    return {"text": text, "metadata": metadata}


def _parse_pull_request_timestamp(pull_request: dict[str, Any]) -> datetime | None:
    for key in ("creationDate", "createdDate"):
        raw_value = pull_request.get(key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        normalized = raw_value.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _pipeline_run_sort_key(run: dict[str, Any]) -> tuple[datetime, str]:
    return (
        _pipeline_run_timestamp(run) or datetime.min.replace(tzinfo=timezone.utc),
        _optional_string(run.get("id")) or "",
    )


def _pipeline_run_timestamp(run: dict[str, Any]) -> datetime | None:
    for key in ("finishedDate", "createdDate", "queueTime"):
        parsed = _parse_datetime(run.get(key))
        if parsed is not None:
            return parsed
    return None


def _pipeline_run_state(run: dict[str, Any]) -> str | None:
    value = _optional_string(run.get("state"))
    return None if value is None else value.strip().lower()


def _pipeline_run_result(run: dict[str, Any]) -> str | None:
    value = _optional_string(run.get("result"))
    return None if value is None else value.strip().lower()


def _pipeline_display_name(run: dict[str, Any], pipeline_id: str) -> str:
    pipeline_payload = run.get("pipeline")
    if isinstance(pipeline_payload, dict):
        pipeline_name = _optional_string(pipeline_payload.get("name"))
        if pipeline_name is not None:
            return pipeline_name
    run_name = _optional_string(run.get("name"))
    return run_name if run_name is not None else pipeline_id


def _pipeline_run_web_url(run: dict[str, Any]) -> str | None:
    links = run.get("_links")
    if isinstance(links, dict):
        web = links.get("web")
        if isinstance(web, dict):
            href = _optional_string(web.get("href"))
            if href is not None:
                return href
    return _optional_string(run.get("url"))


def _record_ado_pipeline_source_query_state(
    query_state_sink: dict[str, dict[str, Any]] | None,
    signal: Signal,
    *,
    as_of: datetime,
    previous_state: dict[str, Any] | None = None,
) -> None:
    if query_state_sink is None:
        return
    metadata = signal.metadata or {}
    if signal.source == "ado/pipeline":
        pipelines = metadata.get("pipelines")
        pipeline_rows: tuple[dict[str, Any], ...] = (
            tuple(pipeline for pipeline in pipelines if isinstance(pipeline, dict))
            if isinstance(pipelines, list)
            else ()
        )
        recent_run_count = sum(_parse_int(pipeline.get("recent_run_count")) or 0 for pipeline in pipeline_rows)
        failed_run_count = sum(_parse_int(pipeline.get("failed_run_count")) or 0 for pipeline in pipeline_rows)
        latest_run_timestamp = max(
            (
                parsed
                for pipeline in pipeline_rows
                if (parsed := _parse_datetime(pipeline.get("latest_run_finished_at"))) is not None
            ),
            default=None,
        )
        _record_ado_pipeline_query_state_values(
            query_state_sink,
            source=signal.source,
            workstream_id=signal.workstream_id,
            metadata=metadata,
            as_of=as_of,
            previous_state=previous_state,
            row_count=recent_run_count,
            numeric_value=float(failed_run_count),
            value_metric="failed_run_count",
            extra_fields={"failed_run_count": failed_run_count},
            zero_rows_ok=False,
            max_data_timestamp=latest_run_timestamp,
            suppress_zero_frozen_warning=True,
        )
    elif signal.source == "ado/pr":
        repositories = metadata.get("repositories")
        repository_rows: tuple[dict[str, Any], ...] = (
            tuple(repo for repo in repositories if isinstance(repo, dict))
            if isinstance(repositories, list)
            else ()
        )
        open_pr_count = sum(_parse_int(repo.get("open_pr_count")) or 0 for repo in repository_rows)
        p90_age_days = max((_parse_float(repo.get("p90_age_days")) or 0.0 for repo in repository_rows), default=0.0)
        _record_ado_pipeline_query_state_values(
            query_state_sink,
            source=signal.source,
            workstream_id=signal.workstream_id,
            metadata=metadata,
            as_of=as_of,
            previous_state=previous_state,
            row_count=open_pr_count,
            numeric_value=float(open_pr_count),
            value_metric="open_pr_count",
            extra_fields={"open_pr_count": open_pr_count, "p90_age_days": p90_age_days},
            zero_rows_ok=False,
            max_data_timestamp=as_of.astimezone(timezone.utc),
            suppress_zero_frozen_warning=True,
        )


def _record_ado_pipeline_query_state_values(
    query_state_sink: dict[str, dict[str, Any]] | None,
    *,
    source: str,
    workstream_id: str | None,
    metadata: dict[str, Any],
    as_of: datetime,
    previous_state: dict[str, Any] | None,
    row_count: int,
    numeric_value: float | None,
    value_metric: str,
    extra_fields: dict[str, Any],
    zero_rows_ok: bool,
    max_data_timestamp: datetime | None,
    suppress_zero_frozen_warning: bool = False,
) -> None:
    if query_state_sink is None:
        return
    timestamp = as_of.astimezone(timezone.utc)
    window_days = _parse_int(metadata.get("window_days")) or 1
    expected_max_age_hours = max(window_days * 24, 24)
    value_last_4 = _roll_query_value_history(previous_state, numeric_value)
    query_id = f"{source.replace('/', '-')}:{workstream_id or 'program'}"
    state: dict[str, Any] = {
        "last_attempted_at": timestamp,
        "last_succeeded_at": timestamp,
        "row_count": row_count,
        "duration_ms": 0,
        "last_cycle_succeeded": True,
        "zero_rows_ok": zero_rows_ok,
        "last_error": None,
        "expected_max_age_hours": expected_max_age_hours,
        "value_metric": value_metric,
        **extra_fields,
    }
    if max_data_timestamp is not None:
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
            and not (suppress_zero_frozen_warning and float(numeric_value) == 0.0)
        )
    query_state_sink[query_id] = state


def _summarize_pipeline_query_state(*, runs: tuple[dict[str, Any], ...], window_start: datetime) -> dict[str, Any]:
    ordered_runs = tuple(sorted(runs, key=_pipeline_run_sort_key, reverse=True))
    recent_runs = tuple(
        run
        for run in ordered_runs
        if (timestamp := _pipeline_run_timestamp(run)) is None or timestamp >= window_start
    )
    failed_runs = tuple(run for run in recent_runs if _pipeline_run_result(run) == "failed")
    latest_run_timestamp = _pipeline_run_timestamp(recent_runs[0]) if recent_runs else None
    return {
        "recent_run_count": len(recent_runs),
        "failed_run_count": len(failed_runs),
        "latest_run_finished_at": latest_run_timestamp,
    }


def _summarize_pull_request_query_state(*, pull_requests: tuple[dict[str, Any], ...], as_of: datetime) -> dict[str, Any]:
    active_pull_requests = tuple(
        pull_request
        for pull_request in pull_requests
        if str(pull_request.get("status") or "").strip().lower() == "active"
    )
    age_days = [
        max((as_of - created_at).total_seconds() / 86400.0, 0.0)
        for pull_request in active_pull_requests
        if (created_at := _parse_pull_request_timestamp(pull_request)) is not None
    ]
    if not age_days:
        return {"open_pr_count": len(active_pull_requests), "p90_age_days": 0.0}
    sorted_ages = sorted(age_days)
    p90_index = max(int(len(sorted_ages) * 0.9 + 0.999999) - 1, 0)
    return {"open_pr_count": len(active_pull_requests), "p90_age_days": round(sorted_ages[p90_index], 1)}


def _ado_pipeline_source_query_state_id(signal: Signal) -> str:
    workstream_id = signal.workstream_id or "program"
    return f"{signal.source.replace('/', '-')}:{workstream_id}"


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


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _roll_query_value_history(previous_state: dict[str, Any] | None, numeric_value: float | None) -> list[float]:
    previous_values = (previous_state or {}).get("value_last_4")
    history: list[float] = []
    if isinstance(previous_values, list):
        for value in previous_values:
            try:
                history.append(float(value))
            except (TypeError, ValueError):
                continue
    if numeric_value is None:
        return history[-4:]
    history.append(float(numeric_value))
    return history[-4:]
