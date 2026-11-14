from __future__ import annotations

import json
import logging
import sqlite3
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast
from unittest.mock import Mock

import pytest

from src.ai.ai_mode import AIMode
from src.ai.client import AIClient, AIClientError, AIPreflightError, BudgetConfigurationError, BudgetExceeded, BudgetStateError
from src.ai import context_tokenizers
from src.ai.context_tokenizers import TiktokenTokenEstimator
from src.ai.cost_guard import CostGuard
from src.ai.deployment_fallback import FallbackAIClient, FallbackStructuredClient, resolve_ai_deployments, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.provider import LLMProvider
from src.ai.request_router import AIRequestRouter
from src.core.ai_telemetry import (
    AiTelemetryRecord,
    AiTelemetryStatus,
    append_ai_telemetry,
    build_feature_cost_summary,
    read_ai_telemetry,
)
from src.core.exceptions import StateError
from src.core.policy_loader import AIFeaturePolicy, AIRequestRouterPolicy


@pytest.fixture(autouse=True)
def isolate_ai_telemetry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def append_to_temporary_root(record: AiTelemetryRecord) -> None:
        append_ai_telemetry(record, programs_root=tmp_path / "telemetry")

    monkeypatch.setattr("src.core.ai_telemetry.PROGRAMS_ROOT", tmp_path / "live-programs")
    monkeypatch.setattr("src.core.ai_telemetry.append_ai_telemetry", append_to_temporary_root)


def _unique_run_id() -> str:
    # A CostGuard's SQLite ledger persists under PROGRAMS_ROOT keyed by
    # (edition, run_id); a fixed literal run_id would accumulate spend
    # across repeated test runs unless PROGRAMS_ROOT is also isolated to a
    # tmp_path (every test below that needs a real trace_context does both).
    return f"run-{uuid.uuid4().hex[:8]}"


def test_fallback_ai_client_surfaces_vertex_first_missing_deployment_guidance() -> None:
    client = FallbackAIClient(
        deployments=(),
        temperature=0.2,
        budget_usd=0.5,
    )

    with pytest.raises(
        AIClientError,
        match="VERTEX_AI_DEPLOYMENT, VERTEX_EXEC_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT",
    ):
        client.chat("system", "user")


def test_fallback_structured_client_surfaces_vertex_first_missing_deployment_guidance() -> None:
    client = FallbackStructuredClient(
        deployments=(),
        temperature=0.2,
        budget_usd=0.5,
    )

    with pytest.raises(
        AIClientError,
        match="supported Vertex deployment aliases",
    ):
        client.structured("system", "user", parser=lambda payload: payload)

class _FakeRateLimitError(Exception):
    pass


class _FakeAPIStatusError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class _FakeResponse:
    def __init__(self, content: str, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


class _FakeAzureOpenAI:
    actions: list[object] = []
    created_kwargs: dict[str, object] | None = None
    create_calls: int = 0
    last_request_kwargs: dict[str, object] | None = None

    def __init__(self, **kwargs) -> None:
        type(self).created_kwargs = kwargs
        type(self).create_calls = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        type(self).last_request_kwargs = kwargs
        action = type(self).actions[type(self).create_calls]
        type(self).create_calls += 1
        if isinstance(action, Exception):
            raise action
        return action


def _telemetry_budget_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, feature_budget: float, run_budget: float,
) -> tuple[AIClient, Mock, Path]:
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "cost")
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    monkeypatch.setattr(
        "src.ai.client.resolve_token_estimator",
        lambda _: SimpleNamespace(estimate=lambda _text: 0),
    )
    router = Mock()
    monkeypatch.setattr("src.ai.client.AIRequestRouter", lambda **_kwargs: router)
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    trace_path = tmp_path / "trace.jsonl"
    client = AIClient(
        "summary-model", 0.2, feature_budget,
        endpoint="https://example.invalid", api_key="test-key",
        trace_context=AITraceContext(
            edition="acme_weekly", run_id="telemetry", caller="test",
            trace_file=trace_path,
            metadata={"program_id": "synthetic", "run_budget_usd": run_budget},
        ),
    )
    return client, router, trace_path


def _retry_reservation_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    feature_budget: float = 10.0,
    run_budget: float = 10.0,
    requests_per_minute: int | None = None,
    sleep_func: Callable[[float], None] = lambda _seconds: None,
    time_func: Callable[[], float] = lambda: 1000.0,
) -> tuple[AIClient, Mock, Path]:
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "cost")
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.request_router.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    estimate = Mock(return_value=100)
    monkeypatch.setattr(
        "src.ai.client.resolve_token_estimator",
        lambda _: SimpleNamespace(estimate=estimate),
    )
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    trace_path = tmp_path / "trace.jsonl"
    client = AIClient(
        "summary-model", 0.2, feature_budget,
        endpoint="https://example.invalid", api_key="test-key",
        requests_per_minute=requests_per_minute, sleep_func=sleep_func, time_func=time_func,
        trace_context=AITraceContext(
            edition="acme_weekly", run_id=_unique_run_id(), caller="test",
            trace_file=trace_path,
            metadata={"program_id": "synthetic", "run_budget_usd": run_budget, "cost_reservation_id": 999},
        ),
    )
    return client, estimate, trace_path


