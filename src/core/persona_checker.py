from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from time import perf_counter
from typing import Any, Protocol
import re

from src.core.ban_list_validator import BanListViolation
from src.core.config_loader import EditorialRules
from src.core.editorial import (
    CountRangeCheck,
    CrossScopeConsistencyCheck,
    FormatMatchesCheck,
    OverridesSerializer,
    PublishedBaselineMatchCheck,
    ScorecardAlignmentCheck,
    SectionStructureCheck,
    TerminologyConsistencyCheck,
)
from src.core.persona_models import (
    PersonaCheck,
    PersonaCheckResult,
    PersonaOverride,
    PersonaRegistry,
    PersonaSignalCoverageReport,
    StructuralRuleViolation,
)
from src.core.scope_resolver import ResolvedScope, ScopeResolver


_SEVERITY_RANK = {"none": 0, "warn": 1, "block": 2}
_BAN_RULE_REF_TO_CATEGORY = {
    "banned_openings": "banned_opening",
    "banned_phrases": "banned_phrase",
    "banned_regex": "banned_regex",
}


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    ban_rule_results: tuple[BanListViolation, ...]
    structural_rule_results: tuple[StructuralRuleViolation, ...]
    editorial_rules: EditorialRules
    evaluation_date: date
    resolver: ScopeResolver
    overrides_scorecard_text: str | None = None
    overrides_risk_map: dict[str, str] | None = None
    overrides_dimension_map: dict[str, Any] | None = None
    published_baseline: dict[str, str] | None = None


class PersonaCheckEvaluator(Protocol):
    def evaluate(
        self,
        *,
        persona_id: str,
        check: PersonaCheck,
        resolved: ResolvedScope,
        effective_severity: str,
        surfaced: bool,
        context: EvaluationContext,
    ) -> PersonaCheckResult: ...


class KeywordPresentCheck:
    def evaluate(self, **kwargs: Any) -> PersonaCheckResult:
        return _keyword_check(expect_present=True, **kwargs)


class KeywordAbsentCheck:
    def evaluate(self, **kwargs: Any) -> PersonaCheckResult:
        return _keyword_check(expect_present=False, **kwargs)


class RegexPresentCheck:
    def evaluate(self, **kwargs: Any) -> PersonaCheckResult:
        return _regex_check(expect_present=True, **kwargs)


class RegexAbsentCheck:
    def evaluate(self, **kwargs: Any) -> PersonaCheckResult:
        return _regex_check(expect_present=False, **kwargs)


class SentenceLengthMaxCheck:
    def evaluate(
        self,
        *,
        persona_id: str,
        check: PersonaCheck,
        resolved: ResolvedScope,
        effective_severity: str,
        surfaced: bool,
        context: EvaluationContext,
    ) -> PersonaCheckResult:
        threshold = check.threshold or 0
        longest = ""
        for sentence in _split_sentences(resolved.text, context.editorial_rules.text_processing.abbreviations):
            if len(sentence.split()) > len(longest.split()):
                longest = sentence
        failed = threshold > 0 and len(longest.split()) > threshold
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text=_truncate(longest) if failed else None,
        )


class StructurePresentCheck:
    def evaluate(self, **kwargs: Any) -> PersonaCheckResult:
        persona_id = kwargs["persona_id"]
        check = kwargs["check"]
        resolved = kwargs["resolved"]
        effective_severity = kwargs["effective_severity"]
        surfaced = kwargs["surfaced"]
        needle = f'data-vertex-block="{check.element}"'
        failed = needle not in resolved.text
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
        )


class DelegateToRuleCheck:
    def evaluate(
        self,
        *,
        persona_id: str,
        check: PersonaCheck,
        resolved: ResolvedScope,
        effective_severity: str,
        surfaced: bool,
        context: EvaluationContext,
    ) -> PersonaCheckResult:
        matched_text: str | None = None
        remediation_hint: str | None = None
        found_known_rule = False
        failed = False
        category = _BAN_RULE_REF_TO_CATEGORY.get(check.rule_ref or "")
        if category is not None:
            found_known_rule = True
            for violation in context.ban_rule_results:
                if violation.category == category and _location_matches(resolved.location, violation.location):
                    failed = True
                    matched_text = _truncate(violation.matched_text)
                    break
        for structural_violation in context.structural_rule_results:
            if structural_violation.rule_id == check.rule_ref:
                found_known_rule = True
                if _location_matches(resolved.location, structural_violation.location):
                    failed = True
                    matched_text = _truncate(structural_violation.matched_text)
                    remediation_hint = structural_violation.autofix_hint
                    break
        if not found_known_rule:
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="quarantined",
                effective_severity="none",
                surfaced=surfaced,
                skip_reason=f"unknown rule_ref: {check.rule_ref}",
            )
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text=matched_text,
            remediation_hint=remediation_hint,
        )


