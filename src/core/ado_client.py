from __future__ import annotations

# Adapted from Shiproom src/ado/client.py

import base64
import logging
import os
import re
import sys
import threading
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.adapters.microsoft.ado_runtime import load_ado_credential_types
from src.core.exceptions import AuthError, CredentialExpired, QueryError, QueryTimeoutError
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES_ADO

if TYPE_CHECKING:
    # ADF-W2.1: type-only import. A module-level runtime import here would
    # create a real circular import (integration_types.py imports
    # PullRequestSummary from ado_pr_client.py, which imports ADOClient from
    # this module) -- each pagination method does its own local import of
    # PaginationOutcome instead, at the point it actually constructs one.
    from src.core.integration_types import PaginationOutcome

AZURE_IDENTITY_AVAILABLE, AZURE_CREDENTIAL_TYPES = load_ado_credential_types()


ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
_AUTH_MODE_ENV = "VERTEX_ADO_AUTH_MODE"
_STRICT_AUTH_MODES: frozenset[str] = frozenset({"azure-cli", "pat"})

#: ADF-W2.1 (Section 8.4.2): default WIQL result cap, made an explicit,
#: importable constant so callers that need to detect "the result was
#: capped, not just naturally small" can compare against the exact value
#: actually used rather than guessing.
ADO_WIQL_DEFAULT_TOP = 2000

#: Safety bound on multi-page fetch loops (revisions/comments/PRs) -- distinct
#: from each provider's per-page size. Reached only for a work item with an
#: implausibly large history; see PaginationOutcome.is_truncated.
_DEFAULT_MAX_PAGES = 10

log = logging.getLogger(__name__)