def _retry_reservation_rows(client: AIClient) -> list[tuple[int, str, float, float | None]]:
    guard = client._run_cost_guard
    assert guard is not None
    with sqlite3.connect(guard.ledger_path) as connection:
        return connection.execute(
            "SELECT reservation_id, status, reserved_usd, actual_usd "
            "FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall()


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("message", ["schema version 5", "reference 429"])
@pytest.mark.parametrize(
    "status_code",
    [200, 400, 401, 403, 404, 409, 422, 499, 600, None, "503", 429.0, False],
)
def test_retry_classification_refuses_misleading_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, message: str, status_code: object,
) -> None:
    sleeps: list[float] = []
    client, estimate, trace_path = _retry_reservation_client(tmp_path, monkeypatch, sleep_func=sleeps.append)
    error = _FakeAPIStatusError(400, message)
    setattr(error, "status_code", status_code)
    _FakeAzureOpenAI.actions = [
        error,
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    with pytest.raises(AIClientError) as raised:
        getattr(client, method)("system", "user", max_tokens=100, **arguments)

    assert raised.value.__cause__ is error
    assert _FakeAzureOpenAI.create_calls == 1
    assert sleeps == []
    estimate.assert_called_once()
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == ["reserved"]
    assert rows[0][2] == pytest.approx(0.2) and rows[0][3] is None
    guard = client._run_cost_guard
    assert guard is not None
    state = guard.current_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0, 0.2, 0))
    assert client.usage_stats.call_count == 0
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1 and records[0]["cost_reservation_id"] == rows[0][0]
    assert "error" in records[0] and "cost_usd" not in records[0]
    telemetry = read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert [record.status for record in telemetry] == [AiTelemetryStatus.OTHER]


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504, 599, "rate-limit-class"])
def test_retry_classification_preserves_retryable_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, status_code: int | str,
) -> None:
    sleeps: list[float] = []
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch, sleep_func=sleeps.append)
    error = (
        _FakeAPIStatusError(status_code, "temporary failure")
        if isinstance(status_code, int)
        else _FakeRateLimitError("throttled")
    )
    _FakeAzureOpenAI.actions = [
        error,
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    result = getattr(client, method)("system", "user", max_tokens=100, **arguments)

    assert result == ({"ok": True} if method == "structured" else '{"ok": true}')
    assert _FakeAzureOpenAI.create_calls == 2 and sleeps == [0.5]
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == ["reserved", "settled"]
    assert [row[2] for row in rows] == pytest.approx([0.2, 0.2])
    assert rows[0][3] is None and rows[1][3] == pytest.approx(0.05)
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["cost_reservation_id"] for record in records] == [row[0] for row in rows]
    assert "error" in records[0] and "error" not in records[1]


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_retry_classification_stops_after_nonretryable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    sleeps: list[float] = []
    client, estimate, trace_path = _retry_reservation_client(tmp_path, monkeypatch, sleep_func=sleeps.append)
    error = _FakeAPIStatusError(400, "invalid version 5")
    _FakeAzureOpenAI.actions = [
        _FakeAPIStatusError(503, "unavailable"),
        error,
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    with pytest.raises(AIClientError) as raised:
        getattr(client, method)("system", "user", max_tokens=100, **arguments)

    assert raised.value.__cause__ is error
    assert _FakeAzureOpenAI.create_calls == estimate.call_count == 2
    assert sleeps == [0.5]
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == ["reserved", "reserved"]
    assert [row[2] for row in rows] == pytest.approx([0.2, 0.2])
    assert all(row[3] is None for row in rows)
    guard = client._run_cost_guard
    assert guard is not None
    state = guard.current_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0, 0.4, 0))
    assert client.usage_stats.call_count == 0
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["cost_reservation_id"] for record in records] == [row[0] for row in rows]
    assert all("error" in record and "cost_usd" not in record for record in records)


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize(
    ("status_code", "exception_name", "retryable"),
    [
        (400, "BadRequestError", False),
        (401, "AuthenticationError", False),
        (404, "NotFoundError", False),
        (429, "RateLimitError", True),
        (503, "InternalServerError", True),
    ],
)
def test_retry_classification_with_installed_sdk_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
    status_code: int, exception_name: str, retryable: bool,
) -> None:
    sdk = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    sleeps: list[float] = []
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch, sleep_func=sleeps.append)
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, sdk.RateLimitError, sdk.APIStatusError),
    )
    client._router = AIRequestRouter(
        endpoint="https://example.invalid", api_key="test-key",
        rate_limit_scope="offline-sdk-exception", sleep_func=sleeps.append,
    )
    response = httpx.Response(
        status_code, request=httpx.Request("POST", "https://example.invalid"),
    )
    error = getattr(sdk, exception_name)("schema version 5, reference 429", response=response, body=None)
    _FakeAzureOpenAI.actions = [
        error,
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    if retryable:
        result = getattr(client, method)("system", "user", max_tokens=100, **arguments)
        assert result == ({"ok": True} if method == "structured" else '{"ok": true}')
    else:
        with pytest.raises(AIClientError) as raised:
            getattr(client, method)("system", "user", max_tokens=100, **arguments)
        assert raised.value.__cause__ is error

    assert _FakeAzureOpenAI.create_calls == (2 if retryable else 1)
    assert sleeps == ([0.5] if retryable else [])
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == (["reserved", "settled"] if retryable else ["reserved"])
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["cost_reservation_id"] for record in records] == [row[0] for row in rows]


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_retry_reservation_id_each_dispatch_is_reserved_and_correlated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("private retry error one"),
        _FakeAPIStatusError(503, "private retry error two"),
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    snapshots: list[list[tuple[int, str, float, float | None]]] = []
    original_create = _FakeAzureOpenAI.create

    def capture_dispatch(fake: _FakeAzureOpenAI, **kwargs: object) -> object:
        snapshots.append(_retry_reservation_rows(client))
        return original_create(fake, **kwargs)

    monkeypatch.setattr(_FakeAzureOpenAI, "create", capture_dispatch)
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    result = getattr(client, method)("system", "user", max_tokens=100, **arguments)
    assert result == ({"ok": True} if method == "structured" else '{"ok": true}')

    rows = _retry_reservation_rows(client)
    assert [len(snapshot) for snapshot in snapshots] == [1, 2, 3]
    assert all(row[1] == "reserved" for snapshot in snapshots for row in snapshot)
    assert [row[1] for row in rows] == ["reserved", "reserved", "settled"]
    assert [row[2] for row in rows] == pytest.approx([0.2, 0.2, 0.2])
    assert rows[-1][3] == pytest.approx(0.05)
    guard = client._run_cost_guard
    assert guard is not None
    state = guard.current_state()
    assert (state.spent_usd, state.reserved_usd) == pytest.approx((0.05, 0.4))
    assert state.ai_calls == client.usage_stats.call_count == 1

    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["cost_reservation_id"] for record in records] == [row[0] for row in rows]
    assert all(record["metadata"]["cost_reservation_id"] == 999 for record in records)
    assert all("error" in record and "cost_usd" not in record for record in records[:2])
    assert all("latency_ms" not in record for record in records[:2])
    assert "error" not in records[-1]
    assert "private retry error" not in json.dumps(records)
    telemetry = read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert [record.status for record in telemetry] == [AiTelemetryStatus.OTHER, AiTelemetryStatus.OTHER, AiTelemetryStatus.OK]
    assert [record.cost_usd for record in telemetry] == [None, None, 0.05]
    assert [record.latency_ms for record in telemetry] == [None, None, records[-1]["latency_ms"]]
    summary = build_feature_cost_summary("synthetic", programs_root=tmp_path / "telemetry")["test"]
    assert (summary.call_count, summary.error_count) == (3, 2)
    assert summary.total_cost_usd == pytest.approx(0.05)
    assert summary.avg_latency_ms == records[-1]["latency_ms"]


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_retry_reservation_id_exhaustion_retains_every_uncertain_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("throttled"),
        _FakeAPIStatusError(503, "unavailable"),
        _FakeAPIStatusError(503, "still unavailable"),
    ]
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    with pytest.raises(AIClientError):
        getattr(client, method)("system", "user", max_tokens=100, **arguments)

    rows = _retry_reservation_rows(client)
    assert _FakeAzureOpenAI.create_calls == 3
    assert [row[1] for row in rows] == ["reserved", "reserved", "reserved"]
    assert [row[2] for row in rows] == pytest.approx([0.2, 0.2, 0.2])
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["cost_reservation_id"] for record in records] == [row[0] for row in rows]
    assert len(read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")) == 3


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("via_fallback", [False, True])
@pytest.mark.parametrize("refusal", ["feature", "run", "configuration", "before-commit", "after-commit"])
def test_retry_reservation_id_refusal_stops_dispatch_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, via_fallback: bool, refusal: str,
) -> None:
    feature_budget = 0.3 if refusal == "feature" else 10.0
    run_budget = 0.3 if refusal == "run" else 10.0
    client, estimate, trace_path = _retry_reservation_client(
        tmp_path, monkeypatch, feature_budget=feature_budget, run_budget=run_budget,
    )
    guard = client._run_cost_guard
    assert guard is not None
    if refusal == "configuration":
        estimate.side_effect = [100, -1]
    elif refusal == "before-commit":
        reserve_call = guard.reserve_call
        reserve_calls = 0

        def fail_second_reservation(estimated_cost: float) -> object:
            nonlocal reserve_calls
            reserve_calls += 1
            if reserve_calls == 2:
                raise StateError("synthetic reservation failure")
            return reserve_call(estimated_cost)

        monkeypatch.setattr(guard, "reserve_call", fail_second_reservation)
    elif refusal == "after-commit":
        persist_projection = guard._persist_projection
        projection_calls = 0

        def fail_second_projection(state: object) -> None:
            nonlocal projection_calls
            projection_calls += 1
            if projection_calls == 2:
                raise OSError("synthetic projection failure")
            persist_projection(state)

        monkeypatch.setattr(guard, "_persist_projection", fail_second_projection)

    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("private first failure"),
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    factory = Mock(return_value=client)
    target: AIClient | FallbackAIClient = client
    if via_fallback:
        target = FallbackAIClient(
            deployments=("summary-model", "summary-backup"),
            temperature=0.2, budget_usd=feature_budget,
            client_factory=factory, retryable_exceptions=(Exception,),
        )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    error_type = (
        BudgetExceeded if refusal in {"feature", "run"}
        else BudgetConfigurationError if refusal == "configuration"
        else BudgetStateError
    )
    with pytest.raises(error_type):
        getattr(target, method)("system", "user", max_tokens=100, **arguments)
    parser.assert_not_called()
    assert _FakeAzureOpenAI.create_calls == 1
    assert factory.call_count == (1 if via_fallback else 0)
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == (
        ["reserved", "settled"] if refusal == "after-commit" else ["reserved"]
    )
    if refusal == "after-commit":
        assert rows[1][3] == 0.0
        assert guard.current_state().reserved_usd == pytest.approx(0.2)
        with sqlite3.connect(guard.ledger_path) as connection:
            assert connection.execute(
                "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
            ).fetchall() == [(rows[1][0],)]
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["cost_reservation_id"] for record in records] == [rows[0][0]]
    assert "private first failure" not in json.dumps(records)
    assert len(read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")) == 1


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("via_fallback", [False, True])
@pytest.mark.parametrize("error_type", [StateError, OSError, UnicodeError])
def test_initial_projection_failure_release_stops_dispatch_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, via_fallback: bool,
    error_type: type[Exception],
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    guard = client._run_cost_guard
    assert guard is not None
    active = guard.reserve_call(0.1)
    projection_before = guard.state_path.read_bytes()
    failure = error_type("synthetic projection failure")

    def fail_projection(_state: object) -> None:
        raise failure

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    _FakeAzureOpenAI.create_calls = 0
    _FakeAzureOpenAI.actions = []
    factory = Mock(return_value=client)
    target: AIClient | FallbackAIClient = client
    if via_fallback:
        target = FallbackAIClient(
            deployments=("summary-model", "summary-backup"),
            temperature=0.2, budget_usd=10.0,
            client_factory=factory, retryable_exceptions=(Exception,),
        )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    with pytest.raises(BudgetStateError):
        getattr(target, method)("system", "user", max_tokens=100, **arguments)

    parser.assert_not_called()
    assert _FakeAzureOpenAI.create_calls == 0
    assert factory.call_count == (1 if via_fallback else 0)
    assert not trace_path.exists()
    assert not read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert not client._feature_budget_state.reservations
    assert guard.current_state().reserved_usd == pytest.approx(0.1)
    assert guard.state_path.read_bytes() == projection_before
    rows = _retry_reservation_rows(client)
    assert rows[0] == (active.reservation_id, "reserved", 0.1, None)
    assert rows[1][1:] == ("settled", 0.2, 0.0)
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(rows[1][0],)]


def test_initial_projection_failure_release_failure_stops_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    guard = client._run_cost_guard
    assert guard is not None
    guard.record_actual(0.0, ai_calls=0)
    projection_before = guard.state_path.read_bytes()
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute(
            "CREATE TABLE ai_cost_non_dispatch_releases "
            "(reservation_id INTEGER PRIMARY KEY, released_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER reject_release BEFORE INSERT ON ai_cost_non_dispatch_releases "
            "BEGIN SELECT RAISE(ABORT, 'synthetic marker failure'); END"
        )

    def fail_projection(_state: object) -> None:
        raise OSError("synthetic private projection failure")

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    _FakeAzureOpenAI.create_calls = 0
    _FakeAzureOpenAI.actions = []
    factory = Mock(return_value=client)
    target = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2, budget_usd=10.0,
        client_factory=factory, retryable_exceptions=(Exception,),
    )
    with pytest.raises(BudgetStateError, match="release.*reconcile") as caught:
        target.chat("system", "user", max_tokens=100)

    assert "private" not in "".join(traceback.format_exception(caught.value))
    factory.assert_called_once()
    assert _FakeAzureOpenAI.create_calls == 0
    assert not trace_path.exists()
    assert not read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert not client._feature_budget_state.reservations
    assert guard.current_state().reserved_usd == pytest.approx(0.2)
    assert guard.state_path.read_bytes() == projection_before
    assert [row[1:] for row in _retry_reservation_rows(client)] == [("reserved", 0.2, None)]
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_cost_non_dispatch_releases").fetchone() == (0,)


