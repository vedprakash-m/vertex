from __future__ import annotations

import json
import sqlite3
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import date, datetime, timezone
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from src.ai.ai_mode import AIMode
from src.ai.claim_extractor import ClaimExtractor, ClaimExtractorBudgetError
from src.ai.client import AIClient, AIClientError, AIPreflightError, BudgetConfigurationError, BudgetExceeded, BudgetStateError
from src.ai.cost_guard import CostGuard
from src.ai.deployment_fallback import FallbackAIClient, FallbackStructuredClient, resolve_ai_deployments
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.ai.structured_feature_runner import BatchProviderResult, FeatureOutcome, FeatureOutcomeStatus
from src.ai.tiered_router import RouteOutcome, Tier
from src.commands.confirm_stages.claim_resolution import resolve_confirm_claim_extraction
from src.core.exceptions import StateError
from src.core.ledger.event_log import read_events
from tests.unit.test_structured_feature_runner import (
    _ceiling_counts,
    _dispatch_frontier,
    _frontier_test_spec,
    _runner,
)
from tests.unit.test_ai_client import (
    _FakeAzureOpenAI,
    _FakeRateLimitError,
    _FakeResponse,
    _retry_reservation_client,
    _retry_reservation_rows,
)


@pytest.fixture(autouse=True)
def synthetic_explicit_fallback_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.ai.client as ai_client

    monkeypatch.setattr(
        ai_client,
        "load_ai_model_cost_policy_exact",
        lambda deployment: ai_client.load_ai_model_cost_policy(deployment),
    )


def test_deployment_resolution_deduplicates_identical_backup() -> None:
    assert resolve_ai_deployments(
        primary_candidates=("shared",),
        backup_candidates=("shared",),
        primary_fallback_envs=(),
        backup_fallback_envs=(),
    ) == ("shared",)


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("budget_at", ["primary", "backup"])
@pytest.mark.parametrize("origin", ["factory", "provider"])
@pytest.mark.parametrize("custom_retry", [False, True])
@pytest.mark.parametrize("error_type", [BudgetExceeded, BudgetConfigurationError])
def test_budget_exhaustion_stops_deployment_fallback(
    client_class, method, budget_at, origin, custom_retry, error_type,
) -> None:
    budget_error = error_type("synthetic budget refusal")
    constructed: list[str] = []
    invoked: list[str] = []

    def factory(*, deployment, **kwargs):
        constructed.append(deployment)
        if deployment == budget_at and origin == "factory":
            raise budget_error
        provider = Mock()

        def call(*args, **kwargs):
            invoked.append(deployment)
            if deployment == budget_at:
                raise budget_error
            if deployment == "primary" and budget_at == "backup":
                raise AIClientError("synthetic transient failure")
            return "must not be returned"

        getattr(provider, method).side_effect = call
        return provider

    client = client_class(
        deployments=("primary", "backup", "third"),
        temperature=0.0,
        budget_usd=1.0,
        client_factory=factory,
        retryable_exceptions=(Exception,) if custom_retry else None,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(error_type) as caught:
        getattr(client, method)("system", "user", **arguments)

    assert caught.value is budget_error
    expected = ["primary"] if budget_at == "primary" else ["primary", "backup"]
    assert constructed == expected
    assert invoked == (expected[:-1] if origin == "factory" else expected)


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_other_retryable_failures_still_use_backup(client_class, method) -> None:
    constructed: list[str] = []

    def factory(*, deployment, **kwargs):
        constructed.append(deployment)
        provider = Mock()
        getattr(provider, method).side_effect = (
            AIClientError("synthetic transient failure") if deployment == "primary" else None
        )
        getattr(provider, method).return_value = "backup response"
        return provider

    client = client_class(
        deployments=("primary", "backup"),
        temperature=0.0,
        budget_usd=1.0,
        client_factory=factory,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    assert getattr(client, method)("system", "user", **arguments) == "backup response"
    assert constructed == ["primary", "backup"]


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_pre_dispatch_feature_budget_factory_failure_does_not_block_reuse(client_class, method) -> None:
    constructed: list[str] = []

    def factory(*, deployment, **kwargs):
        constructed.append(deployment)
        if deployment == "primary" and constructed.count("primary") == 1:
            raise AIClientError("synthetic construction failure")
        provider = Mock()
        getattr(provider, method).return_value = f"{deployment} response"
        return provider

    client = client_class(
        deployments=("primary", "backup"),
        temperature=0.0,
        budget_usd=1.0,
        client_factory=factory,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    assert getattr(client, method)("system", "user", **arguments) == "backup response"
    assert getattr(client, method)("system", "user", **arguments) == "primary response"
    assert getattr(client, method)("system", "user", **arguments) == "primary response"
    assert constructed == ["primary", "backup", "primary"]


def _token_budget_client(
    tmp_path, monkeypatch, *, estimate, custom_retry, budget=1.0, run_budget=None,
    default_factory=False, explicit_builtin_factory=False, client_class=FallbackAIClient, trace_metadata=None,
):
    estimator = Mock()
    estimator.estimate.return_value = estimate
    monkeypatch.setattr("src.ai.client.resolve_token_estimator", Mock(return_value=estimator))
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    trace_writer = Mock()
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", trace_writer)
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    clients = []

    def factory(**kwargs):
        created = AIClient(**kwargs)
        clients.append(created)
        return created

    factory_mock = Mock(side_effect=factory)
    trace = AITraceContext(
        edition="acme_weekly", run_id="token-budget", caller="test",
        metadata={
            "ai_feature": "risk_proposal_generator",
            **({} if run_budget is None else {"run_budget_usd": run_budget}),
            **(trace_metadata or {}),
        },
    )
    client = client_class(
        deployments=("summary-model", "summary-backup"), temperature=0.2, budget_usd=budget,
        trace_context=trace,
        client_factory=AIClient if explicit_builtin_factory else (None if default_factory else factory_mock),
        retryable_exceptions=(Exception,) if custom_retry else None,
    )
    return client, clients, factory_mock, router, trace_writer, estimator


_CHAIN_ATTEMPT_USD = 0.0002 + 0.00125  # default model price for 1k prompt + 1k output tokens


def _chain_run_state():
    return CostGuard(edition="acme_weekly", run_id="token-budget", budget_usd=1.0).current_state()


@pytest.mark.parametrize("default_factory", [False, True])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_fallback_chain_reserves_worst_case_once_before_first_dispatch(
    tmp_path, monkeypatch, method, default_factory,
) -> None:
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=1000, custom_retry=False, run_budget=1.0, default_factory=default_factory,
    )
    reserved_at_dispatch: list[float] = []
    response = router.route.return_value

    def route(**kwargs):
        reserved_at_dispatch.append(_chain_run_state().reserved_usd)
        return response

    router.route.side_effect = route
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    getattr(client, method)("system", "user", max_tokens=1000, **arguments)

    attempts = (AIClient.MAX_RETRIES + 1) * 2
    assert reserved_at_dispatch == pytest.approx([attempts * _CHAIN_ATTEMPT_USD])
    state = _chain_run_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, 0.0, 1))


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_fallback_chain_refuses_before_dispatch_when_worst_case_exceeds_ceiling(
    tmp_path, monkeypatch, method,
) -> None:
    short = 5 * _CHAIN_ATTEMPT_USD
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=1000, custom_retry=True, default_factory=True, budget=1.0, run_budget=short,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetExceeded):
        getattr(client, method)("system", "user", max_tokens=1000, **arguments)

    router.route.assert_not_called()
    assert _chain_run_state().reserved_usd == 0.0


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_fallback_chain_reserves_every_configured_deployment_occurrence(
    tmp_path, monkeypatch, method,
) -> None:
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=1000, custom_retry=True, default_factory=True,
        run_budget=5 * _CHAIN_ATTEMPT_USD,
    )
    client._deployments = ("summary-model", "summary-model")
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetExceeded):
        getattr(client, method)("system", "user", max_tokens=1000, **arguments)

    router.route.assert_not_called()
    assert _chain_run_state().reserved_usd == 0.0


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_fallback_chain_reserves_worst_case_against_feature_ceiling(
    tmp_path, monkeypatch, method,
) -> None:
    feature_ceiling = 5 * _CHAIN_ATTEMPT_USD
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path,
        monkeypatch,
        estimate=1000,
        custom_retry=True,
        default_factory=True,
        budget=feature_ceiling,
        run_budget=1.0,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetExceeded, match="feature ceiling"):
        getattr(client, method)("system", "user", max_tokens=1000, **arguments)

    router.route.assert_not_called()
    assert _chain_run_state().reserved_usd == 0.0


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_fallback_chain_refuses_missing_feature_before_dispatch(
    tmp_path, monkeypatch, method,
) -> None:
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path,
        monkeypatch,
        estimate=1000,
        custom_retry=True,
        default_factory=True,
        run_budget=1.0,
        trace_metadata={"ai_feature": None},
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetConfigurationError, match="feature"):
        getattr(client, method)("system", "user", max_tokens=1000, **arguments)

    router.route.assert_not_called()
    assert _chain_run_state().reserved_usd == 0.0


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_fallback_chain_refuses_unknown_backup_price_before_primary_dispatch(tmp_path, monkeypatch, method) -> None:
    from src.core.exceptions import ConfigError
    from src.core.policy_loader import load_ai_model_cost_policy

    def pricing(deployment):
        if deployment == "summary-backup":
            raise ConfigError("synthetic missing price")
        return load_ai_model_cost_policy(deployment)

    monkeypatch.setattr("src.ai.client.load_ai_model_cost_policy_exact", pricing)
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=1000, custom_retry=True, run_budget=1.0,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetConfigurationError):
        getattr(client, method)("system", "user", max_tokens=1000, **arguments)

    router.route.assert_not_called()


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_fallback_chain_refuses_unknown_feature_before_dispatch(tmp_path, monkeypatch, method) -> None:
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=1000, custom_retry=True, run_budget=1.0,
        trace_metadata={"ai_feature": "synthetic-unknown-feature"},
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetConfigurationError, match="feature"):
        getattr(client, method)("system", "user", max_tokens=1000, **arguments)

    router.route.assert_not_called()
    assert _chain_run_state().reserved_usd == 0.0


