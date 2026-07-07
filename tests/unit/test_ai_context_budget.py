from __future__ import annotations

import pytest

from src.ai.context_budget import ContextBudgetInput, estimate_tokens, extract_dated_updates, truncate_for_ai


def test_estimate_tokens_falls_back_when_tiktoken_is_unavailable(monkeypatch) -> None:
    def _raise_import_error(_name: str):
        raise ImportError("tiktoken not installed")

    monkeypatch.setattr("src.ai.context_budget.importlib.import_module", _raise_import_error)

    assert estimate_tokens("abcdefgh") == 2


def test_extract_dated_updates_splits_static_and_update_blocks() -> None:
    static_content, updates = extract_dated_updates(
        "Background context.\n\n**Update 5/1:**\nFirst update.\n\n**Update 5/3:**\nSecond update."
    )

    assert static_content == "Background context."
    assert len(updates) == 2
    assert "First update." in updates[0]
    assert "Second update." in updates[1]


def test_truncate_for_ai_preserves_recent_updates_and_comments_before_background() -> None:
    background = (
        "Static background start. "
        + ("A" * 600)
        + "\n\n**Update 5/1:**\nOlder update."
        + "\n\n**Update 5/4:**\nMost recent update."
    )
    content = ContextBudgetInput(
        title="Cache warmup safeguard",
        summary="High-risk deployment blocker.",
        background=background,
        recent_comments=("Comment one.", "Comment two.", "Comment three."),
    )

    result = truncate_for_ai(content, max_tokens=80, recent_update_limit=1, recent_comment_limit=2)

    assert result.was_truncated is True
    assert "Most recent update." in result.content
    assert "Older update." not in result.content
    assert "Comment two." in result.content
    assert "Comment three." in result.content
    assert "Background:" in result.content
    assert "[... content truncated ...]" in result.content


def test_truncate_for_ai_keeps_full_background_when_within_budget() -> None:
    content = ContextBudgetInput(
        title="Cache warmup safeguard",
        summary="High-risk deployment blocker.",
        background="Static background.",
        recent_comments=("Comment one.",),
    )

    result = truncate_for_ai(content, max_tokens=400)

    assert result.was_truncated is False
    assert "Static background." in result.content
    assert result.final_tokens <= result.original_tokens