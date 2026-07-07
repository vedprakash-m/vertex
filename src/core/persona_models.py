from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SectionSubRule:
    """A single sub-rule within a `section_structure` check."""

    id: str
    """Stable identifier for this sub-rule (used in failure messages)."""
    pattern: str
    """Regex pattern to test against each line or the full text."""
    message: str
    """Human-readable failure message."""
    require: bool = True
    """If True the pattern must match; if False it must NOT match (forbidden)."""
    min_matching_lines: int = 0
    """When > 0, at least this many lines must match the pattern (require=True mode)."""


@dataclass(frozen=True, slots=True)
class WhenSourcePresent:
    """Conditions used by `terminology_consistency` when a source field is set."""

    forbidden_patterns: tuple[str, ...] = ()
    """Regex patterns that must NOT appear when the source field has a value."""
    required_pattern: str | None = None
    """Regex pattern that MUST appear when the source field has a value (optional)."""


@dataclass(frozen=True, slots=True)
class PersonaCheck:
    """A single machine-checkable persona signal definition."""

    id: str
    type: str
    scope: str
    message: str
    severity: str
    keywords: tuple[str, ...] = ()
    pattern: str | None = None
    regex_flags: str | None = None
    threshold: int | None = None
    element: str | None = None
    rule_ref: str | None = None
    enforce_after: str | None = None
    updated_at: str | None = None
    phase: str | None = None
    requires: tuple[str, ...] = ()
    strict_scope: bool = False
    quarantine_reason: str | None = None

    # ── format_matches ──────────────────────────────────────────────────────
    min: int | None = None
    """Minimum number of regex matches required (format_matches)."""
    max: int | None = None
    """Maximum number of regex matches allowed (format_matches)."""

    # ── cross_scope_consistency ──────────────────────────────────────────────
    source_scope: str | None = None
    """Scope name from which values are extracted (cross_scope_consistency)."""
    source_extract: str | None = None
    """Named extractor id from editorial_contract: config (cross_scope_consistency)."""
    target_scope: str | None = None
    """Scope name in which extracted values must appear (cross_scope_consistency)."""
    require_all_in_target: bool = False
    """All extracted values must appear in target scope text."""
    require_none_in_target: bool = False
    """No extracted value must appear in target scope text."""

    # ── published_baseline_match ─────────────────────────────────────────────
    baseline_scope: str | None = None
    """Which section of the baseline to compare against (published_baseline_match)."""
    check_attributes: tuple[str, ...] = ()
    """Structural attributes to compare: bullet_lines_count, bold_label_count, etc."""
    tolerance: tuple[tuple[str, str], ...] = ()
    """Per-attribute signed tolerance as (attribute_name, signed_int_str) pairs."""

    # ── scorecard_alignment ──────────────────────────────────────────────────
    risk_keyword_map: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Mapping of risk level → list of matching keywords for prose fallback."""

    # ── terminology_consistency ──────────────────────────────────────────────
    source_field: str | None = None
    """Field name in overrides to check for presence (terminology_consistency)."""
    when_source_present: WhenSourcePresent | None = None
    """Forbidden/required patterns when source_field is set on the workstream."""

    # ── count_range ──────────────────────────────────────────────────────────
    extract_numerator_from: str | None = None
    """Named extractor for numerator value (count_range)."""
    extract_denominator_from: str | None = None
    """Named extractor for denominator value (count_range)."""
    count_tolerance: int = 0
    """Allowed absolute deviation between text count and source count (count_range)."""

    # ── section_structure ────────────────────────────────────────────────────
    rules: tuple[SectionSubRule, ...] = ()
    """Sub-rules for structural validation (section_structure)."""


@dataclass(frozen=True, slots=True)
class PersonaDefinition:
    """A reader persona and its checks."""

    id: str
    priority: str
    role: str | None = None
    owner: str | None = None
    frame: str | None = None
    always_active: bool = False
    checks: tuple[PersonaCheck, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonaEnforcementConfig:
    """Top-level persona enforcement configuration."""

    enabled: bool = True
    mode: str = "enforce"
    staleness_threshold_days: int = 90


@dataclass(frozen=True, slots=True)
class PersonaRegistry:
    """The complete persona registry loaded from personas.yaml."""

    schema_version: str
    enforcement: PersonaEnforcementConfig
    personas: tuple[PersonaDefinition, ...]


@dataclass(frozen=True, slots=True)
class PersonaCheckResult:
    """Result of evaluating one persona check at one resolved location."""

    persona_id: str
    check_id: str
    status: str
    declared_severity: str
    effective_severity: str
    scope: str
    location: str
    message: str
    surfaced: bool
    matched_text: str | None = None
    remediation_hint: str | None = None
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PersonaSignalCoverageReport:
    """Aggregated persona signal results for one report run."""

    total_checks: int
    total_evaluations: int
    results: tuple[PersonaCheckResult, ...]
    enforcement_mode: str
    evaluation_time_ms: float

    @property
    def passed(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(result for result in self.results if result.status == "passed")

    @property
    def failed(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(result for result in self.results if result.status == "failed")

    @property
    def warnings(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(
            result
            for result in self.results
            if result.status == "failed" and result.effective_severity == "warn"
        )

    @property
    def blocks(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(
            result
            for result in self.results
            if result.status == "failed" and result.effective_severity == "block"
        )

    @property
    def surfaced_results(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(result for result in self.results if result.surfaced)

    @property
    def quarantined(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(result for result in self.results if result.status == "quarantined")

    @property
    def skipped(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(result for result in self.results if result.status == "skipped")

    @property
    def scope_not_found(self) -> tuple[PersonaCheckResult, ...]:
        return tuple(result for result in self.results if result.status == "scope_not_found")


@dataclass(frozen=True, slots=True)
class StructuralRule:
    """A deterministic editorial structural rule."""

    id: str
    description: str | None = None
    regex_absent: str | None = None
    regex_present: str | None = None
    scope: tuple[str, ...] = ()
    severity: str = "warn"
    autofix_hint: str | None = None


@dataclass(frozen=True, slots=True)
class TextProcessingSettings:
    """Shared text-processing knobs for deterministic editorial checks."""

    abbreviations: tuple[str, ...] = (
        "Dr.",
        "Mr.",
        "Mrs.",
        "Ms.",
        "U.S.",
        "e.g.",
        "i.e.",
        "vs.",
        "etc.",
        "approx.",
    )


@dataclass(frozen=True, slots=True)
class StructuralRuleViolation:
    """Violation emitted by structural editorial rules."""

    rule_id: str
    location: str
    matched_text: str
    autofix_hint: str | None = None
    severity: str = "warn"


@dataclass(frozen=True, slots=True)
class PersonaOverride:
    """Per-issue severity downgrade for a persona check."""

    check_id: str
    override_severity: str
    reason: str
    expires: str
    approved_by: str
    location: str | None = None
    scope: str | None = None
