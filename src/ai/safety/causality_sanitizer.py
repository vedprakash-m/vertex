from __future__ import annotations

import re
from dataclasses import dataclass


_REWRITE_MAP = {
    "due to": "after",
    "caused by": "observed with",
    "led to": "preceded",
    "resulted in": "was followed by",
    "because of": "after",
}

_COMPILED_PATTERNS = tuple(
    (phrase, replacement, re.compile(rf"\b{re.escape(phrase)}\b", flags=re.IGNORECASE))
    for phrase, replacement in _REWRITE_MAP.items()
)


@dataclass(frozen=True, slots=True)
class CausalityViolation:
    phrase: str
    matched_text: str
    replacement: str
    position: int


@dataclass(frozen=True, slots=True)
class SanitizationResult:
    sanitized_text: str
    violations: tuple[CausalityViolation, ...]

    @property
    def changed(self) -> bool:
        return bool(self.violations)


def sanitize_text(text: str) -> SanitizationResult:
    if not text:
        return SanitizationResult(sanitized_text=text, violations=())

    violations: list[CausalityViolation] = []
    sanitized_text = text

    for phrase, replacement, pattern in _COMPILED_PATTERNS:
        matches = list(pattern.finditer(sanitized_text))
        if not matches:
            continue
        for match in matches:
            violations.append(
                CausalityViolation(
                    phrase=phrase,
                    matched_text=match.group(0),
                    replacement=_match_case(match.group(0), replacement),
                    position=match.start(),
                )
            )
        sanitized_text = pattern.sub(lambda match: _match_case(match.group(0), replacement), sanitized_text)

    return SanitizationResult(sanitized_text=sanitized_text, violations=tuple(violations))


def _match_case(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement