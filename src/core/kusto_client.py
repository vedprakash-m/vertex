from __future__ import annotations

# Adapted from Artha scripts/kusto_runner.py

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any, Callable

from src.adapters.microsoft.kusto_runtime import load_kusto_sdk_types
from src.core.exceptions import AuthError, QueryError
from src.core.kusto_result_limits import KustoResultLimits, admit_kusto_rows
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
        result_limits: KustoResultLimits | None = None,
    ) -> list[dict[str, Any]]:
        rows, _schema = self.execute_with_schema(
            cluster,
            database,
            kql,
            timeout=timeout,
            max_retries=max_retries,
            no_safety=no_safety,
            result_limits=result_limits,
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
        result_limits: KustoResultLimits | None = None,
    ) -> tuple[list[dict[str, Any]], tuple[KustoColumn, ...]]:
        client = self._get_or_create_client(cluster)
        request_properties = self._build_request_properties(timeout, no_safety=no_safety)
        if result_limits is not None:
            request_properties.set_option("truncationmaxrecords", result_limits.max_rows)
            request_properties.set_option("truncationmaxsize", result_limits.max_bytes)
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                response = client.execute(database, kql, properties=request_properties)
                errors = getattr(response, "errors_count", 0)
                if type(errors) is not int or errors != 0:
                    raise QueryError("Kusto query completion contains errors or invalid completion metadata; partial results refused.")
                return self._rows_from_response(response, result_limits=result_limits), self._schema_from_response(response)
            except QueryError:
                raise
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

    def _rows_from_response(
        self, response: Any, *, result_limits: KustoResultLimits | None = None,
    ) -> list[dict[str, Any]]:
        tables = getattr(response, "primary_results", []) or []
        if result_limits is not None and not tables:
            raise QueryError("Kusto result has no projected table schema.")

        def converted_rows() -> Iterator[dict[str, Any]]:
            for table in tables:
                columns = [getattr(column, "column_name", str(column)) for column in getattr(table, "columns", [])]
                if result_limits is not None and tuple(columns) != result_limits.projected_columns:
                    raise QueryError("Kusto result schema does not match the declared projected columns.")
                for row in table:
                    yield _row_to_dict(columns, row)

        return admit_kusto_rows(converted_rows(), result_limits)

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
    from src.core.metric_binding_validator import kusto_scalar_cardinality_error

    client = KustoClient()

    def execute(query: KustoQuery) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"no_safety": getattr(query, "kusto_no_safety", False)}
        per_query_timeout = getattr(query, "timeout_seconds", None)
        if per_query_timeout is not None:
            kwargs["timeout"] = per_query_timeout
        if query.result_limits is not None:
            kwargs["result_limits"] = query.result_limits
        rows = client.execute(query.cluster, query.database, query.kql, **kwargs)
        error = kusto_scalar_cardinality_error(
            query.id, getattr(query, "expected_cardinality", "zero_ok"), len(rows),
            metric_id=getattr(query, "metric_id", None),
        )
        if error is not None:
            raise QueryError(error)
        return rows

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
