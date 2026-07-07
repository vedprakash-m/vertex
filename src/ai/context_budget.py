from __future__ import annotations

import importlib
import re
from dataclasses import dataclass


_UPDATE_HEADER_PATTERN = re.compile(r"(\*\*(?:Update\s+)?\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?.*?\*\*|##\s*Update.*?\n)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ContextBudgetInput:
    title: str
    summary: str | None
    background: str | None
    recent_comments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextBudgetResult:
    content: str
    was_truncated: bool
    original_tokens: int
    final_tokens: int


def estimate_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    if not text:
        return 0

    try:
        tiktoken = importlib.import_module("tiktoken")
    except ImportError:
        return max(1, len(text) // 4)

    try:
        encoding = tiktoken.encoding_for_model(model)
    except Exception:
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return max(1, len(text) // 4)
    try:
        return len(encoding.encode(text))
    except Exception:
        return max(1, len(text) // 4)


def extract_dated_updates(text: str) -> tuple[str, tuple[str, ...]]:
    if not text.strip():
        return "", ()

    pieces = _UPDATE_HEADER_PATTERN.split(text)
    if len(pieces) <= 1:
        return text.strip(), ()

    static_content = pieces[0].strip()
    updates: list[str] = []
    for index in range(1, len(pieces), 2):
        header = pieces[index]
        body = pieces[index + 1].strip() if index + 1 < len(pieces) else ""
        updates.append(f"{header}\n{body}".strip())
    return static_content, tuple(updates)


def truncate_for_ai(
    content: ContextBudgetInput,
    *,
    max_tokens: int = 4000,
    recent_update_limit: int = 3,
    recent_comment_limit: int = 5,
) -> ContextBudgetResult:
    static_background, dated_updates = extract_dated_updates(content.background or "")
    protected_text = _render_context(
        title=content.title,
        summary=content.summary,
        dated_updates=dated_updates[-recent_update_limit:],
        recent_comments=content.recent_comments[-recent_comment_limit:],
        background=None,
    )
    original_text = _render_context(
        title=content.title,
        summary=content.summary,
        dated_updates=dated_updates,
        recent_comments=content.recent_comments,
        background=static_background,
    )
    original_tokens = estimate_tokens(original_text)
    protected_tokens = estimate_tokens(protected_text)
    remaining_tokens = max_tokens - protected_tokens

    if remaining_tokens <= 0:
        return ContextBudgetResult(
            content=protected_text,
            was_truncated=True,
            original_tokens=original_tokens,
            final_tokens=estimate_tokens(protected_text),
        )

    static_tokens = estimate_tokens(static_background)
    if static_tokens <= remaining_tokens:
        final_content = protected_text
        if static_background:
            final_content += f"\n\nBackground:\n{static_background}"
        return ContextBudgetResult(
            content=final_content,
            was_truncated=original_tokens > estimate_tokens(final_content),
            original_tokens=original_tokens,
            final_tokens=estimate_tokens(final_content),
        )

    truncated_background = _truncate_middle(static_background, remaining_tokens)
    final_content = protected_text
    if truncated_background:
        final_content += f"\n\nBackground:\n{truncated_background}"
    return ContextBudgetResult(
        content=final_content,
        was_truncated=True,
        original_tokens=original_tokens,
        final_tokens=estimate_tokens(final_content),
    )


def _truncate_middle(text: str, token_budget: int) -> str:
    if not text.strip() or token_budget <= 0:
        return ""

    char_budget = token_budget * 4
    if len(text) <= char_budget:
        return text
    half = max(1, char_budget // 2)
    return text[:half] + "\n\n[... content truncated ...]\n\n" + text[-half:]


def _render_context(
    *,
    title: str,
    summary: str | None,
    dated_updates: tuple[str, ...],
    recent_comments: tuple[str, ...],
    background: str | None,
) -> str:
    parts = [f"Title: {title}"]
    if summary:
        parts.append(f"Summary: {summary}")
    if dated_updates:
        parts.append("Recent Updates:\n" + "\n".join(dated_updates))
    if recent_comments:
        parts.append("Recent Comments:\n" + "\n".join(recent_comments))
    if background:
        parts.append(f"Background:\n{background}")
    return "\n\n".join(parts)
