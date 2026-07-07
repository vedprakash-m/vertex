from __future__ import annotations

from src.ai.safety.causality_sanitizer import sanitize_text


def test_sanitize_text_rewrites_banned_causal_phrases() -> None:
    result = sanitize_text("Risk increased due to rollout delays and resulted in missed validation.")

    assert result.changed is True
    assert result.sanitized_text == "Risk increased after rollout delays and was followed by missed validation."
    assert [violation.phrase for violation in result.violations] == ["due to", "resulted in"]


def test_sanitize_text_preserves_capitalization() -> None:
    result = sanitize_text("Because of the delay, the deployment slipped.")

    assert result.sanitized_text == "After the delay, the deployment slipped."


def test_sanitize_text_leaves_clean_text_unchanged() -> None:
    result = sanitize_text("Risk increased after the rollout window moved and validation remained incomplete.")

    assert result.changed is False
    assert result.sanitized_text == "Risk increased after the rollout window moved and validation remained incomplete."
    assert result.violations == ()


def test_sanitize_text_rewrites_multiple_distinct_causal_phrases() -> None:
    result = sanitize_text("ETA slipped because of testing gaps that led to a deferred sign-off.")

    assert result.sanitized_text == "ETA slipped after testing gaps that preceded a deferred sign-off."
    assert [violation.phrase for violation in result.violations] == ["led to", "because of"] or ["because of", "led to"]