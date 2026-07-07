from __future__ import annotations

# Adapted from Artha scripts/kusto_runner.py

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any, Callable

from src.adapters.microsoft.kusto_runtime import load_kusto_sdk_types
from src.core.exceptions import AuthError, QueryError
from src.core.models_v2 import KustoQuery

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KustoColumn:
    name: str
    type_name: str | None = None


class KustoClient:
    """Executes KQL queries against Azure Data Explorer clusters with cached clients."""

    TIMEOUT_SECONDS = 120
    MAX_RETRIES = 3

    def __init__(
        self,
        *,
        credential: object | None = None,
        sleep_func: Any = time.sleep,
    ) -> None:
        self._clients: dict[str, Any] = {}
        self._credential = credential
        self._sleep = sleep_func

    def execute(
        self,
        cluster: str,
        database: str,
        kql: str,
        timeout: int = TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        no_safety: bool = False,
    ) -> list[dict[str, Any]]:
        rows, _schema = self.execute_with_schema(
            cluster,
            database,
            kql,
            timeout=timeout,
            max_retries=max_retries,
            no_safety=no_safety,
        )
        return rows

    def execute_with_schema(
        self,
        cluster: str,
        database: str,
        kql: str,
        timeout: int = TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        no_safety: bool = False,
    ) -> tuple[list[dict[str, Any]], tuple[KustoColumn, ...]]:
        client = self._get_or_create_client(cluster)
        request_properties = self._build_request_properties(timeout, no_safety=no_safety)
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                response = client.execute(database, kql, properties=request_properties)
                return self._rows_from_response(response), self._schema_from_response(response)
            except Exception as error:  # pragma: no cover - exercised through tests with fakes
                last_error = error
                if self._is_auth_failure(error):
                    raise AuthError(
                        "Kusto authentication failed. Run `vertex admin auth setup` or configure DefaultAzureCredential."
                    ) from error
                if self._is_throttled(error) and attempt < max_retries - 1:
                    self._sleep(float(2 ** (attempt + 1)))
                    continue
                raise QueryError(
                    f"Kusto query failed for {cluster}/{database}: {error}"
                ) from error

        if last_error is not None:
            raise QueryError(f"Kusto query failed for {cluster}/{database}: {last_error}") from last_error
        return [], ()

    def _get_or_create_client(self, cluster: str) -> Any:
        if cluster not in self._clients:
            client_class, kcsb_class, _request_properties_class, credential_class = self._get_sdk_types()
            credential = self._credential
            if credential is None:
                credential = credential_class()
                self._credential = credential
            connection = kcsb_class.with_azure_token_credential(cluster, credential)
            self._clients[cluster] = client_class(connection)
        return self._clients[cluster]

    def _build_request_properties(self, timeout: int, *, no_safety: bool = False) -> Any:
        _client_class, _kcsb_class, request_properties_class, _credential_class = self._get_sdk_types()
        properties = request_properties_class()
        properties.set_option("servertimeout", timedelta(seconds=timeout))
        if not no_safety:
            properties.set_option("max_memory_consumption_per_query_per_node", 8_000_000_000)
            properties.set_option("request_timeout", timedelta(minutes=5))
        return properties

    def _get_sdk_types(self) -> tuple[Any, Any, Any, Any]:
        return load_kusto_sdk_types()

    def _rows_from_response(self, response: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for table in getattr(response, "primary_results", []) or []:
            columns = [getattr(column, "column_name", str(column)) for column in getattr(table, "columns", [])]
            for row in table:
                rows.append(_row_to_dict(columns, row))
        return rows

    def _schema_from_response(self, response: Any) -> tuple[KustoColumn, ...]:
        for table in getattr(response, "primary_results", []) or []:
            return tuple(
                KustoColumn(
                    name=str(getattr(column, "column_name", str(column))),
                    type_name=getattr(column, "column_type", None),
                )
                for column in getattr(table, "columns", [])
            )
        return ()

    def _is_throttled(self, error: Exception) -> bool:
        normalized = str(error).lower()
        return "429" in normalized or "throttl" in normalized or "too many requests" in normalized

    def _is_auth_failure(self, error: Exception) -> bool:
        normalized = str(error).lower()
        return "defaultazurecredential" in normalized or "aadsts" in normalized or "az login" in normalized


def build_live_kusto_query_executor() -> Callable[[KustoQuery], list[dict[str, Any]]]:
    client = KustoClient()

    def execute(query: KustoQuery) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"no_safety": getattr(query, "kusto_no_safety", False)}
        per_query_timeout = getattr(query, "timeout_seconds", None)
        if per_query_timeout is not None:
            kwargs["timeout"] = per_query_timeout
        return client.execute(query.cluster, query.database, query.kql, **kwargs)

    return execute


def build_live_kusto_query_probe(
    *,
    log_failures: bool = True,
) -> Callable[[Iterable[KustoQuery]], frozenset[tuple[str, str]]]:
    """Returns a probe function that pre-flights each distinct (cluster, db) target.

    The returned function returns a frozenset of (cluster, db) pairs whose pre-flight
    failed.  Callers should skip queries that target a failing pair and record the
    errors as integration failures.  An empty frozenset means all clusters passed.
    """
    client = KustoClient()

    def probe(queries: Iterable[KustoQuery]) -> frozenset[tuple[str, str]]:
        seen_targets: set[tuple[str, str]] = set()
        failed_targets: set[tuple[str, str]] = set()
        for query in queries:
            target = (query.cluster.strip(), query.database.strip())
            if target in seen_targets:
                continue
            seen_targets.add(target)
            try:
                client.execute(target[0], target[1], ".show version", timeout=30, max_retries=1)
            except (AuthError, QueryError) as error:
                failed_targets.add(target)
                if log_failures:
                    log.warning(
                        "Kusto pre-flight failed for %s/%s — queries on this cluster will be skipped. Error: %s",
                        target[0],
                        target[1],
                        error,
                    )
        return frozenset(failed_targets)

    return probe

def _row_to_dict(columns: list[str], row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return {column: row.get(column) for column in columns} if columns else dict(row)
    converted: dict[str, Any] = {}
    for index, column in enumerate(columns):
        try:
            converted[column] = row[column]
            continue
        except Exception:
            pass
        try:
            converted[column] = row[index]
            continue
        except Exception:
            pass
        converted[column] = getattr(row, column, None)
    return converted