def test_fallback_cost_telemetry_tracks_resolved_attempts_and_reservations(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.ai_telemetry import append_ai_telemetry, read_ai_telemetry
    from src.core.policy_loader import AIModelCostPolicy

    programs_root = tmp_path / "programs"
    trace_file = tmp_path / "llm-trace.jsonl"
    telemetry_root = tmp_path / "telemetry"
    price = AIModelCostPolicy(
        input_cost_per_1k_tokens=1.0,
        output_cost_per_1k_tokens=2.0,
    )
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _deployment: price,
    )
    monkeypatch.setattr(
        "src.ai.client.resolve_token_estimator",
        lambda _deployment: SimpleNamespace(estimate=lambda _text: 10),
    )
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.core.ai_telemetry.append_ai_telemetry",
        lambda record: append_ai_telemetry(
            record,
            programs_root=telemetry_root,
        ),
    )
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")

    class RetryingPrimaryRouter:
        def __init__(self) -> None:
            self.primary_failed = False

        def route(self, **kwargs):
            if (
                kwargs["deployment"] == "resolved-primary"
                and not self.primary_failed
            ):
                self.primary_failed = True
                kwargs["before_retry"]()
                kwargs["before_retry"]()
                raise RuntimeError("synthetic provider failure")
            return _FakeResponse("ok", prompt_tokens=5, completion_tokens=7)

    monkeypatch.setattr(
        "src.ai.client.AIRequestRouter",
        Mock(return_value=RetryingPrimaryRouter()),
    )
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="resolved-attempts",
        caller="test",
        trace_file=trace_file,
        metadata={
            "program_id": "acme_weekly",
            "feature": "action_extractor",
            "ai_feature": "action_extractor",
            "run_budget_usd": 10.0,
        },
    )
    client = FallbackAIClient(
        deployments=("resolved-primary", "resolved-backup"),
        temperature=0.2,
        budget_usd=10.0,
        trace_context=trace,
        client_factory=lambda **kwargs: AIClient(**kwargs),
    )
    private_system = "synthetic-private-system"
    private_user = "synthetic-private-user"

    assert client.chat(
        private_system,
        private_user,
        max_tokens=10,
        prompt_version="test.v1",
    ) == "ok"

    telemetry = read_ai_telemetry(
        "acme_weekly",
        programs_root=telemetry_root,
    )
    assert [record.resolved_deployment for record in telemetry] == [
        "resolved-primary",
        "resolved-primary",
        "resolved-primary",
        "resolved-backup",
    ]
    assert [record.attempt_index for record in telemetry] == [1, 2, 3, 4]
    assert [record.outcome for record in telemetry] == [
        "retry",
        "retry",
        "failed",
        "succeeded",
    ]
    assert [record.run_id for record in telemetry] == ["resolved-attempts"] * 4
    assert all(record.cost_reservation_id is not None for record in telemetry)
    assert all(record.estimated_cost_usd is not None for record in telemetry)
    assert telemetry[-1].tokens_in == 5
    assert telemetry[-1].tokens_out == 7

    traces = [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["resolved_deployment"] for record in traces] == [
        "resolved-primary",
        "resolved-primary",
        "resolved-primary",
        "resolved-backup",
    ]
    assert [record["attempt_index"] for record in traces] == [1, 2, 3, 4]
    assert [record["outcome"] for record in traces] == [
        "retry",
        "retry",
        "failed",
        "succeeded",
    ]
    assert private_system not in trace_file.read_text(encoding="utf-8")
    assert private_user not in trace_file.read_text(encoding="utf-8")
    serialized_telemetry = (
        telemetry_root / "acme_weekly" / "_state" / "ai_telemetry.jsonl"
    ).read_text(encoding="utf-8")
    assert private_system not in serialized_telemetry
    assert private_user not in serialized_telemetry

    guard = CostGuard(
        edition="acme_weekly",
        run_id="resolved-attempts",
        budget_usd=10.0,
        programs_root=programs_root,
    )
    with sqlite3.connect(guard.ledger_path) as connection:
        reservations = connection.execute(
            "SELECT reservation_id, status "
            "FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall()
    assert [row[1] for row in reservations] == [
        "reserved",
        "reserved",
        "reserved",
        "settled",
    ]
    assert [record.cost_reservation_id for record in telemetry] == [
        row[0] for row in reservations
    ]


def test_fallback_chain_retains_uncertain_attempt_and_releases_remainder(tmp_path, monkeypatch) -> None:
    client, _clients, _factory, router, _trace_writer, _estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=1000, custom_retry=False, run_budget=1.0,
    )
    response = router.route.return_value
    router.route.side_effect = [AIClientError("synthetic uncertain outcome"), response]

    client.chat("system", "user", max_tokens=1000)

    state = _chain_run_state()
    assert (state.spent_usd, state.reserved_usd, state.ai_calls) == pytest.approx((0.0, _CHAIN_ATTEMPT_USD, 1))


@pytest.mark.parametrize(
    "invalid",
    [-1, True, 1.5, 0.0, "synthetic-private-count", None, float("nan"), float("inf"), 10**400],
    ids=["negative", "boolean", "fractional", "float-zero", "string", "missing", "nan", "infinity", "overflow"],
)
@pytest.mark.parametrize("field", ["max_tokens", "estimated_prompt_tokens"])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_pre_dispatch_token_budget_rejects_invalid_counts(
    tmp_path, monkeypatch, invalid, field, method,
) -> None:
    estimate = invalid if field == "estimated_prompt_tokens" else 50
    output = invalid if field == "max_tokens" else 10
    client, clients, factory, router, trace_writer, estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=estimate, custom_retry=True,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetConfigurationError, match="token|cost") as caught:
        getattr(client, method)("synthetic-system", "synthetic-user", max_tokens=output, **arguments)

    assert "synthetic-private-count" not in str(caught.value)
    assert len(clients) == 1
    factory.assert_called_once()
    router.route.assert_not_called()
    trace_writer.assert_not_called()
    assert not (tmp_path / "programs").exists()
    assert clients[0].spent_usd == 0.0 and clients[0].usage_stats.call_count == 0
    if field == "max_tokens" and invalid != 10**400:
        estimator.estimate.assert_not_called()


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("overflow", ["component", "sum"])
def test_pre_dispatch_token_budget_rejects_nonfinite_computed_cost(
    tmp_path, monkeypatch, method, overflow,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(
            input_cost_per_1k_tokens=1e308,
            output_cost_per_1k_tokens=1e308 if overflow == "sum" else 0.0,
        ),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=1000 if overflow == "sum" else 2000, custom_retry=True,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetConfigurationError, match="cost"):
        getattr(client, method)(
            "synthetic-system", "synthetic-user", max_tokens=1000 if overflow == "sum" else 10, **arguments,
        )

    assert len(clients) == 1
    factory.assert_called_once()
    router.route.assert_not_called()
    trace_writer.assert_not_called()
    assert not (tmp_path / "programs").exists()


@pytest.mark.parametrize("field", ["max_tokens", "estimated_prompt_tokens"])
def test_pre_dispatch_token_budget_preserves_existing_cost_state(
    tmp_path, monkeypatch, field,
) -> None:
    estimate = -1 if field == "estimated_prompt_tokens" else 50
    output = -1 if field == "max_tokens" else 10
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=estimate, custom_retry=True,
    )
    primary = client._get_client("summary-model")
    guard = clients[0]._run_cost_guard
    guard.record_actual(0.125)
    state_before = guard.current_state()
    ledger_before = guard.ledger_path.read_bytes()
    projection_before = guard.state_path.read_bytes()
    reserve = Mock(wraps=guard.reserve_call)
    monkeypatch.setattr(guard, "reserve_call", reserve)

    with pytest.raises(BudgetConfigurationError, match="token"):
        primary.chat("synthetic-system", "synthetic-user", max_tokens=output)

    reserve.assert_not_called()
    assert guard.current_state() == state_before
    assert guard.ledger_path.read_bytes() == ledger_before
    assert guard.state_path.read_bytes() == projection_before
    factory.assert_called_once()
    router.route.assert_not_called()
    trace_writer.assert_not_called()


@pytest.mark.parametrize("gate", ["exhausted", "observe_only"])
def test_pre_dispatch_token_budget_preserves_existing_gate_precedence(
    tmp_path, monkeypatch, gate,
) -> None:
    client, clients, factory, router, trace_writer, estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=-1, custom_retry=True,
    )
    primary = client._get_client("summary-model")
    if gate == "exhausted":
        clients[0]._spent_usd = 1.0
        expected = BudgetExceeded
    else:
        monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.OBSERVE_ONLY)
        expected = AIClientError

    with pytest.raises(expected) as caught:
        primary.chat("synthetic-system", "synthetic-user", max_tokens=-1)

    assert type(caught.value) is expected
    estimator.estimate.assert_not_called()
    factory.assert_called_once()
    router.route.assert_not_called()
    assert trace_writer.call_count == (1 if gate == "observe_only" else 0)
    assert not (tmp_path / "programs").exists()


@pytest.mark.parametrize("field", ["max_tokens", "estimated_prompt_tokens"])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_pre_dispatch_token_budget_preserves_valid_zero(
    tmp_path, monkeypatch, field, method,
) -> None:
    estimate = 0 if field == "estimated_prompt_tokens" else 50
    output = 0 if field == "max_tokens" else 10
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=estimate, custom_retry=True,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    result = getattr(client, method)("synthetic-system", "synthetic-user", max_tokens=output, **arguments)

    assert result == ({} if method == "structured" else "{}")
    factory.assert_called_once()
    router.route.assert_called_once()
    assert router.route.call_args.kwargs["max_tokens"] == output
    trace_writer.assert_called_once()
    state = clients[0]._run_cost_guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.0 and state.ai_calls == 1


@pytest.mark.parametrize("field", ["max_tokens", "estimated_prompt_tokens"])
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("custom_retry", [False, True])
def test_pre_dispatch_token_budget_stops_runner_and_fallback(
    tmp_path, monkeypatch, field, batch, custom_retry,
) -> None:
    estimate = -1 if field == "estimated_prompt_tokens" else 50
    output = -1 if field == "max_tokens" else 10
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=estimate, custom_retry=custom_retry,
    )
    provider = Mock(
        side_effect=(
            (lambda *_: client.structured("synthetic-system", "synthetic-user", parser=lambda value: value, max_tokens=output))
            if batch else (lambda *_: client.chat("synthetic-system", "synthetic-user", max_tokens=output))
        ),
    )
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE,
        limitations=("existing limitation",), retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.ABSTAINED for outcome in outcomes)
    assert all(
        outcome.limitations == ("existing limitation", "budget_configuration_invalid") and not outcome.retryable
        for outcome in outcomes
    )
    provider.assert_called_once()
    factory.assert_called_once()
    router.route.assert_not_called()
    trace_writer.assert_not_called()
    assert len(clients) == 1 and clients[0].usage_stats.call_count == 0
    assert not (tmp_path / "programs").exists()
    assert _ceiling_counts(
        tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1",
    ) == {"remote": 1, "frontier": 1}


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("already_spent", [False, True])
def test_pre_dispatch_feature_budget_rejects_estimated_excess(
    tmp_path, monkeypatch, method, already_spent,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=0.25, run_budget=1.0,
    )
    if already_spent:
        router.route.return_value.usage.completion_tokens = 125
        primary = cast(AIClient, client._get_client("summary-model"))
        assert primary.chat("synthetic-system", "synthetic-user", max_tokens=125) == "{}"
        guard = clients[0]._run_cost_guard
        state_before = guard.current_state()
        ledger_before = guard.ledger_path.read_bytes()
        projection_before = guard.state_path.read_bytes()
        router.route.reset_mock()
        trace_writer.reset_mock()
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetExceeded, match="feature"):
        getattr(client, method)(
            "synthetic-system", "synthetic-user", max_tokens=126 if already_spent else 251, **arguments,
        )

    factory.assert_called_once()
    router.route.assert_not_called()
    trace_writer.assert_not_called()
    assert len(clients) == 1
    if already_spent:
        assert clients[0].spent_usd == 0.125
        assert guard.current_state() == state_before
        assert guard.ledger_path.read_bytes() == ledger_before
        assert guard.state_path.read_bytes() == projection_before
    else:
        assert clients[0].spent_usd == 0.0
        assert not (tmp_path / "programs").exists()


