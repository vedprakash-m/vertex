from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Protocol

from src.core.exceptions import QueryError

log = logging.getLogger(__name__)


class SavedQueryClient(Protocol):
    def get_saved_query(self, query_id: str) -> dict[str, Any]: ...

    def execute_wiql(self, wiql: str, top: int = 2000) -> list[int]: ...


class WorkItemBatchClient(Protocol):
    def query_work_items_batch(
        self,
        work_item_ids: list[int],
        fields: tuple[str, ...],
    ) -> list[dict[str, Any]]: ...


def extract_saved_query_wiql(query_payload: dict[str, Any]) -> str | None:
    wiql = query_payload.get("wiql")
    if not isinstance(wiql, str):
        return None
    normalized = wiql.strip()
    return normalized or None


def append_wiql_clause(wiql: str, clause: str, *, wrap_clause: bool = True) -> str:
    if not clause:
        return wiql
    lower_wiql = wiql.lower()
    order_by_index = lower_wiql.rfind(" order by ")
    rendered_clause = f"({clause})" if wrap_clause else clause
    if order_by_index >= 0:
        prefix = wiql[:order_by_index]
        if " where " in lower_wiql[:order_by_index]:
            return f"{prefix} and {rendered_clause}{wiql[order_by_index:]}"
        return f"{prefix} where {rendered_clause}{wiql[order_by_index:]}"
    if " where " in lower_wiql:
        return f"{wiql} and {rendered_clause}"
    return f"{wiql} where {rendered_clause}"


def bound_saved_query_wiql(
    wiql: str,
    *,
    since: datetime,
    additional_clause: str | None = None,
) -> str:
    lower_wiql = wiql.lower()
    order_by_index = lower_wiql.rfind(" order by ")
    where_clause = lower_wiql[:order_by_index] if order_by_index >= 0 else lower_wiql
    where_keyword_index = where_clause.find(" where ")
    where_body = where_clause[where_keyword_index:] if where_keyword_index >= 0 else ""
    filters: list[str] = []
    if "[system.changeddate]" not in where_body:
        filters.append(f"[System.ChangedDate] >= '{since.astimezone(timezone.utc).strftime('%Y-%m-%d')}'")
    if additional_clause:
        filters.append(f"({additional_clause})")
    if not filters:
        return wiql
    return append_wiql_clause(wiql, " and ".join(filters), wrap_clause=False)


def load_saved_query_item_ids(
    client: SavedQueryClient,
    query_ids: tuple[str, ...],
    *,
    since: datetime,
    query_clauses: dict[str, str] | None = None,
    top_cap: int,
    logger: logging.Logger | None = None,
) -> tuple[list[int], dict[int, tuple[str, ...]], int]:
    if not query_ids:
        return [], {}, 0

    resolved_logger = logger or log
    ordered_item_ids: list[int] = []
    seen_item_ids: set[int] = set()
    query_ids_by_item_id: dict[int, list[str]] = {}
    ado_calls = 0
    for query_id in query_ids:
        query_payload = client.get_saved_query(query_id)
        ado_calls += 1
        wiql = extract_saved_query_wiql(query_payload)
        if wiql is None:
            continue
        bounded_wiql = bound_saved_query_wiql(
            wiql,
            since=since,
            additional_clause=None if query_clauses is None else query_clauses.get(query_id),
        )
        try:
            for work_item_id in client.execute_wiql(bounded_wiql, top=top_cap):
                item_query_ids = query_ids_by_item_id.setdefault(work_item_id, [])
                if query_id not in item_query_ids:
                    item_query_ids.append(query_id)
                if work_item_id in seen_item_ids:
                    continue
                seen_item_ids.add(work_item_id)
                ordered_item_ids.append(work_item_id)
        except QueryError as exc:
            resolved_logger.warning("Saved query %s WIQL execution failed — skipping: %s", query_id, exc)
        ado_calls += 1
    return ordered_item_ids, {item_id: tuple(value) for item_id, value in query_ids_by_item_id.items()}, ado_calls


def merge_item_ids(primary_ids: list[int], extra_ids: list[int]) -> list[int]:
    merged = list(primary_ids)
    seen_ids = set(primary_ids)
    for work_item_id in extra_ids:
        if work_item_id in seen_ids:
            continue
        seen_ids.add(work_item_id)
        merged.append(work_item_id)
    return merged


def query_work_item_batch_rows(
    client: WorkItemBatchClient,
    work_item_ids: list[int],
    fields: tuple[str, ...],
    *,
    batch_size: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """Fetches work items by id in batches. A single deleted/inaccessible id
    in a batch (Azure DevOps raises for permission-denied ids rather than
    silently omitting them, unlike simple not-found ids) must not poison the
    whole chunk -- ADF-OM6: "no optional channel can block a required-channel
    result." On a batch failure, falls back to fetching that chunk one id at
    a time, skipping (with a warning) only the individual ids that still
    fail, and keeping every id that succeeds."""
    if not work_item_ids:
        return [], 0

    rows: list[dict[str, Any]] = []
    ado_calls = 0
    for start in range(0, len(work_item_ids), batch_size):
        chunk = work_item_ids[start:start + batch_size]
        ado_calls += 1  # the batch attempt itself always consumes one call, success or failure
        try:
            rows.extend(client.query_work_items_batch(chunk, fields))
        except QueryError as exc:
            log.warning(
                "Work item batch fetch failed for %d id(s) — retrying individually: %s", len(chunk), exc
            )
            for work_item_id in chunk:
                ado_calls += 1
                try:
                    rows.extend(client.query_work_items_batch([work_item_id], fields))
                except QueryError as item_exc:
                    log.warning("Work item %s is unreadable — skipping: %s", work_item_id, item_exc)
    return rows, ado_calls


def query_work_item_snapshot_history_rows(
    client: Any,
    work_item_ids: list[int],
    *,
    select_fields: tuple[str, ...],
    start_date: str | None = None,
    batch_size: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    if not work_item_ids:
        return [], 0

    rows: list[dict[str, Any]] = []
    ado_calls = 0
    history_loader = getattr(client, "query_work_item_snapshot_history", None)
    if not callable(history_loader):
        return [], 0

    for start in range(0, len(work_item_ids), batch_size):
        rows.extend(
            history_loader(
                work_item_ids[start:start + batch_size],
                select_fields=select_fields,
                start_date=start_date,
            )
        )
        ado_calls += 1
    return rows, ado_calls
