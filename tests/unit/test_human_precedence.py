"""ADF-W2.9: unit tests for src/core/human_precedence.py."""

from __future__ import annotations

from src.core.human_precedence import resolve_human_or_ai_text, should_ai_fill


def test_should_ai_fill_true_for_none_and_blank() -> None:
    assert should_ai_fill(None) is True
    assert should_ai_fill("") is True
    assert should_ai_fill("   ") is True


def test_should_ai_fill_false_when_human_text_present() -> None:
    assert should_ai_fill("Human wrote this.") is False


def test_resolve_prefers_human_text_when_present() -> None:
    resolution = resolve_human_or_ai_text(human_text="Human wrote this.", ai_text="AI draft.")
    assert resolution.text == "Human wrote this."
    assert resolution.source == "human"


def test_resolve_never_overwrites_non_empty_human_text_even_if_ai_text_is_also_present() -> None:
    resolution = resolve_human_or_ai_text(human_text="Keep me.", ai_text="Should not appear.")
    assert resolution.text == "Keep me."
    assert resolution.source == "human"


def test_resolve_fills_with_ai_text_when_human_text_is_empty() -> None:
    resolution = resolve_human_or_ai_text(human_text="", ai_text="AI draft.")
    assert resolution.text == "AI draft."
    assert resolution.source == "ai"


def test_resolve_stays_empty_when_both_are_empty() -> None:
    resolution = resolve_human_or_ai_text(human_text=None, ai_text=None)
    assert resolution.text == ""
    assert resolution.source == "human"


def test_resolve_ignores_blank_ai_text() -> None:
    resolution = resolve_human_or_ai_text(human_text="", ai_text="   ")
    assert resolution.text == ""
    assert resolution.source == "human"
