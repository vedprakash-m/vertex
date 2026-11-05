from __future__ import annotations

from datetime import timedelta
import json
import logging

import pytest

from src.core.exceptions import AuthError
from src.core.kusto_client import KustoClient, build_live_kusto_query_probe
from src.core.models_v2 import KustoQuery


class _FakeColumn:
    def __init__(self, column_name: str) -> None:
        self.column_name = column_name


class _FakeTable:
    def __init__(self, columns: list[str], rows: list[object]) -> None:
        self.columns = [_FakeColumn(column) for column in columns]
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeResponse:
    def __init__(self, tables: list[_FakeTable]) -> None:
        self.primary_results = tables


class _FakeKcsb:
    @staticmethod
    def with_azure_token_credential(cluster: str, credential: object) -> tuple[str, object]:
        return (cluster, credential)


class _FakeRequestProperties:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def set_option(self, key: str, value: object) -> None:
        self.options[key] = value


class _FakeCredential:
    pass


def _limited_client(monkeypatch, response, calls: list[object]) -> KustoClient:
    class Client:
        def execute(self, database, kql, properties=None):
            calls.append(properties.options)
            return response

    client = KustoClient(sleep_func=lambda _: pytest.fail("Contract failures cannot retry"))
    monkeypatch.setattr(client, "_get_or_create_client", lambda _: Client())
    monkeypatch.setattr(
        client, "_get_sdk_types",
        lambda: (object, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )
    return client


@pytest.mark.parametrize("errors", [1, 2, -1, True, "0", None])
def test_sdk_error_metadata_cannot_return_partial_success(monkeypatch, errors) -> None:
    from src.core.exceptions import QueryError

    response = _FakeResponse([_FakeTable(["Value"], [(12,)])])
    response.errors_count = errors
    calls: list[object] = []
    client = _limited_client(monkeypatch, response, calls)

    with pytest.raises(QueryError, match="completion"):
        client.execute("https://cluster.example", "Synthetic", "Metrics")
    assert len(calls) == 1


@pytest.mark.parametrize("level", [2, 4])
def test_actual_sdk_completion_information_is_checked(monkeypatch, level: int) -> None:
    from azure.kusto.data.response import KustoResponseDataSetV2

    from src.core.exceptions import QueryError

    response = KustoResponseDataSetV2([
        {
            "FrameType": "DataTable", "TableId": 0, "TableKind": "PrimaryResult", "TableName": "PrimaryResult",
            "Columns": [{"ColumnName": "Value", "ColumnType": "int"}], "Rows": [[12]],
        },
        {
            "FrameType": "DataTable", "TableId": 1, "TableKind": "QueryCompletionInformation",
            "TableName": "QueryCompletionInformation",
            "Columns": [
                {"ColumnName": "Level", "ColumnType": "int"},
                {"ColumnName": "Payload", "ColumnType": "string"},
                {"ColumnName": "ClientRequestId", "ColumnType": "string"},
            ],
            "Rows": [[level, "Synthetic completion", "synthetic"]],
        },
    ])
    calls: list[object] = []
    client = _limited_client(monkeypatch, response, calls)
    if level == 2:
        with pytest.raises(QueryError, match="completion"):
            client.execute("synthetic", "synthetic", "synthetic")
    else:
        assert client.execute("synthetic", "synthetic", "synthetic") == [{"Value": 12}]
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["rows", "bytes", "schema", "second_table", "empty_schema"])
@pytest.mark.parametrize("no_safety", [False, True])
def test_declared_result_limits_refuse_incomplete_results(monkeypatch, case: str, no_safety: bool) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    tables = [_FakeTable(["Value"], [(12,)])]
    if case == "rows":
        tables = [_FakeTable(["Value"], [(12,), (13,)])]
    elif case == "schema":
        tables = [_FakeTable(["Other"], [(12,)])]
    elif case == "second_table":
        tables.append(_FakeTable(["Other"], [(12,)]))
    elif case == "empty_schema":
        tables = [_FakeTable(["Other"], [])]
    limits = KustoResultLimits(
        projected_columns=("Value",), max_rows=1,
        max_bytes=2 if case == "bytes" else 100,
    )
    calls: list[object] = []
    client = _limited_client(monkeypatch, _FakeResponse(tables), calls)

    with pytest.raises(QueryError, match="result"):
        client.execute("https://cluster.example", "Synthetic", "Metrics", result_limits=limits, no_safety=no_safety)
    assert len(calls) == 1
    assert calls[0]["truncationmaxrecords"] == 1
    assert calls[0]["truncationmaxsize"] == limits.max_bytes