@pytest.mark.parametrize("already_spent", [False, True])
def test_pre_dispatch_feature_budget_allows_exact_remaining(
    tmp_path, monkeypatch, already_spent,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    # RELO-W1.4: the run ceiling must admit the whole-chain worst case (3 attempts x 2 deployments).
    budget = 1.625 if already_spent else 1.5
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=budget, run_budget=10.0,
    )
    tokens = 125 if already_spent else 250
    router.route.return_value.usage.completion_tokens = tokens
    if already_spent:
        primary = cast(AIClient, client._get_client("summary-model"))
        assert primary.chat("synthetic-system", "synthetic-user", max_tokens=tokens) == "{}"

    assert client.structured(
        "synthetic-system", "synthetic-user", parser=lambda value: value, max_tokens=tokens,
    ) == {}

    factory.assert_called_once()
    assert router.route.call_count == (2 if already_spent else 1)
    assert trace_writer.call_count == (2 if already_spent else 1)
    assert clients[0].spent_usd == 0.25
    state = clients[0]._run_cost_guard.current_state()
    assert state.spent_usd == 0.25 and state.reserved_usd == 0.0
    assert state.ai_calls == (2 if already_spent else 1)


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_pre_dispatch_feature_budget_counts_same_client_inflight(
    tmp_path, monkeypatch, method,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=3.6, run_budget=10.0,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    entered, release = Event(), Event()
    response = router.route.return_value

    def route(**kwargs):
        if not entered.is_set():
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("synthetic provider release timed out")
        return response

    router.route.side_effect = route
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            getattr(primary, method), "synthetic-system", "synthetic-user", max_tokens=600, **arguments,
        )
        try:
            assert entered.wait(timeout=10)
            guard = clients[0]._run_cost_guard
            before = guard.current_state()
            ledger_before = guard.ledger_path.read_bytes()
            projection_before = guard.state_path.read_bytes()
            second = executor.submit(
                getattr(client, method), "synthetic-system", "synthetic-user", max_tokens=600, **arguments,
            )
            with pytest.raises(BudgetExceeded, match="feature"):
                second.result(timeout=10)
            assert guard.current_state() == before
            assert guard.ledger_path.read_bytes() == ledger_before
            assert guard.state_path.read_bytes() == projection_before
            assert router.route.call_count == 1
            trace_writer.assert_not_called()
        finally:
            release.set()
        assert first.result(timeout=10) == ({} if method == "structured" else "{}")

    assert getattr(client, method)(
        "synthetic-system", "synthetic-user", max_tokens=600, **arguments,
    ) == ({} if method == "structured" else "{}")
    factory.assert_called_once()
    assert router.route.call_count == 2 and trace_writer.call_count == 2
    assert primary.usage_stats.call_count == 2
    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.0 and state.ai_calls == 2


