"""Contract: every AI feature reads its generation parameters from the
``ai_policy.yaml`` single source of truth (Phase 4 / Step 7b, D-12).

These guards exist so that ``vertex/policies/ai_policy.yaml`` stays the only
place that defines per-feature ``max_tokens`` / ``temperature``. They fail if a
module reintroduces a hardcoded token budget or temperature literal at an AI
call site, or stops consuming ``load_ai_feature_policy``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core import policy_loader


_AI_DIR = Path(__file__).resolve().parents[2] / "src" / "ai"

# feature_name -> module file. The feature_name must match an explicit key in
# vertex/policies/ai_policy.yaml (no silent fallback to ``default``).
_FEATURE_MODULES: dict[str, str] = {
    "action_extractor": "action_extractor.py",
    "anticipation_engine": "anticipation_engine.py",
    "backfill_extractor": "backfill_extractor.py",
    "blurb_generator": "blurb_generator.py",
    "claim_extractor": "claim_extractor.py",
    "context_synthesizer": "context_synthesizer.py",
    "decision_brief_advisor": "decision_brief_advisor.py",
    "exec_summary_drafter": "exec_summary_drafter.py",
    "intent_router": "intent_router.py",
    "learning_distiller": "learning_distiller.py",
    "m365_topic_router": "m365_topic_router.py",
    "onboard_assistant": "onboard_assistant.py",
    "summary_generator": "summary_generator.py",
    "synthesizer": "synthesizer.py",
}

# Modules that wire only ``max_tokens`` through policy. The remaining modules
# (extractors / routers that construct their own client) also wire
# ``temperature`` and must carry no temperature literal at the client seam.
_TEMPERATURE_WIRED = frozenset(
    {
        "action_extractor",
        "backfill_extractor",
        "claim_extractor",
        "intent_router",
        "learning_distiller",
        "m365_topic_router",
        "onboard_assistant",
        "summary_generator",
    }
)


def _module_source(module_file: str) -> str:
    return (_AI_DIR / module_file).read_text(encoding="utf-8")


def _numeric_keyword_literals(source: str, keyword_name: str) -> list[int | float]:
    """Return numeric literals passed as ``keyword_name=...`` in *call* sites.

    Annotated parameter defaults in ``def``/Protocol signatures (e.g.
    ``max_tokens: int = 800``) are *not* call keywords, so they are ignored.
    """
    tree = ast.parse(source)
    literals: list[int | float] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != keyword_name:
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)) and not isinstance(
                value.value, bool
            ):
                literals.append(value.value)
    return literals


@pytest.mark.parametrize("feature_name", sorted(_FEATURE_MODULES))
def test_feature_has_explicit_policy_entry(feature_name: str) -> None:
    document = policy_loader._load_ai_policy_document()
    ai_features = document["ai_features"]
    assert feature_name in ai_features, (
        f"ai_features.{feature_name} missing from ai_policy.yaml; "
        "every wired AI feature must have an explicit (non-default) entry."
    )


@pytest.mark.parametrize("feature_name,module_file", sorted(_FEATURE_MODULES.items()))
def test_module_consumes_feature_policy(feature_name: str, module_file: str) -> None:
    source = _module_source(module_file)
    assert "load_ai_feature_policy" in source, (
        f"{module_file} must consume load_ai_feature_policy for its generation params."
    )


@pytest.mark.parametrize("feature_name,module_file", sorted(_FEATURE_MODULES.items()))
def test_no_max_tokens_literal_at_call_sites(feature_name: str, module_file: str) -> None:
    literals = _numeric_keyword_literals(_module_source(module_file), "max_tokens")
    assert not literals, (
        f"{module_file} passes hardcoded max_tokens={literals} at a call site; "
        "route it through load_ai_feature_policy(...) instead."
    )


@pytest.mark.parametrize(
    "feature_name,module_file",
    sorted((name, _FEATURE_MODULES[name]) for name in _TEMPERATURE_WIRED),
)
def test_no_temperature_literal_at_call_sites(feature_name: str, module_file: str) -> None:
    literals = _numeric_keyword_literals(_module_source(module_file), "temperature")
    assert not literals, (
        f"{module_file} passes hardcoded temperature={literals} at a call site; "
        "route it through load_ai_feature_policy(...) instead."
    )
