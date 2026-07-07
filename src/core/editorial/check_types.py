"""Stage γ-Read — Editorial Engine: 7 new deterministic check-type evaluators.

All classes in this module are **Zone A**: purely deterministic, no AI imports,
no network or filesystem I/O during check evaluation.  They implement the
``PersonaCheckEvaluator`` Protocol defined in ``persona_checker.py`` and are
registered in the ``CHECK_TYPE_REGISTRY`` there.

Zone boundary invariant (R-ED-7): every evaluator operates only on data that
has already been loaded and is passed in through the ``EvaluationContext`` or
``ResolvedScope`` arguments.  Any new evaluator added here MUST NOT import from
``src.ai``, ``src.m365``, or make any network/disk call.

Check types implemented
-----------------------
``format_matches``            FormatMatchesCheck
``cross_scope_consistency``   CrossScopeConsistencyCheck
``published_baseline_match``  PublishedBaselineMatchCheck
``scorecard_alignment``       ScorecardAlignmentCheck
``terminology_consistency``   TerminologyConsistencyCheck
``count_range``               CountRangeCheck
``section_structure``         SectionStructureCheck

Helper
------
``OverridesSerializer``  — serialises an ``OverridesDocument`` to the unit-
                           separator (\\x1f) delimited format used by legacy
                           regex extractors.  Primary path = typed accessors.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

# ── local imports (Zone A only) ───────────────────────────────────────────────
from src.core.persona_models import PersonaCheck, PersonaCheckResult
from src.core.scope_resolver import ResolvedScope


# ---------------------------------------------------------------------------
# Type alias — keep the import cycle clean by using TYPE_CHECKING guard for
# the full EvaluationContext / PersonaCheckEvaluator types.
# ---------------------------------------------------------------------------
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.persona_checker import EvaluationContext


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BOLD_LABEL_RE = re.compile(r"^- __[^_\n]+__", re.MULTILINE)
_BULLET_LINE_RE = re.compile(r"^- ", re.MULTILINE)
_WINS_HEADING_RE = re.compile(r"(?i)recent wins|wins this issue|wins:")

_UNIT_SEP = "\x1f"  # ASCII unit separator — cannot appear in workstream names


def _regex_flags(value: str | None) -> int:
    """Parse a pipe-delimited flag string into a ``re`` flags integer."""
    if not value:
        return 0
    flags = 0
    for part in value.split("|"):
        token = part.strip().upper()
        if token == "IGNORECASE":
            flags |= re.IGNORECASE
        elif token == "MULTILINE":
            flags |= re.MULTILINE
        elif token == "DOTALL":
            flags |= re.DOTALL
    return flags


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
    """Construct a PersonaCheckResult with uniform truncation/redaction."""
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
        matched_text=_truncate(matched_text),
        remediation_hint=remediation_hint,
        skip_reason=skip_reason,
    )


def _truncate(text: str | None, limit: int = 200) -> str | None:
    """Normalise whitespace, redact PII tokens, and cap length."""
    if text is None:
        return None
    normalised = re.sub(r"\s+", " ", str(text)).strip()
    normalised = _redact(normalised)
    if len(normalised) <= limit:
        return normalised
    return normalised[: limit - 3] + "..."


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


# ---------------------------------------------------------------------------
# Structural attribute extraction helpers (for published_baseline_match)
# ---------------------------------------------------------------------------

def _count_bullet_lines(text: str) -> int:
    """Count lines that begin with ``- ``."""
    return sum(1 for line in text.splitlines() if line.strip().startswith("- "))


def _count_bold_labels(text: str) -> int:
    """Count occurrences of the ``- __Label__`` pattern."""
    return len(_BOLD_LABEL_RE.findall(text))


def _count_paragraphs(text: str) -> int:
    """Count non-empty, non-bullet, non-heading paragraph blocks."""
    count = 0
    in_para = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("-", "#", "*", ">")):
            if not in_para:
                count += 1
                in_para = True
        else:
            in_para = False
    return count


def _has_wins_section(text: str) -> int:
    """Return 1 if a wins heading is present, else 0 (int for tolerance math)."""
    return 1 if _WINS_HEADING_RE.search(text) else 0


_ATTRIBUTE_FN: dict[str, Any] = {
    "bullet_lines_count": _count_bullet_lines,
    "bold_label_count": _count_bold_labels,
    "paragraph_count": _count_paragraphs,
    "wins_section_present": _has_wins_section,
}


# ---------------------------------------------------------------------------
# 1.  FormatMatchesCheck — `format_matches`
# ---------------------------------------------------------------------------

class FormatMatchesCheck:
    """Verify that a structural regex pattern matches between ``min`` and ``max``
    times in the resolved scope.

    YAML fields used:
        ``pattern``      — regex to count matches
        ``regex_flags``  — optional pipe-delimited flag names
        ``min``          — minimum inclusive match count (default 0)
        ``max``          — maximum inclusive match count (default unlimited)

    Returns ``failed`` when ``count < min`` or ``count > max``.
    Attaches the actual count as ``matched_text`` for operator visibility.
    """

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
        del context  # Zone A — not needed
        pattern = re.compile(
            check.pattern or "",
            flags=_regex_flags(check.regex_flags),
        )
        matches = pattern.findall(resolved.text)
        count = len(matches)
        min_count = check.min if check.min is not None else 0
        max_count = check.max  # None = unlimited

        if count < min_count:
            failed = True
            hint = f"Found {count} match(es); minimum required is {min_count}."
        elif max_count is not None and count > max_count:
            failed = True
            hint = f"Found {count} match(es); maximum allowed is {max_count}."
        else:
            failed = False
            hint = None

        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text=f"count={count}" if failed else None,
            remediation_hint=hint if failed else None,
        )


# ---------------------------------------------------------------------------
# 2.  CrossScopeConsistencyCheck — `cross_scope_consistency`
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _ExtractorResult:
    """Parsed result of running an extractor over a serialised overrides scope."""

    values: tuple[str, ...]
    found: bool = True


class CrossScopeConsistencyCheck:
    """Check that values extracted from ``source_scope`` appear in ``target_scope``.

    The source scope must already be resolved (typically ``overrides_scorecard``
    which exposes the unit-separator serialised text).  Extraction is done via
    regex on the serialised text — this is the legacy extractor path described
    in §15.2.  The primary path (typed accessors) is exercised when
    ``ScopeResolver._build_overrides_scope()`` itself filters by risk level.

    YAML fields used:
        ``source_scope``         — name of the scope providing the values
        ``source_extract``       — name of the extractor (defines the regex)
        ``target_scope``         — name of the scope values must appear in
        ``require_all_in_target``  — all extracted values must appear
        ``require_none_in_target`` — no extracted value must appear

    Note: The ``scope`` field on the PersonaCheck is the *target* for
    ResolvedScope resolution by the main loop; ``source_scope`` and
    ``target_scope`` are resolved explicitly inside this evaluator via the
    ``context.resolver`` (when present) or inferred from ``resolved``.
    """

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
        source_scope = check.source_scope or "overrides_scorecard"
        source_resolved = context.resolver.resolve(source_scope)
        source_text = source_resolved[0].text if source_resolved and source_resolved[0].found else None
        if not source_text:
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="scope_not_found",
                effective_severity="none",
                surfaced=False,
                skip_reason=f"{source_scope} scope not available in context",
            )

        extracted = _run_extractor(source_text, check.source_extract or "")
        if not extracted.found or not extracted.values:
            # No extracted values → nothing to check → pass vacuously.
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="passed",
                effective_severity=effective_severity,
                surfaced=False,
            )

        target_resolved = resolved
        if check.target_scope and check.target_scope != resolved.scope:
            target_candidates = context.resolver.resolve(check.target_scope)
            if not target_candidates or not target_candidates[0].found:
                return _result(
                    persona_id=persona_id,
                    check=check,
                    resolved=resolved,
                    status="scope_not_found",
                    effective_severity="none",
                    surfaced=False,
                    skip_reason=f"{check.target_scope} scope not available in context",
                )
            target_resolved = target_candidates[0]

        target_text_lower = target_resolved.text.lower()
        missing: list[str] = []
        present: list[str] = []

        for value in extracted.values:
            if value.lower() in target_text_lower:
                present.append(value)
            else:
                missing.append(value)

        if check.require_all_in_target:
            failed = bool(missing)
            detail = f"Missing from target: {', '.join(missing)}" if missing else None
        elif check.require_none_in_target:
            failed = bool(present)
            detail = f"Must not appear in target: {', '.join(present)}" if present else None
        else:
            failed = False
            detail = None

        return _result(
            persona_id=persona_id,
            check=check,
            resolved=target_resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text=detail if failed else None,
        )


def _run_extractor(source_text: str, extractor_id: str) -> _ExtractorResult:
    """Run a built-in named extractor over the serialised overrides scorecard text.

    Extractors are defined in §15.2 of the spec.  The unit-separator (\\x1f)
    format is: ``ws\\x1f<name>\\x1frisk=<level>`` and ``p0:open=<n>`` etc.

    This function implements the *extractor registry* for the cross-scope check.
    Operators can extend this by adding entries to the extractor mapping below.
    """
    sep = re.escape(_UNIT_SEP)
    _EXTRACTOR_PATTERNS: dict[str, re.Pattern[str]] = {
        "risk_levels_high": re.compile(
            rf"^ws{sep}(.+?){sep}risk=high$", re.MULTILINE | re.IGNORECASE
        ),
        "risk_levels_medium": re.compile(
            rf"^ws{sep}(.+?){sep}risk=medium$", re.MULTILINE | re.IGNORECASE
        ),
        "risk_levels_low": re.compile(
            rf"^ws{sep}(.+?){sep}risk=low$", re.MULTILINE | re.IGNORECASE
        ),
        "workstreams_done": re.compile(
            rf"^ws{sep}(.+?){sep}risk=done$", re.MULTILINE | re.IGNORECASE
        ),
        "workstreams_removed": re.compile(
            rf"^removed{sep}(.+)$", re.MULTILINE
        ),
        "p0_open_count": re.compile(
            r"^p0:open=(\d+)$", re.MULTILINE
        ),
        "p0_total_count": re.compile(
            r"^p0:total=(\d+)$", re.MULTILINE
        ),
        "p1_open_count": re.compile(
            r"^p1:open=(\d+)$", re.MULTILINE
        ),
        "p1_total_count": re.compile(
            r"^p1:total=(\d+)$", re.MULTILINE
        ),
    }

    pattern = _EXTRACTOR_PATTERNS.get(extractor_id)
    if pattern is None:
        return _ExtractorResult(values=(), found=False)

    values = tuple(m.group(1).strip() for m in pattern.finditer(source_text))
    return _ExtractorResult(values=values, found=True)


# ---------------------------------------------------------------------------
# 3.  PublishedBaselineMatchCheck — `published_baseline_match`
# ---------------------------------------------------------------------------

class PublishedBaselineMatchCheck:
    """Compare structural attributes of the current scope against the most
    recently confirmed published issue (the baseline).

    YAML fields used:
        ``scope``             — scope to evaluate (e.g. ``exec_summary``)
        ``baseline_scope``    — which section of the baseline (e.g. ``exec_summary``)
        ``check_attributes``  — tuple of attribute names to compare
        ``tolerance``         — tuple of (attribute, signed_int_str) pairs

    Supported attributes: ``bullet_lines_count``, ``bold_label_count``,
    ``paragraph_count``, ``wins_section_present``.

    The baseline text is stored in ``context.published_baseline_text``
    (keyed on ``baseline_scope``).  If the baseline is absent the check
    returns ``scope_not_found`` (not ``failed``) — correct behaviour for the
    first issue in a new program (R-ED-3).
    """

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
        baseline_map: dict[str, str] | None = getattr(
            context, "published_baseline", None
        )
        baseline_key = check.baseline_scope or check.scope
        if not baseline_map or baseline_key not in baseline_map:
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="scope_not_found",
                effective_severity="none",
                surfaced=False,
                skip_reason="published_baseline not available (first issue or not loaded)",
            )

        baseline_text = baseline_map[baseline_key]
        tolerance_map: dict[str, int] = {}
        for attr, signed_str in check.tolerance:
            try:
                tolerance_map[attr] = int(signed_str)
            except ValueError:
                tolerance_map[attr] = 0

        violations: list[str] = []
        for attr in check.check_attributes:
            fn = _ATTRIBUTE_FN.get(attr)
            if fn is None:
                continue
            current_val: int = fn(resolved.text)
            baseline_val: int = fn(baseline_text)
            tol = tolerance_map.get(attr, 0)
            # tolerance is *signed*: "-2" means current may be up to 2 below baseline.
            lower_bound = baseline_val + tol  # tol is usually negative
            if tol < 0 and current_val < lower_bound:
                violations.append(
                    f"{attr}: baseline={baseline_val}, current={current_val} "
                    f"(regression beyond tolerance {tol})"
                )
            elif tol == 0 and current_val != baseline_val:
                violations.append(
                    f"{attr}: baseline={baseline_val}, current={current_val} (must match exactly)"
                )
            elif tol > 0 and current_val > baseline_val + tol:
                violations.append(
                    f"{attr}: baseline={baseline_val}, current={current_val} "
                    f"(exceeds upper tolerance +{tol})"
                )

        failed = bool(violations)
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text="; ".join(violations) if failed else None,
        )


# ---------------------------------------------------------------------------
# 4.  ScorecardAlignmentCheck — `scorecard_alignment`
# ---------------------------------------------------------------------------

class ScorecardAlignmentCheck:
    """Verify that a narrative's opening risk phrase matches the overrides.yaml
    risk level for the corresponding workstream.

    **Primary path** (§12.4 — reviewer ChatGPT): parse the structured opening
    label ``__WorkstreamName__ (Risk Level)`` and compare the risk level token
    to the override.  The prose keyword cluster is an *advisory fallback* only
    when no structured label is found.

    YAML fields used:
        ``scope``            — ``each_narrative`` or specific narrative scope
        ``risk_keyword_map`` — tuple of (risk_level, keywords_tuple) pairs

    The workstream ID is derived from the resolved location string
    (``narrative:<ws_id>``).  The override risk is read from
    ``context.overrides_risk_map`` (a dict mapping ws_id → risk_level string).
    """

    # Matches the structured opening: __Label__ (Risk Level)
    _STRUCTURED_RE = re.compile(
        r"__[^_]+__\s*\((?P<risk>High|Medium|Low|Done|At Risk)\)",
        re.IGNORECASE,
    )
    # Identifies where the first sentence ends for prose fallback
    _OPENING_CHARS = 200

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
        # Derive workstream ID from location "narrative:<ws_id>"
        location = resolved.location
        if ":" in location:
            ws_id = location.split(":", 1)[1]
        else:
            ws_id = location

        overrides_risk_map: dict[str, str] | None = getattr(
            context, "overrides_risk_map", None
        )
        if overrides_risk_map is None:
            # No overrides loaded — cannot evaluate; skip silently.
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="scope_not_found",
                effective_severity="none",
                surfaced=False,
                skip_reason="overrides_risk_map not available in context",
            )

        expected_risk = overrides_risk_map.get(ws_id)
        if expected_risk is None:
            # Workstream not in overrides → not a violation.
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="passed",
                effective_severity=effective_severity,
                surfaced=False,
            )

        opening = resolved.text[: self._OPENING_CHARS]
        detected_risk: str | None = None

        # --- Primary path: structured label parse ---
        struct_match = self._STRUCTURED_RE.search(opening)
        if struct_match:
            detected_risk = struct_match.group("risk").lower()
            if detected_risk == "at risk":
                detected_risk = "high"
        else:
            # --- Advisory fallback: prose keyword scan ---
            opening_lower = opening.lower()
            keyword_map = dict(check.risk_keyword_map)
            for level, keywords in keyword_map.items():
                for kw in keywords:
                    if kw.lower() in opening_lower:
                        detected_risk = level.lower()
                        break
                if detected_risk:
                    break

        if detected_risk is None:
            # Cannot determine risk from narrative → advisory warn, not block.
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="failed",
                effective_severity=_min_severity(effective_severity, "warn"),
                surfaced=surfaced,
                matched_text=f"ws={ws_id}: no risk phrase found in opening; expected {expected_risk}",
                remediation_hint=f"Add opening phrase indicating {expected_risk} risk level.",
            )

        expected_norm = expected_risk.lower()
        failed = detected_risk != expected_norm
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text=(
                f"ws={ws_id}: narrative says '{detected_risk}', override says '{expected_norm}'"
                if failed
                else None
            ),
        )


def _min_severity(left: str, right: str) -> str:
    _RANK = {"none": 0, "warn": 1, "block": 2}
    return left if _RANK.get(left, 0) <= _RANK.get(right, 0) else right


# ---------------------------------------------------------------------------
# 5.  TerminologyConsistencyCheck — `terminology_consistency`
# ---------------------------------------------------------------------------

class TerminologyConsistencyCheck:
    """Enforce terminology rules when a source overrides field has a value.

    The canonical example (CI-5): when ``eta:`` is set in overrides.yaml for a
    workstream, the narrative MUST NOT contain "DFD" or "date for a date" and
    SHOULD contain "ETA|Rollout|Delivery|Available".

    YAML fields used:
        ``scope``             — ``each_narrative`` or specific scope
        ``source_field``      — override field to check (e.g. ``eta``)
        ``when_source_present.forbidden_patterns`` — patterns that must be absent
        ``when_source_present.required_pattern``   — pattern that must be present

    The workstream's override entry is retrieved from ``context.overrides_map``
    (dict mapping ws_id → DimensionOverride or compatible object).
    """

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
        location = resolved.location
        ws_id = location.split(":", 1)[1] if ":" in location else location

        overrides_map: dict[str, Any] | None = getattr(context, "overrides_dimension_map", None)
        if overrides_map is None:
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="scope_not_found",
                effective_severity="none",
                surfaced=False,
                skip_reason="overrides_dimension_map not available in context",
            )

        dimension = overrides_map.get(ws_id)
        if dimension is None:
            # Workstream not in overrides → rule does not apply.
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="passed",
                effective_severity=effective_severity,
                surfaced=False,
            )

        # Check if the source field has a non-None value on this dimension.
        source_value = getattr(dimension, check.source_field or "", None)
        if source_value is None:
            # Source field not set → conditions do not apply.
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="passed",
                effective_severity=effective_severity,
                surfaced=False,
            )

        wsp = check.when_source_present
        if wsp is None:
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="passed",
                effective_severity=effective_severity,
                surfaced=False,
            )

        text = resolved.text
        violations: list[str] = []

        for raw_pattern in wsp.forbidden_patterns:
            compiled = re.compile(raw_pattern)
            m = compiled.search(text)
            if m:
                violations.append(f"forbidden pattern '{raw_pattern}' found: '{m.group(0)}'")

        if wsp.required_pattern:
            required = re.compile(wsp.required_pattern)
            if not required.search(text):
                violations.append(f"required pattern '{wsp.required_pattern}' not found")

        failed = bool(violations)
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text=(
                f"ws={ws_id} ({check.source_field}={source_value}): "
                + "; ".join(violations)
                if failed
                else None
            ),
        )


# ---------------------------------------------------------------------------
# 6.  CountRangeCheck — `count_range`
# ---------------------------------------------------------------------------

class CountRangeCheck:
    """Validate numeric claims in the resolved text against structured source data.

    The check extracts two capture groups (numerator, denominator) from the text
    using ``pattern``, then compares them against values obtained by running
    the named extractors ``extract_numerator_from`` and
    ``extract_denominator_from`` over the serialised overrides scorecard.

    YAML fields used:
        ``scope``                   — scope to scan for the count pattern
        ``pattern``                 — regex with two capture groups (num, denom)
        ``regex_flags``             — optional flags
        ``extract_numerator_from``  — named extractor for expected numerator
        ``extract_denominator_from``— named extractor for expected denominator
        ``count_tolerance``         — allowed absolute deviation (default 0)

    Returns ``failed`` when ``abs(text_num - expected_num) > tolerance`` or
    ``abs(text_denom - expected_denom) > tolerance``.  Returns
    ``scope_not_found`` when the numeric pattern does not appear in the text
    (the claim is absent — the operator should be warned).
    """

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
        source_text: str | None = getattr(context, "overrides_scorecard_text", None)
        if source_text is None:
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="scope_not_found",
                effective_severity="none",
                surfaced=False,
                skip_reason="overrides_scorecard_text not available",
            )

        pattern = re.compile(
            check.pattern or "",
            flags=_regex_flags(check.regex_flags),
        )
        text_match = pattern.search(resolved.text)
        if text_match is None:
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="failed",
                effective_severity=_min_severity(effective_severity, "warn"),
                surfaced=surfaced,
                matched_text="count pattern not found in scope text",
                remediation_hint=f"Add a count statement matching pattern: {check.pattern}",
            )

        try:
            text_num: int | None = int(text_match.group(1)) if text_match.lastindex and text_match.lastindex >= 1 else None
            text_denom: int | None = int(text_match.group(2)) if text_match.lastindex and text_match.lastindex >= 2 else None
        except (IndexError, ValueError):
            return _result(
                persona_id=persona_id,
                check=check,
                resolved=resolved,
                status="quarantined",
                effective_severity="none",
                surfaced=False,
                skip_reason="count_range pattern must have exactly 2 numeric capture groups",
            )

        tol = check.count_tolerance
        violations: list[str] = []

        if check.extract_numerator_from and text_num is not None:
            num_result = _run_extractor(source_text, check.extract_numerator_from)
            if num_result.found and num_result.values:
                try:
                    expected_num = int(num_result.values[0])
                    if abs(text_num - expected_num) > tol:
                        violations.append(
                            f"numerator: text={text_num}, source={expected_num} "
                            f"(tolerance={tol})"
                        )
                except ValueError:
                    pass

        if check.extract_denominator_from and text_denom is not None:
            denom_result = _run_extractor(source_text, check.extract_denominator_from)
            if denom_result.found and denom_result.values:
                try:
                    expected_denom = int(denom_result.values[0])
                    if abs(text_denom - expected_denom) > tol:
                        violations.append(
                            f"denominator: text={text_denom}, source={expected_denom} "
                            f"(tolerance={tol})"
                        )
                except ValueError:
                    pass

        failed = bool(violations)
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text="; ".join(violations) if failed else None,
        )


# ---------------------------------------------------------------------------
# 7.  SectionStructureCheck — `section_structure`
# ---------------------------------------------------------------------------

class SectionStructureCheck:
    """Evaluate a list of structural sub-rules against the resolved scope.

    Each sub-rule in ``check.rules`` can:
    - ``require=True``  — pattern must match at least once (or ``min_matching_lines``
                          lines must match if specified)
    - ``require=False`` — pattern must NOT match (forbidden pattern)

    YAML fields used:
        ``scope``    — scope to evaluate
        ``rules``    — tuple of SectionSubRule objects
        ``severity`` — applied when any sub-rule fails

    Returns the first failing sub-rule's details.  If multiple sub-rules fail,
    the ``matched_text`` lists all failures for operator visibility.
    """

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
        del context  # Zone A — not needed
        text = resolved.text
        failures: list[str] = []

        for sub_rule in check.rules:
            compiled = re.compile(sub_rule.pattern, flags=re.MULTILINE)

            if sub_rule.min_matching_lines > 0:
                # Count how many lines satisfy the pattern.
                matching_lines = sum(
                    1
                    for line in text.splitlines()
                    if compiled.search(line)
                )
                if matching_lines < sub_rule.min_matching_lines:
                    failures.append(
                        f"[{sub_rule.id}] {sub_rule.message} "
                        f"(found {matching_lines}, need ≥{sub_rule.min_matching_lines})"
                    )
            elif sub_rule.require:
                # Pattern must be present.
                if not compiled.search(text):
                    failures.append(f"[{sub_rule.id}] {sub_rule.message}")
            else:
                # Pattern must be absent (forbidden).
                m = compiled.search(text)
                if m:
                    failures.append(
                        f"[{sub_rule.id}] {sub_rule.message} "
                        f"(found: '{_truncate(m.group(0), 80)}')"
                    )

        failed = bool(failures)
        return _result(
            persona_id=persona_id,
            check=check,
            resolved=resolved,
            status="failed" if failed else "passed",
            effective_severity=effective_severity,
            surfaced=surfaced and failed,
            matched_text="; ".join(failures) if failed else None,
        )


# ---------------------------------------------------------------------------
# OverridesSerializer — legacy extractor compatibility
# ---------------------------------------------------------------------------

class OverridesSerializer:
    """Serialise an ``OverridesDocument`` to the unit-separator (\\x1f) delimited
    format consumed by legacy regex extractors (§13.1, §15.2).

    Primary path = typed accessors on the parsed ``EditionOverrides`` structure.
    This serialiser exists only for the ``overrides_scorecard`` scope text that
    ``CrossScopeConsistencyCheck`` and ``CountRangeCheck`` consume via the
    extractor registry.

    **Schema version:** ``1`` — any breaking change to the line format must
    bump the version constant and update ``_run_extractor``'s patterns.

    Format spec:
    ::

        ws\\x1f<name>\\x1frisk=<level>
        ws\\x1f<name>\\x1feta=<iso_date>
        ws\\x1f<name>\\x1fhide=true         (when hide_from_scorecard=True)
        p0:open=<n>
        p0:total=<n>
        p1:open=<n>
        p1:total=<n>
        removed\\x1f<scorecard_name>\\x1f<dimension_name>
        schema_version=1

    Lines are sorted deterministically (alphabetically within each section)
    so the serialised text is stable across runs (R-ED-2).
    """

    SCHEMA_VERSION = 1
    SEP = _UNIT_SEP

    def serialise(self, overrides: Any) -> str:
        """Return the serialised scorecard text for *overrides*.

        *overrides* is expected to be an ``OverridesDocument`` instance (or any
        object with ``.scorecards`` and ``.removed_dimensions``).  Uses
        ``getattr`` throughout to remain resilient to schema evolution.
        """
        lines: list[str] = []
        sep = self.SEP
        scorecards = getattr(overrides, "scorecards", ()) or ()

        # --- Per-workstream lines ---
        ws_lines: list[str] = []
        for scorecard in scorecards:
            dimensions = getattr(scorecard, "dimensions", ()) or ()
            for dim in dimensions:
                name: str = getattr(dim, "name", "") or ""
                if not name:
                    continue
                risk = getattr(dim, "risk", None)
                if risk is not None:
                    risk_str = str(risk.value) if hasattr(risk, "value") else str(risk)
                    ws_lines.append(f"ws{sep}{name}{sep}risk={risk_str.lower()}")
                eta = getattr(dim, "eta", None)
                if eta is not None:
                    ws_lines.append(f"ws{sep}{name}{sep}eta={eta}")
                hide = getattr(dim, "hide_from_scorecard", False)
                if hide:
                    ws_lines.append(f"ws{sep}{name}{sep}hide=true")
        ws_lines.sort()
        lines.extend(ws_lines)

        # --- P0 / P1 counts (derived from overrides if present) ---
        # These may be set on a governance or top-3 block; surface them if found.
        governance = getattr(overrides, "governance", None)
        top3 = getattr(overrides, "top_3_now", ()) or ()

        # Count P0 items in top_3_now as a proxy (per §15.2 legacy compat).
        p0_total = sum(
            1 for entry in top3
            if str(getattr(entry, "type", "")).lower().startswith("p0")
        )
        p0_open = sum(
            1 for entry in top3
            if str(getattr(entry, "type", "")).lower().startswith("p0")
            and getattr(entry, "by_date", None) is None
        )
        if p0_total > 0:
            lines.append(f"p0:open={p0_open}")
            lines.append(f"p0:total={p0_total}")

        # --- Removed dimensions ---
        removed = getattr(overrides, "removed_dimensions", ()) or ()
        removed_lines: list[str] = []
        for rd in removed:
            sc_name = getattr(rd, "scorecard_name", "") or ""
            dim_name = getattr(rd, "dimension_name", "") or ""
            removed_lines.append(f"removed{sep}{sc_name}{sep}{dim_name}")
        removed_lines.sort()
        lines.extend(removed_lines)

        # --- Removed sections (whole workstreams removed from rendering) ---
        removed_sections = getattr(overrides, "removed_sections", ()) or ()
        for section in sorted(removed_sections):
            lines.append(f"removed{sep}{section}")

        # --- Schema version sentinel (stability / contract test anchor) ---
        lines.append(f"schema_version={self.SCHEMA_VERSION}")

        return "\n".join(lines)

    def workstreams_with_risk(self, overrides: Any, risk_level: str) -> tuple[str, ...]:
        """Return workstream names whose risk equals *risk_level* (typed accessor).

        This is the *primary* path for cross-scope consistency checks — avoids
        regex parsing of the serialised text entirely.
        """
        scorecards = getattr(overrides, "scorecards", ()) or ()
        result: list[str] = []
        for scorecard in scorecards:
            dimensions = getattr(scorecard, "dimensions", ()) or ()
            for dim in dimensions:
                risk = getattr(dim, "risk", None)
                if risk is None:
                    continue
                risk_str = str(risk.value) if hasattr(risk, "value") else str(risk)
                if risk_str.lower() == risk_level.lower():
                    result.append(getattr(dim, "name", "") or "")
        return tuple(sorted(r for r in result if r))

    def risk_map(self, overrides: Any) -> dict[str, str]:
        """Return a mapping of workstream name → risk level string.

        Used to populate ``context.overrides_risk_map`` for
        ``ScorecardAlignmentCheck``.
        """
        scorecards = getattr(overrides, "scorecards", ()) or ()
        result: dict[str, str] = {}
        for scorecard in scorecards:
            dimensions = getattr(scorecard, "dimensions", ()) or ()
            for dim in dimensions:
                name = getattr(dim, "name", "") or ""
                risk = getattr(dim, "risk", None)
                if name and risk is not None:
                    risk_str = str(risk.value) if hasattr(risk, "value") else str(risk)
                    result[name] = risk_str.lower()
        return result

    def dimension_map(self, overrides: Any) -> dict[str, Any]:
        """Return a mapping of workstream name → ``DimensionOverride`` object.

        Used to populate ``context.overrides_dimension_map`` for
        ``TerminologyConsistencyCheck``.
        """
        scorecards = getattr(overrides, "scorecards", ()) or ()
        result: dict[str, Any] = {}
        for scorecard in scorecards:
            dimensions = getattr(scorecard, "dimensions", ()) or ()
            for dim in dimensions:
                name = getattr(dim, "name", "") or ""
                if name:
                    result[name] = dim
        return result
