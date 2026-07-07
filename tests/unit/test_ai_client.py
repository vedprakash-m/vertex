from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest

from src.ai.client import AIClient, AIClientError, BudgetExceeded
from src.ai.cost_guard import CostGuard
from src.ai.deployment_fallback import FallbackAIClient, FallbackStructuredClient, resolve_ai_deployments, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.provider import LLMProvider
from src.ai.request_router import AIRequestRouter
from src.core.policy_loader import AIFeaturePolicy, AIRequestRouterPolicy


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

    with pytest.raises(RuntimeError, match="requirements.txt"):
        AIClient("blurb-model", 0.2, 0.5)


def test_ai_client_retries_on_429_and_5xx_and_tracks_usage(monkeypatch, caplog) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
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

    client = AIClient("blurb-model", 0.2, 0.5, sleep_func=lambda seconds: sleep_calls.append(seconds))
    with caplog.at_level("INFO"):
        result = client.chat("system", "user", prompt_version="blurb.v1")

    assert result == "grounded text [#123]"
    assert sleep_calls == [0.5, 1.0]
    assert client.usage_stats.prompt_tokens == 1000
    assert client.usage_stats.completion_tokens == 400
    assert client.usage_stats.total_tokens == 1400
    assert client.spent_usd == pytest.approx(0.0007)
    assert _FakeAzureOpenAI.created_kwargs == {
        "azure_endpoint": "https://example.openai.azure.com",
        "api_key": "test-key",
        "api_version": "2024-02-01",
        "timeout": 20,
        "max_retries": 0,
    }
    assert "blurb.v1" in caplog.text


def test_ai_client_blocks_after_budget_is_spent(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    _FakeAzureOpenAI.actions = [
        _FakeResponse("first", prompt_tokens=1000, completion_tokens=1000),
        _FakeResponse("second", prompt_tokens=100, completion_tokens=100),
    ]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    client = AIClient("summary-model", 0.2, 0.001)

    assert client.chat("system", "user") == "first"
    assert client.spent_usd > 0.001

    with pytest.raises(BudgetExceeded, match="Spent"):
        client.chat("system", "user")

    assert _FakeAzureOpenAI.create_calls == 1


def test_ai_client_throttles_requests_per_minute(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
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
    )

    assert client.chat("system", "user") == "first"
    assert client.chat("system", "user") == "second"
    assert sleep_calls == [60.0]


def test_ai_client_surfaces_non_retryable_errors(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    _FakeAzureOpenAI.actions = [RuntimeError("bad request")]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    client = AIClient("summary-model", 0.2, 0.5)

    with pytest.raises(AIClientError, match="bad request"):
        client.chat("system", "user")


def test_ai_client_structured_returns_typed_payload_and_sets_json_response_format(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    _FakeAzureOpenAI.actions = [
        _FakeResponse('{"summary": "grounded text", "risk": "high"}', prompt_tokens=200, completion_tokens=60),
    ]
    monkeypatch.setattr(
        AIRequestRouter,
        "_get_sdk_types",
        lambda self: (_FakeAzureOpenAI, _FakeRateLimitError, _FakeAPIStatusError),
    )

    client = AIClient("structured-model", 0.2, 0.5)
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

    assert first_client.chat("system", "user") == "first"
    with pytest.raises(BudgetExceeded, match="actual AI spend exceeded the run ceiling"):
        second_client.chat("system", "user")

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
