from __future__ import annotations

import csv
import json

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import doctor
from src.commands.doctor_checks import kusto_checks
from src.core.exceptions import AuthError, QueryError
from src.core.failure_taxonomy import _PATTERN_TABLE, classify_exception, classify_message
from src.core.kusto_client import KustoClient
from tests.unit.test_doctor_kusto_probe_result import (
    _BAD,
    _GOOD,
    _query,
    configured_doctor,
    repo_root,
)


_PRIVATE = "SYNTHETIC_KUSTO_PROVIDER_DETAIL"
_REAL_EXECUTE = KustoClient.execute
_PATTERNS = tuple(pattern for _, patterns, _ in _PATTERN_TABLE for pattern in patterns) + ("novel failure",)


@pytest.mark.parametrize("error_type", [AuthError, QueryError])
@pytest.mark.parametrize("pattern", _PATTERNS)
def test_query_probe_caught_errors_preserve_safe_classification(
    error_type: type[Exception], pattern: str,
) -> None:
    query = _query("one", _GOOD, "HealthyDb")
    error = error_type(f"{_PRIVATE} {pattern}")

    def fail(_query):
        raise error

    failures = kusto_checks.probe_kusto_queries((query,), probe=fail)
    assert len(failures) == 1
    assert failures[0].startswith("one: Kusto query probe failed")
    assert _PRIVATE not in failures[0]
    assert classify_message(failures[0]).category == classify_exception(f"one: {error}").category


@pytest.mark.parametrize("query_id,message", [
    ("query-429", "403"),
    ("query-403", "429"),
    ("query-auth", "403"),
    ("query", "unknown error"),
])
def test_query_id_and_provider_text_keep_taxonomy_priority(query_id: str, message: str) -> None:
    query = _query(query_id, _GOOD, "HealthyDb")
    error = QueryError(f"{_PRIVATE} {message}")

    def fail(_query):
        raise error

    failure, = kusto_checks.probe_kusto_queries((query,), probe=fail)
    assert failure.startswith(f"{query_id}: ")
    assert _PRIVATE not in failure
    assert classify_message(failure).category == classify_exception(f"{query_id}: {error}").category


@pytest.mark.parametrize("error_type", [AuthError, QueryError])
def test_query_probe_preserves_failure_limits_and_successes(error_type: type[Exception]) -> None:
    queries = tuple(_query(name, _GOOD, "HealthyDb") for name in ("one", "two", "three", "four"))
    calls: list[str] = []

    def probe(query):
        calls.append(query.id)
        if query.id in {"one", "three", "four"}:
            raise error_type(f"{_PRIVATE} 403")

    failures = kusto_checks.probe_kusto_queries(queries, probe=probe)
    if error_type is AuthError:
        assert calls == ["one"] and len(failures) == 1
    else:
        assert calls == ["one", "two", "three"] and len(failures) == 2
        assert failures[1].startswith("three: ")
    assert all(_PRIVATE not in failure for failure in failures)


def test_query_probe_success_and_unexpected_failure_are_unchanged() -> None:
    queries = (_query("one", _GOOD, "HealthyDb"), _query("two", _BAD, "BadDb"))
    calls: list[str] = []
    assert kusto_checks.probe_kusto_queries(queries, probe=lambda query: calls.append(query.id)) == ()
    assert calls == ["one", "two"]

    def unexpected(_query):
        raise ValueError("synthetic unexpected failure")

    with pytest.raises(ValueError, match="synthetic unexpected failure"):
        kusto_checks.probe_kusto_queries(queries, probe=unexpected)


def test_multiple_query_failures_keep_combined_taxonomy_priority() -> None:
    queries = (_query("one", _GOOD, "HealthyDb"), _query("two", _BAD, "BadDb"))
    errors = {"one": QueryError(f"{_PRIVATE} 403"), "two": QueryError(f"{_PRIVATE} 429")}

    def fail(query):
        raise errors[query.id]

    failures = kusto_checks.probe_kusto_queries(queries, probe=fail)
    original = "; ".join(f"{query.id}: {errors[query.id]}" for query in queries)
    assert len(failures) == 2 and all(_PRIVATE not in failure for failure in failures)
    assert classify_message("; ".join(failures)).category == classify_exception(original).category


def test_auth_failure_after_success_stops_query_probe() -> None:
    queries = tuple(_query(name, _GOOD, "HealthyDb") for name in ("one", "two", "three"))
    calls: list[str] = []

    def probe(query):
        calls.append(query.id)
        if query.id == "two":
            raise AuthError(f"{_PRIVATE} 401")

    failures = kusto_checks.probe_kusto_queries(queries, probe=probe)
    assert calls == ["one", "two"]
    assert failures == ("two: Kusto query probe failed; matched pattern: '401'",)


def _fail_at_provider(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    class FailingProvider:
        def execute(self, _database, kql, **_kwargs):
            calls.append(kql)
            raise RuntimeError(f"{_PRIVATE} 403")

    monkeypatch.setattr(KustoClient, "execute", _REAL_EXECUTE)
    monkeypatch.setattr(KustoClient, "_get_or_create_client", lambda *_args: FailingProvider())
    monkeypatch.setattr(KustoClient, "_build_request_properties", lambda *_args, **_kwargs: object())


def test_actual_client_query_probe_omits_provider_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _fail_at_provider(monkeypatch, calls)
    query = _query("one", _GOOD, "HealthyDb")
    failure, = kusto_checks.probe_kusto_queries((query,), probe=kusto_checks.build_live_kusto_probe())
    assert failure == "one: Kusto query probe failed; matched pattern: '403'"
    assert _PRIVATE not in failure and _GOOD not in failure and "HealthyDb" not in failure
    assert len(calls) == 1 and calls[0].endswith("| take 0")


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_actual_kusto_doctor_cli_omits_provider_text(
    configured_doctor, isolated_cli_startup, monkeypatch: pytest.MonkeyPatch, output_format: str,
) -> None:
    monkeypatch.setattr(
        kusto_checks, "load_doctor_kusto_queries",
        lambda *_args, **_kwargs: (_query("one", _GOOD, "HealthyDb"),),
    )
    monkeypatch.setattr(doctor, "_live_kusto_probe", kusto_checks.build_live_kusto_probe)
    calls: list[str] = []
    _fail_at_provider(monkeypatch, calls)
    result = CliRunner().invoke(
        app, ["doctor", "--edition", "acme_weekly", "--kusto", "--format", output_format],
    )
    assert result.exit_code == 1, result.output
    assert _PRIVATE not in result.output
    assert "one: Kusto query probe failed; matched pattern: '403'" in result.output
    assert len(calls) == 1 and calls[0].endswith("| take 0")
    expected_metadata = {
        "query_count": 1,
        "query_ids": ["one"],
        "cluster_targets": [f"{_GOOD.rstrip('/')}/HealthyDb"],
    }
    if output_format == "json":
        payload = json.loads(result.output)
        check = next(row for row in payload["checks"] if row["label"] == "Kusto Probe")
        assert check["status"] == "fail"
        assert check["metadata"] == expected_metadata
    elif output_format == "csv":
        check = next(row for row in csv.DictReader(result.output.splitlines()) if row["label"] == "Kusto Probe")
        assert check["status"] == "fail"
        assert json.loads(check["metadata_json"]) == expected_metadata
    else:
        assert "FAIL Kusto Probe" in result.output
        assert expected_metadata["cluster_targets"][0] in result.output