def test_initial_projection_failure_release_unexpected_cleanup_stops_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    guard = client._run_cost_guard
    assert guard is not None

    def fail_projection(_state: object) -> None:
        raise OSError("synthetic projection failure")

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic private cleanup failure")

    monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    monkeypatch.setattr(guard, "_finish_call", fail_cleanup)
    _FakeAzureOpenAI.create_calls = 0
    _FakeAzureOpenAI.actions = []
    factory = Mock(return_value=client)
    target = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2, budget_usd=10.0,
        client_factory=factory, retryable_exceptions=(Exception,),
    )
    with pytest.raises(AIPreflightError, match="RuntimeError.*reconcile") as caught:
        target.chat("system", "user", max_tokens=100)

    assert "private" not in "".join(traceback.format_exception(caught.value))
    factory.assert_called_once()
    assert _FakeAzureOpenAI.create_calls == 0
    assert not trace_path.exists()
    assert not read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert not client._feature_budget_state.reservations
    assert guard.current_state().reserved_usd == pytest.approx(0.2)
    assert [row[1:] for row in _retry_reservation_rows(client)] == [("reserved", 0.2, None)]


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_retry_reservation_id_unexpected_preflight_error_is_not_a_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    client, estimate, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    estimate.side_effect = [100, ValueError("synthetic estimator failure")]
    _FakeAzureOpenAI.actions = [_FakeRateLimitError("private first failure")]
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(AIPreflightError, match=r"AI budget reservation failed \(ValueError\)"):
        getattr(client, method)("system", "user", max_tokens=100, **arguments)

    assert _FakeAzureOpenAI.create_calls == 1
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["cost_reservation_id"] == _retry_reservation_rows(client)[0][0]
    assert "private first failure" not in json.dumps(records)
    assert len(read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")) == 1


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("via_fallback", [False, True], ids=["direct", "fallback"])
@pytest.mark.parametrize("failure_stage", ["initial", "retry"])
def test_resolved_tokenizer_failure_blocks_cost_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, via_fallback: bool, failure_stage: str,
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    failure = ValueError("private encoder detail")
    encode = Mock(side_effect=[failure] if failure_stage == "initial" else [[1] * 100, failure])
    encoding = SimpleNamespace(encode_ordinary=encode)
    module = SimpleNamespace(
        encoding_name_for_model=lambda _: "synthetic_encoding",
        get_encoding=lambda _: encoding,
    )
    monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
    estimator = TiktokenTokenEstimator("synthetic-model")
    monkeypatch.setattr("src.ai.client.resolve_token_estimator", lambda _: estimator)
    monkeypatch.setattr(_FakeAzureOpenAI, "create_calls", 0)
    _FakeAzureOpenAI.actions = [_FakeRateLimitError("private provider failure")]
    factory = Mock(return_value=client)
    target: AIClient | FallbackAIClient = client
    if via_fallback:
        target = FallbackAIClient(
            deployments=("summary-model", "summary-backup"),
            temperature=0.2, budget_usd=10.0, client_factory=factory, retryable_exceptions=(Exception,),
        )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    system, user = "private system", "private user"

    with pytest.raises(AIPreflightError, match=r"AI budget reservation failed \(ContextCompileRejected\)") as error:
        getattr(target, method)(system, user, max_tokens=100, **arguments)

    rendered = "".join(traceback.format_exception(error.value))
    assert "private encoder detail" not in rendered
    assert "private system" not in rendered
    assert "private user" not in rendered
    parser.assert_not_called()
    assert factory.call_count == (1 if via_fallback else 0)
    assert encode.call_count == (1 if failure_stage == "initial" else 2)
    guard = client._run_cost_guard
    assert guard is not None
    if failure_stage == "initial":
        assert _FakeAzureOpenAI.create_calls == 0
        assert not guard.ledger_path.exists()
        assert not guard.state_path.exists()
        assert not trace_path.exists()
    else:
        assert _FakeAzureOpenAI.create_calls == 1
        rows = _retry_reservation_rows(client)
        assert [row[1] for row in rows] == ["reserved"]
        records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        assert [record["cost_reservation_id"] for record in records] == [rows[0][0]]
        assert "private" not in json.dumps(records)


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("via_fallback", [False, True], ids=["direct", "fallback"])
@pytest.mark.parametrize("failure_stage", ["initial", "retry"])
@pytest.mark.parametrize("failure_kind", ["asset", "package-import", "missing-dependency"])
def test_resolved_tokenizer_resolution_failure_blocks_cost_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, via_fallback: bool,
    failure_stage: str, failure_kind: str,
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    detail, system, user = "private asset detail", "private system", "private user"
    encoding = SimpleNamespace(name="synthetic_encoding", encode_ordinary=Mock(return_value=[1] * 100))
    failed = {"value": failure_stage == "initial"}

    def load(_name: str) -> object:
        if failure_kind == "asset" and failed["value"]:
            raise OSError(detail)
        return encoding

    module = SimpleNamespace(
        encoding_name_for_model=lambda _: "synthetic_encoding",
        encoding_for_model=lambda name: load("synthetic_encoding"),
        get_encoding=load,
    )
    if failure_kind == "asset":
        monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
        error_name = "OSError"
    else:
        original_import = context_tokenizers.importlib.import_module
        failure: ImportError = (
            ImportError(detail) if failure_kind == "package-import"
            else ModuleNotFoundError(detail, name="regex")
        )

        def import_module(name: str, package: str | None = None) -> object:
            if name != "tiktoken":
                return original_import(name, package)
            if failed["value"]:
                raise failure
            return module

        monkeypatch.setattr(context_tokenizers.importlib, "import_module", import_module)
        error_name = type(failure).__name__
    resolver = context_tokenizers.resolve_token_estimator
    resolver.cache_clear()
    monkeypatch.setattr("src.ai.client.resolve_token_estimator", resolver)
    monkeypatch.setattr(_FakeAzureOpenAI, "create_calls", 0)
    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("private provider failure"),
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    create = _FakeAzureOpenAI.create

    def change_assets_after_dispatch(fake: _FakeAzureOpenAI, **kwargs: object) -> object:
        try:
            return create(fake, **kwargs)
        finally:
            if failure_stage == "retry":
                failed["value"] = True
                resolver.cache_clear()

    monkeypatch.setattr(_FakeAzureOpenAI, "create", change_assets_after_dispatch)
    factory = Mock(return_value=client)
    target: AIClient | FallbackAIClient = client
    if via_fallback:
        target = FallbackAIClient(
            deployments=("summary-model", "summary-backup"),
            temperature=0.2, budget_usd=10.0, client_factory=factory, retryable_exceptions=(Exception,),
        )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    try:
        with pytest.raises(AIPreflightError, match=rf"AI budget reservation failed \({error_name}\)") as error:
            getattr(target, method)(system, user, max_tokens=100, **arguments)

        rendered = "".join(traceback.format_exception(error.value))
        assert detail not in rendered
        assert system not in rendered
        assert user not in rendered
        parser.assert_not_called()
        assert factory.call_count == (1 if via_fallback else 0)
        guard = client._run_cost_guard
        assert guard is not None
        if failure_stage == "initial":
            assert _FakeAzureOpenAI.create_calls == 0
            assert not guard.ledger_path.exists()
            assert not guard.state_path.exists()
            assert not trace_path.exists()
        else:
            assert _FakeAzureOpenAI.create_calls == 1
            rows = _retry_reservation_rows(client)
            assert [row[1] for row in rows] == ["reserved"]
            records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
            assert [record["cost_reservation_id"] for record in records] == [rows[0][0]]
            assert "private" not in json.dumps(records)
    finally:
        resolver.cache_clear()


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("target_kind", ["direct", "fallback-default", "fallback-broad"])
@pytest.mark.parametrize("failure_type", [RuntimeError, ValueError])
@pytest.mark.parametrize(
    "failure_stage",
    ["initial-estimate", "initial-projection", "retry-estimate", "retry-projection", "retry-trace"],
)
def test_retry_reservation_id_unexpected_preflight_failures_stop_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    target_kind: str,
    failure_type: type[Exception],
    failure_stage: str,
) -> None:
    client, estimate, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    guard = client._run_cost_guard
    assert guard is not None
    failure = failure_type("private synthetic preflight detail")
    retry = failure_stage.startswith("retry")
    trace_calls = 0

    if failure_stage.endswith("estimate"):
        estimate.side_effect = [100, failure, 100] if retry else [failure, 100]
    elif failure_stage.endswith("projection"):
        persist_projection = guard._persist_projection
        projection_calls = 0

        def fail_projection(state: object) -> None:
            nonlocal projection_calls
            projection_calls += 1
            if projection_calls == (2 if retry else 1):
                raise failure
            persist_projection(state)

        monkeypatch.setattr(guard, "_persist_projection", fail_projection)
    else:
        write_trace = client._write_trace

        def fail_retry_trace(**kwargs: object) -> None:
            nonlocal trace_calls
            trace_calls += 1
            if trace_calls == 1:
                raise failure
            write_trace(**kwargs)

        monkeypatch.setattr(client, "_write_trace", fail_retry_trace)

    response = _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0)
    _FakeAzureOpenAI.actions = [_FakeRateLimitError("private provider detail"), response] if retry else [response]
    factory = Mock(return_value=client)
    target: AIClient | FallbackAIClient = client
    if target_kind != "direct":
        target = FallbackAIClient(
            deployments=("summary-model", "summary-backup"),
            temperature=0.2,
            budget_usd=10.0,
            client_factory=factory,
            retryable_exceptions=(Exception,) if target_kind == "fallback-broad" else None,
        )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    with pytest.raises(AIClientError) as captured:
        getattr(target, method)("system", "user", max_tokens=100, **arguments)

    assert type(captured.value) is AIPreflightError
    assert failure_type.__name__ in str(captured.value)
    assert "private synthetic preflight detail" not in "".join(traceback.format_exception(captured.value))
    parser.assert_not_called()
    assert _FakeAzureOpenAI.create_calls == (1 if retry else 0)
    assert factory.call_count == (0 if target_kind == "direct" else 1)
    if failure_stage == "initial-estimate":
        assert not guard.ledger_path.exists()
    else:
        rows = _retry_reservation_rows(client)
        expected_reservations = 2 if failure_stage == "retry-projection" else 1
        assert len(rows) == expected_reservations
        assert all(row[1] == "reserved" for row in rows)
    records = (
        [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        if trace_path.exists() else []
    )
    expected_traces = int(retry and failure_stage != "retry-trace")
    assert len(records) == expected_traces
    assert len(read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")) == expected_traces
    assert trace_calls == int(failure_stage == "retry-trace")


def test_retry_reservation_id_rate_limit_admits_each_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"value": 1000.0}
    sleeps: list[float] = []
    dispatch_times: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["value"] += seconds

    client, _, _ = _retry_reservation_client(
        tmp_path, monkeypatch, requests_per_minute=1,
        sleep_func=sleep, time_func=lambda: clock["value"],
    )
    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("throttled"),
        _FakeAPIStatusError(503, "unavailable"),
        _FakeResponse("ok", prompt_tokens=50, completion_tokens=0),
    ]
    original_create = _FakeAzureOpenAI.create

    def capture_dispatch(fake: _FakeAzureOpenAI, **kwargs: object) -> object:
        dispatch_times.append(clock["value"])
        return original_create(fake, **kwargs)

    monkeypatch.setattr(_FakeAzureOpenAI, "create", capture_dispatch)
    assert client.chat("system", "user", max_tokens=100) == "ok"
    assert dispatch_times == pytest.approx([1000.0, 1060.0, 1120.0])
    assert sleeps == pytest.approx([0.5, 59.5, 1.0, 59.0])


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("target_kind", ["direct", "fallback"])
@pytest.mark.parametrize("stage", ["initial-mode", "initial-observe", "initial-mode-error", "initial-rate", "retry-rate"])
def test_non_dispatch_release_stops_fallback_without_provider_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, target_kind: str, stage: str,
) -> None:
    clock_calls = 0

    def clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if stage == "initial-rate" or (stage == "retry-rate" and clock_calls > 1):
            raise ValueError("private synthetic local-admission detail")
        return 1000.0

    client, _, trace_path = _retry_reservation_client(
        tmp_path, monkeypatch, requests_per_minute=1, time_func=clock,
    )
    guard = client._run_cost_guard
    assert guard is not None
    if stage == "initial-mode":
        monkeypatch.setattr("src.ai.request_router.get_ai_mode", lambda: AIMode.DISABLED)
    elif stage == "initial-observe":
        monkeypatch.setattr("src.ai.request_router.get_ai_mode", lambda: AIMode.OBSERVE_ONLY)
    elif stage == "initial-mode-error":
        def fail_mode() -> AIMode:
            raise ValueError("private synthetic local-admission detail")

        monkeypatch.setattr("src.ai.request_router.get_ai_mode", fail_mode)
    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("private provider failure"),
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    factory = Mock(return_value=client)
    target: AIClient | FallbackAIClient = client
    if target_kind == "fallback":
        target = FallbackAIClient(
            deployments=("summary-model", "summary-backup"),
            temperature=0.2,
            budget_usd=10.0,
            client_factory=factory,
            retryable_exceptions=(Exception,),
        )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    with pytest.raises(AIPreflightError) as captured:
        getattr(target, method)("system", "user", max_tokens=100, **arguments)

    assert "private synthetic local-admission detail" not in "".join(traceback.format_exception(captured.value))
    assert _FakeAzureOpenAI.create_calls == (1 if stage == "retry-rate" else 0)
    assert factory.call_count == (1 if target_kind == "fallback" else 0)
    parser.assert_not_called()
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == (["reserved", "settled"] if stage == "retry-rate" else ["settled"])
    assert rows[-1][3] == 0.0
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(rows[-1][0],)]
    state = guard.current_state()
    assert state.reserved_usd == pytest.approx(0.2 if stage == "retry-rate" else 0.0)
    assert state.spent_usd == 0.0
    assert state.ai_calls == 0
    assert len(client._feature_budget_state.reservations) == (1 if stage == "retry-rate" else 0)
    traces = (
        [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        if trace_path.exists() else []
    )
    assert len(traces) == (1 if stage == "retry-rate" else 0)
    if traces:
        assert traces[0]["cost_reservation_id"] == rows[0][0]
    assert len(read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")) == len(traces)


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("target_kind", ["direct", "fallback"])
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("failure_stage", ["before-commit", "after-commit"])
def test_non_dispatch_release_failure_preserves_local_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str, retry: bool,
    target_kind: str, method: str,
) -> None:
    clock_calls = 0

    def clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if retry and clock_calls == 1:
            return 1000.0
        raise ValueError("private rate admission")

    client, _, trace_path = _retry_reservation_client(
        tmp_path, monkeypatch, requests_per_minute=1, time_func=clock,
    )
    guard = client._run_cost_guard
    assert guard is not None
    if failure_stage == "before-commit":
        def fail_release(_reservation: object) -> None:
            raise StateError("private release state")

        monkeypatch.setattr(guard, "_release_undispatched_call", fail_release, raising=False)
    else:
        persist_projection = guard._persist_projection
        projection_calls = 0

        def fail_release_projection(state: object) -> None:
            nonlocal projection_calls
            projection_calls += 1
            if projection_calls == (3 if retry else 2):
                raise StateError("private release projection")
            persist_projection(state)

        monkeypatch.setattr(guard, "_persist_projection", fail_release_projection)
    response = _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0)
    _FakeAzureOpenAI.actions = [_FakeRateLimitError("private provider failure"), response] if retry else [response]
    factory = Mock(return_value=client)
    target: AIClient | FallbackAIClient = client
    if target_kind == "fallback":
        target = FallbackAIClient(
            deployments=("summary-model", "summary-backup"),
            temperature=0.2,
            budget_usd=10.0,
            client_factory=factory,
            retryable_exceptions=(Exception,),
        )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}

    with pytest.raises(BudgetStateError) as captured:
        getattr(target, method)("system", "user", max_tokens=100, **arguments)

    assert "private " not in "".join(traceback.format_exception(captured.value))
    assert _FakeAzureOpenAI.create_calls == int(retry)
    assert factory.call_count == (1 if target_kind == "fallback" else 0)
    parser.assert_not_called()
    assert len(client._feature_budget_state.reservations) == (2 if retry else 1)
    rows = _retry_reservation_rows(client)
    assert len(rows) == (2 if retry else 1)
    if retry:
        assert rows[0][1] == "reserved"
    assert rows[-1][1] == ("reserved" if failure_stage == "before-commit" else "settled")
    expected_reserved = (0.2 if retry else 0.0) + (0.2 if failure_stage == "before-commit" else 0.0)
    assert guard.current_state().reserved_usd == pytest.approx(expected_reserved)
    if failure_stage == "after-commit":
        with sqlite3.connect(guard.ledger_path) as connection:
            assert connection.execute(
                "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
            ).fetchall() == [(rows[-1][0],)]
    traces = (
        [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        if trace_path.exists() else []
    )
    assert len(traces) == int(retry)
    if traces:
        assert traces[0]["cost_reservation_id"] == rows[0][0]
    assert len(read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")) == len(traces)


def test_non_dispatch_release_allows_later_call_to_reuse_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def clock() -> float:
        raise ValueError("private rate admission")

    client, _, trace_path = _retry_reservation_client(
        tmp_path, monkeypatch, feature_budget=0.2, run_budget=0.2,
        requests_per_minute=1, time_func=clock,
    )
    guard = client._run_cost_guard
    assert guard is not None
    _FakeAzureOpenAI.actions = [_FakeResponse("ok", prompt_tokens=50, completion_tokens=0)]

    with pytest.raises(AIPreflightError):
        client.chat("system", "user", max_tokens=100)
    assert not client._feature_budget_state.reservations
    assert guard.current_state().reserved_usd == 0.0

    monkeypatch.setattr(client._router, "_time", lambda: 1000.0)
    assert client.chat("system", "user", max_tokens=100) == "ok"
    rows = _retry_reservation_rows(client)
    assert [row[1:] for row in rows] == [("settled", 0.2, 0.0), ("settled", 0.2, 0.05)]
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT reservation_id FROM ai_cost_non_dispatch_releases"
        ).fetchall() == [(rows[0][0],)]
    assert guard.current_state().ai_calls == 1
    assert guard.current_state().spent_usd == pytest.approx(0.05)
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("stage", ["interruption", "retry-backoff"])
def test_non_dispatch_release_retains_on_interruption_or_retry_backoff_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    def interrupted_clock() -> float:
        raise KeyboardInterrupt()

    def failed_backoff(_seconds: float) -> None:
        raise ValueError("private backoff failure")

    client, _, _ = _retry_reservation_client(
        tmp_path, monkeypatch,
        requests_per_minute=1 if stage == "interruption" else None,
        time_func=interrupted_clock if stage == "interruption" else lambda: 1000.0,
        sleep_func=failed_backoff,
    )
    guard = client._run_cost_guard
    assert guard is not None
    _FakeAzureOpenAI.actions = [_FakeRateLimitError("private provider failure")]
    expected_error = KeyboardInterrupt if stage == "interruption" else AIClientError

    with pytest.raises(expected_error):
        client.chat("system", "user", max_tokens=100)

    assert _FakeAzureOpenAI.create_calls == (0 if stage == "interruption" else 1)
    assert [row[1] for row in _retry_reservation_rows(client)] == ["reserved"]
    assert guard.current_state().reserved_usd == pytest.approx(0.2)
    assert len(client._feature_budget_state.reservations) == 1
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ai_cost_non_dispatch_releases'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("stage", ["mode", "rate"])
@pytest.mark.parametrize("callback", [False, True])
def test_non_dispatch_release_router_preserves_failure_without_raising_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback: bool, stage: str,
) -> None:
    failure = ValueError("synthetic admission failure")

    def clock() -> float:
        raise failure

    client, _, _ = _retry_reservation_client(
        tmp_path, monkeypatch, requests_per_minute=1, time_func=clock,
    )
    if stage == "mode":
        monkeypatch.setattr("src.ai.request_router.get_ai_mode", lambda: AIMode.DISABLED)
    on_failure = Mock() if callback else None
    with pytest.raises(RuntimeError if stage == "mode" else ValueError) as captured:
        client._router.route(
            deployment="summary-model", system="system", user="user",
            temperature=0.2, max_tokens=100, on_pre_dispatch_failure=on_failure,
        )

    if stage == "rate":
        assert captured.value is failure
    else:
        assert str(captured.value) == "AI execution is disabled for this invocation."
    if on_failure is not None:
        on_failure.assert_called_once_with(captured.value)
    assert _FakeAzureOpenAI.create_calls == 0
    assert client._run_cost_guard is not None
    assert not client._run_cost_guard.ledger_path.exists()


