from __future__ import annotations

from enum import Enum

from src.core.policy_loader import load_ai_request_router_policy


class AIMode(str, Enum):
    ACTIVE = "active"
    OBSERVE_ONLY = "observe_only"
    DISABLED = "disabled"


_CURRENT_MODE: AIMode = AIMode.ACTIVE


def set_ai_mode(mode: AIMode) -> None:
    global _CURRENT_MODE
    _CURRENT_MODE = mode


def get_ai_mode() -> AIMode:
    if _CURRENT_MODE is not AIMode.ACTIVE:
        return _CURRENT_MODE
    if load_ai_request_router_policy().observe_only:
        return AIMode.OBSERVE_ONLY
    return AIMode.ACTIVE