class ADOClient:
    def __init__(
        self,
        organization: str,
        project: str,
        timeout: int = 30,
        pat_env: str = "ADO_PAT",
        show_progress: bool = True,
        slow_warning_seconds: int = 15,
        progress_poll_seconds: float = 0.2,
        progress_stream: Any | None = None,
    ) -> None:
        self.organization = organization
        self.project = project
        self.timeout = timeout
        self.pat_env = pat_env
        self.show_progress = show_progress
        self.slow_warning_seconds = slow_warning_seconds
        self.progress_poll_seconds = progress_poll_seconds
        self.progress_stream = progress_stream or sys.stderr
        self.auth_method = "unknown"
        self._credential: Any | None = None
        self._credential_lock = threading.Lock()
        self._cached_access_token: str | None = None
        self._cached_access_token_expires_on = 0.0
        self._session = self._build_session()
        # ADF-W1.1: a separate, non-retrying session for mutations. Retrying a
        # POST/PATCH whose response was lost (timeout, connection reset) can
        # duplicate or double-apply a write; reads keep automatic retry via
        # ``_session``, mutations never do (INV-ADF-9).
        self._mutation_session = self._build_mutation_session()
        self._odata_base_url = (
            f"https://analytics.dev.azure.com/{organization}/{project}/_odata/v4.0-preview/"
        )
        self._rest_base_url = f"https://dev.azure.com/{organization}/{project}/_apis/wit/"
        self._init_auth()

    def fork_read_client(self) -> "ADOClient":
        """Return a read-only client with an isolated HTTP session.

        Gather hydration can concurrently read independent work items, but a
        ``requests.Session`` must not be shared across those workers.  The
        already-acquired credential/token is copied instead of constructing a
        new Azure CLI credential per worker (which would spawn a token helper
        process for each detail request).
        """
        clone = object.__new__(ADOClient)
        clone.organization = self.organization
        clone.project = self.project
        clone.timeout = self.timeout
        clone.pat_env = self.pat_env
        clone.show_progress = False
        clone.slow_warning_seconds = self.slow_warning_seconds
        clone.progress_poll_seconds = self.progress_poll_seconds
        clone.progress_stream = self.progress_stream
        clone.auth_method = self.auth_method
        clone._credential = self._credential
        clone._credential_lock = threading.Lock()
        with self._credential_lock:
            clone._cached_access_token = self._cached_access_token
            clone._cached_access_token_expires_on = self._cached_access_token_expires_on
        clone._session = clone._build_session()
        clone._mutation_session = clone._build_mutation_session()
        clone._odata_base_url = self._odata_base_url
        clone._rest_base_url = self._rest_base_url
        return clone

    def query_work_items(
        self,
        filter_expression: str,
        select_fields: tuple[str, ...] = ("WorkItemId", "WorkItemType"),
        top: int = 1000,
    ) -> list[dict[str, Any]]:
        params = {
            "$filter": filter_expression,
            "$select": ",".join(select_fields),
            "$expand": "Area",
            "$count": "true",
            "$top": str(top),
        }
        return self.query_odata_all("WorkItems", params)

    def query_work_item_snapshot(
        self,
        filter_expression: str,
        select_fields: tuple[str, ...] = ("DateSK", "WorkItemId"),
        top: int | None = None,
    ) -> list[dict[str, Any]]:
        # WorkItemSnapshot has no flat AreaPath/IterationPath scalar
        # properties -- only the Area/Iteration navigation properties expose
        # them (requesting the flat name in $select raises VS403522). Ask
        # for whichever of these two callers actually requested via
        # $expand=<Nav>($select=<Field>), then flatten the nested payload
        # back onto each row so callers keep seeing the flat field name they
        # asked for.
        nav_property_by_flat_field = {"AreaPath": "Area", "IterationPath": "Iteration"}
        requested_flat_fields = tuple(
            field for field in select_fields if field in nav_property_by_flat_field
        )
        query_select_fields = tuple(
            field for field in select_fields if field not in nav_property_by_flat_field
        )
        params = {
            "$filter": filter_expression,
            "$select": ",".join(query_select_fields),
        }
        if requested_flat_fields:
            params["$expand"] = ",".join(
                f"{nav_property_by_flat_field[field]}($select={field})"
                for field in requested_flat_fields
            )
        if top is not None:
            params["$top"] = str(top)
        rows = self.query_odata_all("WorkItemSnapshot", params)
        if not requested_flat_fields:
            return rows
        flattened_rows = []
        for row in rows:
            flattened = dict(row)
            for field in requested_flat_fields:
                nav_property = nav_property_by_flat_field[field]
                nested = flattened.pop(nav_property, None)
                flattened[field] = nested.get(field, "") if isinstance(nested, dict) else ""
            flattened_rows.append(flattened)
        return flattened_rows

    def query_all(
        self,
        filter_expression: str,
        select_fields: tuple[str, ...] = ("WorkItemId", "WorkItemType"),
        top: int = 1000,
    ) -> list[dict[str, Any]]:
        return self.query_work_items(
            filter_expression=filter_expression,
            select_fields=select_fields,
            top=top,
        )

    def query_areas(
        self,
        filter_expression: str | None = None,
        select_fields: tuple[str, ...] = ("AreaPath",),
        top: int = 1000,
    ) -> list[dict[str, Any]]:
        params = {
            "$select": ",".join(select_fields),
            "$top": str(top),
        }
        if filter_expression:
            params["$filter"] = filter_expression
        return self.query_odata_all("Areas", params)

    def area_path_exists(self, area_path: str) -> bool:
        escaped = area_path.replace("'", "''")
        areas = self.query_areas(
            filter_expression=f"AreaPath eq '{escaped}'",
            top=1,
        )
        return bool(areas)

    def find_area_scope_matches(self, area_path: str, top: int = 20) -> tuple[str, ...]:
        normalized = area_path.rstrip("\\")
        if not normalized:
            return tuple()
        escaped = normalized.replace("'", "''")
        descendant_prefix = f"{escaped}\\"
        areas = self.query_areas(
            filter_expression=(
                f"AreaPath eq '{escaped}' or startswith(AreaPath, '{descendant_prefix}')"
            ),
            top=top,
        )
        suggestions = [area.get("AreaPath", "") for area in areas if area.get("AreaPath")]
        seen: set[str] = set()
        ordered: list[str] = []
        for suggestion in suggestions:
            if suggestion in seen:
                continue
            seen.add(suggestion)
            ordered.append(suggestion)
        return tuple(ordered)

    def area_scope_exists(self, area_path: str) -> bool:
        return bool(self.find_area_scope_matches(area_path, top=1))

    def suggest_area_paths(self, area_path: str, top: int = 20) -> tuple[str, ...]:
        fragments = [fragment for fragment in re.split(r"\\+", area_path) if fragment]
        search_terms = [fragment for fragment in fragments[-2:] if len(fragment) >= 2]
        if not search_terms:
            search_terms = fragments[-1:]
        if not search_terms:
            return tuple()
        escaped = [term.replace("'", "''") for term in search_terms]
        conditions = [f"contains(AreaPath, '{e}')" for e in escaped]
        filter_expression = " or ".join(conditions)
        areas = self.query_areas(filter_expression=filter_expression, top=top)
        suggestions = [area.get("AreaPath", "") for area in areas if area.get("AreaPath")]
        seen: set[str] = set()
        ordered: list[str] = []
        for suggestion in suggestions:
            if suggestion in seen:
                continue
            seen.add(suggestion)
            ordered.append(suggestion)
        return tuple(ordered)

    def query_odata_all(self, entity_set: str, params: dict[str, str]) -> list[dict[str, Any]]:
        url = f"{self._odata_base_url}{entity_set}"
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params: dict[str, str] | None = params

        while next_url:
            payload = self._request_json("GET", next_url, params=next_params)
            items.extend(payload.get("value", []))
            next_url = payload.get("@odata.nextLink")
            next_params = None

        return items

    def probe_rest_batch(self, work_item_ids: list[int]) -> dict[str, Any]:
        url = f"{self._rest_base_url}workitemsbatch?api-version=7.1"
        payload = {
            "ids": work_item_ids,
            "fields": [
                "System.Id",
                "System.WorkItemType",
                "System.Title",
            ],
        }
        return self._request_json("POST", url, json=payload)

    def query_work_items_batch(
        self,
        work_item_ids: list[int],
        fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not work_item_ids:
            return []
        url = f"{self._rest_base_url}workitemsbatch?api-version=7.1"
        payload = {
            "ids": work_item_ids,
            "fields": list(fields),
        }
        response = self._request_json("POST", url, json=payload)
        return response.get("value", [])

    def get_work_items(
        self,
        work_item_ids: list[int],
        *,
        fields: tuple[str, ...] = (),
        expand: str | None = None,
    ) -> list[dict[str, Any]]:
        if not work_item_ids:
            return []
        rows: list[dict[str, Any]] = []
        batch_size = 200
        for start in range(0, len(work_item_ids), batch_size):
            batch_ids = work_item_ids[start:start + batch_size]
            url = f"{self._rest_base_url}workitems?ids={','.join(str(work_item_id) for work_item_id in batch_ids)}&api-version=7.1"
            params: dict[str, str] = {}
            if fields:
                params["fields"] = ",".join(fields)
            if expand is not None:
                params["$expand"] = expand
            response = self._request_json("GET", url, params=params if params else None)
            rows.extend(response.get("value", []))
        return rows

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, Any]]:
        try:
            return self.get_work_items(work_item_ids, expand="relations")
        except QueryError:
            rows: list[dict[str, Any]] = []
            for work_item_id in work_item_ids:
                url = f"{self._rest_base_url}workItems/{work_item_id}?$expand=relations&api-version=7.1"
                rows.append(self._request_json("GET", url))
            return rows

    def query_work_item_snapshot_history(
        self,
        work_item_ids: list[int],
        *,
        select_fields: tuple[str, ...],
        start_date: str | None = None,
    ) -> list[dict[str, Any]]:
        if not work_item_ids:
            return []
        ordered_ids = ",".join(str(work_item_id) for work_item_id in sorted(dict.fromkeys(work_item_ids)))
        filters = [f"WorkItemId in ({ordered_ids})"]
        if start_date is not None:
            filters.append(f"DateValue ge {start_date}")
        return self.query_work_item_snapshot(
            filter_expression=" and ".join(filters),
            select_fields=select_fields,
        )

    def list_work_item_revisions(
        self,
        work_item_id: int,
        *,
        page_size: int = 200,
        max_pages: int = _DEFAULT_MAX_PAGES,
        on_pagination: Callable[[PaginationOutcome], None] | None = None,
    ) -> list[dict[str, Any]]:
        """ADF-W2.1 (Section 8.4.2): pages via ``$top``/``$skip`` rather than
        a single request. ``on_pagination``, if given, fires once with the
        fetch's completeness outcome -- callers that don't care about
        truncation (most of them; a work item rarely has >2000 revisions)
        can omit it and simply receive the fully-paged result."""
        url = f"{self._rest_base_url}workItems/{work_item_id}/revisions?api-version=7.1"
        all_rows: list[dict[str, Any]] = []
        page_count = 0
        is_truncated = False
        skip = 0
        while page_count < max_pages:
            response = self._request_json("GET", url, params={"$top": str(page_size), "$skip": str(skip)})
            page_rows = response.get("value", [])
            all_rows.extend(page_rows)
            page_count += 1
            if len(page_rows) < page_size:
                break
            skip += page_size
        else:
            is_truncated = True
        if on_pagination is not None:
            from src.core.integration_types import PaginationOutcome

            on_pagination(PaginationOutcome(total_fetched=len(all_rows), page_count=page_count, is_truncated=is_truncated))
        return all_rows

    def list_work_item_comments(
        self,
        work_item_id: int,
        *,
        max_pages: int = _DEFAULT_MAX_PAGES,
        on_pagination: Callable[[PaginationOutcome], None] | None = None,
    ) -> list[dict[str, Any]]:
        """ADF-W2.1 (Section 8.4.2): follows the comments API's
        ``continuationToken`` response header across pages, rather than
        returning only the first page. ``on_pagination`` is the same
        optional completeness callback as ``list_work_item_revisions``."""
        all_comments: list[dict[str, Any]] = []
        page_count = 0
        is_truncated = False
        continuation_token: str | None = None
        while page_count < max_pages:
            url = f"{self._rest_base_url}workItems/{work_item_id}/comments?api-version=7.1-preview.4"
            params = {"continuationToken": continuation_token} if continuation_token else None
            response = self._request_response("GET", url, params=params)
            body = response.json()
            page_comments = body.get("comments", [])
            all_comments.extend(page_comments)
            page_count += 1
            continuation_token = response.headers.get("x-ms-continuationtoken")
            if not continuation_token:
                break
        else:
            is_truncated = True
        if on_pagination is not None:
            from src.core.integration_types import PaginationOutcome

            on_pagination(
                PaginationOutcome(total_fetched=len(all_comments), page_count=page_count, is_truncated=is_truncated)
            )
        return all_comments

    def get_saved_query(self, query_id: str) -> dict[str, Any]:
        encoded_query = quote(query_id, safe="/")
        url = f"{self._rest_base_url}queries/{encoded_query}?$expand=wiql&api-version=7.1"
        return self._request_json("GET", url)

    def create_saved_query(
        self,
        parent_query_path: str,
        *,
        name: str,
        wiql: str | None = None,
        is_folder: bool = False,
        validate_wiql_only: bool = False,
    ) -> dict[str, Any]:
        encoded_parent = quote(parent_query_path, safe="/")
        url = f"{self._rest_base_url}queries/{encoded_parent}?api-version=7.1"
        if validate_wiql_only:
            url = f"{url}&validateWiqlOnly=true"

        payload: dict[str, Any] = {
            "name": name,
            "isFolder": is_folder,
        }
        if wiql is not None:
            payload["wiql"] = wiql

        if validate_wiql_only:
            response = self._request_with_progress("POST", url, json=payload)
            if response.status_code >= 400:
                raise QueryError(
                    f"ADO request failed with status {response.status_code}: {response.text[:500]}"
                )
            return {}

        return self._request_json("POST", url, json=payload)

    def count_work_items(self, filter_expression: str) -> int:
        """Return the item count for a filter without fetching any rows."""
        params = {
            "$filter": filter_expression,
            "$count": "true",
            "$top": "0",
        }
        payload = self._request_json("GET", f"{self._odata_base_url}WorkItems", params=params)
        return int(payload.get("@odata.count", 0))

    def execute_wiql(
        self,
        wiql: str,
        top: int = ADO_WIQL_DEFAULT_TOP,
        *,
        on_pagination: Callable[[PaginationOutcome], None] | None = None,
    ) -> list[int]:
        """WIQL's endpoint has no ``$skip``/continuation mechanism -- unlike
        revisions/comments/PRs, a result at the cap cannot be paged further
        without a different querying strategy (date/ID-range splitting).
        ``on_pagination``, if given, still fires so a cap hit is a
        structured, actionable signal (ADF-W2.1, Section 8.4.2) rather than
        only the pre-existing log line below."""
        url = f"{self._rest_base_url}wiql?api-version=7.1&$top={top}"
        response = self._request_json("POST", url, json={"query": wiql})
        ordered_ids: list[int] = []
        seen_ids: set[int] = set()

        for item in response.get("workItems", []):
            if not isinstance(item, dict):
                continue
            work_item_id = int(item.get("id") or 0)
            if work_item_id <= 0 or work_item_id in seen_ids:
                continue
            seen_ids.add(work_item_id)
            ordered_ids.append(work_item_id)

        for relation in response.get("workItemRelations", []):
            if not isinstance(relation, dict):
                continue
            for relation_end in ("source", "target"):
                relation_item = relation.get(relation_end)
                if not isinstance(relation_item, dict):
                    continue
                work_item_id = int(relation_item.get("id") or 0)
                if work_item_id <= 0 or work_item_id in seen_ids:
                    continue
                seen_ids.add(work_item_id)
                ordered_ids.append(work_item_id)

        is_capped = len(ordered_ids) >= top
        if is_capped:
            logging.getLogger(__name__).warning(
                "WIQL query returned %d IDs at cap %d — results are likely truncated; review query scope",
                len(ordered_ids),
                top,
            )
        if on_pagination is not None:
            from src.core.integration_types import PaginationOutcome

            on_pagination(PaginationOutcome(total_fetched=len(ordered_ids), page_count=1, is_truncated=is_capped))
        return ordered_ids

    def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, Any]]:
        team_path = "" if team is None else f"/{quote(team, safe='')}"
        url = (
            f"https://dev.azure.com/{self.organization}/{self.project}{team_path}"
            "/_apis/work/teamsettings/iterations?api-version=7.1"
        )
        params: dict[str, str] = {}
        if timeframe is not None:
            params["$timeframe"] = timeframe
        response = self._request_json("GET", url, params=params)
        values = response.get("value")
        if not isinstance(values, list):
            values = response.get("values")
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    def list_iteration_capacities(self, iteration_id: str, team: str | None = None) -> list[dict[str, Any]]:
        team_path = "" if team is None else f"/{quote(team, safe='')}"
        url = (
            f"https://dev.azure.com/{self.organization}/{self.project}{team_path}/_apis/work/teamsettings/iterations/"
            f"{iteration_id}/capacities?api-version=6.0"
        )
        response = self._request_json("GET", url)
        values = response.get("value")
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    def list_pipeline_runs(self, pipeline_id: str, top: int = 10) -> list[dict[str, Any]]:
        url = (
            f"https://dev.azure.com/{self.organization}/{self.project}"
            f"/_apis/pipelines/{quote(str(pipeline_id), safe='')}/runs?api-version=7.1"
        )
        response = self._request_json("GET", url, params={"$top": str(top)})
        values = response.get("value")
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    def list_pull_requests(
        self,
        repository_id: str,
        *,
        status: str = "active",
        top: int = 100,
        max_pages: int = _DEFAULT_MAX_PAGES,
        on_pagination: Callable[[PaginationOutcome], None] | None = None,
    ) -> list[dict[str, Any]]:
        """ADF-W2.1 (Section 8.4.2): pages via ``$top``/``$skip`` across the
        full result set rather than returning only the first ``top`` PRs --
        the same pagination loop ``list_work_item_revisions`` and
        ``ADOPRClient.list_pull_requests`` already use. ``top`` is the page
        size; ``max_pages`` is the safety cap (distinct from each provider's
        per-page size). ``on_pagination``, if given, fires once with the
        fetch's completeness outcome. Callers that don't care about truncation
        can omit it and simply receive the fully-paged result."""
        url = (
            f"https://dev.azure.com/{self.organization}/{self.project}"
            f"/_apis/git/repositories/{quote(str(repository_id), safe='')}/pullrequests?api-version=7.1"
        )
        all_rows: list[dict[str, Any]] = []
        page_count = 0
        is_truncated = False
        skip = 0
        while page_count < max_pages:
            response = self._request_json(
                "GET",
                url,
                params={
                    "searchCriteria.status": status,
                    "$top": str(top),
                    "$skip": str(skip),
                },
            )
            page_rows = response.get("value", [])
            if isinstance(page_rows, list):
                all_rows.extend(row for row in page_rows if isinstance(row, dict))
            page_count += 1
            if len(page_rows) < top:
                break
            skip += top
        else:
            is_truncated = True
        if on_pagination is not None:
            from src.core.integration_types import PaginationOutcome

            on_pagination(PaginationOutcome(total_fetched=len(all_rows), page_count=page_count, is_truncated=is_truncated))
        return all_rows

    def list_repositories(self) -> list[dict[str, Any]]:
        url = f"https://dev.azure.com/{self.organization}/{self.project}/_apis/git/repositories?api-version=7.1"
        response = self._request_json("GET", url)
        values = response.get("value")
        return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []

    def list_area_paths(
        self,
        *,
        days: int = 90,
        top: int = 200,
    ) -> tuple[str, ...]:
        """Return distinct AreaPath values from work items changed in the last N days.

        Uses ``$expand=Area`` on the WorkItems OData entity so that AreaPath values
        are drawn from items that have actually had recent activity, not just
        from the full area hierarchy.
        """
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        items = self.query_work_items(
            filter_expression=f"ChangedDate ge {cutoff}T00:00:00Z",
            select_fields=("WorkItemId",),
            top=top,
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            area = item.get("Area") or {}
            path = str(area.get("AreaPath", "") or "")
            if path and path not in seen:
                seen.add(path)
                ordered.append(path)
        return tuple(sorted(ordered))

    def get_recent_work_items_summary(
        self,
        area_path: str,
        *,
        days: int = 30,
        top: int = 200,
    ) -> list[dict[str, Any]]:
        """Return lightweight work item summaries under area_path changed in last N days.

        Excludes terminal states (Closed, Completed, Resolved, Removed, Cut).
        Returns fields: id, title, type, area_path, assigned_to, state, target_date.
        """
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        escaped = area_path.rstrip("\\").replace("'", "''")
        descendant_prefix = f"{escaped}\\\\"
        state_clauses = " and ".join(f"State ne '{state}'" for state in TERMINAL_WORK_ITEM_STATES_ADO)
        filter_expr = (
            f"(AreaPath eq '{escaped}' or startswith(AreaPath, '{descendant_prefix}')) "
            f"and ChangedDate ge {cutoff}T00:00:00Z "
            f"and {state_clauses}"
        )
        items = self.query_work_items(
            filter_expression=filter_expr,
            select_fields=("WorkItemId", "WorkItemType", "Title", "State", "AssignedTo", "TargetDate"),
            top=top,
        )
        result: list[dict[str, Any]] = []
        for item in items:
            area = item.get("Area") or {}
            assigned = item.get("AssignedTo") or {}
            result.append({
                "id": item.get("WorkItemId"),
                "title": str(item.get("Title", "") or ""),
                "type": str(item.get("WorkItemType", "") or ""),
                "area_path": str(area.get("AreaPath", "") or ""),
                "assigned_to": (
                    str(assigned.get("UniqueName", "") or "")
                    if isinstance(assigned, dict)
                    else str(assigned)
                ),
                "state": str(item.get("State", "") or ""),
                "target_date": item.get("TargetDate"),
            })
        return result

    def _build_session(self) -> requests.Session:
        """Read session: safe to retry (GET, and the read-only WIQL/batch POSTs)."""
        session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _build_mutation_session(self) -> requests.Session:
        """ADF-W1.1 (INV-ADF-9): mutation session with no automatic retry.

        A lost response after a committed server-side write must never be
        silently retried by the transport layer -- that is exactly how a
        duplicate work item gets created. Rate-limit courtesy (429) is
        handled once, explicitly, by the caller (``ADOWriter._request_json``)
        rather than via a transparent retry loop here.
        """
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _init_auth(self) -> None:
        requested_mode = os.environ.get(_AUTH_MODE_ENV, "").strip().lower()
        if requested_mode and requested_mode not in _STRICT_AUTH_MODES:
            raise AuthError(
                f"{_AUTH_MODE_ENV} must be azure-cli or pat when set; got {requested_mode!r}."
            )

        if requested_mode == "pat":
            if os.environ.get(self.pat_env):
                self.auth_method = "pat"
                return
            raise AuthError("PAT authentication was selected but ADO_PAT is not set.")

        if AZURE_IDENTITY_AVAILABLE:
            credential_classes = tuple(AZURE_CREDENTIAL_TYPES)
            for credential_class, auth_method in zip(
                credential_classes,
                ("azure_cli", "default_credential"),
                strict=False,
            ):
                if requested_mode == "azure-cli" and auth_method != "azure_cli":
                    continue
                try:
                    credential = credential_class()
                    access_token = credential.get_token(ADO_RESOURCE)
                    self._credential = credential
                    self._cache_access_token(access_token)
                    self.auth_method = auth_method
                    return
                except Exception:
                    continue

        if requested_mode == "azure-cli":
            raise AuthError(
                "Azure CLI authentication was selected but no Azure CLI Azure DevOps token "
                "could be acquired. Run 'vertex admin auth setup' in the task principal context."
            )

        if os.environ.get(self.pat_env):
            self.auth_method = "pat"
            return

        raise AuthError(
            "No Azure DevOps credential available. Run 'vertex admin auth setup' or set ADO_PAT."
        )

    def _headers(self) -> dict[str, str]:
        if self._credential is not None:
            token = self._get_bearer_token()
            return {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }

        pat = os.environ.get(self.pat_env)
        if not pat:
            raise AuthError("PAT authentication was selected but ADO_PAT is not set.")
        basic_token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {basic_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get_bearer_token(self) -> str:
        """Use a cached AAD token until shortly before its expiry.

        AzureCliCredential's ``get_token`` launches the Azure CLI.  Calling it
        for every REST request turns bounded work-item hydration into dozens
        of subprocess launches, so cache the standard access token under a
        lock and refresh it one minute before expiry.
        """
        credential_lock = getattr(self, "_credential_lock", None)
        if credential_lock is None:
            credential_lock = threading.Lock()
            self._credential_lock = credential_lock
        with credential_lock:
            token = getattr(self, "_cached_access_token", None)
            expires_on = float(getattr(self, "_cached_access_token_expires_on", 0.0) or 0.0)
            if token and expires_on > time.time() + 60:
                return token
            if self._credential is None:
                raise AuthError("Failed to acquire Azure DevOps token: no credential is configured.")
            try:
                access_token = self._credential.get_token(ADO_RESOURCE)
            except Exception as error:
                raise AuthError("Failed to acquire Azure DevOps token.") from error
            self._cache_access_token(access_token)
            return self._cached_access_token or ""

    def _cache_access_token(self, access_token: Any) -> None:
        self._cached_access_token = str(access_token.token)
        self._cached_access_token_expires_on = float(getattr(access_token, "expires_on", 0.0) or 0.0)

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return self._request_response(method, url, **kwargs).json()

    def _request_response(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """ADF-W2.1: split out from ``_request_json`` so pagination loops that
        need response HEADERS (e.g. comments' ``continuationToken``), not just
        the parsed body, can share the same auth/error handling."""
        response = self._request_with_progress(method, url, **kwargs)
        if response.status_code == 401:
            www_auth = response.headers.get("WWW-Authenticate", "")
            raise CredentialExpired(
                f"ADO returned 401 Unauthorized — PAT or AAD token may have expired. "
                f"Run 'vertex admin auth setup' to refresh credentials. "
                f"({www_auth[:200]})",
                auth_method=self.auth_method or "unknown",
                connector="ADO",
            )
        if response.status_code >= 400:
            raise QueryError(
                f"ADO request failed with status {response.status_code}: {response.text[:500]}"
            )
        return response

    def _request_with_progress(self, method: str, url: str, **kwargs: Any):
        if not self.show_progress:
            return self._session.request(
                method,
                url,
                headers=self._headers(),
                timeout=self.timeout,
                **kwargs,
            )

        response_holder: dict[str, Any] = {}
        error_holder: dict[str, BaseException] = {}
        completed = threading.Event()

        def perform_request() -> None:
            try:
                response_holder["response"] = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    timeout=self.timeout,
                    **kwargs,
                )
            except BaseException as error:  # pragma: no cover - surfaced through the main thread
                error_holder["error"] = error
            finally:
                completed.set()

        worker = threading.Thread(target=perform_request, daemon=True)
        worker.start()

        spinner_frames = "|/-\\"
        spinner_index = 0
        warned = False
        start = time.monotonic()
        while not completed.wait(self.progress_poll_seconds):
            elapsed = time.monotonic() - start
            if elapsed >= self.timeout:
                self._clear_progress_line()
                raise QueryTimeoutError(f"ADO fetch timed out after {self.timeout}s.")
            if elapsed >= self.slow_warning_seconds and not warned:
                warned = True
                self._write_progress("\r⚠ ADO slow (15s elapsed). Still waiting…\n")
                continue
            self._write_progress(f"\rado fetching… {spinner_frames[spinner_index % len(spinner_frames)]}")
            spinner_index += 1

        elapsed = time.monotonic() - start
        if elapsed >= self.slow_warning_seconds and not warned:
            self._write_progress("\r⚠ ADO slow (15s elapsed). Still waiting…\n")
        self._clear_progress_line()
        if "error" in error_holder:
            error = error_holder["error"]
            if isinstance(error, requests.Timeout):
                raise QueryTimeoutError(f"ADO fetch timed out after {self.timeout}s.") from error
            if isinstance(error, requests.RequestException):
                raise QueryError(str(error)) from error
            raise error
        return response_holder["response"]

    def _write_progress(self, message: str) -> None:
        self.progress_stream.write(message)
        self.progress_stream.flush()

    def _clear_progress_line(self) -> None:
        self._write_progress("\r" + (" " * 48) + "\r")