def test_pre_dispatch_feature_budget_allows_same_client_concurrency_at_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=3.6, run_budget=10.0,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    guard = clients[0]._run_cost_guard
    guard.record_actual(0.0, ai_calls=0)
    barrier = Barrier(2, timeout=10)
    response = router.route.return_value
    response.usage.completion_tokens = 125

    def route(**kwargs):
        barrier.wait()
        return response

    router.route.side_effect = route
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(primary.chat, "synthetic-system", "synthetic-user", max_tokens=500)
        second = executor.submit(primary.chat, "synthetic-system", "synthetic-user", max_tokens=500)
        assert first.result(timeout=15) == "{}"
        assert second.result(timeout=15) == "{}"

    factory.assert_called_once()
    assert router.route.call_count == 2 and trace_writer.call_count == 2
    assert primary.usage_stats.call_count == 2 and primary.usage_stats.completion_tokens == 250
    assert primary.spent_usd == 0.25
    state = guard.current_state()
    assert state.spent_usd == 0.25 and state.reserved_usd == 0.0 and state.ai_calls == 2


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_pre_dispatch_feature_budget_reuses_client_on_concurrent_first_call(
    tmp_path, monkeypatch, method,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=3.6, run_budget=10.0,
    )
    CostGuard(
        edition="acme_weekly", run_id="token-budget", budget_usd=10.0, programs_root=tmp_path / "programs",
    ).record_actual(0.0, ai_calls=0)
    first_lookups = Barrier(2, timeout=10)

    class SynchronizedFirstLookups(dict):
        def __init__(self):
            super().__init__()
            self.count = 0
            self.lock = Lock()

        def get(self, key, default=None):
            value = super().get(key, default)
            with self.lock:
                self.count += 1
                synchronize = self.count <= 2
            if synchronize:
                first_lookups.wait()
            return value

    client._clients = SynchronizedFirstLookups()
    entered, release = Event(), Event()
    response = router.route.return_value

    def route(**kwargs):
        if not entered.is_set():
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("synthetic provider release timed out")
        return response

    router.route.side_effect = route
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                getattr(client, method), "synthetic-system", "synthetic-user", max_tokens=600, **arguments,
            )
            for _ in range(2)
        ]
        try:
            assert entered.wait(timeout=10)
            completed, pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
            assert len(completed) == len(pending) == 1
            with pytest.raises(BudgetExceeded, match="feature"):
                next(iter(completed)).result()
            factory.assert_called_once()
            router.route.assert_called_once()
            trace_writer.assert_not_called()
        finally:
            release.set()
        assert next(iter(pending)).result(timeout=10) == ({} if method == "structured" else "{}")

    assert len(clients) == 1
    assert trace_writer.call_count == 1
    state = clients[0]._run_cost_guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.0 and state.ai_calls == 1


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_pre_dispatch_feature_budget_retains_same_client_uncertain_call(
    tmp_path, monkeypatch, method,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    router.route.side_effect = [TimeoutError("synthetic provider outcome unknown"), router.route.return_value]
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(AIClientError):
        getattr(primary, method)("synthetic-system", "synthetic-user", max_tokens=600, **arguments)
    guard = clients[0]._run_cost_guard
    before = guard.current_state()
    ledger_before = guard.ledger_path.read_bytes()
    projection_before = guard.state_path.read_bytes()
    assert before.spent_usd == 0.0 and before.reserved_usd == 0.6

    with pytest.raises(BudgetExceeded, match="feature"):
        getattr(client, method)("synthetic-system", "synthetic-user", max_tokens=600, **arguments)

    factory.assert_called_once()
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    assert guard.current_state() == before
    assert guard.ledger_path.read_bytes() == ledger_before
    assert guard.state_path.read_bytes() == projection_before


def test_pre_dispatch_feature_budget_replaces_settled_reservation_with_actual(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    first_response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=125),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    router.route.side_effect = [first_response, router.route.return_value]

    assert primary.chat("synthetic-system", "synthetic-user", max_tokens=600) == "{}"
    assert primary.spent_usd == 0.125
    assert primary.chat("synthetic-system", "synthetic-user", max_tokens=875) == "{}"

    factory.assert_called_once()
    assert router.route.call_count == 2 and trace_writer.call_count == 2
    state = clients[0]._run_cost_guard.current_state()
    assert state.spent_usd == 0.125 and state.reserved_usd == 0.0 and state.ai_calls == 2


def test_pre_dispatch_feature_budget_preserves_uncertain_settlement(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    guard = clients[0]._run_cost_guard
    router.route.return_value.usage.completion_tokens = 125
    monkeypatch.setattr(guard, "settle_call", Mock(side_effect=StateError("synthetic settlement failure")))

    with pytest.raises(BudgetStateError, match="settlement"):
        primary.chat("synthetic-system", "synthetic-user", max_tokens=600)
    before = guard.current_state()
    ledger_before = guard.ledger_path.read_bytes()
    projection_before = guard.state_path.read_bytes()
    assert primary.spent_usd == 0.125 and before.reserved_usd == 0.6

    with pytest.raises(BudgetStateError, match="settlement"):
        client.chat("synthetic-system", "synthetic-user", max_tokens=300)

    factory.assert_called_once()
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    assert guard.current_state() == before
    assert guard.ledger_path.read_bytes() == ledger_before
    assert guard.state_path.read_bytes() == projection_before


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("custom_retry", [False, True])
@pytest.mark.parametrize("explicit_builtin_factory", [False, True])
def test_pre_dispatch_feature_budget_counts_fallback_uncertain(
    tmp_path, monkeypatch, client_class, method, custom_retry, explicit_builtin_factory,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=custom_retry, budget=3.6, run_budget=10.0,
        default_factory=True, explicit_builtin_factory=explicit_builtin_factory, client_class=client_class,
    )
    router.route.side_effect = [TimeoutError("synthetic outcome unknown"), router.route.return_value]
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    assert getattr(client, method)(
        "synthetic-system", "synthetic-user", max_tokens=600, **arguments,
    ) == ({} if method == "structured" else "{}")

    assert set(client._clients) == {"summary-model", "summary-backup"}
    assert router.route.call_count == 2
    assert trace_writer.call_count == 2
    guard = cast(AIClient, client._clients["summary-model"])._run_cost_guard
    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.6 and state.ai_calls == 1
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations ORDER BY reservation_id"
        ).fetchall() == [("reserved",), ("settled",)]


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("explicit_builtin_factory", [False, True])
def test_pre_dispatch_feature_budget_allows_fallback_at_exact_remaining(
    tmp_path, monkeypatch, client_class, method, explicit_builtin_factory,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda deployment: SimpleNamespace(
            input_cost_per_1k_tokens=0.0,
            output_cost_per_1k_tokens=1.5 if deployment == "summary-backup" else 1.0,
        ),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=False, budget=3.0, run_budget=10.0,
        default_factory=True, explicit_builtin_factory=explicit_builtin_factory, client_class=client_class,
    )
    router.route.side_effect = [TimeoutError("synthetic outcome unknown"), router.route.return_value]
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    assert getattr(client, method)(
        "synthetic-system", "synthetic-user", max_tokens=400, **arguments,
    ) == ({} if method == "structured" else "{}")

    assert [call.kwargs["deployment"] for call in router.route.call_args_list] == [
        "summary-model", "summary-backup",
    ]
    assert trace_writer.call_count == 2
    guard = cast(AIClient, client._clients["summary-model"])._run_cost_guard
    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == pytest.approx(0.4) and state.ai_calls == 1
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall() == [("reserved",), ("settled",)]


@pytest.mark.parametrize("custom_retry", [False, True])
@pytest.mark.parametrize("explicit_builtin_factory", [False, True])
def test_pre_dispatch_feature_budget_counts_fallback_settled_usage(
    tmp_path, monkeypatch, custom_retry, explicit_builtin_factory,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=custom_retry, budget=1.0, run_budget=10.0,
        default_factory=True, explicit_builtin_factory=explicit_builtin_factory,
    )
    router.route.return_value.usage.completion_tokens = 600
    primary = cast(AIClient, client._get_client("summary-model"))
    assert primary.chat("synthetic-system", "synthetic-user", max_tokens=600) == "{}"

    with pytest.raises(BudgetExceeded, match="feature"):
        client.structured(
            "synthetic-system",
            "synthetic-user",
            parser=lambda value: value,
            max_tokens=600,
        )

    assert set(client._clients) == {"summary-model"}
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    assert primary.spent_usd == 0.6
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == 0.6 and state.reserved_usd == 0.0 and state.ai_calls == 1


def test_pre_dispatch_feature_budget_settles_separately_and_accumulates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=False, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    backup = cast(AIClient, client._get_client("summary-backup"))
    router.route.return_value.usage.completion_tokens = 600
    assert primary.chat("synthetic-system", "synthetic-user", max_tokens=600) == "{}"
    router.route.return_value.usage.completion_tokens = 300
    assert backup.structured(
        "synthetic-system", "synthetic-user", parser=lambda value: value, max_tokens=400,
    ) == {}

    with pytest.raises(BudgetExceeded, match="feature"):
        primary.chat("synthetic-system", "synthetic-user", max_tokens=200)

    assert primary.spent_usd == 0.6 and backup.spent_usd == 0.3
    assert primary.usage_stats.call_count == backup.usage_stats.call_count == 1
    assert router.route.call_count == trace_writer.call_count == 2
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == pytest.approx(0.9) and state.reserved_usd == 0.0 and state.ai_calls == 2
    with sqlite3.connect(primary._run_cost_guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall() == [("settled",), ("settled",)]


def test_pre_dispatch_feature_budget_stops_exhausted_shared_spend(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, estimator = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=False, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    backup = cast(AIClient, client._get_client("summary-backup"))
    router.route.return_value.usage.completion_tokens = 1000
    assert primary.chat("synthetic-system", "synthetic-user", max_tokens=1000) == "{}"
    guard = primary._run_cost_guard
    state = guard.current_state()
    ledger_before, projection_before = guard.ledger_path.read_bytes(), guard.state_path.read_bytes()

    with pytest.raises(BudgetExceeded, match="feature ceiling is exhausted"):
        backup.chat("synthetic-system", "synthetic-user", max_tokens=0)

    estimator.estimate.assert_called_once()
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    assert guard.current_state() == state
    assert guard.ledger_path.read_bytes() == ledger_before
    assert guard.state_path.read_bytes() == projection_before


def test_pre_dispatch_feature_budget_rejects_shared_usage_overflow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1e308),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True,
        budget=float.fromhex("0x1.fffffffffffffp+1023"), run_budget=float.fromhex("0x1.fffffffffffffp+1023"),
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    backup = cast(AIClient, client._get_client("summary-backup"))
    router.route.return_value.usage.completion_tokens = 1000
    assert primary.chat("synthetic-system", "synthetic-user", max_tokens=0) == "{}"

    with pytest.raises(BudgetStateError, match="accounted for"):
        backup.chat("synthetic-system", "synthetic-user", max_tokens=0)

    assert primary.spent_usd == 1e308 and backup.spent_usd == 0.0
    assert primary.usage_stats.call_count == 1 and backup.usage_stats.call_count == 0
    assert router.route.call_count == trace_writer.call_count == 2
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == 1e308 and state.reserved_usd == 0.0 and state.ai_calls == 1
    with sqlite3.connect(primary._run_cost_guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall() == [("settled",), ("reserved",)]


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("explicit_builtin_factory", [False, True])
def test_pre_dispatch_feature_budget_counts_fallback_inflight(
    tmp_path, monkeypatch, method, explicit_builtin_factory,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=2.4, run_budget=10.0,
        default_factory=True, explicit_builtin_factory=explicit_builtin_factory,
    )
    entered, release = Event(), Event()
    route_lock = Lock()
    route_count = 0
    response = router.route.return_value

    def route(**kwargs):
        nonlocal route_count
        with route_lock:
            route_count += 1
            attempt = route_count
        if attempt == 1:
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("synthetic provider release timed out")
        elif attempt == 2:
            raise TimeoutError("synthetic outcome unknown")
        return response

    router.route.side_effect = route
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            getattr(client, method), "synthetic-system", "synthetic-user", max_tokens=400, **arguments,
        )
        try:
            assert entered.wait(timeout=10)
            second = executor.submit(
                getattr(client, method), "synthetic-system", "synthetic-user", max_tokens=400, **arguments,
            )
            with pytest.raises(BudgetExceeded, match="feature"):
                second.result(timeout=10)
            assert router.route.call_count == 1
            trace_writer.assert_not_called()
            guard = cast(AIClient, client._clients["summary-model"])._run_cost_guard
            state = guard.current_state()
            # The in-flight chain holds its complete 0.4 x 3 x 2 bound.
            assert state.spent_usd == 0.0 and state.reserved_usd == pytest.approx(2.4) and state.ai_calls == 0
        finally:
            release.set()
        assert first.result(timeout=10) == ({} if method == "structured" else "{}")

    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.0 and state.ai_calls == 1
    assert trace_writer.call_count == 1


def test_pre_dispatch_feature_budget_counts_fallback_settlement_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    backup = cast(AIClient, client._get_client("summary-backup"))
    guard = primary._run_cost_guard
    router.route.return_value.usage.completion_tokens = 125
    monkeypatch.setattr(guard, "settle_call", Mock(side_effect=StateError("synthetic settlement failure")))

    with pytest.raises(BudgetStateError, match="settlement"):
        primary.chat("synthetic-system", "synthetic-user", max_tokens=600)
    state = guard.current_state()
    ledger_before, projection_before = guard.ledger_path.read_bytes(), guard.state_path.read_bytes()

    with pytest.raises(BudgetStateError, match="settlement"):
        backup.chat("synthetic-system", "synthetic-user", max_tokens=300)

    assert primary.spent_usd == 0.125 and backup.spent_usd == 0.0
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    assert guard.current_state() == state
    assert guard.ledger_path.read_bytes() == ledger_before
    assert guard.state_path.read_bytes() == projection_before


def test_pre_dispatch_feature_budget_keeps_distinct_ledger_reservation_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    _, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=False, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    wrapper = FallbackAIClient(
        deployments=("summary-model", "summary-backup"), temperature=0.2, budget_usd=1.0,
    )
    with use_trace_context(AITraceContext(
        edition="first", run_id="first", caller="test", metadata={"run_budget_usd": 10.0},
    )):
        primary = cast(AIClient, wrapper._get_client("summary-model"))
    with use_trace_context(AITraceContext(
        edition="second", run_id="second", caller="test", metadata={"run_budget_usd": 10.0},
    )):
        backup = cast(AIClient, wrapper._get_client("summary-backup"))

    both_entered, release = Barrier(3), Event()
    route_lock = Lock()
    route_count = 0
    response = router.route.return_value

    def route(**kwargs):
        nonlocal route_count
        with route_lock:
            route_count += 1
            attempt = route_count
        if attempt <= 2:
            both_entered.wait(timeout=10)
            if not release.wait(timeout=10):
                raise TimeoutError("synthetic provider release timed out")
        return response

    router.route.side_effect = route
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(client.chat, "synthetic-system", "synthetic-user", max_tokens=400)
            for client in (primary, backup)
        ]
        try:
            both_entered.wait(timeout=10)
            with pytest.raises(BudgetExceeded, match="feature"):
                primary.chat("synthetic-system", "synthetic-user", max_tokens=400)
            assert router.route.call_count == 2
            for client in (primary, backup):
                with sqlite3.connect(client._run_cost_guard.ledger_path) as connection:
                    assert connection.execute(
                        "SELECT reservation_id, status FROM ai_cost_reservations",
                    ).fetchall() == [(1, "reserved")]
        finally:
            release.set()
        assert [future.result(timeout=10) for future in futures] == ["{}", "{}"]
    assert trace_writer.call_count == 2


def test_pre_dispatch_feature_budget_does_not_change_custom_factory_arguments() -> None:
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return Mock(chat=Mock(return_value="synthetic result"))

    client = FallbackAIClient(
        deployments=("summary-model",), temperature=0.2, budget_usd=1.0, client_factory=factory,
    )

    assert client.chat("synthetic-system", "synthetic-user") == "synthetic result"
    assert calls == [{"deployment": "summary-model", "temperature": 0.2, "budget_usd": 1.0}]


def test_pre_dispatch_feature_budget_preserves_replaced_default_factory(monkeypatch) -> None:
    calls = []

    class Replacement:
        def __init__(self, *, deployment, temperature, budget_usd):
            calls.append((deployment, temperature, budget_usd))

        def chat(self, system, user, *, max_tokens, prompt_version):
            return "synthetic result"

    monkeypatch.setattr("src.ai.deployment_fallback.AIClient", Replacement)
    client = FallbackAIClient(deployments=("summary-model",), temperature=0.2, budget_usd=1.0)

    assert client.chat("synthetic-system", "synthetic-user") == "synthetic result"
    assert calls == [("summary-model", 0.2, 1.0)]


def test_pre_dispatch_feature_budget_remains_separate_for_distinct_wrappers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    first, _, _, router, _, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=False, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    second = FallbackAIClient(
        deployments=("summary-model",), temperature=0.2, budget_usd=1.8, trace_context=first._trace_context,
    )
    router.route.side_effect = [TimeoutError("synthetic outcome unknown"), router.route.return_value]

    with pytest.raises(AIClientError):
        cast(AIClient, first._get_client("summary-model")).chat(
            "synthetic-system", "synthetic-user", max_tokens=600,
        )
    assert second.chat("synthetic-system", "synthetic-user", max_tokens=600) == "{}"
    assert router.route.call_count == 2


def test_pre_dispatch_feature_budget_does_not_hold_refused_run_reservation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    guard = clients[0]._run_cost_guard
    reserve = guard.reserve_call
    attempts = 0

    def reserve_once(cost):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BudgetExceeded("synthetic run refusal")
        return reserve(cost)

    monkeypatch.setattr(guard, "reserve_call", reserve_once)
    with pytest.raises(BudgetExceeded, match="run"):
        primary.chat("synthetic-system", "synthetic-user", max_tokens=600)
    router.route.assert_not_called()

    assert primary.chat("synthetic-system", "synthetic-user", max_tokens=600) == "{}"
    assert attempts == 2
    factory.assert_called_once()
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.0 and state.ai_calls == 1


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("custom_retry", [False, True])
def test_pre_dispatch_feature_budget_stops_runner_and_fallback(
    tmp_path, monkeypatch, batch, custom_retry,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=custom_retry, budget=0.25, run_budget=1.0,
    )
    provider = Mock(
        side_effect=(
            (lambda *_: client.structured("synthetic-system", "synthetic-user", parser=lambda value: value, max_tokens=251))
            if batch else (lambda *_: client.chat("synthetic-system", "synthetic-user", max_tokens=251))
        ),
    )
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE,
        limitations=("existing limitation",), retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.ABSTAINED for outcome in outcomes)
    assert all(
        outcome.limitations == ("existing limitation", "budget_exceeded") and not outcome.retryable
        for outcome in outcomes
    )
    provider.assert_called_once()
    factory.assert_called_once()
    router.route.assert_not_called()
    trace_writer.assert_not_called()
    assert len(clients) == 1 and clients[0].usage_stats.call_count == 0
    assert not (tmp_path / "programs").exists()
    assert _ceiling_counts(
        tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1",
    ) == {"remote": 1, "frontier": 1}


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("custom_retry", [False, True])
@pytest.mark.parametrize(("run_budget", "ceiling"), [(10.0, "feature"), (1.0, "run")])
def test_post_usage_feature_budget_rejects_actual_overrun(
    tmp_path, monkeypatch, client_class, method, custom_retry, run_budget, ceiling,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=custom_retry, budget=1.0, run_budget=run_budget,
        default_factory=True, client_class=client_class,
    )
    router.route.return_value.usage.prompt_tokens = 1250
    parser = Mock(return_value={})
    arguments = {"parser": parser} if method == "structured" else {}

    with pytest.raises(BudgetExceeded, match=f"actual AI spend exceeded the {ceiling} ceiling"):
        # RELO-W1.4: max_tokens keeps the whole-chain worst case (0.6) inside the 1.0 run ceiling.
        getattr(client, method)("system", "user", max_tokens=100, **arguments)

    primary = cast(AIClient, client._clients["summary-model"])
    assert set(client._clients) == {"summary-model"}
    assert primary.spent_usd == 1.25 and primary.usage_stats.call_count == 1
    router.route.assert_called_once()
    parser.assert_not_called()
    trace_writer.assert_called_once()
    assert f"actual AI spend exceeded the {ceiling} ceiling" in trace_writer.call_args.kwargs["error"]
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == 1.25 and state.reserved_usd == 0.0 and state.ai_calls == 1
    with sqlite3.connect(primary._run_cost_guard.ledger_path) as connection:
        assert connection.execute("SELECT status FROM ai_cost_reservations").fetchall() == [("settled",)]

    with pytest.raises(BudgetExceeded):
        getattr(client, method)("system", "user", max_tokens=0, **arguments)
    router.route.assert_called_once()


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_post_usage_feature_budget_allows_exact_ceiling_once(tmp_path, monkeypatch, method) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
    )
    router.route.return_value.usage.prompt_tokens = 1000
    parser = Mock(return_value={})
    arguments = {"parser": parser} if method == "structured" else {}
    primary = cast(AIClient, client._get_client("summary-model"))

    assert getattr(primary, method)("system", "user", max_tokens=500, **arguments) == (
        {} if method == "structured" else "{}"
    )
    with pytest.raises(BudgetExceeded):
        getattr(primary, method)("system", "user", max_tokens=0, **arguments)

    factory.assert_called_once()
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    assert trace_writer.call_args.kwargs["error"] is None
    assert parser.call_count == (1 if method == "structured" else 0)
    state = clients[0]._run_cost_guard.current_state()
    assert state.spent_usd == 1.0 and state.reserved_usd == 0.0 and state.ai_calls == 1