CHECK_TYPE_REGISTRY: dict[str, type[PersonaCheckEvaluator]] = {
    "keyword_present": KeywordPresentCheck,
    "keyword_absent": KeywordAbsentCheck,
    "regex_present": RegexPresentCheck,
    "regex_absent": RegexAbsentCheck,
    "sentence_length_max": SentenceLengthMaxCheck,
    "structure_present": StructurePresentCheck,
    "delegate_to_rule": DelegateToRuleCheck,
    "format_matches": FormatMatchesCheck,
    "cross_scope_consistency": CrossScopeConsistencyCheck,
    "published_baseline_match": PublishedBaselineMatchCheck,
    "scorecard_alignment": ScorecardAlignmentCheck,
    "terminology_consistency": TerminologyConsistencyCheck,
    "count_range": CountRangeCheck,
    "section_structure": SectionStructureCheck,
}


def run_persona_checks(
    *,
    registry: PersonaRegistry | None,
    exec_summary_text: str,
    workstream_blurbs: dict[str, str],
    loaded_narratives: dict[str, str],
    rendered_html: str,
    subject_line: str,
    ban_rule_results: tuple[BanListViolation, ...],
    structural_rule_results: tuple[StructuralRuleViolation, ...],
    editorial_rules: EditorialRules,
    overrides: Any,
    program_phase: str | None,
    evaluation_date: date,
    published_baseline: dict[str, str] | None = None,
) -> PersonaSignalCoverageReport | None:
    if registry is None or not registry.enforcement.enabled:
        return None
    started = perf_counter()
    overrides_scorecard_text = None
    overrides_risk_map = None
    overrides_dimension_map = None
    if overrides is not None:
        serializer = OverridesSerializer()
        overrides_scorecard_text = serializer.serialise(overrides)
        overrides_risk_map = serializer.risk_map(overrides)
        overrides_dimension_map = serializer.dimension_map(overrides)
    resolver = ScopeResolver(
        exec_summary_text=exec_summary_text,
        workstream_blurbs=workstream_blurbs,
        loaded_narratives=loaded_narratives,
        rendered_html=rendered_html,
        subject_line=subject_line,
        overrides_scorecard_text=overrides_scorecard_text,
        published_baseline=published_baseline,
    )
    context = EvaluationContext(
        ban_rule_results=ban_rule_results,
        structural_rule_results=structural_rule_results,
        editorial_rules=editorial_rules,
        evaluation_date=evaluation_date,
        resolver=resolver,
        overrides_scorecard_text=overrides_scorecard_text,
        overrides_risk_map=overrides_risk_map,
        overrides_dimension_map=overrides_dimension_map,
        published_baseline=published_baseline,
    )
    persona_overrides = tuple(getattr(overrides, "persona_overrides", ()) or ())
    results: list[PersonaCheckResult] = []
    prior_status: dict[tuple[str, str, str], str] = {}
    total_checks = sum(len(persona.checks) for persona in registry.personas)
    for persona in registry.personas:
        for check in _ordered_checks(persona.checks):
            raw_html = check.type == "structure_present"
            resolved_scopes = resolver.resolve(check.scope, raw_html=raw_html)
            if check.quarantine_reason:
                for resolved in resolved_scopes:
                    result = _result(
                        persona_id=persona.id,
                        check=check,
                        resolved=resolved,
                        status="quarantined",
                        effective_severity="none",
                        surfaced=registry.enforcement.mode != "shadow",
                        skip_reason=check.quarantine_reason,
                    )
                    results.append(result)
                    prior_status[(persona.id, check.id, resolved.location)] = result.status
                continue
            if not persona.always_active and check.phase is not None and check.phase != program_phase:
                for resolved in resolved_scopes:
                    result = _result(
                        persona_id=persona.id,
                        check=check,
                        resolved=resolved,
                        status="skipped",
                        effective_severity="none",
                        surfaced=False,
                        skip_reason=f"phase mismatch: check requires {check.phase}, program is {program_phase}",
                    )
                    results.append(result)
                    prior_status[(persona.id, check.id, resolved.location)] = result.status
                continue
            for resolved in resolved_scopes:
                effective = _effective_severity(
                    check=check,
                    mode=registry.enforcement.mode,
                    evaluation_date=evaluation_date,
                    overrides=persona_overrides,
                    location=resolved.location,
                )
                surfaced = registry.enforcement.mode != "shadow"
                missing_scope = not resolved.found
                if missing_scope:
                    status = "failed" if check.strict_scope else "scope_not_found"
                    result = _result(
                        persona_id=persona.id,
                        check=check,
                        resolved=resolved,
                        status=status,
                        effective_severity=effective if status == "failed" else "none",
                        surfaced=surfaced and status == "failed",
                    )
                    results.append(result)
                    prior_status[(persona.id, check.id, resolved.location)] = result.status
                    continue
                dependency_failure = _dependency_failure(persona.id, check, resolved.location, prior_status)
                if dependency_failure is not None:
                    result = _result(
                        persona_id=persona.id,
                        check=check,
                        resolved=resolved,
                        status="skipped",
                        effective_severity="none",
                        surfaced=False,
                        skip_reason=f"dependency failed: {dependency_failure}",
                    )
                    results.append(result)
                    prior_status[(persona.id, check.id, resolved.location)] = result.status
                    continue
                evaluator_type = CHECK_TYPE_REGISTRY.get(check.type)
                if evaluator_type is None:
                    result = _result(
                        persona_id=persona.id,
                        check=check,
                        resolved=resolved,
                        status="quarantined",
                        effective_severity="none",
                        surfaced=surfaced,
                        skip_reason=f"unknown check type: {check.type}",
                    )
                else:
                    evaluator = evaluator_type()
                    result = evaluator.evaluate(
                        persona_id=persona.id,
                        check=check,
                        resolved=resolved,
                        effective_severity=effective,
                        surfaced=surfaced,
                        context=context,
                    )
                results.append(result)
                prior_status[(persona.id, check.id, resolved.location)] = result.status
    return PersonaSignalCoverageReport(
        total_checks=total_checks,
        total_evaluations=len(results),
        results=tuple(results),
        enforcement_mode=registry.enforcement.mode,
        evaluation_time_ms=(perf_counter() - started) * 1000,
    )


