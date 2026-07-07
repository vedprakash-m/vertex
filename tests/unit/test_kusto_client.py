from __future__ import annotations

from datetime import timedelta
import logging

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


def test_kusto_client_execute_returns_row_dicts_and_caches_client(monkeypatch) -> None:
    created_clients: list[tuple[str, object]] = []

    class FakeAzureKustoClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            created_clients.append(connection)

        def execute(self, database: str, kql: str, properties=None):
            assert database == "xdataanalytics"
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

    first_rows = client.execute("https://adventure.kusto.windows.net", "xdataanalytics", "Metrics | take 1")
    second_rows = client.execute("https://adventure.kusto.windows.net", "xdataanalytics", "Metrics | take 1")

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

    rows = client.execute("https://adventure.kusto.windows.net", "xdataanalytics", "Metrics | take 1", no_safety=True)

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
        client.execute("https://adventure.kusto.windows.net", "xdataanalytics", "Metrics | take 1")
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
                database="xdataanalytics",
                kql="Metrics | take 1",
                section="Deployment Velocity",
                render_as="metric_highlight",
                confidence="high",
            ),
            KustoQuery(
                id="fleet-health",
                cluster="https://adventure.kusto.windows.net",
                database="xdataanalytics",
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
        ("https://adventure.kusto.windows.net", "xdataanalytics", ".show version"),
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
                database="xdataanalytics",
                kql="Metrics | take 1",
                section="Deployment Velocity",
                render_as="metric_highlight",
                confidence="high",
            ),
        )
    )

    assert ("https://adventure.kusto.windows.net", "xdataanalytics") in failed
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
        database="xdataanalytics",
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
            cluster="https://apdmdata.kusto.windows.net",
            database="DeviceManager",
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
            "database": "DeviceManager",
            "kql": "NOVAOSCompliance | take 1",
            "options": {"servertimeout": timedelta(minutes=2)},
        }
    ]
