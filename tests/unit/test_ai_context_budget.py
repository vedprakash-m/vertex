from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from src.ai.context_budget import ContextBudgetInput, estimate_tokens, extract_dated_updates, truncate_for_ai


def test_estimate_tokens_falls_back_when_tiktoken_is_unavailable(monkeypatch) -> None:
    def _raise_import_error(_name: str):
        raise ImportError("tiktoken not installed")

    # ADF-W0.14 moved the real implementation (and its `import importlib`)
    # to src/core/context_budget.py; src/ai/context_budget.py is now a pure
    # re-export shim with no `importlib` name of its own to patch.
    monkeypatch.setattr("src.core.context_budget.importlib.import_module", _raise_import_error)

    assert estimate_tokens("abcdefgh") == 2


@pytest.mark.parametrize("phase", ["import", "lookup", "asset", "encode"])
def test_legacy_estimate_preserves_existing_fallbacks(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    def encode(_text: str) -> list[int]:
        if phase == "encode":
            raise ValueError("synthetic encoder failure")
        return [1, 2, 3]

    def resolve(_model: str) -> SimpleNamespace:
        if phase in {"lookup", "asset"}:
            raise OSError("synthetic lookup failure")
        return SimpleNamespace(encode_ordinary=encode)

    def load(_name: str) -> SimpleNamespace:
        if phase == "asset":
            raise OSError("synthetic fallback failure")
        return SimpleNamespace(encode_ordinary=encode)

    module = SimpleNamespace(encoding_for_model=resolve, get_encoding=load)
    original_import = importlib.import_module

    def import_module(name: str, package: str | None = None) -> object:
        if name == "tiktoken":
            if phase == "import":
                raise ImportError("synthetic import failure")
            return module
        return original_import(name, package)

    monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
    assert estimate_tokens("abcdefgh") == (3 if phase == "lookup" else 2)


def test_default_estimator_strict_helper_still_resolves_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = importlib.import_module
    available = False
    calls = 0
    module = SimpleNamespace(
        encoding_name_for_model=lambda _model: "synthetic-encoding",
        get_encoding=lambda _name: SimpleNamespace(encode_ordinary=lambda _text: [1, 2, 3]),
    )

    def import_module(name: str, package: str | None = None) -> object:
        nonlocal calls
        if name != "tiktoken":
            return original_import(name, package)
        calls += 1
        if not available:
            raise ModuleNotFoundError("absent", name="tiktoken")
        return module

    monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
    assert estimate_tokens("", fail_closed=True) == 0
    assert estimate_tokens("abcdefgh", fail_closed=True) == 2
    available = True
    assert estimate_tokens("abcdefgh", fail_closed=True) == 3
    assert calls == 2


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4"])
def test_estimate_tokens_counts_literal_special_token_as_text(model: str) -> None:
    tiktoken = pytest.importorskip("tiktoken")
    text = "prefix <|endoftext|> " + "\u6f22\u8a9e\U0001f642" * 24
    expected = len(tiktoken.encoding_for_model(model).encode_ordinary(text))

    assert expected > len(text) // 4
    assert estimate_tokens(text, model=model) == expected


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