def _keyword_check(
    *,
    expect_present: bool,
    persona_id: str,
    check: PersonaCheck,
    resolved: ResolvedScope,
    effective_severity: str,
    surfaced: bool,
    context: EvaluationContext,
) -> PersonaCheckResult:
    del context
    lowered = resolved.text.lower()
    match = next((keyword for keyword in check.keywords if keyword.lower() in lowered), None)
    failed = (match is None) if expect_present else (match is not None)
    return _result(
        persona_id=persona_id,
        check=check,
        resolved=resolved,
        status="failed" if failed else "passed",
        effective_severity=effective_severity,
        surfaced=surfaced and failed,
        matched_text=match,
    )


def _regex_check(
    *,
    expect_present: bool,
    persona_id: str,
    check: PersonaCheck,
    resolved: ResolvedScope,
    effective_severity: str,
    surfaced: bool,
    context: EvaluationContext,
) -> PersonaCheckResult:
    del context
    if resolved.truncated and not expect_present:
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed",
            effective_severity=effective_severity,
            surfaced=surfaced,
            skip_reason="scope exceeded evaluation cap for absence check",
        )
    pattern = re.compile(check.pattern or "", flags=_regex_flags(check.regex_flags))
    match = pattern.search(resolved.text)
    failed = (match is None) if expect_present else (match is not None)
    return _result(
        persona_id=persona_id,
        check=check,
        resolved=resolved,
        status="failed" if failed else "passed",
        effective_severity=effective_severity,
        surfaced=surfaced and failed,
        matched_text=_truncate(match.group(0)) if match is not None else None,
    )


