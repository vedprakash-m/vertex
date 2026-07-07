from __future__ import annotations

import inspect

LEGACY_DEPLOYMENT_ALIAS_NOTICE = "configure one of the supported Vertex deployment aliases."

_MISSING_AZURE_OPENAI_DEPLOYMENT_MESSAGE = (
    "No Azure OpenAI deployment is configured. "
    "Set VERTEX_AI_DEPLOYMENT, VERTEX_EXEC_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT; "
    f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE}"
)
import os
from typing import Any, Callable, TypeVar

from src.ai.client import AIClient, AIClientError
from src.ai.llm_trace import AITraceContext
from src.ai.provider import LLMProvider
from src.core.policy_loader import load_ai_feature_policy


StructuredResponse = TypeVar("StructuredResponse")


def resolve_ai_deployments(
    *,
    primary_candidates: tuple[str | None, ...],
    backup_candidates: tuple[str | None, ...],
    primary_fallback_envs: tuple[str, ...],
    backup_fallback_envs: tuple[str, ...],
) -> tuple[str, ...]:
    deployments: list[str] = []

    primary = _resolve_first_deployment(primary_candidates, fallback_envs=primary_fallback_envs)
    if primary is not None:
        deployments.append(primary)

    backup = _resolve_first_deployment(backup_candidates, fallback_envs=backup_fallback_envs)
    if backup is not None and backup not in deployments:
        deployments.append(backup)

    return tuple(deployments)


def resolve_ai_deployments_for_feature(
    *,
    feature_name: str,
    primary_candidates: tuple[str | None, ...],
    backup_candidates: tuple[str | None, ...],
    primary_fallback_envs: tuple[str, ...],
    backup_fallback_envs: tuple[str, ...],
) -> tuple[str, ...]:
    """Resolve the deployment list for a feature, honoring the
    `frontier_eligible` flag in `ai_policy.yaml`.

    Phase 4 frontier_eligible enforcement (rev. 335): if the feature's
    policy has `frontier_eligible: false`, return an empty tuple so
    the feature falls back to its deterministic path entirely. The
    feature is responsible for handling the empty-deployment case
    (typically by raising a deterministic-only error or returning a
    deterministic fallback).

    Why:** `frontier_eligible` is a per-feature kill switch that says
    \"this feature is OK to call the frontier model.\" Setting it to
    `false` enforces a hard no-frontier policy at the deployment
    resolution layer, which is the earliest possible point in the
    call chain. The alternative (checking the flag at the call site
    inside each feature) is fragile and easy to forget.
    **How to apply:** in `ai_policy.yaml`, set
    `ai_features.<feature>.frontier_eligible: false` to force a
    feature into deterministic-only mode. Useful for: (a) features
    where the frontier adds cost without accuracy gain, (b)
    features that have a known-good deterministic path that we
    want to keep tested, (c) features being retired from frontier
    use temporarily.
    """
    policy = load_ai_feature_policy(feature_name)
    if not policy.frontier_eligible:
        # Frontier is disabled for this feature. Return an empty
        # tuple so the caller (typically `FallbackAIClient` /
        # `client_factory`) sees no deployments and falls back to
        # the deterministic path.
        return ()
    return resolve_ai_deployments(
        primary_candidates=primary_candidates,
        backup_candidates=backup_candidates,
        primary_fallback_envs=_expand_feature_fallback_envs(
            primary_fallback_envs,
            model_tier=policy.model_tier,
        ),
        backup_fallback_envs=_expand_feature_fallback_envs(
            backup_fallback_envs,
            model_tier=policy.model_tier,
        ),
    )