@pytest.mark.parametrize("extra_byte", [0, -1])
def test_decoded_utf8_byte_budget_is_exact(monkeypatch, extra_byte: int) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    rows = [{"Value": "é"}, {"Value": "✓"}]
    size = len(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    limits = KustoResultLimits(projected_columns=("Value",), max_rows=2, max_bytes=size + extra_byte)
    calls: list[object] = []
    client = _limited_client(monkeypatch, _FakeResponse([_FakeTable(["Value"], rows)]), calls)
    if extra_byte:
        with pytest.raises(QueryError, match="byte"):
            client.execute("https://cluster.example", "Synthetic", "Metrics", result_limits=limits)
    else:
        assert client.execute("https://cluster.example", "Synthetic", "Metrics", result_limits=limits) == rows
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [
    {}, {"projected_columns": [], "max_rows": 1, "max_bytes": 100},
    {"projected_columns": ["Value", "Value"], "max_rows": 1, "max_bytes": 100},
    {"projected_columns": ["Value"], "max_rows": True, "max_bytes": 100},
    {"projected_columns": ["Value"], "max_rows": 0, "max_bytes": 100},
    {"projected_columns": ["Value"], "max_rows": 1, "max_bytes": 1},
    {"projected_columns": ["Value"], "max_rows": "1", "max_bytes": 100},
    {"projected_columns": ["Value"], "max_rows": 1, "max_bytes": 100, "typo": 1},
])
def test_malformed_declared_result_limits_fail_explicitly(bad) -> None:
    from src.core.exceptions import ConfigError
    from src.core.kusto_result_limits import parse_kusto_result_limits

    with pytest.raises(ConfigError, match="result_limits"):
        parse_kusto_result_limits(bad)


@pytest.mark.parametrize("bad", [
    True, [], {"projected_columns": "Value", "max_rows": 1, "max_bytes": 100},
    {"projected_columns": [None], "max_rows": 1, "max_bytes": 100},
    {"projected_columns": [" "], "max_rows": 1, "max_bytes": 100},
    {"projected_columns": ["Value"], "max_rows": 1, "max_bytes": True},
])
def test_result_limit_shape_is_strict(bad) -> None:
    from src.core.exceptions import ConfigError
    from src.core.kusto_result_limits import parse_kusto_result_limits

    with pytest.raises(ConfigError, match="result_limits"):
        parse_kusto_result_limits(bad)


def test_result_limit_value_object_rejects_mutable_projection_and_accepts_no_declaration() -> None:
    from src.core.exceptions import ConfigError
    from src.core.kusto_result_limits import KustoResultLimits, parse_kusto_result_limits

    assert parse_kusto_result_limits(None) is None
    with pytest.raises(ConfigError, match="projected_columns"):
        KustoResultLimits(["Value"], 1, 100)


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_nonfinite_decimal_is_not_stringified(monkeypatch, value: str) -> None:
    from decimal import Decimal

    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    client = _limited_client(monkeypatch, _FakeResponse([_FakeTable(["Value"], [(Decimal(value),)])]), [])
    with pytest.raises(QueryError, match="byte"):
        client.execute("synthetic", "synthetic", "synthetic", result_limits=KustoResultLimits(("Value",), 1, 100))