def test_non_dispatch_release_never_releases_an_uncertain_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _retry_reservation_client(tmp_path, monkeypatch)
    guard = client._run_cost_guard
    assert guard is not None
    _FakeAzureOpenAI.actions = [ValueError("private provider failure")]

    with pytest.raises(AIClientError):
        client.chat("system", "user", max_tokens=100)

    assert _FakeAzureOpenAI.create_calls == 1
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == ["reserved"]
    assert guard.current_state().reserved_usd == pytest.approx(0.2)
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'ai_cost_non_dispatch_releases'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("error_type", [OSError, StateError, UnicodeError])
def test_retry_reservation_id_trace_failure_does_not_mask_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    client, _, trace_path = _retry_reservation_client(tmp_path, monkeypatch)
    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("throttled"),
        _FakeResponse("ok", prompt_tokens=50, completion_tokens=0),
    ]
    write_trace = client._write_trace
    calls = 0

    def fail_first_trace(**kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error_type("private trace failure")
        write_trace(**kwargs)

    monkeypatch.setattr(client, "_write_trace", fail_first_trace)
    with caplog.at_level(logging.ERROR):
        assert client.chat("system", "user", max_tokens=100) == "ok"
    rows = _retry_reservation_rows(client)
    assert [row[1] for row in rows] == ["reserved", "settled"]
    assert json.loads(trace_path.read_text(encoding="utf-8"))["cost_reservation_id"] == rows[1][0]
    assert "private trace failure" not in caplog.text
    assert "trace failed" in caplog.text.lower()


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("success", "settled"),
        ("feature-overrun", "settled"),
        ("run-overrun", "settled"),
        ("invalid-usage", "reserved"),
        ("provider-failure", "reserved"),
        ("settlement-failure", "reserved"),
        ("projection-failure-after-settlement", "settled"),
    ],
)
def test_cost_reservation_id_correlates_trace_with_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, outcome: str, expected_status: str,
) -> None:
    feature_budget = 1.0 if outcome == "feature-overrun" else 10.0
    run_budget = 1.0 if outcome == "run-overrun" else 10.0
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=feature_budget, run_budget=run_budget,
    )
    assert client._trace_context is not None
    client._trace_context.metadata["cost_reservation_id"] = 999
    guard = client._run_cost_guard
    assert guard is not None
    prompt_tokens = 1250 if "overrun" in outcome else 200
    if outcome == "invalid-usage":
        prompt_tokens = -1
    router.route.return_value = _FakeResponse('{"ok": true}', prompt_tokens=prompt_tokens, completion_tokens=0)
    if outcome == "provider-failure":
        router.route.side_effect = RuntimeError("synthetic provider failure")
    elif outcome == "settlement-failure":
        monkeypatch.setattr(guard, "settle_call", Mock(side_effect=StateError("synthetic settlement failure")))
    elif outcome == "projection-failure-after-settlement":
        persist_projection = guard._persist_projection
        projection_calls = 0

        def fail_second_projection(state: object) -> None:
            nonlocal projection_calls
            projection_calls += 1
            if projection_calls == 2:
                raise OSError("synthetic projection failure")
            persist_projection(state)

        monkeypatch.setattr(guard, "_persist_projection", fail_second_projection)

    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    if outcome == "success":
        result = getattr(client, method)("system", "user", max_tokens=0, **arguments)
        assert result == ({"ok": True} if method == "structured" else '{"ok": true}')
        assert parser.call_count == (1 if method == "structured" else 0)
    else:
        exception = (
            BudgetExceeded if "overrun" in outcome
            else AIClientError if outcome == "provider-failure"
            else BudgetStateError
        )
        with pytest.raises(exception):
            getattr(client, method)("system", "user", max_tokens=0, **arguments)
        parser.assert_not_called()

    router.route.assert_called_once()
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute(
            "SELECT reservation_id, edition, run_id, status FROM ai_cost_reservations"
        ).fetchall()
    assert len(rows) == 1
    reservation_id, edition, run_id, status = rows[0]
    record = json.loads(trace_path.read_text(encoding="utf-8"))
    assert (edition, run_id, status) == ("acme_weekly", "telemetry", expected_status)
    assert (record["edition"], record["run_id"], record["cost_reservation_id"]) == (
        edition, run_id, reservation_id,
    )
    assert record["metadata"]["cost_reservation_id"] == 999
    assert ("error" in record) is (outcome != "success")


