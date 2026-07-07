"""Tier-1 local semantic matching (keyword-graph, no cloud tokens) — WI-4.4.

Economy-lane between Tier 0 (deterministic) and Tier 2 (frontier). Matches
text against keyword sets with a fixed confidence ceiling (< 1.0) so the
tiered router only promotes to frontier when the local tier is uncertain.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LocalMatchResult:
    """Result of a Tier-1 keyword match."""

    value: str
    confidence: float


class LocalTierMatcher:
    """Economy-lane Tier-1 matcher using keyword graphs.

    Scans ``text`` for any of the registered keywords (case-insensitive) and
    returns the first hit with a fixed confidence of ``match_confidence``.
    Returns ``None`` when no keyword matches (signals: try the next tier).
    """

    _DEFAULT_CONFIDENCE = 0.7

    def __init__(
        self,
        keywords: tuple[str, ...] = (),
        *,
        match_confidence: float = _DEFAULT_CONFIDENCE,
    ) -> None:
        self._keywords = keywords
        self._match_confidence = match_confidence

    def match(self, text: str) -> LocalMatchResult | None:
        """Return the first matching keyword as a ``LocalMatchResult``, or ``None``."""
        if not self._keywords:
            return None
        text_lower = text.lower()
        for kw in self._keywords:
            if kw.lower() in text_lower:
                return LocalMatchResult(value=kw, confidence=self._match_confidence)
        return None
