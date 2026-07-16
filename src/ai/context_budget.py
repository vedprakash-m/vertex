"""Re-export shim for the Zone-A context budget (ADF-W0.14).

The deterministic token estimation and truncation logic moved to
``src/core/context_budget.py`` so the context-budget authority lives in Zone A
(INV-ADF-17). This module remains so existing imports under ``src.ai`` keep
working; new code should import from ``src.core.context_budget`` directly.
"""

from __future__ import annotations

from src.core.context_budget import (  # noqa: F401
    ContextBudgetInput,
    ContextBudgetResult,
    estimate_tokens,
    extract_dated_updates,
    truncate_for_ai,
)

__all__ = [
    "ContextBudgetInput",
    "ContextBudgetResult",
    "estimate_tokens",
    "extract_dated_updates",
    "truncate_for_ai",
]