def test_post_usage_feature_budget_counts_cumulative_deployment_spend(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    backup = cast(AIClient, client._get_client("summary-backup"))
    router.route.return_value.usage.prompt_tokens = 600
    assert primary.chat("system", "user", max_tokens=400) == "{}"
    router.route.return_value.usage.prompt_tokens = 500

    with pytest.raises(BudgetExceeded, match="actual AI spend exceeded the feature ceiling"):
        backup.chat("system", "user", max_tokens=400)

    assert primary.spent_usd == 0.6 and backup.spent_usd == 0.5
    assert primary.usage_stats.call_count == backup.usage_stats.call_count == 1
    assert router.route.call_count == trace_writer.call_count == 2
    assert trace_writer.call_args.kwargs["error"].endswith("actual AI spend exceeded the feature ceiling.")
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == pytest.approx(1.1) and state.reserved_usd == 0.0 and state.ai_calls == 2


def test_post_usage_feature_budget_checks_concurrent_settlements(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    backup = cast(AIClient, client._get_client("summary-backup"))
    barrier = Barrier(2)
    response = router.route.return_value
    response.usage.prompt_tokens = 700

    def route(**kwargs):
        barrier.wait(timeout=10)
        return response

    router.route.side_effect = route
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(provider.chat, "system", "user", max_tokens=500)
            for provider in (primary, backup)
        ]
        results = []
        for future in futures:
            try:
                results.append(future.result(timeout=20))
            except BudgetExceeded as error:
                results.append(error)

    assert sum(result == "{}" for result in results) == 1
    failures = [result for result in results if isinstance(result, BudgetExceeded)]
    assert len(failures) == 1 and "feature ceiling" in str(failures[0])
    assert router.route.call_count == trace_writer.call_count == 2
    assert sum(call.kwargs["error"] is not None for call in trace_writer.call_args_list) == 1
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == pytest.approx(1.4) and state.reserved_usd == 0.0 and state.ai_calls == 2


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("custom_retry", [False, True])
@pytest.mark.parametrize("released_fallback", [False, True])
def test_post_usage_feature_budget_reaches_runner_without_retry(
    tmp_path, monkeypatch, batch, custom_retry, released_fallback,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=custom_retry, budget=1.0, run_budget=10.0,
    )
    router.route.return_value.usage.prompt_tokens = 1250
    parser = Mock(return_value={})
    primary = cast(AIClient, client._get_client("summary-model"))
    provider = Mock(
        side_effect=(
            (lambda *_: primary.structured("system", "user", parser=parser, max_tokens=500))
            if batch else (lambda *_: primary.chat("system", "user", max_tokens=500))
        ),
    )
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.RELEASED if released_fallback else FeatureOutcomeStatus.ABSTAINED,
        value="safe local value" if released_fallback else None, tier=Tier.NONE,
        limitations=("existing limitation",), retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
    decisions = []

    outcomes = _dispatch_frontier(_runner(tmp_path, decision_sink=decisions.append), spec, batch)

    expected_status = (
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK if released_fallback else FeatureOutcomeStatus.ABSTAINED
    )
    assert all(outcome.status == expected_status for outcome in outcomes)
    assert all(
        outcome.limitations == ("existing limitation", "budget_exceeded") and not outcome.retryable
        for outcome in outcomes
    )
    provider.assert_called_once()
    factory.assert_called_once()
    router.route.assert_called_once()
    parser.assert_not_called()
    trace_writer.assert_called_once()
    state = clients[0]._run_cost_guard.current_state()
    assert state.spent_usd == 1.25 and state.reserved_usd == 0.0 and state.ai_calls == 1
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {
        "remote": 1, "frontier": 1,
    }
    assert decisions[-1].outcome is RouteOutcome.FRONTIER_BLOCKED and decisions[-1].frontier_called


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("trace_error", [OSError, StateError, UnicodeError])
@pytest.mark.parametrize(("run_budget", "ceiling"), [(10.0, "feature"), (1.0, "run")])
def test_post_usage_feature_budget_trace_failure_remains_nonretryable(
    tmp_path, monkeypatch, caplog, method, trace_error, run_budget, ceiling,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=run_budget,
    )
    router.route.return_value.usage.prompt_tokens = 1250
    trace_writer.side_effect = trace_error("synthetic private trace failure")
    parser = Mock(return_value={})
    arguments = {"parser": parser} if method == "structured" else {}

    with pytest.raises(BudgetExceeded, match=f"actual AI spend exceeded the {ceiling} ceiling"):
        # RELO-W1.4: max_tokens keeps the whole-chain worst case (0.6) inside the 1.0 run ceiling.
        getattr(client, method)("system", "user", max_tokens=100, **arguments)

    factory.assert_called_once()
    router.route.assert_called_once()
    parser.assert_not_called()
    trace_writer.assert_called_once()
    state = clients[0]._run_cost_guard.current_state()
    assert state.spent_usd == 1.25 and state.reserved_usd == 0.0 and state.ai_calls == 1
    assert "AI trace failed while reporting a budget failure." in caplog.text
    assert "synthetic private trace failure" not in caplog.text


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("trace_error", [OSError, StateError, UnicodeError])
def test_post_usage_feature_budget_preserves_trace_failure_without_overrun(
    tmp_path, monkeypatch, caplog, method, trace_error,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=False, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    router.route.return_value.usage.prompt_tokens = 200
    trace_writer.side_effect = [trace_error("synthetic trace failure"), None]
    parser = Mock(return_value={})
    arguments = {"parser": parser} if method == "structured" else {}

    with pytest.raises(AIClientError) as caught:
        getattr(primary, method)("system", "user", max_tokens=500, **arguments)

    assert not isinstance(caught.value, BudgetExceeded)
    assert isinstance(caught.value.__cause__, trace_error)
    router.route.assert_called_once()
    parser.assert_not_called()
    assert trace_writer.call_count == 2
    assert trace_writer.call_args_list[0].kwargs["error"] is None
    assert trace_writer.call_args_list[1].kwargs["error"] is not None
    assert "AI trace failed while reporting a budget failure." not in caplog.text
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == 0.2 and state.reserved_usd == 0.0 and state.ai_calls == 1


def test_post_usage_feature_budget_preserves_settlement_failure_precedence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, _, _, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=0, custom_retry=True, budget=1.0, run_budget=10.0,
        default_factory=True,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    router.route.return_value.usage.prompt_tokens = 1250
    monkeypatch.setattr(primary._run_cost_guard, "settle_call", Mock(side_effect=StateError("settlement failed")))

    with pytest.raises(BudgetStateError, match="settlement failed"):
        primary.chat("system", "user", max_tokens=500)

    assert set(client._clients) == {"summary-model"}
    assert primary.spent_usd == 1.25 and primary.usage_stats.call_count == 1
    router.route.assert_called_once()
    trace_writer.assert_called_once()
    assert trace_writer.call_args.kwargs["error"] == "Unable to settle AI cost state; reconcile before retrying."
    state = primary._run_cost_guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.5 and state.ai_calls == 0


@pytest.mark.parametrize("field_name", ["input_cost_per_1k_tokens", "output_cost_per_1k_tokens"])
@pytest.mark.parametrize("surface", ["client", "single", "batch"])
@pytest.mark.parametrize("custom_retry", [False, True])
def test_model_price_admission_stops_fallback_and_runner(
    tmp_path, monkeypatch, field_name, surface, custom_retry,
) -> None:
    valid_prices = {"input_cost_per_1k_tokens": 0.001, "output_cost_per_1k_tokens": 0.001}
    invalid_prices = dict(valid_prices)
    invalid_prices[field_name] = -0.000001
    monkeypatch.setattr(
        "src.core.policy_loader._load_policy_defaults",
        lambda: {"ai": {"models": {"primary": invalid_prices, "backup": valid_prices}}},
    )
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", Mock())
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    router_factory = Mock(return_value=router)
    monkeypatch.setattr("src.ai.client.AIRequestRouter", router_factory)
    factory = Mock(side_effect=lambda **kwargs: AIClient(**kwargs))
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="price-admission",
        caller="test",
        metadata={"ai_feature": "risk_proposal_generator"},
    )
    client = FallbackAIClient(
        deployments=("primary", "backup"), temperature=0.2, budget_usd=1.0,
        trace_context=trace, client_factory=factory,
        retryable_exceptions=(Exception,) if custom_retry else None,
    )

    if surface == "client":
        with pytest.raises(BudgetConfigurationError, match="pricing"):
            client.chat("system", "user", max_tokens=10)
    else:
        batch = surface == "batch"
        provider = Mock(
            side_effect=(
                (lambda *_: client.structured("system", "user", parser=lambda value: value, max_tokens=10))
                if batch else (lambda *_: client.chat("system", "user", max_tokens=10))
            ),
        )
        fallback = FeatureOutcome(
            status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE,
            limitations=("existing limitation",), retryable=True,
        )
        spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
        outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)
        provider.assert_called_once()
        assert all(outcome.status == FeatureOutcomeStatus.ABSTAINED for outcome in outcomes)
        assert all(
            outcome.limitations == ("existing limitation", "budget_configuration_invalid") and not outcome.retryable
            for outcome in outcomes
        )
    assert factory.call_count == 1
    router_factory.assert_not_called()
    router.route.assert_not_called()
    assert not (tmp_path / "programs").exists()