def test_cost_reservation_id_distinguishes_same_run_equal_cost_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=10.0, run_budget=10.0,
    )
    router.route.return_value = _FakeResponse("ok", prompt_tokens=200, completion_tokens=0)
    assert client.chat("system", "user", max_tokens=0) == "ok"
    assert client.chat("system", "user", max_tokens=0) == "ok"

    guard = client._run_cost_guard
    assert guard is not None
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute(
            "SELECT reservation_id, status, actual_usd FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall()
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(records) == 2
    assert rows[0][0] != rows[1][0]
    assert [row[1:] for row in rows] == [("settled", 0.2), ("settled", 0.2)]
    assert [record["cost_reservation_id"] for record in records] == [row[0] for row in rows]


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_cost_reservation_id_distinguishes_fallback_deployments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=10.0, run_budget=10.0,
    )
    router.route.side_effect = [
        RuntimeError("synthetic provider failure"),
        _FakeResponse('{"ok": true}', prompt_tokens=200, completion_tokens=0),
    ]

    def factory(**kwargs: object) -> AIClient:
        return AIClient(endpoint="https://example.invalid", api_key="test-key", **kwargs)

    fallback = FallbackAIClient(
        deployments=("primary", "backup"), temperature=0.2, budget_usd=10.0,
        trace_context=client._trace_context, client_factory=factory,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    result = getattr(fallback, method)("system", "user", max_tokens=0, **arguments)
    assert result == ({"ok": True} if method == "structured" else '{"ok": true}')

    guard = client._run_cost_guard
    assert guard is not None
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute(
            "SELECT reservation_id, status FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall()
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(records) == 2
    assert [row[1] for row in rows] == ["reserved", "settled"]
    assert [record["cost_reservation_id"] for record in records] == [row[0] for row in rows]
    assert [record["deployment"] for record in records] == ["primary", "backup"]


def test_cost_reservation_id_cannot_trace_unreturned_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=10.0, run_budget=10.0,
    )
    guard = client._run_cost_guard
    assert guard is not None
    monkeypatch.setattr(guard, "_persist_projection", Mock(side_effect=OSError("synthetic failure")))

    with pytest.raises(BudgetStateError):
        client.chat("system", "user", max_tokens=0)

    router.route.assert_not_called()
    assert not trace_path.exists()
    with sqlite3.connect(guard.ledger_path) as connection:
        rows = connection.execute("SELECT status FROM ai_cost_reservations").fetchall()
        releases = connection.execute("SELECT reservation_id FROM ai_cost_non_dispatch_releases").fetchall()
    assert rows == [("settled",)]
    assert len(releases) == 1


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize(
    ("feature_budget", "run_budget", "prompt_tokens", "ceiling"),
    [
        (1.0, 10.0, 1250, "feature"),
        (10.0, 1.0, 1250, "run"),
        (1.0, 1.0, 1250, "run"),
        (1.0, 10.0, 1000, None),
        (1.0, 10.0, 200, None),
    ],
    ids=["feature-overrun", "run-overrun", "both-overrun", "feature-equality", "under-budget"],
)
def test_post_usage_budget_telemetry_records_actual_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
    feature_budget: float, run_budget: float, prompt_tokens: int, ceiling: str | None,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=feature_budget, run_budget=run_budget,
    )
    router.route.return_value = _FakeResponse('{"ok": true}', prompt_tokens=prompt_tokens, completion_tokens=0)
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    expected_error = None

    if ceiling is None:
        result = getattr(client, method)("system", "user", max_tokens=0, **arguments)
        assert result == ({"ok": True} if method == "structured" else '{"ok": true}')
        assert parser.call_count == (1 if method == "structured" else 0)
    else:
        with pytest.raises(BudgetExceeded, match=f"actual AI spend exceeded the {ceiling} ceiling") as caught:
            getattr(client, method)("system", "user", max_tokens=0, **arguments)
        expected_error = str(caught.value)
        parser.assert_not_called()

    router.route.assert_called_once()
    state = client._run_cost_guard.current_state()
    assert state.spent_usd == pytest.approx(prompt_tokens / 1000)
    assert state.reserved_usd == 0.0 and state.ai_calls == 1
    assert client.usage_stats.call_count == 1 and client.usage_stats.total_tokens == prompt_tokens
    records = read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert len(records) == 1
    record = records[0]
    assert record.status == (AiTelemetryStatus.BUDGET_EXCEEDED if ceiling else AiTelemetryStatus.OK)
    assert record.tokens_in == prompt_tokens and record.tokens_out == 0
    assert record.cost_usd == pytest.approx(prompt_tokens / 1000)
    assert record.error_detail == expected_error
    summary = build_feature_cost_summary("synthetic", programs_root=tmp_path / "telemetry")["test"]
    assert summary.call_count == 1 and summary.error_count == (1 if ceiling else 0)
    assert summary.total_cost_usd == pytest.approx(prompt_tokens / 1000)
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["prompt_tokens"] == prompt_tokens and trace["completion_tokens"] == 0
    assert ("error" in trace) is (ceiling is not None)
    if ceiling is not None:
        assert trace["error"] == expected_error


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("synthetic provider budget failure", AiTelemetryStatus.OTHER),
        ("synthetic provider disabled", AiTelemetryStatus.FALLBACK),
    ],
)
def test_post_usage_budget_telemetry_preserves_provider_error_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, message: str, status: str,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=1.0, run_budget=10.0,
    )
    router.route.side_effect = RuntimeError(message)
    parser = Mock()
    arguments = {"parser": parser} if method == "structured" else {}

    with pytest.raises(AIClientError, match=message):
        getattr(client, method)("system", "user", max_tokens=0, **arguments)

    parser.assert_not_called()
    router.route.assert_called_once()
    records = read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert len(records) == 1 and records[0].status == status
    assert records[0].tokens_in is None and records[0].cost_usd is None
    assert message in records[0].error_detail
    assert message in json.loads(trace_path.read_text(encoding="utf-8"))["error"]


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_post_usage_budget_telemetry_append_failure_does_not_mask_overrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, method: str,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=1.0, run_budget=10.0,
    )
    router.route.return_value = _FakeResponse('{"ok": true}', prompt_tokens=1250, completion_tokens=0)
    append = Mock(side_effect=OSError("synthetic telemetry failure"))
    monkeypatch.setattr("src.core.ai_telemetry.append_ai_telemetry", append)
    arguments = {"parser": Mock()} if method == "structured" else {}

    with pytest.raises(BudgetExceeded, match="actual AI spend exceeded the feature ceiling"):
        getattr(client, method)("system", "user", max_tokens=0, **arguments)

    router.route.assert_called_once()
    append.assert_called_once()
    assert append.call_args.args[0].status == AiTelemetryStatus.BUDGET_EXCEEDED
    assert "AI telemetry append failed" in caplog.text
    assert client._run_cost_guard.current_state().spent_usd == pytest.approx(1.25)
    assert "actual AI spend exceeded the feature ceiling" in json.loads(
        trace_path.read_text(encoding="utf-8")
    )["error"]


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "fallback-wrapper"])
@pytest.mark.parametrize(
    ("feature_budget", "run_budget", "prompt_tokens", "ceiling"),
    [(1.0, 10.0, 200, None), (1.0, 10.0, 1250, "feature"), (10.0, 1.0, 1250, "run")],
    ids=["success", "feature-overrun", "run-overrun"],
)
def test_real_trace_write_failure_preserves_result_budget_and_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    method: str, wrapped: bool, feature_budget: float, run_budget: float,
    prompt_tokens: int, ceiling: str | None,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=feature_budget, run_budget=run_budget,
    )
    trace_path.mkdir()
    router.route.return_value = _FakeResponse('{"ok": true}', prompt_tokens=prompt_tokens, completion_tokens=0)
    factory = Mock(return_value=client)
    provider = (
        FallbackAIClient(
            deployments=("primary", "backup"), temperature=0.2, budget_usd=feature_budget,
            client_factory=factory, retryable_exceptions=(Exception,),
        )
        if wrapped else client
    )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    if ceiling is None:
        result = getattr(provider, method)("system", "user", max_tokens=0, **arguments)
        assert result == ({"ok": True} if method == "structured" else '{"ok": true}')
        assert parser.call_count == (1 if method == "structured" else 0)
    else:
        with pytest.raises(BudgetExceeded, match=f"actual AI spend exceeded the {ceiling} ceiling"):
            getattr(provider, method)("system", "user", max_tokens=0, **arguments)
        parser.assert_not_called()

    router.route.assert_called_once()
    if wrapped:
        factory.assert_called_once()
        assert factory.call_args.kwargs["deployment"] == "primary"
    state = client._run_cost_guard.current_state()
    assert state.spent_usd == pytest.approx(prompt_tokens / 1000)
    assert state.reserved_usd == 0.0 and state.ai_calls == 1
    records = read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert len(records) == 1
    assert records[0].status == (AiTelemetryStatus.BUDGET_EXCEEDED if ceiling else AiTelemetryStatus.OK)
    assert records[0].tokens_in == prompt_tokens and records[0].cost_usd == pytest.approx(prompt_tokens / 1000)
    warnings = [record for record in caplog.records if record.name == "src.ai.llm_trace"]
    assert len(warnings) == 1 and warnings[0].getMessage() == "LLM trace write failed."
    assert str(trace_path) not in caplog.text


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "fallback-wrapper"])
@pytest.mark.parametrize(
    ("feature_budget", "run_budget", "prompt_tokens", "ceiling"),
    [(1.0, 10.0, 200, None), (1.0, 10.0, 1250, "feature"), (10.0, 1.0, 1250, "run")],
    ids=["success", "feature-overrun", "run-overrun"],
)
def test_real_telemetry_append_failure_warns_without_private_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    method: str, wrapped: bool, feature_budget: float, run_budget: float,
    prompt_tokens: int, ceiling: str | None,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=feature_budget, run_budget=run_budget,
    )
    assert client._trace_context is not None
    client._trace_context.metadata["feature"] = "synthetic-private-feature"
    telemetry_path = tmp_path / "telemetry" / "synthetic" / "_state" / "ai_telemetry.jsonl"
    telemetry_path.mkdir(parents=True)
    router.route.return_value = _FakeResponse('{"ok": true}', prompt_tokens=prompt_tokens, completion_tokens=0)
    factory = Mock(return_value=client)
    provider = (
        FallbackAIClient(
            deployments=("primary", "backup"), temperature=0.2, budget_usd=feature_budget,
            client_factory=factory, retryable_exceptions=(Exception,),
        )
        if wrapped else client
    )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}
    if ceiling is None:
        result = getattr(provider, method)("system", "user", max_tokens=0, **arguments)
        assert result == ({"ok": True} if method == "structured" else '{"ok": true}')
        assert parser.call_count == (1 if method == "structured" else 0)
    else:
        with pytest.raises(BudgetExceeded, match=f"actual AI spend exceeded the {ceiling} ceiling"):
            getattr(provider, method)("system", "user", max_tokens=0, **arguments)
        parser.assert_not_called()

    router.route.assert_called_once()
    if wrapped:
        factory.assert_called_once()
        assert factory.call_args.kwargs["deployment"] == "primary"
    state = client._run_cost_guard.current_state()
    assert state.spent_usd == pytest.approx(prompt_tokens / 1000)
    assert state.reserved_usd == 0.0 and state.ai_calls == 1
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["prompt_tokens"] == prompt_tokens and ("error" in trace) is (ceiling is not None)
    assert telemetry_path.is_dir()
    warnings = [
        record for record in caplog.records
        if record.name == "src.ai.client" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1 and warnings[0].getMessage() == "AI telemetry append failed."
    assert warnings[0].exc_info is None
    assert "synthetic-private-feature" not in caplog.text
    assert str(telemetry_path) not in caplog.text


def test_ai_client_requires_azure_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="Vertex requires Azure OpenAI"):
        AIClient("blurb-model", 0.2, 0.5)


def test_ai_client_surfaces_missing_optional_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")

    def _raise_import_error(_name: str):
        raise ImportError("openai not installed")

    monkeypatch.setattr("src.ai.request_router.importlib.import_module", _raise_import_error)

    with pytest.raises(RuntimeError, match=r'pip install -e "\.\[ai\]"'):
        AIClient("blurb-model", 0.2, 0.5)