@pytest.mark.parametrize("columns", [("First", "Second"), ("Second", "First")])
def test_projection_order_and_combined_table_budget_are_enforced(monkeypatch, columns) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    tables = [_FakeTable(list(columns), [(1, 2)]), _FakeTable(list(columns), [(3, 4)])]
    client = _limited_client(monkeypatch, _FakeResponse(tables), [])
    limits = KustoResultLimits(("First", "Second"), 2, 100)
    if columns == limits.projected_columns:
        assert client.execute("synthetic", "synthetic", "synthetic", result_limits=limits) == [
            {"First": 1, "Second": 2}, {"First": 3, "Second": 4},
        ]
    else:
        with pytest.raises(QueryError, match="schema"):
            client.execute("synthetic", "synthetic", "synthetic", result_limits=limits)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), b"bytes", "\ud800"])
def test_unmeasurable_rows_cannot_bypass_byte_budget(monkeypatch, value) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    calls: list[object] = []
    client = _limited_client(monkeypatch, _FakeResponse([_FakeTable(["Value"], [(value,)])]), calls)
    with pytest.raises(QueryError, match="byte"):
        client.execute("synthetic", "synthetic", "synthetic", result_limits=KustoResultLimits(("Value",), 1, 1000))
    assert len(calls) == 1