@pytest.mark.parametrize(
    "invalid",
    [None, -1, True, "100", 1.5, float("nan"), float("inf"), 10**400],
    ids=["missing", "negative", "boolean", "string", "fractional", "nan", "infinity", "overflow"],
)
@pytest.mark.parametrize("field_name", ["prompt_tokens", "completion_tokens"])
@pytest.mark.parametrize("method", ["chat", "structured"])
def test_invalid_provider_usage_preserves_reservation_and_stops_fallback(
    tmp_path, monkeypatch, invalid, field_name, method,
) -> None:
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=100)
    setattr(usage, field_name, invalid)
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    trace_writer = Mock()
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", trace_writer)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="invalid-usage",
        caller="test",
        metadata={"ai_feature": "risk_proposal_generator"},
    )
    factory = Mock(side_effect=lambda **kwargs: AIClient(**kwargs))
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"), temperature=0.2, budget_usd=1.0,
        trace_context=trace, client_factory=factory, retryable_exceptions=(Exception,),
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetStateError, match="usage"):
        getattr(client, method)("system", "user", max_tokens=10, **arguments)

    assert factory.call_count == 1
    router.route.assert_called_once()
    active_client = cast(AIClient, client._clients["summary-model"])
    assert active_client.spent_usd == 0.0
    assert active_client.usage_stats.call_count == 0
    guard = CostGuard(
        edition="acme_weekly", run_id="invalid-usage", budget_usd=1.0, programs_root=tmp_path / "programs",
    )
    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd > 0 and state.ai_calls == 0
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute("SELECT status FROM ai_cost_reservations").fetchall() == [("reserved",)]
    trace_writer.assert_called_once()
    assert trace_writer.call_args.kwargs["prompt_tokens"] is None
    assert trace_writer.call_args.kwargs["completion_tokens"] is None
    assert trace_writer.call_args.kwargs["cost_usd"] is None


@pytest.mark.parametrize("missing", ["usage", "prompt_tokens", "completion_tokens"])
def test_missing_provider_usage_preserves_reservation(tmp_path, monkeypatch, missing) -> None:
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=100)
    if missing != "usage":
        delattr(usage, missing)
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=None if missing == "usage" else usage,
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", Mock())
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="missing-usage",
        caller="test",
        metadata={"ai_feature": "risk_proposal_generator"},
    )
    client = AIClient("summary-model", 0.2, 1.0, trace_context=trace)

    with pytest.raises(BudgetStateError, match="usage"):
        client.chat("system", "user", max_tokens=10)

    router.route.assert_called_once()
    state = client._run_cost_guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd > 0 and state.ai_calls == 0


@pytest.mark.parametrize("surface", ["client", "single", "batch"])
def test_provider_usage_cost_overflow_preserves_reservation_and_stops_fallback(tmp_path, monkeypatch, surface) -> None:
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10000, completion_tokens=0),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    trace_writer = Mock()
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1e308, output_cost_per_1k_tokens=0.0),
    )
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", trace_writer)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="overflow-usage",
        caller="test",
        metadata={"ai_feature": "risk_proposal_generator"},
    )
    factory = Mock(side_effect=lambda **kwargs: AIClient(**kwargs))
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"), temperature=0.2, budget_usd=1e308,
        trace_context=trace, client_factory=factory, retryable_exceptions=(Exception,),
    )
    if surface == "client":
        with pytest.raises(BudgetStateError, match="usage"):
            client.chat("system", "user", max_tokens=10)
    else:
        batch = surface == "batch"
        provider = Mock(
            side_effect=(
                (lambda *_: client.structured("system", "user", parser=lambda value: value, max_tokens=10))
                if batch else (lambda *_: client.chat("system", "user", max_tokens=10))
            ),
        )
        fallback = FeatureOutcome(
            status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE,
            limitations=("existing limitation",), retryable=True,
        )
        spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
        outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)
        assert all(
            outcome.limitations == ("existing limitation", "budget_state_invalid") and not outcome.retryable
            for outcome in outcomes
        )
    assert factory.call_count == 1
    router.route.assert_called_once()
    active_client = cast(AIClient, client._clients["summary-model"])
    assert active_client.spent_usd == 0.0 and active_client.usage_stats.call_count == 0
    guard = CostGuard(
        edition="acme_weekly", run_id="overflow-usage", budget_usd=1e308, programs_root=tmp_path / "programs",
    )
    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd > 0 and state.ai_calls == 0
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute("SELECT status FROM ai_cost_reservations").fetchall() == [("reserved",)]
    trace_writer.assert_called_once()
    assert trace_writer.call_args.kwargs["cost_usd"] is None


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_provider_usage_cumulative_spend_overflow_retains_reservation(tmp_path, monkeypatch, method) -> None:
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=0),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    trace_writer = Mock()
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1e308, output_cost_per_1k_tokens=0.0),
    )
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", trace_writer)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="cumulative-overflow",
        caller="test",
        metadata={"ai_feature": "risk_proposal_generator"},
    )
    factory = Mock(side_effect=lambda **kwargs: AIClient(**kwargs))
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"), temperature=0.2, budget_usd=1.7e308,
        trace_context=trace, client_factory=factory, retryable_exceptions=(Exception,),
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    call = getattr(client, method)

    call("system", "user", max_tokens=10, **arguments)
    with pytest.raises(BudgetStateError) as caught:
        call("system", "user", max_tokens=10, **arguments)

    active_client = cast(AIClient, client._clients["summary-model"])
    assert factory.call_count == 1 and router.route.call_count == 2
    assert active_client.spent_usd == 1e308
    assert active_client.usage_stats.call_count == 1
    assert active_client.usage_stats.prompt_tokens == 1000
    guard = CostGuard(
        edition="acme_weekly", run_id="cumulative-overflow",
        budget_usd=1.7e308, programs_root=tmp_path / "programs",
    )
    state = guard.current_state()
    assert state.spent_usd == 1e308 and state.reserved_usd > 0 and state.ai_calls == 1
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute(
            "SELECT status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall() == [("settled",), ("reserved",)]
    assert trace_writer.call_count == 2
    assert trace_writer.call_args.kwargs["cost_usd"] == 1e308
    assert "usage" in str(caught.value)


@pytest.mark.parametrize("method", ["chat", "structured"])
def test_zero_provider_usage_settles_reservation(tmp_path, monkeypatch, method) -> None:
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    trace_writer = Mock()
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", trace_writer)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="zero-usage",
        caller="test",
        metadata={"ai_feature": "risk_proposal_generator"},
    )
    client = AIClient("summary-model", 0.2, 1.0, trace_context=trace)
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    result = getattr(client, method)("system", "user", max_tokens=10, **arguments)

    assert result == ({} if method == "structured" else "{}")
    assert client.spent_usd == 0.0 and client.usage_stats.call_count == 1
    assert client.usage_stats.total_tokens == 0
    state = client._run_cost_guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.0 and state.ai_calls == 1
    with sqlite3.connect(client._run_cost_guard.ledger_path) as connection:
        assert connection.execute("SELECT status FROM ai_cost_reservations").fetchall() == [("settled",)]
    trace_writer.assert_called_once()
    assert trace_writer.call_args.kwargs["prompt_tokens"] == 0
    assert trace_writer.call_args.kwargs["completion_tokens"] == 0
    assert trace_writer.call_args.kwargs["cost_usd"] == 0.0


def test_ai_call_count_overflow_preserves_reservation_and_stops_fallback(tmp_path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    guard = CostGuard(
        edition="acme_weekly", run_id="call-count-overflow", budget_usd=1.0, programs_root=programs_root,
    )
    guard.record_actual(0.0, ai_calls=2**63 - 1)
    projection = []
    router = Mock()

    def route(**kwargs):
        projection.append(guard.state_path.read_bytes())
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100),
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
        )

    router.route.side_effect = route
    trace_writer = Mock()
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", trace_writer)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", programs_root)
    trace = AITraceContext(
        edition="acme_weekly",
        run_id="call-count-overflow",
        caller="test",
        metadata={"ai_feature": "risk_proposal_generator"},
    )
    factory = Mock(side_effect=lambda **kwargs: AIClient(**kwargs))
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"), temperature=0.2, budget_usd=1.0,
        trace_context=trace, client_factory=factory, retryable_exceptions=(Exception,),
    )

    with pytest.raises(BudgetStateError, match="Invalid cost guard ai_calls value"):
        client.chat("system", "user", max_tokens=10)

    factory.assert_called_once()
    router.route.assert_called_once()
    state = guard.current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd > 0 and state.ai_calls == 2**63 - 1
    assert guard.state_path.read_bytes() == projection[0]
    with sqlite3.connect(guard.ledger_path) as connection:
        assert connection.execute("SELECT status FROM ai_cost_reservations").fetchall() == [("reserved",)]
    trace_writer.assert_called_once()
    assert trace_writer.call_args.kwargs["error"] == "Unable to settle AI cost state; reconcile before retrying."


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("custom_retry", [False, True])
@pytest.mark.parametrize("phase", ["reservation", "settlement"])
@pytest.mark.parametrize("error_type", [StateError, OSError, UnicodeError])
def test_cost_state_exception_chain_is_not_formatted(
    tmp_path, monkeypatch, client_class, method, custom_retry, phase, error_type,
) -> None:
    monkeypatch.setattr(
        "src.ai.client.load_ai_model_cost_policy",
        lambda _: SimpleNamespace(input_cost_per_1k_tokens=1.0, output_cost_per_1k_tokens=1.0),
    )
    client, clients, factory, router, trace_writer, _ = _token_budget_client(
        tmp_path, monkeypatch, estimate=50, custom_retry=custom_retry, budget=1.0, run_budget=10.0,
        client_class=client_class,
    )
    primary = cast(AIClient, client._get_client("summary-model"))
    guard = primary._run_cost_guard
    router.route.return_value.usage.prompt_tokens = 100
    router.route.return_value.usage.completion_tokens = 50
    private_value = "synthetic-private-cost-cause"

    def fail(*_args, **_kwargs):
        try:
            raise ValueError(private_value)
        except ValueError as cause:
            message = "Synthetic cost-state failure" if error_type is StateError else private_value
            raise error_type(message) from cause

    monkeypatch.setattr(guard, "reserve_call" if phase == "reservation" else "settle_call", fail)
    arguments = {"parser": lambda value: value} if method == "structured" else {}
    with pytest.raises(BudgetStateError) as caught:
        getattr(client, method)("system", "user", max_tokens=10, **arguments)

    expected = "Synthetic cost-state failure" if error_type is StateError else "Unable to access AI cost state."
    assert str(caught.value) == expected
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert private_value not in "".join(traceback.format_exception(caught.value))
    factory.assert_called_once()
    assert len(clients) == 1
    assert router.route.call_count == (0 if phase == "reservation" else 1)
    assert trace_writer.call_count == (0 if phase == "reservation" else 1)
    if phase == "reservation":
        assert not guard.ledger_path.exists()
        assert primary.spent_usd == 0.0 and primary.usage_stats.call_count == 0
    else:
        state = guard.current_state()
        assert state.spent_usd == 0.0 and state.reserved_usd == pytest.approx(0.06) and state.ai_calls == 0
        assert primary.spent_usd == pytest.approx(0.15) and primary.usage_stats.call_count == 1
        assert trace_writer.call_args.kwargs["error"] == "Unable to settle AI cost state; reconcile before retrying."