def test_ai_client_retries_on_429_and_5xx_and_tracks_usage(monkeypatch, caplog, tmp_path) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path)
    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("429 throttled"),
        _FakeAPIStatusError(503, "temporary outage"),
        _FakeResponse("grounded text [#123]", prompt_tokens=1000, completion_tokens=400),
    ]
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    client = AIClient(
        "blurb-model",
        0.2,
        0.5,
        sleep_func=lambda seconds: sleep_calls.append(seconds),
        trace_context=AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test"),
    )
    with caplog.at_level("INFO"):
        result = client.chat("system", "user", prompt_version="blurb.v1")

    assert result == "grounded text [#123]"
    assert sleep_calls == [0.5, 1.0]
    assert client.usage_stats.prompt_tokens == 1000
    assert client.usage_stats.completion_tokens == 400
    assert client.usage_stats.total_tokens == 1400
    assert client.spent_usd == pytest.approx(0.0007)
    telemetry = read_ai_telemetry("acme_weekly", programs_root=tmp_path / "telemetry")
    assert [record.status for record in telemetry] == [AiTelemetryStatus.OTHER, AiTelemetryStatus.OTHER, AiTelemetryStatus.OK]
    assert all(record.feature == "test" for record in telemetry)
    assert all(record.deployment_id == "blurb-model" for record in telemetry)
    assert _FakeAzureOpenAI.created_kwargs == {
        "azure_endpoint": "https://example.openai.azure.com",
        "api_key": "test-key",
        "api_version": "2024-02-01",
        "timeout": 20,
        "max_retries": 0,
    }
    assert "blurb.v1" in caplog.text


def test_ai_client_blocks_after_budget_is_spent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path)
    _FakeAzureOpenAI.actions = [
        _FakeResponse("first", prompt_tokens=1000, completion_tokens=1000),
        _FakeResponse("second", prompt_tokens=100, completion_tokens=100),
    ]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    # RELO-W1.4's conservative worst-case reservation reserves for the full
    # requested max_tokens (default 800) before every dispatch, so budget
    # must clear that pre-flight floor (~0.0010004 at default pricing) for
    # the first call to even be attempted; 0.0015 clears it while still
    # being too little for a second full reservation once the first call's
    # actual (1000+1000 token) spend settles.
    client = AIClient(
        "summary-model",
        0.2,
        0.0015,
        trace_context=AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test"),
    )

    assert client.chat("system", "user") == "first"
    assert client.spent_usd > 0.001

    with pytest.raises(BudgetExceeded, match="Spent"):
        client.chat("system", "user")

    assert _FakeAzureOpenAI.create_calls == 1


def test_untraced_remote_call_is_rejected_before_provider_dispatch(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    _FakeAzureOpenAI.actions = [_FakeResponse("should-not-run", prompt_tokens=1, completion_tokens=1)]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    client = AIClient("summary-model", 0.2, 0.5)

    with pytest.raises(AIClientError, match="trace context is required"):
        client.chat("system", "user")
    assert _FakeAzureOpenAI.create_calls == 0


def test_reservation_blocks_before_provider_dispatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path)
    _FakeAzureOpenAI.actions = [_FakeResponse("should-not-run", prompt_tokens=1, completion_tokens=1)]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    client = AIClient(
        "summary-model", 0.2, 0.5,
        trace_context=AITraceContext(
            edition="acme_weekly", run_id="reservation-block", caller="test",
            metadata={"run_budget_usd": 0.000001},
        ),
    )

    with pytest.raises(BudgetExceeded, match="would exceed"):
        client.chat("system", "user", max_tokens=800)
    assert _FakeAzureOpenAI.create_calls == 0


@pytest.mark.parametrize("field_name", ["input_cost_per_1k_tokens", "output_cost_per_1k_tokens"])
@pytest.mark.parametrize(
    "invalid_price",
    [-0.000001, float("nan"), float("inf"), float("-inf"), True, "private-price-value", 10**400],
    ids=["negative", "nan", "positive-infinity", "negative-infinity", "boolean", "text", "float-overflow"],
)
def test_model_price_admission_blocks_client_before_router(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field_name: str, invalid_price: object,
) -> None:
    prices: dict[str, object] = {"input_cost_per_1k_tokens": 0.001, "output_cost_per_1k_tokens": 0.001}
    prices[field_name] = invalid_price
    monkeypatch.setattr(
        "src.core.policy_loader._load_policy_defaults",
        lambda: {"ai": {"models": {"default": prices}}},
    )
    constructed: list[object] = []
    monkeypatch.setattr("src.ai.client.AIRequestRouter", lambda **kwargs: constructed.append(kwargs))
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    trace = AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test")

    with pytest.raises(BudgetConfigurationError, match="pricing") as error:
        AIClient("selected", 0.2, 1.0, trace_context=trace)

    assert not constructed
    assert not (tmp_path / "programs").exists()
    assert "private-price-value" not in str(error.value)


@pytest.mark.parametrize(
    "policy_document",
    [
        "policy_schema_version: private-schema-value\n",
        'policy_schema_version: "1"\nai: [private-yaml-value\n',
    ],
)
def test_model_price_admission_hides_policy_failure_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, policy_document: str,
) -> None:
    from src.core import policy_loader

    policy_path = tmp_path / "defaults.yaml"
    policy_path.write_text(policy_document, encoding="utf-8")
    monkeypatch.setattr(policy_loader, "DEFAULT_POLICY_PATH", policy_path)
    constructed: list[object] = []
    monkeypatch.setattr("src.ai.client.AIRequestRouter", lambda **kwargs: constructed.append(kwargs))
    policy_loader._load_policy_defaults.cache_clear()
    try:
        with pytest.raises(BudgetConfigurationError, match="pricing") as caught:
            AIClient("selected", 0.2, 1.0)
    finally:
        policy_loader._load_policy_defaults.cache_clear()

    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-schema-value" not in rendered
    assert "private-yaml-value" not in rendered
    assert str(policy_path) not in rendered
    assert not constructed


@pytest.mark.parametrize(
    ("run_budget", "error_type"),
    [
        (0.0, BudgetExceeded), (-0.5, BudgetConfigurationError),
        (True, BudgetConfigurationError), (False, BudgetConfigurationError),
        (float("nan"), BudgetConfigurationError), (float("inf"), BudgetConfigurationError),
        (float("-inf"), BudgetConfigurationError), ("nan", BudgetConfigurationError),
        ("0", BudgetExceeded), ("-0.5", BudgetConfigurationError),
        ("private-budget-value", BudgetConfigurationError), ("", BudgetConfigurationError),
        (10**400, BudgetConfigurationError), (["private-budget-value"], BudgetConfigurationError),
    ],
)
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_run_budget_admission_blocks_invalid_values_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_budget: object,
    error_type: type[AIClientError], method: str,
) -> None:
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    _FakeAzureOpenAI.create_calls = 0
    _FakeAzureOpenAI.actions = [_FakeResponse("should-not-run", prompt_tokens=1, completion_tokens=1)]
    trace = AITraceContext(
        edition="acme_weekly", run_id=_unique_run_id(), caller="test",
        metadata={"run_budget_usd": run_budget},
    )

    with pytest.raises(error_type, match="budget") as error:
        client = AIClient(
            "summary-model", 0.2, 0.5, endpoint="https://example.openai.azure.com",
            api_key="synthetic", trace_context=trace,
        )
        if method == "chat":
            client.chat("system", "user", max_tokens=10)
        else:
            client.structured("system", "user", parser=lambda payload: payload, max_tokens=10)

    assert _FakeAzureOpenAI.create_calls == 0
    assert not (tmp_path / "programs").exists()
    assert "private-budget-value" not in str(error.value)


@pytest.mark.parametrize("run_budget", [0.5, "0.5", None, ...])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_run_budget_admission_preserves_valid_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, run_budget: object, method: str,
) -> None:
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    _FakeAzureOpenAI.create_calls = 0
    _FakeAzureOpenAI.actions = [_FakeResponse("{}", prompt_tokens=1, completion_tokens=1)]
    trace = AITraceContext(
        edition="acme_weekly", run_id=_unique_run_id(), caller="test",
        metadata={} if run_budget is ... else {"run_budget_usd": run_budget},
    )
    client = AIClient(
        "summary-model", 0.2, 0.5, endpoint="https://example.openai.azure.com",
        api_key="synthetic", trace_context=trace,
    )

    if method == "chat":
        assert client.chat("system", "user", max_tokens=10) == "{}"
    else:
        assert client.structured("system", "user", parser=lambda payload: payload, max_tokens=10) == {}
    assert _FakeAzureOpenAI.create_calls == 1
    assert client._run_cost_guard.current_state().ai_calls == 1


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_zero_feature_budget_admission_blocks_without_guard_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str,
) -> None:
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    _FakeAzureOpenAI.create_calls = 0
    _FakeAzureOpenAI.actions = [_FakeResponse("{}", prompt_tokens=1, completion_tokens=1)]
    trace = AITraceContext(
        edition="acme_weekly", run_id=_unique_run_id(), caller="test",
        metadata={"run_budget_usd": 0.5},
    )
    client = AIClient(
        "summary-model", 0.2, 0.0, endpoint="https://example.openai.azure.com",
        api_key="synthetic", trace_context=trace,
    )

    with pytest.raises(BudgetExceeded):
        if method == "chat":
            client.chat("system", "user", max_tokens=10)
        else:
            client.structured("system", "user", parser=lambda payload: payload, max_tokens=10)
    assert _FakeAzureOpenAI.create_calls == 0
    assert not (tmp_path / "programs").exists()


@pytest.mark.parametrize("feature_budget", [-0.5, True, float("nan"), float("inf"), float("-inf"), 10**400])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_feature_budget_admission_blocks_invalid_values_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, feature_budget: float, method: str,
) -> None:
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    _FakeAzureOpenAI.create_calls = 0
    _FakeAzureOpenAI.actions = [_FakeResponse("should-not-run", prompt_tokens=1, completion_tokens=1)]
    trace = AITraceContext(
        edition="acme_weekly", run_id=_unique_run_id(), caller="test",
        metadata={"run_budget_usd": 0.5},
    )

    with pytest.raises(BudgetConfigurationError, match="budget"):
        client = AIClient(
            "summary-model", 0.2, feature_budget, endpoint="https://example.openai.azure.com",
            api_key="synthetic", trace_context=trace,
        )
        if method == "chat":
            client.chat("system", "user", max_tokens=10)
        else:
            client.structured("system", "user", parser=lambda payload: payload, max_tokens=10)

    assert _FakeAzureOpenAI.create_calls == 0
    assert not (tmp_path / "programs").exists()