class FallbackAIClient:
    def __init__(
        self,
        *,
        deployments: tuple[str, ...],
        temperature: float,
        budget_usd: float,
        requests_per_minute: int | None = None,
        trace_context: AITraceContext | None = None,
        client_factory: Callable[..., LLMProvider] | None = None,
        retryable_exceptions: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self._deployments = deployments
        self._temperature = temperature
        self._budget_usd = budget_usd
        self._requests_per_minute = requests_per_minute
        self._trace_context = trace_context
        self._client_factory = client_factory or AIClient
        self._retryable_exceptions = retryable_exceptions or (AIClientError, RuntimeError)
        self._clients: dict[str, LLMProvider] = {}

    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> str:
        last_error: BaseException | None = None

        for deployment in self._deployments:
            try:
                client = self._get_client(deployment)
                return client.chat(
                    system,
                    user,
                    max_tokens=max_tokens,
                    prompt_version=prompt_version,
                )
            except self._retryable_exceptions as error:
                last_error = error
                continue

        if last_error is not None:
            raise AIClientError(str(last_error)) from last_error
        raise AIClientError(_MISSING_AZURE_OPENAI_DEPLOYMENT_MESSAGE)

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> StructuredResponse:
        last_error: BaseException | None = None

        for deployment in self._deployments:
            try:
                client = self._get_client(deployment)
                return client.structured(
                    system,
                    user,
                    parser=parser,
                    max_tokens=max_tokens,
                    prompt_version=prompt_version,
                )
            except self._retryable_exceptions as error:
                last_error = error
                continue

        if last_error is not None:
            raise AIClientError(str(last_error)) from last_error
        raise AIClientError(_MISSING_AZURE_OPENAI_DEPLOYMENT_MESSAGE)

    def _get_client(self, deployment: str) -> LLMProvider:
        client = self._clients.get(deployment)
        if client is not None:
            return client

        client_kwargs: dict[str, Any] = {
            "deployment": deployment,
            "temperature": self._temperature,
            "budget_usd": self._budget_usd,
        }
        if self._requests_per_minute is not None and _client_factory_supports_parameter(self._client_factory, "requests_per_minute"):
            client_kwargs["requests_per_minute"] = self._requests_per_minute
        if self._trace_context is not None and _client_factory_supports_parameter(self._client_factory, "trace_context"):
            client_kwargs["trace_context"] = self._trace_context

        client = self._client_factory(**client_kwargs)
        self._clients[deployment] = client
        return client


class FallbackStructuredClient(FallbackAIClient):
    pass


def _client_factory_supports_trace_context(client_factory: Callable[..., LLMProvider]) -> bool:
    return _client_factory_supports_parameter(client_factory, "trace_context")


def _client_factory_supports_parameter(client_factory: Callable[..., LLMProvider], parameter_name: str) -> bool:
    try:
        parameters = inspect.signature(client_factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == parameter_name
        for parameter in parameters
    )


def _resolve_first_deployment(
    candidates: tuple[str | None, ...],
    *,
    fallback_envs: tuple[str, ...],
) -> str | None:
    for candidate in candidates:
        resolved = expand_ai_deployment(candidate)
        if resolved is not None:
            return resolved

    for env_name in fallback_envs:
        for candidate_env in _expand_fallback_env_names(env_name):
            resolved = expand_ai_deployment(os.environ.get(candidate_env))
            if resolved is not None:
                return resolved

    return None


def _expand_fallback_env_names(env_name: str) -> tuple[str, ...]:
    names = (env_name,)
    deduped: list[str] = []
    for name in names:
        if name not in deduped:
            deduped.append(name)
    return tuple(deduped)


def _expand_feature_fallback_envs(
    env_names: tuple[str, ...],
    *,
    model_tier: str,
) -> tuple[str, ...]:
    expanded: list[str] = []
    for env_name in env_names:
        for candidate in _tier_specific_env_names(env_name, model_tier=model_tier):
            if candidate not in expanded:
                expanded.append(candidate)
        if env_name not in expanded:
            expanded.append(env_name)
    return tuple(expanded)


def _tier_specific_env_names(env_name: str, *, model_tier: str) -> tuple[str, ...]:
    if model_tier == "standard":
        tier_token = "STANDARD"
    elif model_tier == "premium":
        tier_token = "PREMIUM"
    elif model_tier == "mini":
        tier_token = "MINI"
    else:
        return ()

    if env_name.endswith("_BACKUP_DEPLOYMENT"):
        prefix = env_name[: -len("_BACKUP_DEPLOYMENT")]
        return (f"{prefix}_{tier_token}_BACKUP_DEPLOYMENT",)
    if env_name.endswith("_DEPLOYMENT"):
        prefix = env_name[: -len("_DEPLOYMENT")]
        return (f"{prefix}_{tier_token}_DEPLOYMENT",)
    return ()


def expand_ai_deployment(value: str | None) -> str | None:
    if value is None:
        return None

    stripped = value.strip()
    if not stripped:
        return None

    if stripped.startswith("${") and stripped.endswith("}"):
        env_name = stripped[2:-1].strip()
        resolved = os.environ.get(env_name)
        return resolved.strip() if resolved and resolved.strip() else None

    if "${" in stripped:
        return None

    return stripped
