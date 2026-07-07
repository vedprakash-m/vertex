from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.narrative_store import strip_scaffold_comments


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SCOPE_TEXT_LIMIT = 100_000


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    scope: str
    location: str
    text: str
    found: bool = True
    truncated: bool = False


class ScopeResolver:
    """Resolve shared editorial scopes into deterministic text inputs."""

    def __init__(
        self,
        *,
        exec_summary_text: str | None,
        workstream_blurbs: dict[str, str] | None,
        loaded_narratives: dict[str, str] | None,
        rendered_html: str | None,
        subject_line: str | None,
        overrides_scorecard_text: str | None = None,
        published_baseline: dict[str, str] | None = None,
        text_limit: int = _SCOPE_TEXT_LIMIT,
    ) -> None:
        self._exec_summary_text = exec_summary_text
        self._workstream_blurbs = workstream_blurbs or {}
        self._loaded_narratives = loaded_narratives or {}
        self._rendered_html = rendered_html
        self._subject_line = subject_line
        self._overrides_scorecard_text = overrides_scorecard_text
        self._published_baseline = published_baseline or {}
        self._text_limit = text_limit

    def resolve(self, scope: str, *, raw_html: bool = False) -> tuple[ResolvedScope, ...]:
        if scope == "exec_summary":
            return (self._single(scope, "exec_summary", self._exec_summary_text, markdown=True),)
        if scope == "exec_summary_bullets":
            return (self._resolve_exec_summary_bullets(scope),)
        if scope.startswith("workstream:"):
            section_id = scope.split(":", 1)[1]
            if section_id not in self._workstream_blurbs:
                return (ResolvedScope(scope=scope, location=scope, text="", found=False),)
            return (self._single(scope, scope, self._workstream_blurbs.get(section_id), markdown=True),)
        if scope == "each_narrative":
            if not self._loaded_narratives:
                return (ResolvedScope(scope=scope, location="narrative:*", text="", found=False),)
            return tuple(
                self._single(scope, f"narrative:{narrative_id}", text, markdown=True)
                for narrative_id, text in sorted(self._loaded_narratives.items())
            )
        if scope == "rendered_html":
            if self._rendered_html is None:
                return (ResolvedScope(scope=scope, location=scope, text="", found=False),)
            text = self._rendered_html if raw_html else _visible_html_text(self._rendered_html)
            return (self._cap(scope, scope, text),)
        if scope == "subject_line":
            return (self._single(scope, "subject_line", self._subject_line, markdown=True),)
        if scope == "overrides_scorecard":
            return (self._single(scope, "overrides_scorecard", self._overrides_scorecard_text, markdown=False),)
        if scope.startswith("published_baseline:"):
            baseline_key = scope.split(":", 1)[1]
            return (self._single(scope, scope, self._published_baseline.get(baseline_key), markdown=True),)
        return (ResolvedScope(scope=scope, location=scope, text="", found=False),)

    def _single(self, scope: str, location: str, text: str | None, *, markdown: bool) -> ResolvedScope:
        if text is None:
            return ResolvedScope(scope=scope, location=location, text="", found=False)
        cleaned = _strip_markdown_comments(text) if markdown else text
        return self._cap(scope, location, cleaned)

    def _cap(self, scope: str, location: str, text: str) -> ResolvedScope:
        if len(text) <= self._text_limit:
            return ResolvedScope(scope=scope, location=location, text=text)
        return ResolvedScope(scope=scope, location=location, text=text[: self._text_limit], truncated=True)

    def _resolve_exec_summary_bullets(self, scope: str) -> ResolvedScope:
        if self._exec_summary_text is None:
            return ResolvedScope(scope=scope, location="exec_summary_bullets", text="", found=False)
        cleaned = _strip_markdown_comments(self._exec_summary_text)
        bullet_lines = [line for line in cleaned.splitlines() if line.strip().startswith("- ")]
        return self._cap(scope, "exec_summary_bullets", "\n".join(bullet_lines))


def _strip_markdown_comments(text: str) -> str:
    return strip_scaffold_comments(_HTML_COMMENT_RE.sub(" ", text))


def _visible_html_text(html: str) -> str:
    without_comments = _HTML_COMMENT_RE.sub(" ", html)
    without_tags = _HTML_TAG_RE.sub(" ", without_comments)
    return _WHITESPACE_RE.sub(" ", without_tags).strip()
