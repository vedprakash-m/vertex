from __future__ import annotations

from src.ai.ai_mode import AIMode, get_ai_mode, set_ai_mode
from src.core.policy_loader import AIRequestRouterPolicy


def test_get_ai_mode_uses_observe_only_policy_when_invocation_mode_active(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.ai_mode.load_ai_request_router_policy",
        lambda: AIRequestRouterPolicy(observe_only=True),
    )
    set_ai_mode(AIMode.ACTIVE)
    try:
        assert get_ai_mode() is AIMode.OBSERVE_ONLY
    finally:
        set_ai_mode(AIMode.ACTIVE)


def test_get_ai_mode_preserves_explicit_disabled_mode_over_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.ai_mode.load_ai_request_router_policy",
        lambda: AIRequestRouterPolicy(observe_only=True),
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        assert get_ai_mode() is AIMode.DISABLED
    finally:
        set_ai_mode(AIMode.ACTIVE)