def _cost_state_failure_fixture(tmp_path, monkeypatch, phase):
    programs_root = tmp_path / "programs"
    guard = CostGuard(
        edition="acme_weekly", run_id="fallback-state", budget_usd=1.0, programs_root=programs_root,
    )
    trace = AITraceContext(
        edition="acme_weekly", run_id="fallback-state", caller="test",
        metadata={"run_budget_usd": 1.0, "ai_feature": "risk_proposal_generator"},
    )
    projection = []
    if phase in ("before", "preflight-io", "pre-projection-io", "pre-projection-unicode", "pre-projection-json"):
        guard.record_actual(0.0, ai_calls=0)
        if phase == "before":
            with sqlite3.connect(guard.ledger_path) as connection:
                connection.execute("UPDATE ai_cost_runs SET spent_usd = -1.0")
        elif phase == "pre-projection-unicode":
            guard.state_path.write_bytes(b"\xff")
        elif phase == "pre-projection-json":
            guard.state_path.write_bytes(b'{"edition": "private-projection"')
        projection.append(guard.state_path.read_bytes())
    if phase == "preflight-io":
        def fail_preflight_read(path, *, edition):
            raise PermissionError("synthetic private projection error")

        monkeypatch.setattr("src.ai.cost_guard._projection_payload_for_write", fail_preflight_read)
    if phase in ("projection", "projection-oserror", "pre-projection-io"):
        persist_projection = CostGuard._persist_projection

        def fail_projection(self, state):
            if bool(state.ai_calls) != (phase == "pre-projection-io"):
                if phase in ("projection-oserror", "pre-projection-io"):
                    raise OSError("synthetic cost projection failure")
                raise StateError("synthetic cost projection failure")
            return persist_projection(self, state)

        monkeypatch.setattr(CostGuard, "_persist_projection", fail_projection)

    router = Mock()

    def route(**kwargs):
        if kwargs["deployment"] == "summary-model":
            if phase == "post-projection-json":
                guard.state_path.write_bytes(b'{"edition": "private-projection"')
            projection.append(guard.state_path.read_bytes())
            if phase == "reservation":
                with sqlite3.connect(guard.ledger_path) as connection:
                    connection.execute("UPDATE ai_cost_reservations SET reserved_usd = -1.0")
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100),
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
        )

    router.route.side_effect = route
    trace_writer = Mock()
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", trace_writer)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", programs_root)
    return guard, trace, router, projection, trace_writer


def _assert_cost_state_failure(guard, router, projection, trace_writer, phase):
    before_dispatch = phase in (
        "before", "preflight-io", "pre-projection-io", "pre-projection-unicode", "pre-projection-json",
    )
    assert router.route.call_count == (0 if before_dispatch else 1)
    assert trace_writer.call_count == (0 if before_dispatch else 1)
    if not before_dispatch:
        trace = trace_writer.call_args.kwargs
        assert trace["prompt_tokens"] == 100 and trace["completion_tokens"] == 100
        assert trace["cost_usd"] > 0
        assert trace["error"] == "Unable to settle AI cost state; reconcile before retrying."
    assert guard.state_path.read_bytes() == projection[0]
    with sqlite3.connect(guard.ledger_path) as connection:
        state = connection.execute(
            "SELECT spent_usd, reserved_usd, ai_calls FROM ai_cost_runs",
        ).fetchone()
        reservations = connection.execute(
            "SELECT reserved_usd, status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall()
    if phase == "before":
        assert state == (-1.0, 0.0, 0)
        assert reservations == []
    elif phase in ("preflight-io", "pre-projection-unicode", "pre-projection-json"):
        assert state == (0.0, 0.0, 0)
        assert reservations == []
    elif before_dispatch:
        assert state == (0.0, 0.0, 0)
        assert len(reservations) == 1 and reservations[0][0] > 0.0 and reservations[0][1] == "settled"
        with sqlite3.connect(guard.ledger_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM ai_cost_non_dispatch_releases"
            ).fetchone() == (1,)
    elif phase == "reservation":
        assert state is not None
        assert state[0] == 0.0 and state[1] > 0.0 and state[2] == 0
        assert reservations == [(-1.0, "reserved")]
    else:
        assert state is not None
        assert state[0] > 0.0 and state[1] == 0.0 and state[2] == 1
        assert len(reservations) == 1 and reservations[0][1] == "settled"


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize(
    "phase", [
        "before", "preflight-io", "pre-projection-io", "pre-projection-unicode", "pre-projection-json",
        "reservation", "projection", "projection-oserror", "post-projection-json",
    ],
)
@pytest.mark.parametrize("custom_retry", [False, True])
def test_cost_state_fallback_stops_before_another_deployment(
    tmp_path, monkeypatch, client_class, method, phase, custom_retry,
) -> None:
    guard, trace, router, projection, trace_writer = _cost_state_failure_fixture(tmp_path, monkeypatch, phase)

    def factory(**kwargs):
        return AIClient(endpoint="https://example.invalid", api_key="test-key", **kwargs)

    client_factory = Mock(side_effect=factory)
    client = client_class(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2,
        budget_usd=1.0,
        trace_context=trace,
        client_factory=client_factory,
        retryable_exceptions=(Exception,) if custom_retry else None,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    with pytest.raises(BudgetStateError) as caught:
        getattr(client, method)("system", "user", max_tokens=10, **arguments)

    assert isinstance(caught.value, AIClientError) and isinstance(caught.value, StateError)
    if phase in ("preflight-io", "pre-projection-io", "pre-projection-unicode", "projection-oserror"):
        assert str(caught.value) == "Unable to access AI cost state."
    client_factory.assert_called_once()
    _assert_cost_state_failure(guard, router, projection, trace_writer, phase)


@pytest.mark.parametrize(
    "phase", [
        "before", "preflight-io", "pre-projection-io", "pre-projection-unicode", "pre-projection-json",
        "reservation", "projection", "projection-oserror", "post-projection-json",
    ],
)
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("released_fallback", [False, True])
@pytest.mark.parametrize("custom_retry", [False, True])
def test_cost_state_failure_reaches_runner_as_nonretryable(
    tmp_path, monkeypatch, phase, batch, released_fallback, custom_retry,
) -> None:
    guard, trace, router, projection, trace_writer = _cost_state_failure_fixture(tmp_path, monkeypatch, phase)

    def factory(**kwargs):
        return AIClient(endpoint="https://example.invalid", api_key="test-key", **kwargs)

    client_factory = Mock(side_effect=factory)
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2,
        budget_usd=1.0,
        trace_context=trace,
        client_factory=client_factory,
        retryable_exceptions=(Exception,) if custom_retry else None,
    )
    if batch:
        provider = Mock(side_effect=lambda *_: client.structured("system", "user", parser=lambda value: value))
    else:
        provider = Mock(side_effect=lambda *_: client.chat("system", "user"))
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.RELEASED if released_fallback else FeatureOutcomeStatus.ABSTAINED,
        value="safe local value" if released_fallback else None, tier=Tier.NONE,
        limitations=("existing limitation",), evidence_refs=("safe-evidence",),
        retryable=True, next_action="existing next action",
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
    decisions = []

    outcomes = _dispatch_frontier(_runner(tmp_path, decision_sink=decisions.append), spec, batch)

    provider.assert_called_once()
    client_factory.assert_called_once()
    _assert_cost_state_failure(guard, router, projection, trace_writer, phase)
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {
        "remote": 1, "frontier": 1,
    }
    expected_status = (
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK if released_fallback else FeatureOutcomeStatus.ABSTAINED
    )
    assert all(outcome.status == expected_status for outcome in outcomes)
    assert all(
        outcome.limitations == ("existing limitation", "budget_state_invalid") and not outcome.retryable
        for outcome in outcomes
    )
    assert all(outcome.evidence_refs == ("safe-evidence",) for outcome in outcomes)
    if not released_fallback:
        assert all(outcome.next_action == "existing next action" for outcome in outcomes)
    assert decisions[-1].tier is Tier.FRONTIER
    assert decisions[-1].outcome is RouteOutcome.FRONTIER_BLOCKED
    assert decisions[-1].frontier_called


@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("custom_retry", [False, True])
def test_cost_state_handling_does_not_change_provider_state_error_retry(tmp_path, monkeypatch, method, custom_retry) -> None:
    guard, trace, router, _projection, trace_writer = _cost_state_failure_fixture(tmp_path, monkeypatch, "provider")

    def route(**kwargs):
        if kwargs["deployment"] == "summary-model":
            raise StateError("synthetic provider state failure")
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100),
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
        )

    router.route.side_effect = route
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2,
        budget_usd=1.0,
        trace_context=trace,
        client_factory=lambda **kwargs: AIClient(endpoint="https://example.invalid", api_key="test-key", **kwargs),
        retryable_exceptions=(Exception,) if custom_retry else None,
    )
    arguments = {"parser": lambda value: value} if method == "structured" else {}

    result = getattr(client, method)("system", "user", max_tokens=10, **arguments)

    assert result == ({} if method == "structured" else "{}")
    assert [call.kwargs["deployment"] for call in router.route.call_args_list] == ["summary-model", "summary-backup"]
    assert trace_writer.call_count == 2
    with sqlite3.connect(guard.ledger_path) as connection:
        reservations = connection.execute(
            "SELECT status FROM ai_cost_reservations ORDER BY reservation_id",
        ).fetchall()
    assert reservations == [("reserved",), ("settled",)]


@pytest.mark.parametrize("surface", ["client", "single", "batch"])
@pytest.mark.parametrize("trace_error", [OSError, StateError, UnicodeError])
@pytest.mark.parametrize("phase", ["projection-oserror", "reservation"])
def test_cost_state_trace_failure_remains_nonretryable(
    tmp_path, monkeypatch, caplog, surface, trace_error, phase,
) -> None:
    guard, trace, router, projection, trace_writer = _cost_state_failure_fixture(
        tmp_path, monkeypatch, phase,
    )
    trace_writer.side_effect = trace_error("synthetic private trace failure")
    client_factory = Mock(
        side_effect=lambda **kwargs: AIClient(endpoint="https://example.invalid", api_key="test-key", **kwargs),
    )
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2,
        budget_usd=1.0,
        trace_context=trace,
        client_factory=client_factory,
        retryable_exceptions=(Exception,),
    )

    if surface == "client":
        try:
            with pytest.raises(BudgetStateError) as caught:
                client.chat("system", "user", max_tokens=10)
        finally:
            assert router.route.call_count == 1
        assert str(caught.value) == (
            "Unable to access AI cost state."
            if phase == "projection-oserror" else "Invalid cost guard reserved_usd value"
        )
    else:
        batch = surface == "batch"
        provider = Mock(
            side_effect=(
                (lambda *_: client.structured("system", "user", parser=lambda value: value))
                if batch else (lambda *_: client.chat("system", "user"))
            ),
        )
        fallback = FeatureOutcome(
            status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE, retryable=True,
        )
        spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
        outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)
        assert all(outcome.limitations == ("budget_state_invalid",) and not outcome.retryable for outcome in outcomes)

    client_factory.assert_called_once()
    _assert_cost_state_failure(guard, router, projection, trace_writer, phase)
    assert "AI trace failed while reporting a cost-state failure." in caplog.text
    assert "synthetic private trace failure" not in caplog.text


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("custom_retry", [False, True])
@pytest.mark.parametrize("released_fallback", [False, True])
@pytest.mark.parametrize(
    ("run_budget", "feature_budget", "reason"),
    [
        (0.0, 0.5, "budget_exceeded"),
        (-0.5, 0.5, "budget_configuration_invalid"),
        ("invalid", 0.5, "budget_configuration_invalid"),
        (0.5, float("nan"), "budget_configuration_invalid"),
    ],
)
def test_fallback_budget_admission_reaches_runner_without_retry_or_dispatch(
    tmp_path, monkeypatch, batch, custom_retry, released_fallback, run_budget, feature_budget, reason,
) -> None:
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", Mock())
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    trace = AITraceContext(
        edition="acme_weekly", run_id="fallback-admission", caller="test",
        metadata={"run_budget_usd": run_budget, "ai_feature": "risk_proposal_generator"},
    )

    def factory(**kwargs):
        return AIClient(endpoint="https://example.invalid", api_key="test-key", **kwargs)

    client_factory = Mock(side_effect=factory)
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2,
        budget_usd=feature_budget,
        trace_context=trace,
        client_factory=client_factory,
        retryable_exceptions=(Exception,) if custom_retry else None,
    )
    if batch:
        provider = Mock(side_effect=lambda *_: client.structured("system", "user", parser=lambda value: value))
    else:
        provider = Mock(side_effect=lambda *_: client.chat("system", "user"))
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.RELEASED if released_fallback else FeatureOutcomeStatus.ABSTAINED,
        value="safe local value" if released_fallback else None, tier=Tier.NONE,
        limitations=("existing limitation",), retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
    decisions = []

    outcomes = _dispatch_frontier(_runner(tmp_path, decision_sink=decisions.append), spec, batch)

    provider.assert_called_once()
    client_factory.assert_called_once()
    router.route.assert_not_called()
    assert not (tmp_path / "programs").exists()
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {
        "remote": 1, "frontier": 1,
    }
    expected_status = (
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK if released_fallback else FeatureOutcomeStatus.ABSTAINED
    )
    assert all(outcome.status == expected_status for outcome in outcomes)
    assert all(outcome.limitations == ("existing limitation", reason) and not outcome.retryable for outcome in outcomes)
    assert decisions[-1].tier is Tier.FRONTIER
    assert decisions[-1].outcome is RouteOutcome.FRONTIER_BLOCKED
    assert decisions[-1].frontier_called


