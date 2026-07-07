"""Learning-distiller construction helpers for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). These build the
``LearningDistiller`` and its AI trace context used by confirm's post-archive
learning distillation. ``build_default_learning_distiller`` retains the
backward-compatible ``inspect.signature`` shim so a patched/older
``build_learning_distiller`` that does not accept ``trace_context`` still works.
``confirm.py`` imports the two entry points it calls
(``build_default_learning_distiller``, ``build_learning_distillation_trace_context``)
under their historical private aliases.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.learning_distiller import LearningDistiller
from src.ai.llm_trace import AITraceContext, use_trace_context


def build_learning_distiller(*, trace_context: AITraceContext | None = None) -> LearningDistiller:
    with use_trace_context(trace_context):
        return LearningDistiller.from_environment(trace_context=trace_context)


def build_learning_distillation_trace_context(
    *,
    edition_name: str,
    issue_number: int,
) -> AITraceContext:
    current_time = datetime.now(timezone.utc)
    return AITraceContext(
        edition=edition_name,
        run_id=f"{edition_name}:confirm:learning:{issue_number:03d}:{current_time.strftime('%Y%m%dT%H%M%SZ')}",
        caller="src.commands.confirm._record_learning_distillation",
        metadata={
            "edition_name": edition_name,
            "issue_number": issue_number,
            "task_type": "learning_distillation",
            "run_budget_usd": 0.5,
        },
    )


def build_default_learning_distiller(*, trace_context: AITraceContext) -> LearningDistiller:
    if get_ai_mode() == AIMode.DISABLED:
        return LearningDistiller(client=None)
    if "trace_context" in inspect.signature(build_learning_distiller).parameters:
        return build_learning_distiller(trace_context=trace_context)
    return build_learning_distiller()