@pytest.mark.parametrize("storage", ["json", "sqlite"])
@pytest.mark.parametrize(("field_name", "invalid_value"), [("spent_usd", -1.0), ("budget_usd", float("inf"))])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_ai_client_refuses_invalid_retained_cost_state_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, storage: str,
    field_name: str, invalid_value: float, method: str,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    _FakeAzureOpenAI.actions = [_FakeResponse("{}", prompt_tokens=1, completion_tokens=1)]
    trace = AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test")
    guard = CostGuard(edition=trace.edition, run_id=trace.run_id, budget_usd=0.5, programs_root=programs_root)
    if storage == "json":
        guard.state_path.parent.mkdir(parents=True, exist_ok=True)
        guard.state_path.write_text(
            json.dumps({
                "edition": trace.edition,
                "runs": {
                    trace.run_id: {
                        "budget_usd": 0.5,
                        "spent_usd": 0.0,
                        "reserved_usd": 0.0,
                        "ai_calls": 0,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:00:00+00:00",
                        field_name: invalid_value,
                    },
                },
            }),
            encoding="utf-8",
        )
    else:
        guard.record_actual(0.0)
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute(
                f"UPDATE ai_cost_runs SET {field_name} = ? WHERE edition = ? AND run_id = ?",
                (invalid_value, trace.edition, trace.run_id),
            )
    client = AIClient(
        "summary-model", 0.2, 0.5, endpoint="https://example.openai.azure.com",
        api_key="synthetic", trace_context=trace,
    )

    with pytest.raises(StateError, match=f"Invalid cost guard {field_name} value"):
        if method == "chat":
            client.chat("system", "user", max_tokens=10)
        else:
            client.structured("system", "user", parser=lambda payload: payload, max_tokens=10)
    assert _FakeAzureOpenAI.create_calls == 0


@pytest.mark.parametrize("storage", ["json", "sqlite"])
@pytest.mark.parametrize("field_name", ["started_at", "updated_at"])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_invalid_retained_cost_state_timestamps_do_not_disclose_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str, field_name: str, method: str,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=1.0, run_budget=1.0,
    )
    guard = client._run_cost_guard
    private_value = "synthetic-private-timestamp"
    if storage == "json":
        guard.state_path.parent.mkdir(parents=True, exist_ok=True)
        run = {
            "budget_usd": 1.0,
            "spent_usd": 0.0,
            "reserved_usd": 0.0,
            "ai_calls": 0,
            "started_at": "2026-05-16T00:00:00+00:00",
            "updated_at": "2026-05-16T00:00:00+00:00",
        }
        run[field_name] = private_value
        guard.state_path.write_text(
            json.dumps({"edition": "acme_weekly", "runs": {"telemetry": run}}),
            encoding="utf-8",
        )
    else:
        guard.record_actual(0.0, ai_calls=0)
        with sqlite3.connect(guard.ledger_path) as connection:
            connection.execute(
                f"UPDATE ai_cost_runs SET {field_name} = ? WHERE edition = ? AND run_id = ?",
                (private_value, "acme_weekly", "telemetry"),
            )
    projection_before = guard.state_path.read_bytes()
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    def run_with_active_exception(action: Callable[[], object]) -> None:
        try:
            raise RuntimeError("synthetic-private-active-exception")
        except RuntimeError:
            action()

    with pytest.raises(StateError, match=f"Invalid cost guard {field_name} value") as direct:
        run_with_active_exception(guard.current_state)
    with pytest.raises(BudgetStateError, match=f"Invalid cost guard {field_name} value") as wrapped:
        run_with_active_exception(lambda: getattr(client, method)("system", "user", max_tokens=10, **arguments))

    for error in (direct.value, wrapped.value):
        rendered = "".join(traceback.format_exception(error))
        assert private_value not in str(error) and private_value not in rendered
        assert "synthetic-private-active-exception" not in rendered
    router.route.assert_not_called()
    assert not trace_path.exists()
    assert guard.state_path.read_bytes() == projection_before
    if storage == "sqlite":
        with sqlite3.connect(guard.ledger_path) as connection:
            assert connection.execute(f"SELECT {field_name} FROM ai_cost_runs").fetchone() == (private_value,)
            assert connection.execute("SELECT COUNT(*) FROM ai_cost_reservations").fetchone() == (0,)
    else:
        assert not guard.ledger_path.exists()


@pytest.mark.parametrize("storage", ["json", "sqlite"])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_ai_client_preserves_retained_zero_cost_budget_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, storage: str, method: str,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    _FakeAzureOpenAI.actions = [_FakeResponse("{}", prompt_tokens=1, completion_tokens=1)]
    trace = AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test")
    guard = CostGuard(edition=trace.edition, run_id=trace.run_id, budget_usd=0.0, programs_root=programs_root)
    if storage == "json":
        guard.state_path.parent.mkdir(parents=True, exist_ok=True)
        guard.state_path.write_text(
            json.dumps({
                "edition": trace.edition,
                "runs": {
                    trace.run_id: {
                        "budget_usd": 0.0,
                        "spent_usd": 0.0,
                        "reserved_usd": 0.0,
                        "ai_calls": 0,
                        "started_at": "2026-05-16T00:00:00+00:00",
                        "updated_at": "2026-05-16T00:00:00+00:00",
                    },
                },
            }),
            encoding="utf-8",
        )
    else:
        guard.record_actual(0.0)
    client = AIClient(
        "summary-model", 0.2, 0.5, endpoint="https://example.openai.azure.com",
        api_key="synthetic", trace_context=trace,
    )

    with pytest.raises(BudgetExceeded):
        if method == "chat":
            client.chat("system", "user", max_tokens=10)
        else:
            client.structured("system", "user", parser=lambda payload: payload, max_tokens=10)
    assert _FakeAzureOpenAI.create_calls == 0


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_configured_run_budget_blocks_client_before_provider_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=10.0, run_budget=0.5,
    )
    original = CostGuard(
        edition="acme_weekly", run_id="telemetry", budget_usd=1.0, programs_root=tmp_path / "cost",
    )
    original.record_actual(0.375)
    projection_before = original.state_path.read_bytes()
    router.route.return_value = _FakeResponse('{"ok": true}', prompt_tokens=0, completion_tokens=1)
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetExceeded, match=r"\$0\.50.*run ceiling"):
        getattr(client, method)("system", "user", max_tokens=250, **arguments)

    router.route.assert_not_called()
    assert not trace_path.exists()
    assert original.state_path.read_bytes() == projection_before
    state = original.current_state()
    assert (state.budget_usd, state.spent_usd, state.reserved_usd, state.ai_calls) == (1.0, 0.375, 0.0, 1)
    with sqlite3.connect(original.ledger_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_cost_reservations").fetchone() == (0,)


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize(
    "completion_tokens,other_reservation,overrun",
    [(500, False, True), (375, False, False), (375, True, False)],
    ids=["overrun", "equality", "equality-with-other-reservation"],
)
def test_configured_run_budget_reports_actual_spend_after_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
    completion_tokens: int, other_reservation: bool, overrun: bool,
) -> None:
    client, router, trace_path = _telemetry_budget_client(
        tmp_path, monkeypatch, feature_budget=10.0, run_budget=0.5,
    )
    original = CostGuard(
        edition="acme_weekly", run_id="telemetry", budget_usd=1.0, programs_root=tmp_path / "cost",
    )
    original.record_actual(0.125)
    if other_reservation:
        original.reserve_call(0.125)
    router.route.return_value = _FakeResponse(
        '{"ok": true}', prompt_tokens=0, completion_tokens=completion_tokens,
    )
    parser = Mock(return_value={"ok": True})
    arguments = {"parser": parser} if method == "structured" else {}

    if overrun:
        with pytest.raises(BudgetExceeded, match=r"\$0\.50.*actual AI spend exceeded the run ceiling"):
            getattr(client, method)("system", "user", max_tokens=125, **arguments)
        parser.assert_not_called()
    else:
        result = getattr(client, method)("system", "user", max_tokens=125, **arguments)
        assert result == ({"ok": True} if method == "structured" else '{"ok": true}')
        assert parser.call_count == (1 if method == "structured" else 0)

    router.route.assert_called_once()
    state = original.current_state()
    assert (state.budget_usd, state.spent_usd, state.reserved_usd, state.ai_calls) == (
        1.0, 0.125 + completion_tokens / 1000, 0.125 if other_reservation else 0.0, 2,
    )
    with sqlite3.connect(original.ledger_path) as connection:
        assert connection.execute(
            "SELECT status, actual_usd FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall() == (
            [("reserved", None)] if other_reservation else []
        ) + [("settled", completion_tokens / 1000)]
    telemetry = read_ai_telemetry("synthetic", programs_root=tmp_path / "telemetry")
    assert [entry.status for entry in telemetry] == [
        AiTelemetryStatus.BUDGET_EXCEEDED if overrun else AiTelemetryStatus.OK,
    ]
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4"])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_literal_special_token_is_counted_before_cost_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest, model: str, method: str,
) -> None:
    tiktoken = pytest.importorskip("tiktoken")
    from src.ai.context_tokenizers import resolve_token_estimator

    encoding = tiktoken.Encoding(
        name="synthetic_bytes",
        pat_str=r"(?s).+",
        mergeable_ranks={bytes([value]): value for value in range(256)},
        special_tokens={"<|endoftext|>": 256},
    )
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _name: encoding)
    resolve_token_estimator.cache_clear()
    request.addfinalizer(resolve_token_estimator.cache_clear)
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    _FakeAzureOpenAI.actions = [_FakeResponse("{}", prompt_tokens=1, completion_tokens=1)]
    trace = AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test")
    client = AIClient(
        model, 0.2, 1.0, endpoint="https://example.openai.azure.com",
        api_key="synthetic", trace_context=trace,
    )
    system = "system"
    user = "<|endoftext|> " + "\u6f22\u8a9e\U0001f642" * 24
    text = f"{system}\n{user}"
    expected = len(encoding.encode_ordinary(text))
    heuristic = max(1, len(text) // 4)
    assert expected > heuristic
    ceiling = (client._estimate_cost(expected, 10) + client._estimate_cost(heuristic, 10)) / 2
    client._run_cost_guard = CostGuard(
        edition=trace.edition, run_id=trace.run_id, budget_usd=ceiling, programs_root=tmp_path,
    )

    with pytest.raises(BudgetExceeded, match="would exceed"):
        if method == "chat":
            client.chat(system, user, max_tokens=10)
        else:
            client.structured(system, user, parser=lambda payload: payload, max_tokens=10)

    assert _FakeAzureOpenAI.create_calls == 0
    assert client._run_cost_guard.current_state().reserved_usd == 0


def test_ai_client_throttles_requests_per_minute(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path)
    _FakeAzureOpenAI.actions = [
        _FakeResponse("first", prompt_tokens=10, completion_tokens=5),
        _FakeResponse("second", prompt_tokens=10, completion_tokens=5),
    ]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    AIRequestRouter._RATE_LIMIT_WINDOWS.clear()

    current_time = {"value": 1000.0}
    sleep_calls: list[float] = []

    def _sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        current_time["value"] += seconds

    client = AIClient(
        "summary-model",
        0.2,
        0.5,
        requests_per_minute=1,
        sleep_func=_sleep,
        time_func=lambda: current_time["value"],
        trace_context=AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test"),
    )

    assert client.chat("system", "user") == "first"
    assert client.chat("system", "user") == "second"
    assert sleep_calls == [60.0]


def test_ai_client_surfaces_non_retryable_errors(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path)
    _FakeAzureOpenAI.actions = [RuntimeError("bad request")]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    client = AIClient(
        "summary-model",
        0.2,
        0.5,
        trace_context=AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test"),
    )

    with pytest.raises(AIClientError, match="bad request"):
        client.chat("system", "user")


def test_ai_client_structured_returns_typed_payload_and_sets_json_response_format(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path)
    _FakeAzureOpenAI.actions = [
        _FakeResponse('{"summary": "grounded text", "risk": "high"}', prompt_tokens=200, completion_tokens=60),
    ]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    client = AIClient(
        "structured-model",
        0.2,
        0.5,
        trace_context=AITraceContext(edition="acme_weekly", run_id=_unique_run_id(), caller="test"),
    )
    result = client.structured(
        "system",
        "user",
        parser=lambda payload: (payload["summary"], payload["risk"]),
        prompt_version="structured.v1",
    )

    assert result == ("grounded text", "high")
    assert _FakeAzureOpenAI.last_request_kwargs is not None
    assert _FakeAzureOpenAI.last_request_kwargs["response_format"] == {"type": "json_object"}
    assert client.usage_stats.total_tokens == 260


def test_ai_client_emits_llm_trace_record_when_trace_context_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    _FakeAzureOpenAI.actions = [_FakeResponse("traced output", prompt_tokens=200, completion_tokens=60)]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    trace_file = tmp_path / "llm_trace.jsonl"
    client = AIClient(
        "trace-model",
        0.2,
        0.5,
        trace_context=AITraceContext(
            edition="acme_weekly",
            run_id="run-123",
            caller="src.commands.report._synthesize_v2_ai_content",
            trace_file=trace_file,
            metadata={"task": "report"},
        ),
    )

    assert client.chat("system", "user", prompt_version="trace.v1") == "traced output"

    record = json.loads(trace_file.read_text(encoding="utf-8").strip())
    assert record["edition"] == "acme_weekly"
    assert record["run_id"] == "run-123"
    assert record["caller"] == "src.commands.report._synthesize_v2_ai_content"
    assert record["model"] == "trace-model"
    assert record["deployment"] == "trace-model"
    assert record["prompt_version"] == "trace.v1"
    assert record["prompt_tokens"] == 200
    assert record["completion_tokens"] == 60
    assert record["total_tokens"] == 260
    assert record["cost_usd"] == pytest.approx(0.000115)
    assert record["metadata"] == {"task": "report"}
    assert record["latency_ms"] >= 0.0
    telemetry = read_ai_telemetry("acme_weekly", programs_root=tmp_path / "telemetry")
    assert len(telemetry) == 1
    assert telemetry[0].deployment_id == "trace-model"


def test_ai_client_blocks_frontier_calls_in_observe_only_mode_and_writes_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        "src.ai.ai_mode.load_ai_request_router_policy",
        lambda: AIRequestRouterPolicy(observe_only=True),
    )
    _FakeAzureOpenAI.actions = [_FakeResponse("should not run", prompt_tokens=10, completion_tokens=5)]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    trace_file = tmp_path / "llm_trace.jsonl"
    client = AIClient(
        "trace-model",
        0.2,
        0.5,
        trace_context=AITraceContext(
            edition="acme_weekly",
            run_id="run-123",
            caller="unit-test",
            trace_file=trace_file,
        ),
    )

    with pytest.raises(AIClientError, match="observe-only mode"):
        client.chat("system", "user", prompt_version="observe.v1")

    assert _FakeAzureOpenAI.create_calls == 0
    records = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["error"] == "AI request router is in observe-only mode; frontier calls are blocked."
    assert records[0]["prompt_version"] == "observe.v1"
    assert "cost_reservation_id" not in records[0]
    telemetry = read_ai_telemetry("acme_weekly", programs_root=tmp_path / "telemetry")
    assert len(telemetry) == 1
    assert telemetry[0].feature == "unit-test"


