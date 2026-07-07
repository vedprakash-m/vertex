"""D-20: process trace context (ContextVar) mechanism + AIClient fallback."""

from __future__ import annotations

import pytest

from src.ai.client import AIClient
from src.ai.llm_trace import (
    AITraceContext,
    get_current_trace_context,
    use_trace_context,
)


def _ctx(run_id: str) -> AITraceContext:
    return AITraceContext(edition="demo", run_id=run_id, caller="test")


def test_default_is_none() -> None:
    assert get_current_trace_context() is None


def test_use_trace_context_sets_and_restores() -> None:
    ctx = _ctx("run-1")
    assert get_current_trace_context() is None
    with use_trace_context(ctx):
        assert get_current_trace_context() is ctx
    assert get_current_trace_context() is None


def test_use_trace_context_nests_and_restores_previous() -> None:
    outer = _ctx("outer")
    inner = _ctx("inner")
    with use_trace_context(outer):
        assert get_current_trace_context() is outer
        with use_trace_context(inner):
            assert get_current_trace_context() is inner
        assert get_current_trace_context() is outer
    assert get_current_trace_context() is None


def test_use_trace_context_restores_on_exception() -> None:
    ctx = _ctx("run-err")
    with pytest.raises(RuntimeError):
        with use_trace_context(ctx):
            raise RuntimeError("boom")
    assert get_current_trace_context() is None


def test_ai_client_falls_back_to_context_var() -> None:
    ctx = _ctx("run-fallback")
    with use_trace_context(ctx):
        client = AIClient(deployment="default", temperature=0.0, budget_usd=1.0, endpoint="https://example.invalid", api_key="test-key")
    assert client._trace_context is ctx


def test_ai_client_explicit_trace_context_wins_over_context_var() -> None:
    bound = _ctx("bound")
    explicit = _ctx("explicit")
    with use_trace_context(bound):
        client = AIClient(
            deployment="default",
            temperature=0.0,
            budget_usd=1.0,
            endpoint="https://example.invalid",
            api_key="test-key",
            trace_context=explicit,
        )
    assert client._trace_context is explicit


def test_ai_client_without_context_var_has_no_trace_context() -> None:
    client = AIClient(deployment="default", temperature=0.0, budget_usd=1.0, endpoint="https://example.invalid", api_key="test-key")
    assert client._trace_context is None
