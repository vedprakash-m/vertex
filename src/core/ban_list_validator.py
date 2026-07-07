from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from src.core.config_loader import EditorialRules
from src.core.persona_models import StructuralRuleViolation
from src.core.scope_resolver import ScopeResolver


DEFAULT_BANNED_PHRASES = (
    "due to",
    "caused by",
    "led to",
    "resulted in",
    "because of",
    "delve",
    "tapestry",
    "furthermore",
    "crucial",
    "testament",
    "in conclusion",
    "it is worth noting",
    "navigate the landscape",
    "unlock",
    "leverage",
    "somewhat",
    "perhaps",
    "various",
    "numerous",
    "many",
)
DEFAULT_BANNED_OPENINGS = (
    "This week",
    "As mentioned",
    "It should be noted",
)
DEFAULT_BANNED_REGEX_PATTERNS = (
    ("SEM0100", re.compile(r"\bSEM0100\b", flags=re.IGNORECASE)),
    ("kusto.windows.net", re.compile(r"\bkusto\.windows\.net\b", flags=re.IGNORECASE)),
    (
        "raw cluster URI",
        re.compile(r"https://[a-z0-9.-]+\.kusto\.windows\.net\b", flags=re.IGNORECASE),
    ),
)
_CAUSAL_BANNED_PHRASES = frozenset(
    {
        "due to",
        "caused by",
        "led to",
        "resulted in",
        "because of",
    }
)


class PolicyProfile(str, Enum):
    STANDARD = "standard"
    RETROSPECTIVE = "retrospective"


@dataclass(frozen=True, slots=True)
class BanListViolation:
    rule_id: str
    location: str
    phrase: str
    matched_text: str
    category: str = "banned_phrase"


def find_ban_list_violations(
    rendered_strings: dict[str, str],
    editorial_rules: EditorialRules,
    program_banned_phrases: tuple[str, ...] = (),
    profile: PolicyProfile = PolicyProfile.STANDARD,
    location_profiles: dict[str, PolicyProfile] | None = None,
) -> tuple[BanListViolation, ...]:
    opening_patterns = _compile_opening_patterns(
        DEFAULT_BANNED_OPENINGS + editorial_rules.banned_openings,
        start_rule_id=100,
    )
    regex_patterns = _compile_regex_patterns(DEFAULT_BANNED_REGEX_PATTERNS, start_rule_id=200)
    violations: list[BanListViolation] = []
    compiled_phrase_patterns: dict[PolicyProfile, list[tuple[str, str, re.Pattern[str]]]] = {}
    for location, text in rendered_strings.items():
        effective_profile = profile if location_profiles is None else location_profiles.get(location, profile)
        phrase_patterns = compiled_phrase_patterns.get(effective_profile)
        if phrase_patterns is None:
            phrase_patterns = _compile_phrase_patterns(
                _filter_banned_phrases(
                    DEFAULT_BANNED_PHRASES + editorial_rules.banned_phrases + program_banned_phrases,
                    profile=effective_profile,
                ),
                start_rule_id=1,
            )
            compiled_phrase_patterns[effective_profile] = phrase_patterns
        for rule_id, phrase, pattern in phrase_patterns + opening_patterns + regex_patterns:
            for match in pattern.finditer(text):
                violations.append(
                    BanListViolation(
                        rule_id=rule_id,
                        location=location,
                        phrase=phrase,
                        matched_text=match.group(0),
                        category=_category_for_rule_id(rule_id),
                    )
                )
    return tuple(violations)


def find_structural_rule_violations(
    *,
    resolver: ScopeResolver,
    editorial_rules: EditorialRules,
) -> tuple[StructuralRuleViolation, ...]:
    violations: list[StructuralRuleViolation] = []
    for rule in editorial_rules.structural_rules:
        pattern_text = rule.regex_absent or rule.regex_present
        if pattern_text is None:
            continue
        pattern = re.compile(pattern_text, flags=re.IGNORECASE)
        for scope in rule.scope:
            for resolved in resolver.resolve(scope):
                if not resolved.found:
                    continue
                if rule.regex_absent is not None:
                    for match in pattern.finditer(resolved.text):
                        violations.append(
                            StructuralRuleViolation(
                                rule_id=rule.id,
                                location=resolved.location,
                                matched_text=_truncate(match.group(0)),
                                autofix_hint=rule.autofix_hint,
                                severity=rule.severity,
                            )
                        )
                elif rule.regex_present is not None and pattern.search(resolved.text) is None:
                    violations.append(
                        StructuralRuleViolation(
                            rule_id=rule.id,
                            location=resolved.location,
                            matched_text="",
                            autofix_hint=rule.autofix_hint,
                            severity=rule.severity,
                        )
                    )
    return tuple(violations)


def _filter_banned_phrases(
    phrases: tuple[str, ...],
    *,
    profile: PolicyProfile,
) -> tuple[str, ...]:
    if profile != PolicyProfile.RETROSPECTIVE:
        return phrases
    return tuple(phrase for phrase in phrases if phrase.lower() not in _CAUSAL_BANNED_PHRASES)


def _compile_phrase_patterns(
    phrases: tuple[str, ...],
    start_rule_id: int,
) -> list[tuple[str, str, re.Pattern[str]]]:
    unique_phrases = []
    seen: set[str] = set()
    for phrase in phrases:
        normalized = phrase.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_phrases.append(phrase)
    compiled: list[tuple[str, str, re.Pattern[str]]] = []
    for index, phrase in enumerate(unique_phrases, start=start_rule_id):
        compiled.append(
            (
                f"BF-{index}",
                phrase,
                re.compile(rf"\b{re.escape(phrase)}\b", flags=re.IGNORECASE),
            )
        )
    return compiled


def _compile_opening_patterns(
    openings: tuple[str, ...],
    start_rule_id: int,
) -> list[tuple[str, str, re.Pattern[str]]]:
    unique_openings = []
    seen: set[str] = set()
    for opening in openings:
        normalized = opening.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_openings.append(opening)
    compiled: list[tuple[str, str, re.Pattern[str]]] = []
    for index, opening in enumerate(unique_openings, start=start_rule_id):
        compiled.append(
            (
                f"BF-{index}",
                opening,
                re.compile(rf"^{re.escape(opening)}", flags=re.IGNORECASE | re.MULTILINE),
            )
        )
    return compiled


def _compile_regex_patterns(
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    start_rule_id: int,
) -> list[tuple[str, str, re.Pattern[str]]]:
    compiled: list[tuple[str, str, re.Pattern[str]]] = []
    for index, (label, pattern) in enumerate(patterns, start=start_rule_id):
        compiled.append((f"BF-{index}", label, pattern))
    return compiled


def _category_for_rule_id(rule_id: str) -> str:
    try:
        number = int(rule_id.split("-", 1)[1])
    except (IndexError, ValueError):
        return "banned_phrase"
    if number >= 200:
        return "banned_regex"
    if number >= 100:
        return "banned_opening"
    return "banned_phrase"


def _truncate(text: str, limit: int = 200) -> str:
    normalized = text.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