def test_ai_client_enforces_shared_run_budget_via_trace_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    _FakeAzureOpenAI.actions = [
        _FakeResponse("first", prompt_tokens=1000, completion_tokens=400),
        _FakeResponse("second", prompt_tokens=1000, completion_tokens=400),
    ]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )
    import uuid
    unique_run_id = f"run-guarded-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path)

    trace_context = AITraceContext(
        edition="acme_weekly",
        run_id=unique_run_id,
        caller="src.commands.report._synthesize_v2_ai_content",
        metadata={"run_budget_usd": 0.001},
    )
    first_client = AIClient("shared-budget-model", 0.2, 0.5, trace_context=trace_context)
    second_client = AIClient("shared-budget-model", 0.2, 0.5, trace_context=trace_context)

    # RELO-W1.4's worst-case reservation reserves for the full requested
    # max_tokens before dispatch; a small explicit max_tokens keeps both
    # calls' pre-flight reservations tiny so each is admitted, letting the
    # *actual* usage the fake response returns (regardless of what was
    # requested) be what pushes cumulative real spend over budget — the
    # post-hoc path this test exists to verify, not the pre-flight one.
    assert first_client.chat("system", "user", max_tokens=10) == "first"
    with pytest.raises(BudgetExceeded, match="actual AI spend exceeded the run ceiling"):
        second_client.chat("system", "user", max_tokens=10)

    run_state = CostGuard(
        edition="acme_weekly",
        run_id=unique_run_id,
        budget_usd=0.001,
        programs_root=tmp_path,
    ).current_state()
    assert run_state.spent_usd == pytest.approx(0.0014)
    assert run_state.ai_calls == 2


def test_ai_clients_satisfy_llm_provider_protocol(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    _FakeAzureOpenAI.actions = [_FakeResponse("protocol ok", prompt_tokens=10, completion_tokens=5)]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    direct_client = AIClient("protocol-model", 0.2, 0.5)
    fallback_client = FallbackAIClient(
        deployments=("protocol-model",),
        temperature=0.2,
        budget_usd=0.5,
        client_factory=lambda **_kwargs: direct_client,
    )

    assert isinstance(direct_client, LLMProvider)
    assert isinstance(fallback_client, LLMProvider)


def test_fallback_ai_client_passes_trace_context_to_runtime_clients() -> None:
    seen_trace_contexts: list[object] = []
    seen_requests_per_minute: list[int | None] = []

    class _RuntimeClient:
        def __init__(
            self,
            *,
            deployment: str,
            temperature: float,
            budget_usd: float,
            requests_per_minute: int | None = None,
            trace_context=None,
        ) -> None:
            del deployment, temperature, budget_usd
            seen_trace_contexts.append(trace_context)
            seen_requests_per_minute.append(requests_per_minute)

        def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None) -> str:
            del system, user, max_tokens, prompt_version
            return "ok"

    trace_context = AITraceContext(
        edition="acme",
        run_id="acme:summarize:all:20260510T120000Z",
        caller="src.commands.summarize.summarize_program",
        metadata={"run_budget_usd": 0.5},
    )
    client = FallbackAIClient(
        deployments=("summary-primary",),
        temperature=0.2,
        budget_usd=0.5,
        requests_per_minute=7,
        trace_context=trace_context,
        client_factory=lambda **kwargs: cast(LLMProvider, _RuntimeClient(**kwargs)),
    )

    assert client.chat("system", "user") == "ok"
    assert seen_trace_contexts == [trace_context]
    assert seen_requests_per_minute == [7]


def test_resolve_ai_deployments_supports_vertex_env_aliases(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-primary")
    monkeypatch.setenv("VERTEX_AI_BACKUP_DEPLOYMENT", "vertex-ai-backup")

    deployments = resolve_ai_deployments(
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )

    assert deployments == ("vertex-ai-primary", "vertex-ai-backup")


def test_resolve_ai_deployments_supports_vertex_exec_env_aliases(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_EXEC_DEPLOYMENT", "vertex-exec-primary")
    monkeypatch.setenv("VERTEX_EXEC_BACKUP_DEPLOYMENT", "vertex-exec-backup")

    deployments = resolve_ai_deployments(
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_EXEC_DEPLOYMENT",),
        backup_fallback_envs=("VERTEX_EXEC_BACKUP_DEPLOYMENT",),
    )

    assert deployments == ("vertex-exec-primary", "vertex-exec-backup")


def test_resolve_ai_deployments_for_feature_prefers_tier_specific_aliases(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_AI_MINI_DEPLOYMENT", "vertex-ai-mini")
    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-generic")
    monkeypatch.setenv("VERTEX_AI_MINI_BACKUP_DEPLOYMENT", "vertex-ai-mini-backup")
    monkeypatch.setenv("VERTEX_AI_BACKUP_DEPLOYMENT", "vertex-ai-generic-backup")
    monkeypatch.setattr(
        "src.ai.deployment_fallback.load_ai_feature_policy",
        lambda feature_name: AIFeaturePolicy(
            max_tokens=200,
            temperature=0.0,
            model_tier="mini",
            frontier_eligible=True,
        ),
    )

    deployments = resolve_ai_deployments_for_feature(
        feature_name="summary_generator",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )

    assert deployments == ("vertex-ai-mini", "vertex-ai-mini-backup")


def test_resolve_ai_deployments_for_feature_falls_back_to_generic_aliases(monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_AI_PREMIUM_DEPLOYMENT", raising=False)
    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-generic")
    monkeypatch.setattr(
        "src.ai.deployment_fallback.load_ai_feature_policy",
        lambda feature_name: AIFeaturePolicy(
            max_tokens=200,
            temperature=0.0,
            model_tier="premium",
            frontier_eligible=True,
        ),
    )

    deployments = resolve_ai_deployments_for_feature(
        feature_name="exec_summary_drafter",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=(),
    )

    assert deployments == ("vertex-ai-generic",)


def test_resolve_ai_deployments_for_feature_returns_empty_when_frontier_disabled(
    monkeypatch,
) -> None:
    """Phase 4 frontier_eligible enforcement: if a feature's
    `frontier_eligible: false`, the deployment resolver returns an
    empty tuple regardless of which deployments are configured in
    the environment. The empty tuple signals to the caller
    (typically `FallbackAIClient` / `client_factory`) that no AI
    deployment is available, which forces the feature onto its
    deterministic fallback path.

    This is the per-feature kill switch promised in
    `ai_policy.yaml`: setting `frontier_eligible: false` makes
    the feature a hard-no-frontier call, enforced at the
    earliest possible point in the call chain.
    """
    # Set a real deployment in the env so we can prove the policy
    # is what blanks the result (not a missing deployment).
    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-generic")
    monkeypatch.setenv("VERTEX_AI_BACKUP_DEPLOYMENT", "vertex-ai-generic-backup")
    monkeypatch.setattr(
        "src.ai.deployment_fallback.load_ai_feature_policy",
        lambda feature_name: AIFeaturePolicy(
            max_tokens=200,
            temperature=0.0,
            model_tier="standard",
            frontier_eligible=False,
        ),
    )

    deployments = resolve_ai_deployments_for_feature(
        feature_name="claim_extractor",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )

    assert deployments == (), (
        "frontier_eligible=False must return an empty deployment "
        "tuple so the caller falls back to the deterministic path."
    )


def test_resolve_ai_deployments_for_feature_returns_empty_for_default_policy_too(
    monkeypatch,
) -> None:
    """The enforcement applies to any feature, including the
    `default` policy. This means a feature that is not in
    `ai_features` and falls back to the `default` policy is
    still subject to the kill switch — the only way to bypass
    the enforcement is to remove the policy file (or call
    `resolve_ai_deployments` directly, which is the no-policy
    path)."""
    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-generic")
    monkeypatch.setattr(
        "src.ai.deployment_fallback.load_ai_feature_policy",
        lambda feature_name: AIFeaturePolicy(
            max_tokens=500,
            temperature=0.2,
            model_tier="standard",
            frontier_eligible=False,
        ),
    )

    deployments = resolve_ai_deployments_for_feature(
        feature_name="unknown_feature_falls_back_to_default",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=(),
    )
    assert deployments == ()