@pytest.mark.parametrize("batch", [False, True])
def test_budget_admission_runner_callback_may_have_dispatched(tmp_path, batch) -> None:
    dispatched = Mock()

    def provider(*_args):
        dispatched()
        raise BudgetConfigurationError("synthetic configuration error")

    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE, retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
    decisions = []

    outcomes = _dispatch_frontier(_runner(tmp_path, decision_sink=decisions.append), spec, batch)

    dispatched.assert_called_once()
    assert decisions[-1].tier is Tier.FRONTIER
    assert decisions[-1].outcome is RouteOutcome.FRONTIER_BLOCKED
    assert decisions[-1].frontier_called
    assert all(outcome.limitations == ("budget_configuration_invalid",) and not outcome.retryable for outcome in outcomes)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("post_usage", [False, True])
def test_actual_client_budget_exhaustion_through_fallback_reaches_runner(
    tmp_path, monkeypatch, batch, post_usage,
) -> None:
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10000, completion_tokens=10000),
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.client.AIClient._write_trace", Mock())
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    budget = 0.01 if post_usage else 0.000001
    trace = AITraceContext(
        edition="acme_weekly", run_id="fallback-budget", caller="test",
        metadata={"run_budget_usd": budget, "ai_feature": "risk_proposal_generator"},
    )

    def factory(**kwargs):
        return AIClient(endpoint="https://example.invalid", api_key="test-key", **kwargs)

    client_factory = Mock(side_effect=factory)
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2,
        budget_usd=budget,
        trace_context=trace,
        client_factory=client_factory,
    )
    if batch:
        provider = Mock(side_effect=lambda *_: client.structured("system", "user", parser=lambda value: value))
    else:
        provider = Mock(side_effect=lambda *_: client.chat("system", "user"))
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE, retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    provider.assert_called_once()
    client_factory.assert_called_once()
    assert router.route.call_count == (1 if post_usage else 0)
    state = CostGuard(
        edition="acme_weekly", run_id="fallback-budget", budget_usd=budget,
        programs_root=tmp_path / "programs",
    ).current_state()
    assert state.reserved_usd == 0.0
    assert state.ai_calls == (1 if post_usage else 0)
    if post_usage:
        assert state.spent_usd > budget
    else:
        assert state.spent_usd == 0.0
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {
        "remote": 1, "frontier": 1,
    }
    assert all(outcome.status == FeatureOutcomeStatus.ABSTAINED for outcome in outcomes)
    assert all(outcome.limitations == ("budget_exceeded",) and not outcome.retryable for outcome in outcomes)


def test_fallback_budget_refusal_preserves_claim_extractor_hard_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ai.tiered_router.get_ai_mode", lambda: AIMode.ACTIVE)
    budget_error = BudgetExceeded("synthetic budget exhaustion")
    provider = Mock()
    provider.structured.side_effect = budget_error
    factory = Mock(return_value=provider)
    client = FallbackStructuredClient(
        deployments=("primary", "backup"),
        temperature=0.0,
        budget_usd=1.0,
        client_factory=factory,
    )

    with pytest.raises(ClaimExtractorBudgetError) as caught:
        ClaimExtractor(client=client).extract_claims(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=1,
            claim_date=date(2026, 1, 1),
            narratives={"exec_summary.md": "Synthetic narrative."},
            programs_root=tmp_path,
        )

    assert caught.value.__cause__ is budget_error
    factory.assert_called_once()
    provider.structured.assert_called_once()
    decisions = [
        event for event in read_events("acme", programs_root=tmp_path)
        if event.event_type == "ai.release_decision.v1"
    ]
    assert len(decisions) == 1
    assert decisions[0].payload["terminal"] == "discarded"


def test_fallback_budget_refusal_propagates_through_claim_resolution(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.ai.tiered_router.get_ai_mode", lambda: AIMode.ACTIVE)
    budget_error = BudgetExceeded("synthetic budget exhaustion")
    provider = Mock()
    provider.structured.side_effect = budget_error
    factory = Mock(return_value=provider)
    client = FallbackStructuredClient(
        deployments=("primary", "backup"),
        temperature=0.0,
        budget_usd=1.0,
        client_factory=factory,
    )
    monkeypatch.setattr(
        ClaimExtractor, "from_program", classmethod(lambda cls, program: cls(client=client)),
    )
    resolved = SimpleNamespace(raw_program={"ai": {"enabled": True}}, program=SimpleNamespace(id="acme"))

    with pytest.raises(RuntimeError, match="AOAI cost guard triggered") as caught:
        resolve_confirm_claim_extraction(
            resolved=resolved,
            edition_name="acme_weekly",
            issue_number=1,
            confirmed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            narratives={"exec_summary.md": "Synthetic narrative."},
            items=(),
            valid_workstream_ids=(),
            legacy_regex_extractor=False,
            programs_root=tmp_path,
        )

    assert isinstance(caught.value.__cause__, ClaimExtractorBudgetError)
    assert caught.value.__cause__.__cause__ is budget_error
    factory.assert_called_once()
    provider.structured.assert_called_once()


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("released_fallback", [False, True])
@pytest.mark.parametrize("failure_stage", ["initial-estimate", "retry-estimate", "retry-projection"])
def test_preflight_failure_reaches_runner_without_retry(
    tmp_path, monkeypatch, batch, released_fallback, failure_stage,
) -> None:
    from src.core.ai_telemetry import append_ai_telemetry

    monkeypatch.setattr(
        "src.core.ai_telemetry.append_ai_telemetry",
        lambda record: append_ai_telemetry(record, programs_root=tmp_path / "telemetry"),
    )
    primary, estimate, _ = _retry_reservation_client(tmp_path, monkeypatch)
    guard = primary._run_cost_guard
    assert guard is not None
    failure = ValueError("private synthetic preflight detail")
    if failure_stage == "initial-estimate":
        estimate.side_effect = [failure, 100]
    elif failure_stage == "retry-estimate":
        estimate.side_effect = [100, failure, 100]
    else:
        persist_projection = guard._persist_projection
        projection_calls = 0

        def fail_second_projection(state):
            nonlocal projection_calls
            projection_calls += 1
            if projection_calls == 2:
                raise failure
            persist_projection(state)

        monkeypatch.setattr(guard, "_persist_projection", fail_second_projection)

    _FakeAzureOpenAI.actions = [
        _FakeRateLimitError("private provider error"),
        _FakeResponse('{"ok": true}', prompt_tokens=50, completion_tokens=0),
    ]
    _FakeAzureOpenAI.create_calls = 0
    factory = Mock(return_value=primary)
    client = FallbackAIClient(
        deployments=("summary-model", "summary-backup"),
        temperature=0.2,
        budget_usd=10.0,
        client_factory=factory,
        retryable_exceptions=(Exception,),
    )
    provider = Mock(
        side_effect=(
            (lambda *_: client.structured("system", "user", parser=lambda value: value, max_tokens=100))
            if batch else (lambda *_: client.chat("system", "user", max_tokens=100))
        ),
    )
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.RELEASED if released_fallback else FeatureOutcomeStatus.ABSTAINED,
        value="safe local value" if released_fallback else None,
        tier=Tier.NONE,
        limitations=("existing limitation",),
        evidence_refs=("safe-evidence",),
        retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)
    decisions = []

    outcomes = _dispatch_frontier(_runner(tmp_path, decision_sink=decisions.append), spec, batch)

    factory.assert_called_once()
    provider.assert_called_once()
    assert _FakeAzureOpenAI.create_calls == (0 if failure_stage == "initial-estimate" else 1)
    if failure_stage == "initial-estimate":
        assert not guard.ledger_path.exists()
    else:
        rows = _retry_reservation_rows(primary)
        assert len(rows) == (2 if failure_stage == "retry-projection" else 1)
        assert all(row[1] == "reserved" for row in rows)
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {
        "remote": 1, "frontier": 1,
    }
    expected_status = (
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK if released_fallback else FeatureOutcomeStatus.ABSTAINED
    )
    assert all(outcome.status == expected_status for outcome in outcomes)
    assert all(
        outcome.limitations == ("existing limitation", "ai_preflight_failed") and not outcome.retryable
        for outcome in outcomes
    )
    assert all(outcome.evidence_refs == ("safe-evidence",) for outcome in outcomes)
    assert decisions[-1].outcome is RouteOutcome.FRONTIER_BLOCKED
    assert decisions[-1].frontier_called


@pytest.mark.parametrize("batch", [False, True])
def test_preflight_failure_does_not_stop_later_independent_batch_items(tmp_path, batch) -> None:
    provider = Mock(
        side_effect=[
            AIPreflightError("synthetic preflight failure"),
            BatchProviderResult(responses={"second": "safe"}) if batch else "safe",
        ],
    )
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE, retryable=True,
    )
    spec = replace(
        _frontier_test_spec(tmp_path, batch, provider, None, None),
        fallback_handler=lambda _: fallback,
        max_batch_size=1 if batch else None,
    )

    outcomes = list(_runner(tmp_path).run_batch(spec, ["first", "second"]).values())

    assert provider.call_count == 2
    assert outcomes[0].status == FeatureOutcomeStatus.ABSTAINED
    assert outcomes[0].limitations == ("ai_preflight_failed",)
    assert not outcomes[0].retryable
    assert outcomes[1].status == FeatureOutcomeStatus.RELEASED
    assert outcomes[1].value == "safe"
