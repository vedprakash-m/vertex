"""Contract: PB-18 — AI degrade is loud, not silent.

When all AI deployments are exhausted (missing endpoint / expired creds /
all-fail), ``FallbackAIClient`` raises ``AIClientError`` immediately.
The caller is responsible for deciding whether to surface the error or fall
back to deterministic output, but the AI layer never swallows the exception
silently.

§9a P2 acceptance criterion: "loud degrade + telemetry":

  (a) ``FallbackAIClient.chat()`` with empty ``deployments=()`` raises
      ``AIClientError`` — not ``None``, not a silent empty string.
  (b) ``FallbackAIClient.chat()`` with all-failing deployments raises
      ``AIClientError`` (exception is propagated, not swallowed).
  (c) The no-deployment error message is actionable (references env var names).
  (d) ``FallbackAIClient.structured()`` with empty deployments also raises
      ``AIClientError`` (not only ``chat``).
  (e) ``AiTelemetryStatus.FALLBACK`` constant exists so callers that catch
      ``AIClientError`` and fall back deterministically can record the degrade
      event in the telemetry sidecar.
"""
from __future__ import annotations

import pytest

from src.ai.client import AIClientError
from src.ai.deployment_fallback import (
    FallbackAIClient,
    _MISSING_AZURE_OPENAI_DEPLOYMENT_MESSAGE,
)
from src.core.ai_telemetry import AiTelemetryStatus


# ---------------------------------------------------------------------------
# (a) Empty deployments → AIClientError raised (not silent)
# ---------------------------------------------------------------------------

def test_fallback_client_empty_deployments_chat_raises() -> None:
    """chat() with deployments=() must raise AIClientError, not return silently."""
    client = FallbackAIClient(
        deployments=(),
        temperature=0.0,
        budget_usd=1.0,
    )
    with pytest.raises(AIClientError):
        client.chat("system", "user")


def test_fallback_client_empty_deployments_error_mentions_deployment() -> None:
    """The no-deployment error must mention 'deployment' (actionable message)."""
    client = FallbackAIClient(
        deployments=(),
        temperature=0.0,
        budget_usd=1.0,
    )
    with pytest.raises(AIClientError) as exc_info:
        client.chat("system", "user")
    assert "deployment" in str(exc_info.value).lower(), (
        f"AIClientError message must mention 'deployment' to be actionable; "
        f"got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# (b) All-failing deployments → AIClientError propagated (not swallowed)
# ---------------------------------------------------------------------------

def test_fallback_client_all_failing_deployments_chat_raises() -> None:
    """chat() with all-failing deployments must propagate AIClientError."""

    def _bad_factory(**_kwargs):
        raise AIClientError("endpoint unavailable: 503")

    client = FallbackAIClient(
        deployments=("fake-deployment-1", "fake-deployment-2"),
        temperature=0.0,
        budget_usd=1.0,
        client_factory=_bad_factory,
    )
    with pytest.raises(AIClientError, match="endpoint unavailable"):
        client.chat("system", "user")


# ---------------------------------------------------------------------------
# (d) structured() with empty deployments also raises (not only chat)
# ---------------------------------------------------------------------------

def test_fallback_client_empty_deployments_structured_raises() -> None:
    """structured() with deployments=() must also raise AIClientError."""
    client = FallbackAIClient(
        deployments=(),
        temperature=0.0,
        budget_usd=1.0,
    )
    with pytest.raises(AIClientError):
        client.structured("system", "user", parser=lambda x: x)


# ---------------------------------------------------------------------------
# (c) _MISSING_AZURE_OPENAI_DEPLOYMENT_MESSAGE is actionable
# ---------------------------------------------------------------------------

def test_missing_deployment_message_is_actionable() -> None:
    """_MISSING_AZURE_OPENAI_DEPLOYMENT_MESSAGE must reference actionable env var names."""
    msg = _MISSING_AZURE_OPENAI_DEPLOYMENT_MESSAGE
    assert "VERTEX_AI_DEPLOYMENT" in msg or "AZURE_OPENAI_DEPLOYMENT" in msg, (
        "The no-deployment message must reference at least one env var name "
        f"so operators know what to set. Got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# (e) AiTelemetryStatus.FALLBACK constant exists for callers recording degrade
# ---------------------------------------------------------------------------

def test_ai_telemetry_status_fallback_constant_exists() -> None:
    """AiTelemetryStatus.FALLBACK must exist so callers can record degrade events."""
    assert hasattr(AiTelemetryStatus, "FALLBACK"), (
        "AiTelemetryStatus must expose a FALLBACK constant. "
        "Feature callers that catch AIClientError and fall back deterministically "
        "need this status to record the degrade event in the telemetry sidecar."
    )
    assert AiTelemetryStatus.FALLBACK == "fallback"


def test_ai_telemetry_status_fallback_distinct_from_other_statuses() -> None:
    """FALLBACK must not collide with any other status code."""
    all_statuses = {
        AiTelemetryStatus.OK,
        AiTelemetryStatus.RATE_LIMIT,
        AiTelemetryStatus.CONTEXT_LENGTH,
        AiTelemetryStatus.AUTH,
        AiTelemetryStatus.TIMEOUT,
        AiTelemetryStatus.BUDGET_EXCEEDED,
        AiTelemetryStatus.OTHER,
    }
    assert AiTelemetryStatus.FALLBACK not in all_statuses, (
        f"FALLBACK status value {AiTelemetryStatus.FALLBACK!r} collides with another status."
    )
