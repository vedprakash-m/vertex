from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.ai.ai_mode import AIMode
from src.ai.claim_extractor import ClaimExtractor, ClaimExtractorBudgetError
from src.ai.client import AIClient, AIClientError, BudgetExceeded
from src.ai.cost_guard import CostGuard
from src.ai.deployment_fallback import FallbackAIClient, FallbackStructuredClient
from src.ai.llm_trace import AITraceContext
from src.ai.structured_feature_runner import FeatureOutcome, FeatureOutcomeStatus
from src.ai.tiered_router import Tier
from src.commands.confirm_stages.claim_resolution import resolve_confirm_claim_extraction
from src.core.ledger.event_log import read_events
from tests.unit.test_structured_feature_runner import (
    _ceiling_counts,
    _dispatch_frontier,
    _frontier_test_spec,
    _runner,
)


@pytest.mark.parametrize("client_class", [FallbackAIClient, FallbackStructuredClient])
@pytest.mark.parametrize("method", ["chat", "structured"])
@pytest.mark.parametrize("budget_at", ["primary", "backup"])
@pytest.mark.parametrize("origin", ["factory", "provider"])
@pytest.mark.parametrize("custom_retry", [False, True])
def test_budget_exhaustion_stops_deployment_fallback(
    client_class, method, budget_at, origin, custom_retry,
) -> None:
    budget_error = BudgetExceeded("synthetic budget exhaustion")
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

    with pytest.raises(BudgetExceeded) as caught:
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
        metadata={"run_budget_usd": budget},
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