def test_sdk_typed_values_are_measured_without_changing_returned_rows(monkeypatch) -> None:
    from datetime import date, datetime, timezone
    from decimal import Decimal
    from uuid import UUID

    from src.core.kusto_result_limits import KustoResultLimits

    values = [
        date(2026, 11, 5), datetime(2026, 11, 5, tzinfo=timezone.utc),
        timedelta(seconds=3), Decimal("1.25"), UUID(int=1), None,
    ]
    rows = [{"Value": value} for value in values]
    size = len(json.dumps(rows, default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    # datetime's ISO separator is T, matching the length of default=str's space.
    calls: list[object] = []
    client = _limited_client(monkeypatch, _FakeResponse([_FakeTable(["Value"], rows)]), calls)
    response = client.execute("synthetic", "synthetic", "synthetic", result_limits=KustoResultLimits(("Value",), 6, size))
    assert response == rows
    assert response[1]["Value"] is values[1]


@pytest.mark.parametrize("tables", [[], [_FakeTable(["Value"], [])]])
def test_empty_result_requires_declared_schema(monkeypatch, tables) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    client = _limited_client(monkeypatch, _FakeResponse(tables), [])
    limits = KustoResultLimits(("Value",), 1, 2)
    if tables:
        rows, schema = client.execute_with_schema("synthetic", "synthetic", "synthetic", result_limits=limits)
        assert rows == []
        assert [column.name for column in schema] == ["Value"]
    else:
        with pytest.raises(QueryError, match="schema"):
            client.execute("synthetic", "synthetic", "synthetic", result_limits=limits)


def test_legacy_empty_response_is_not_certified_as_a_declared_projection(monkeypatch) -> None:
    client = _limited_client(monkeypatch, _FakeResponse([]), [])
    assert client.execute_with_schema("synthetic", "synthetic", "synthetic") == ([], ())


@pytest.mark.parametrize("row_count", [0, 1, 2])
def test_common_executor_enforces_existing_scalar_cardinality(monkeypatch, row_count: int) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_client import build_live_kusto_query_executor

    calls: list[object] = []
    client = _limited_client(monkeypatch, _FakeResponse([_FakeTable(["Value"], [(1,)] * row_count)]), calls)
    monkeypatch.setattr("src.core.kusto_client.KustoClient", lambda: client)
    query = KustoQuery(
        id="scalar", cluster="synthetic", database="synthetic", kql="synthetic", section="Synthetic",
        render_as="table", confidence="high", expected_cardinality="scalar_required", timeout_seconds=45,
    )
    execute = build_live_kusto_query_executor()
    if row_count == 1:
        assert execute(query) == [{"Value": 1}]
    else:
        with pytest.raises(QueryError, match="scalar_required"):
            execute(query)
    assert calls[0]["servertimeout"] == timedelta(seconds=45)


@pytest.mark.parametrize("consumer", ["hydration", "report"])
@pytest.mark.parametrize("failure,injected", [
    ("partial", False),
    *[(failure, injected) for failure in ("rows", "bytes", "schema", "scalar") for injected in (False, True)],
])
def test_common_result_refusal_reaches_actual_consumers(
    monkeypatch, tmp_path, consumer: str, failure: str, injected: bool,
) -> None:
    from datetime import datetime, timezone

    from src.core.config_loader import KustoQuerySettings, KustoSettings
    from src.core.integration_types import ChannelRegistration, RegistrationStatus
    from src.core.kusto_client import build_live_kusto_query_executor
    from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
    from src.core.kusto_rendering import build_kusto_sections
    from src.core.kusto_result_limits import KustoResultLimits

    response = _FakeResponse([_FakeTable(
        ["Other"] if failure == "schema" else ["Value"], [(1,), (2,)] if failure in {"rows", "scalar"} else [(1,)],
    )])
    response.errors_count = 1 if failure == "partial" else 0
    calls: list[object] = []
    client = _limited_client(monkeypatch, response, calls)
    monkeypatch.setattr("src.core.kusto_client.KustoClient", lambda: client)
    execute = build_live_kusto_query_executor()
    if injected:
        execute = lambda _query: [
            {"Other" if failure == "schema" else "Value": value}
            for value in ([1, 2] if failure in {"rows", "scalar"} else [1])
        ]
    limits = KustoResultLimits(("Value",), 2 if failure == "scalar" else 1, 2 if failure == "bytes" else 100)
    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic", section="Synthetic",
        render_as="table", confidence="high", validated=True, result_limits=limits,
        expected_cardinality="scalar_required" if failure == "scalar" else "zero_ok",
    )
    if consumer == "hydration":
        now = datetime.now(timezone.utc)
        registration = ChannelRegistration(
            channel="kusto", program_id="synthetic", provider_instance_id="default", ref_id=query.id,
            ref_kind="kusto_query", status=RegistrationStatus.ACTIVE, first_discovered_at=now, last_seen_at=now,
        )
        provider = KustoHydrationProvider(executor=execute, query_loader=lambda *_args, **_kwargs: (query,))
        result = provider.hydrate((registration,), now, "synthetic", KustoHydrationConfig(programs_root=tmp_path))
        assert result.resources.result_sets == ()
        assert result.hydrated_ref_ids == ()
        assert result.failed_ref_ids == ((query.id, "kusto_query"),)
        assert result.errors[0].message
    else:
        settings = KustoSettings(enabled=True, queries=(KustoQuerySettings(
            id=query.id, cluster=query.cluster, database=query.database, kql=query.kql, section=query.section,
            render_as=query.render_as, confidence=query.confidence, kusto_section_validates_slice=False,
            caveats=(), reference_url="https://example.test/telemetry", result_limits=limits,
            expected_cardinality=query.expected_cardinality,
        ),))
        sections, observations, warnings = build_kusto_sections(settings, execute)
        assert sections[0].is_degraded
        assert not sections[0].rows
        assert observations[0].execution_state == "degraded"
        assert warnings
    assert len(calls) == (0 if injected else 1)


def test_streaming_row_admission_stops_before_returning_a_truncated_subset() -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits, admit_kusto_rows

    visited: list[int] = []

    def rows():
        for value in range(10):
            visited.append(value)
            yield {"Value": value}

    with pytest.raises(QueryError, match="row budget"):
        admit_kusto_rows(rows(), KustoResultLimits(("Value",), 1, 100))
    assert visited == [0, 1]


@pytest.mark.parametrize("row", [None, 42, ["Value"], {"Other": 1}])
def test_declared_admission_rejects_malformed_row_shape(row) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits, admit_kusto_rows

    with pytest.raises(QueryError, match="schema"):
        admit_kusto_rows([row], KustoResultLimits(("Value",), 1, 100))


def test_shared_row_admission_preserves_original_objects_and_metric_error_priority() -> None:
    from src.core.kusto_result_limits import KustoResultLimits, admit_kusto_rows
    from src.core.metric_binding_validator import kusto_metric_result_error

    row = {"Value": 0}
    assert admit_kusto_rows([row], KustoResultLimits(("Value",), 1, 100))[0] is row
    assert admit_kusto_rows([row], None)[0] is row
    limits = KustoResultLimits(("Other",), 1, 2)
    assert "finite numeric" in kusto_metric_result_error(
        "synthetic", "zero_ok", [{"Value": None}], metric_id="synthetic.value",
        result_column="Value", result_limits=limits,
    )
    assert "scalar_required" in kusto_metric_result_error(
        "synthetic", "scalar_required", [], result_limits=limits,
    )
    assert kusto_metric_result_error("synthetic", "zero_ok", [], result_limits=limits) is None


def test_kusto_client_execute_returns_row_dicts_and_caches_client(monkeypatch) -> None:
    created_clients: list[tuple[str, object]] = []

    class FakeAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            created_clients.append(connection)

        def execute(self, database: str, kql: str, properties=None):
            assert database == "ExampleDb4"
            assert "take 1" in kql
            assert properties.options["servertimeout"] == timedelta(minutes=2)
            assert properties.options["max_memory_consumption_per_query_per_node"] == 8_000_000_000
            assert properties.options["request_timeout"] == timedelta(minutes=5)
            return _FakeResponse([_FakeTable(["Metric", "Value"], [("P50", 12.5)])])

    client = KustoClient()
    monkeypatch.setattr(
        client,
        "_get_sdk_types",
        lambda: (FakeAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    first_rows = client.execute("https://adventure.kusto.windows.net", "ExampleDb4", "Metrics | take 1")
    second_rows = client.execute("https://adventure.kusto.windows.net", "ExampleDb4", "Metrics | take 1")

    assert first_rows == [{"Metric": "P50", "Value": 12.5}]
    assert second_rows == first_rows
    assert len(created_clients) == 1


def test_kusto_client_execute_skips_safety_options_when_opted_out(monkeypatch) -> None:
    captured_options: dict[str, object] = {}

    class FakeAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            self.connection = connection

        def execute(self, database: str, kql: str, properties=None):
            captured_options.update(properties.options)
            return _FakeResponse([_FakeTable(["Metric"], [(1,)])])

    client = KustoClient()
    monkeypatch.setattr(
        client,
        "_get_sdk_types",
        lambda: (FakeAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    rows = client.execute("https://adventure.kusto.windows.net", "ExampleDb4", "Metrics | take 1", no_safety=True)

    assert rows == [{"Metric": 1}]
    assert captured_options == {"servertimeout": timedelta(minutes=2)}


def test_kusto_client_retries_on_429(monkeypatch) -> None:
    sleep_calls: list[float] = []

    class FlakyAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            self.calls = 0

        def execute(self, database: str, kql: str, properties=None):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("429 throttled")
            return _FakeResponse([_FakeTable(["IncidentId", "Severity"], [(12345, 2)])])

    client = KustoClient(sleep_func=lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr(
        client,
        "_get_sdk_types",
        lambda: (FlakyAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    rows = client.execute("https://icmcluster.kusto.windows.net", "IcMDataWarehouse", "Incidents | take 1")

    assert rows == [{"IncidentId": 12345, "Severity": 2}]
    assert sleep_calls == [2.0, 4.0]


def test_kusto_client_surfaces_auth_failures_with_az_login_hint(monkeypatch) -> None:
    class AuthFailingAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            pass

        def execute(self, database: str, kql: str, properties=None):
            raise RuntimeError("DefaultAzureCredential failed to retrieve a token")

    client = KustoClient()
    monkeypatch.setattr(
        client,
        "_get_sdk_types",
        lambda: (AuthFailingAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    try:
        client.execute("https://adventure.kusto.windows.net", "ExampleDb4", "Metrics | take 1")
    except AuthError as error:
        assert "vertex admin auth setup" in str(error)
    else:  # pragma: no cover
        raise AssertionError("Expected AuthError for credential failures")


def test_build_live_kusto_query_probe_checks_each_cluster_once(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class FakeAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            self.connection = connection

        def execute(self, database: str, kql: str, properties=None):
            calls.append((self.connection[0], database, kql))
            return _FakeResponse([_FakeTable(["BuildVersion"], [("1.0",)])])

    monkeypatch.setattr(
        "src.core.kusto_client.KustoClient._get_sdk_types",
        lambda self: (FakeAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    probe = build_live_kusto_query_probe()
    failed = probe(
        (
            KustoQuery(
                id="velocity-p50",
                cluster="https://adventure.kusto.windows.net",
                database="ExampleDb4",
                kql="Metrics | take 1",
                section="Deployment Velocity",
                render_as="metric_highlight",
                confidence="high",
            ),
            KustoQuery(
                id="fleet-health",
                cluster="https://adventure.kusto.windows.net",
                database="ExampleDb4",
                kql="FleetHealth | take 1",
                section="Fleet Health",
                render_as="table",
                confidence="medium",
            ),
            KustoQuery(
                id="icm-active",
                cluster="https://icmcluster.kusto.windows.net",
                database="IcMDataWarehouse",
                kql="Incidents | take 1",
                section="Active Incidents",
                render_as="table",
                confidence="high",
            ),
        )
    )

    assert failed == frozenset()
    assert calls == [
        ("https://adventure.kusto.windows.net", "ExampleDb4", ".show version"),
        ("https://icmcluster.kusto.windows.net", "IcMDataWarehouse", ".show version"),
    ]


def test_build_live_kusto_query_probe_returns_failed_target_on_auth_error(monkeypatch) -> None:
    class AuthFailingAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            pass

        def execute(self, database: str, kql: str, properties=None):
            raise RuntimeError("DefaultAzureCredential failed to retrieve a token")

    monkeypatch.setattr(
        "src.core.kusto_client.KustoClient._get_sdk_types",
        lambda self: (AuthFailingAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    probe = build_live_kusto_query_probe()
    failed = probe(
        (
            KustoQuery(
                id="velocity-p50",
                cluster="https://adventure.kusto.windows.net",
                database="ExampleDb4",
                kql="Metrics | take 1",
                section="Deployment Velocity",
                render_as="metric_highlight",
                confidence="high",
            ),
        )
    )

    assert ("https://adventure.kusto.windows.net", "ExampleDb4") in failed
    assert len(failed) == 1


def test_build_live_kusto_query_probe_can_suppress_failure_logs(monkeypatch, caplog) -> None:
    class AuthFailingAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            pass

        def execute(self, database: str, kql: str, properties=None):
            raise RuntimeError("DefaultAzureCredential failed to retrieve a token")

    monkeypatch.setattr(
        "src.core.kusto_client.KustoClient._get_sdk_types",
        lambda self: (AuthFailingAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    query = KustoQuery(
        id="velocity-p50",
        cluster="https://adventure.kusto.windows.net",
        database="ExampleDb4",
        kql="Metrics | take 1",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="high",
    )

    with caplog.at_level(logging.WARNING, logger="src.core.kusto_client"):
        build_live_kusto_query_probe(log_failures=False)((query,))

    assert "Kusto pre-flight failed" not in caplog.text


def test_build_live_kusto_query_executor_honors_query_safety_opt_out(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            self.connection = connection

        def execute(self, database: str, kql: str, properties=None):
            calls.append({"database": database, "kql": kql, "options": dict(properties.options)})
            return _FakeResponse([_FakeTable(["Metric"], [(1,)])])

    monkeypatch.setattr(
        "src.core.kusto_client.KustoClient._get_sdk_types",
        lambda self: (FakeAzureKustoClient, _FakeKcsb, _FakeRequestProperties, _FakeCredential),
    )

    executor = __import__("src.core.kusto_client", fromlist=["build_live_kusto_query_executor"]).build_live_kusto_query_executor()
    rows = executor(
        KustoQuery(
            id="acme-os-compliance",
            cluster="https://example1.kusto.windows.net",
            database="ExampleDb1",
            kql="NOVAOSCompliance | take 1",
            section="OS Compliance",
            render_as="metric_highlight",
            confidence="low",
            kusto_no_safety=True,
        )
    )

    assert rows == [{"Metric": 1}]
    assert calls == [
        {
            "database": "ExampleDb1",
            "kql": "NOVAOSCompliance | take 1",
            "options": {"servertimeout": timedelta(minutes=2)},
        }
    ]
