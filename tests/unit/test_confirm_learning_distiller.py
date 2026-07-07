"""Direct coverage for the extracted learning-distiller construction helpers.

Guards the D-25 / Phase 3 extraction from ``src/commands/confirm.py`` into
``src/commands/confirm_stages/learning_distiller.py``, including the
backward-compatible ``inspect.signature`` shim in
``build_default_learning_distiller``.
"""

from __future__ import annotations

from pathlib import Path

from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.llm_trace import AITraceContext, get_current_trace_context
from src.commands.confirm_stages import learning_distiller
from src.commands.confirm_stages.learning_distiller import (
    build_default_learning_distiller,
    build_learning_distillation_trace_context,
)


def test_trace_context_shape() -> None:
    ctx = build_learning_distillation_trace_context(
        edition_name="acme_weekly", issue_number=7
    )
    assert ctx.edition == "acme_weekly"
    assert ctx.run_id.startswith("acme_weekly:confirm:learning:007:")
    assert ctx.caller == "src.commands.confirm._record_learning_distillation"
    assert ctx.metadata["task_type"] == "learning_distillation"
    assert ctx.metadata["issue_number"] == 7
    assert ctx.metadata["run_budget_usd"] == 0.5


def test_default_distiller_passes_trace_context_when_supported(monkeypatch) -> None:
    captured = {}

    def _fake_build(*, trace_context=None):
        captured["trace_context"] = trace_context
        return "distiller"

    monkeypatch.setattr(learning_distiller, "build_learning_distiller", _fake_build)
    # build_default_learning_distiller short-circuits to a disabled distiller when
    # the process-global AI mode is DISABLED (learning_distiller.py:49). Pin to
    # ACTIVE so this test does not depend on whatever mode a parallel xdist worker
    # left behind — the assertion below only holds on the build path.
    set_ai_mode(AIMode.ACTIVE)
    try:
        result = build_default_learning_distiller(trace_context="ctx-sentinel")
    finally:
        set_ai_mode(AIMode.ACTIVE)
    assert result == "distiller"
    assert captured["trace_context"] == "ctx-sentinel"


def test_default_distiller_falls_back_when_no_trace_param(monkeypatch) -> None:
    calls = {"n": 0}

    def _legacy_build():  # no trace_context parameter
        calls["n"] += 1
        return "legacy-distiller"

    monkeypatch.setattr(learning_distiller, "build_learning_distiller", _legacy_build)
    # See note above: pin AI mode to ACTIVE so the build path runs regardless of
    # parallel-worker global state. Without this, the test flakes under xdist when
    # another worker leaves the mode at DISABLED.
    set_ai_mode(AIMode.ACTIVE)
    try:
        result = build_default_learning_distiller(trace_context="ignored")
    finally:
        set_ai_mode(AIMode.ACTIVE)
    assert result == "legacy-distiller"
    assert calls["n"] == 1


def test_default_distiller_returns_disabled_distiller_without_calling_builder(monkeypatch) -> None:
    monkeypatch.setattr(
        learning_distiller,
        "build_learning_distiller",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("build_learning_distiller should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        result = build_default_learning_distiller(trace_context="ctx-sentinel")
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert isinstance(result, learning_distiller.LearningDistiller)


def test_build_learning_distiller_binds_trace_context_for_nested_helpers(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def _fake_from_environment(*, trace_context=None):
        seen["explicit"] = trace_context
        seen["bound"] = get_current_trace_context()
        return "distiller"

    monkeypatch.setattr(learning_distiller.LearningDistiller, "from_environment", _fake_from_environment)
    trace_context = AITraceContext(
        edition="acme_weekly",
        run_id="acme_weekly:confirm:learning:007:20260609T000000Z",
        caller="src.commands.confirm._record_learning_distillation",
        metadata={"task_type": "learning_distillation"},
    )

    result = learning_distiller.build_learning_distiller(trace_context=trace_context)

    assert result == "distiller"
    assert seen == {"explicit": trace_context, "bound": trace_context}
