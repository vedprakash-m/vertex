from __future__ import annotations

from src.ai.decision_brief_advisor import _FALLBACK_PROMPT, _load_prompt_template, _parse_advice
from src.ai.prompt_registry import PromptRegistryError


def test_parse_advice_scrubs_pii_from_reasoning_and_suggested_text() -> None:
    advice = _parse_advice(
        {
            "verdict": "REVISE",
            "reasoning": "Ask foo@gmail.com to confirm whether the risk is still open.",
            "suggested_text": "Escalate the dependency owner at foo@gmail.com before Friday.",
        }
    )

    assert advice is not None
    assert advice.verdict == "REVISE"
    assert "foo@gmail.com" not in advice.reasoning
    assert "foo@gmail.com" not in (advice.suggested_text or "")
    assert "[PII-FILTERED-EMAIL]" in advice.reasoning
    assert "[PII-FILTERED-EMAIL]" in (advice.suggested_text or "")


def test_parse_advice_downgrades_revise_without_suggested_text() -> None:
    advice = _parse_advice(
        {
            "verdict": "REVISE",
            "reasoning": "The section needs revision but no usable replacement was provided.",
            "suggested_text": None,
        }
    )

    assert advice is not None
    assert advice.verdict == "DEFER"
    assert advice.suggested_text is None


def test_load_prompt_template_falls_back_when_registry_resolution_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.decision_brief_advisor.load_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PromptRegistryError("missing")),
    )

    assert _load_prompt_template() == _FALLBACK_PROMPT