def _effective_severity(
    *,
    check: PersonaCheck,
    mode: str,
    evaluation_date: date,
    overrides: tuple[PersonaOverride, ...],
    location: str,
) -> str:
    declared = check.severity
    for override in overrides:
        if override.check_id != check.id:
            continue
        if override.location is not None and override.location != location:
            continue
        if override.scope is not None and override.scope != location and override.scope != check.scope:
            continue
        try:
            if date.fromisoformat(override.expires) < evaluation_date:
                continue
        except ValueError:
            continue
        declared = _min_severity(declared, override.override_severity)
    if mode == "shadow":
        return "none"
    if mode == "warn":
        return _min_severity(declared, "warn")
    if check.enforce_after is not None:
        try:
            if evaluation_date < date.fromisoformat(check.enforce_after):
                return _min_severity(declared, "warn")
        except ValueError:
            return _min_severity(declared, "warn")
    return declared


def _min_severity(left: str, right: str) -> str:
    return left if _SEVERITY_RANK[left] <= _SEVERITY_RANK[right] else right


def _dependency_failure(
    persona_id: str,
    check: PersonaCheck,
    location: str,
    prior_status: dict[tuple[str, str, str], str],
) -> str | None:
    for dependency in check.requires:
        status = prior_status.get((persona_id, dependency, location))
        if status is None:
            matching = [value for (pid, cid, _), value in prior_status.items() if pid == persona_id and cid == dependency]
            if matching and all(value == "passed" for value in matching):
                continue
            return dependency
        if status != "passed":
            return dependency
    return None


def _ordered_checks(checks: tuple[PersonaCheck, ...]) -> tuple[PersonaCheck, ...]:
    by_id = {check.id: check for check in checks}
    ordered: list[PersonaCheck] = []
    visited: set[str] = set()

    def visit(check: PersonaCheck) -> None:
        if check.id in visited:
            return
        for dependency in check.requires:
            if dependency in by_id:
                visit(by_id[dependency])
        visited.add(check.id)
        ordered.append(check)

    for check in checks:
        visit(check)
    return tuple(ordered)


def _result(
    *,
    persona_id: str,
    check: PersonaCheck,
    resolved: ResolvedScope,
    status: str,
    effective_severity: str,
    surfaced: bool,
    matched_text: str | None = None,
    remediation_hint: str | None = None,
    skip_reason: str | None = None,
) -> PersonaCheckResult:
    return PersonaCheckResult(
        persona_id=persona_id,
        check_id=check.id,
        status=status,
        declared_severity=check.severity,
        effective_severity=effective_severity,
        scope=check.scope,
        location=resolved.location,
        message=check.message,
        surfaced=surfaced,
        matched_text=_truncate(matched_text) if matched_text else None,
        remediation_hint=remediation_hint,
        skip_reason=skip_reason,
    )


def _regex_flags(value: str | None) -> int:
    if not value:
        return 0
    flags = 0
    for flag in value.split("|"):
        normalized = flag.strip().upper()
        if normalized == "IGNORECASE":
            flags |= re.IGNORECASE
        elif normalized == "MULTILINE":
            flags |= re.MULTILINE
        elif normalized == "DOTALL":
            flags |= re.DOTALL
    return flags


def _split_sentences(text: str, abbreviations: tuple[str, ...]) -> tuple[str, ...]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[str] = []
    for line in lines:
        if re.match(r"^(\-|\*|\d+\.)\s+", line):
            candidates.append(re.sub(r"^(\-|\*|\d+\.)\s+", "", line))
        else:
            candidates.extend(_split_sentence_line(line, abbreviations))
    return tuple(candidate.strip() for candidate in candidates if candidate.strip())


def _split_sentence_line(line: str, abbreviations: tuple[str, ...]) -> tuple[str, ...]:
    protected = line
    replacements: dict[str, str] = {}
    for index, abbreviation in enumerate(abbreviations):
        token = f"__ABBR_{index}__"
        replacements[token] = abbreviation
        protected = protected.replace(abbreviation, token)
    parts = re.split(r"\.\s+(?=[A-Z])|\.$", protected)
    restored = []
    for part in parts:
        for token, abbreviation in replacements.items():
            part = part.replace(token, abbreviation)
        restored.append(part)
    return tuple(restored)


def _location_matches(expected: str, actual: str) -> bool:
    return expected == actual


def _truncate(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    normalized = re.sub(r"\s+", " ", str(text)).strip()
    normalized = _redact(normalized)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _redact(text: str) -> str:
    patterns = [
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        r"https?://\S+",
        r"\b\+?\d[\d(). -]{7,}\d\b",
    ]
    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)
    return redacted
