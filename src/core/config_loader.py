from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator, FormatChecker

from src.core.chapter_contract_loader import ChapterContract, load_chapter_contract, load_chapter_contract_for_edition
from src.core.exceptions import ConfigError, PersonaSchemaError
from src.core.models import EditionType
from src.core.persona_models import (
    PersonaCheck,
    PersonaDefinition,
    PersonaEnforcementConfig,
    PersonaRegistry,
    SectionSubRule,
    StructuralRule,
    TextProcessingSettings,
    WhenSourcePresent,
)
from src.core.slice_contract_loader import SliceContract, load_slice_contract, load_slice_contract_for_edition
from src.core.template_contract_loader import TemplateContract, load_template_contract, load_template_contract_for_edition
from src.core.yaml_utils import load_yaml_mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
# Legacy compatibility alias. Edition YAMLs now live under programs/<id>/editions/.
EDITIONS_ROOT = REPO_ROOT / "editions"
PROGRAMS_ROOT = REPO_ROOT / "programs"
SCHEMAS_ROOT = REPORTS_ROOT / "schemas"
CORE_SCHEMAS_ROOT = REPO_ROOT / "src" / "core" / "schemas"
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
_PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

if TYPE_CHECKING:
    from src.core.models_v2 import Program


def _is_example_edition_id(edition_id: str) -> bool:
    return edition_id.startswith("example_")


@dataclass(frozen=True, slots=True)
class EditionSettings:
    name: str
    type: str
    title: str
    cadence: str
    send_day: str | None
    send_time_local: str | None
    timezone: str | None


@dataclass(frozen=True, slots=True)
class AuthorSettings:
    display_name: str
    email: str


@dataclass(frozen=True, slots=True)
class DistributionSettings:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    channels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ADOSettings:
    organization: str
    project: str
    area_paths: tuple[str, ...]
    work_item_types: tuple[str, ...]
    excluded_states: tuple[str, ...]
    date_window_days: int
    api_timeout_seconds: int | None
    proposal_ttl_hours: int = 72


@dataclass(frozen=True, slots=True)
class ScorecardDimensionSettings:
    name: str
    description: str | None
    ado_filter: str
    linked_scorecard_name: str | None = None
    linked_dimension_name: str | None = None
    dfd_proximity_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class ScorecardSettings:
    name: str
    dimensions: tuple[ScorecardDimensionSettings, ...]


@dataclass(frozen=True, slots=True)
class CadenceNoteSettings:
    detailed: str
    focused: str
    first_issue_override: str | None


@dataclass(frozen=True, slots=True)
class ReviewerSettings:
    name: str
    sections: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewSettings:
    reviewers: tuple[ReviewerSettings, ...]
    required: bool


@dataclass(frozen=True, slots=True)
class AISettings:
    enabled: bool
    budget_usd_per_run: float
    blurb_deployment: str | None
    blurb_backup_deployment: str | None
    exec_summary_deployment: str | None
    exec_summary_backup_deployment: str | None
    temperature: float | None


@dataclass(frozen=True, slots=True)
class KustoQuerySettings:
    id: str
    cluster: str
    database: str
    kql: str
    section: str
    render_as: str
    confidence: str
    kusto_section_validates_slice: bool
    caveats: tuple[str, ...]
    reference_url: str | None
    kusto_no_safety: bool = False
    # Chart pipeline fields (R3)
    chart_renderer_id: str | None = None
    chart_config: dict[str, Any] | None = None
    attachment_target: str | None = None
    attachment_position: Literal["after"] = "after"
    attachment_fallback: Literal["standalone", "suppress"] = "standalone"
    chart_cache_ttl_hours: int = 26
    chart_blocks_publish: bool = False
    fallback_on_empty_rows: bool = False
    fallback_kql: str | None = None  # FR-SG-31: secondary KQL if primary returns zero rows or query error


@dataclass(frozen=True, slots=True)
class KustoSettings:
    enabled: bool
    queries: tuple[KustoQuerySettings, ...]


@dataclass(frozen=True, slots=True)
class ArchiveSettings:
    root: str


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str
    json: bool


@dataclass(frozen=True, slots=True)
class M365WorkIQSettings:
    newsletter_search: str | None
    feedback_search: str | None
    teams_search: str | None


@dataclass(frozen=True, slots=True)
class M365BluebirdSettings:
    teams_channels: tuple[str, ...]
    lookback_days: int


@dataclass(frozen=True, slots=True)
class M365OfflineSettings:
    newsletter_dir: str | None
    transcript_dir: str | None


@dataclass(frozen=True, slots=True)
class M365Settings:
    enabled: bool
    prefer_agency: bool
    workiq: M365WorkIQSettings
    bluebird: M365BluebirdSettings
    offline: M365OfflineSettings
    teams_incoming_webhook_url: str | None = None
    artifact_base_url: str | None = None
    sharepoint: "SharePointConfig | None" = None  # SP2-3: SharePoint gather config


@dataclass(frozen=True, slots=True)
class SharePointLtDeckConfig:
    """SP2-3: LT deck location config for program.yaml m365.sharepoint.lt_deck section."""
    folder_url: str
    current_deck_url: str | None = None   # direct link to current month's .pptx
    current_deck_date: date | None = None  # actual presentation date
    cadence_days: int = 30


@dataclass(frozen=True, slots=True)
class SharePointConfig:
    """SP2-3: SharePoint gather config for program.yaml m365.sharepoint section."""
    lt_deck: SharePointLtDeckConfig | None = None
    ref_docs_enabled: bool = True
    gather_timeout_seconds: int = 300  # per-doc WorkIQ timeout; matches P4-22 value
    allow_cli_fallback: bool = True    # must be True — MCP path is unreliable per §5.1
    max_docs_per_run: int = 5          # prevents >2500s serial gather time


@dataclass(frozen=True, slots=True)
class ChartRendererModule:
    module_name: str  # e.g. "programs.acme.charts.renderers"


@dataclass(frozen=True, slots=True)
class ChartEditionSettings:
    enabled: bool = True
    renderer_modules: tuple[ChartRendererModule, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportConfig:
    schema_version: str
    edition: EditionSettings
    author: AuthorSettings
    distribution: DistributionSettings
    ado: ADOSettings
    scorecards: tuple[ScorecardSettings, ...]
    ai: AISettings
    kusto: KustoSettings
    m365: M365Settings
    archive: ArchiveSettings
    logging: LoggingSettings
    layout_mode: str = "dashboard"
    cadence_note: CadenceNoteSettings | None = None
    scorecard_sort: str = "risk_desc"
    scorecard_plain_text_only: bool = False
    brand_name: str | None = None
    brand_header_url: str | None = None
    # ADF-W1.12: productivity_dividend_published retired (formula-derived
    # productivity_dividend_hours claim removed). A legacy program.yaml/config
    # may still set this key; the schema's additionalProperties:true and this
    # dataclass's absence of the field both tolerate it harmlessly (dict-based
    # parsing below never required its presence).
    ado_fetch_timeout_seconds: int = 45
    forecast_enabled: bool = False
    mobile_safe_scorecards: str | None = None
    type_scale_v2: bool = False
    calibration_pilot: bool = False
    charts: ChartEditionSettings | None = None


@dataclass(frozen=True, slots=True)
class ProgramWorkstream:
    name: str
    aliases: tuple[str, ...]
    area_paths: tuple[str, ...]
    dri_email: str | None
    alternate_owner: str | None
    description: str | None
    why_it_matters: str | None = None
    history_summary: str | None = None
    leadership_sensitivity: str | None = None
    current_blocker: str | None = None


@dataclass(frozen=True, slots=True)
class ProgramDependency:
    source: str
    target: str
    impact: str


@dataclass(frozen=True, slots=True)
class ProgramPerson:
    email: str
    display_name: str | None
    role: str | None
    workstreams: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LeadershipReader:
    name: str
    role: str | None
    cares_about: tuple[str, ...]
    prefers: str | None
    pet_peeves: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PersonaVoiceRules:
    """Machine-checkable voice rules scoped to one leadership persona."""

    banned_patterns: tuple[str, ...]  # lowercased substring patterns to flag
    required_signals: tuple[str, ...]  # at least one must appear (lowercased)


@dataclass(frozen=True, slots=True)
class LeadershipPersona:
    """A named leadership persona with explicit voice and communication expectations.

    Populated from the ``leadership_personas`` mapping in ``program.yaml``.
    The ``persona_id`` is the dict key (e.g. ``editorial_quality``, ``jordan_lee``).
    """

    persona_id: str
    role: str | None = None
    question_style: str | None = None
    typical_questions: tuple[str, ...] = ()
    cares_about: str | None = None
    pet_peeves: str | None = None
    communication_bar: str | None = None
    voice_rules: PersonaVoiceRules | None = None


@dataclass(frozen=True, slots=True)
class WorkstreamOwnerProfile:
    name: str
    areas: tuple[str, ...]
    style_note: str | None
    timezone: str | None
    alternate: str | None


@dataclass(frozen=True, slots=True)
class WritingStyle:
    voice: str | None
    structure: str | None
    risk_framing: dict[str, str]
    preferred_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToneCalibration:
    overall: str | None
    per_theme_override: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProgramSubProgram:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    summary: str | None = None
    status: str | None = None
    primary_area_paths: tuple[str, ...] = ()
    why_distinct: str | None = None


@dataclass(frozen=True, slots=True)
class NarrativeProgramContext:
    schema_version: str
    program_name: str
    objective: str | None
    mission: str | None
    pillars: tuple[str, ...]
    glossary: dict[str, str]
    workstreams: tuple[ProgramWorkstream, ...]
    people: tuple[ProgramPerson, ...]
    leadership_readers: tuple[LeadershipReader, ...] = ()
    workstream_owners: tuple[WorkstreamOwnerProfile, ...] = ()
    recurring_themes: tuple[str, ...] = ()
    writing_style: WritingStyle | None = None
    tone_calibration: ToneCalibration | None = None
    current_phase: str | None = None
    key_dependency_chain: tuple[ProgramDependency, ...] = ()
    sub_programs: tuple[ProgramSubProgram, ...] = ()
    leadership_personas: tuple[LeadershipPersona, ...] = ()


@dataclass(frozen=True, slots=True)
class EditionVerbosityLimits:
    detailed: int | None = None
    focused: int | None = None
    condensed: int | None = None
    narrative: int | None = None
    deck: int | None = None

    def for_edition(self, edition_type: EditionType | None) -> int | None:
        if edition_type is None or edition_type == EditionType.LOOKBACK:
            return None
        return getattr(self, edition_type.value, None)


@dataclass(frozen=True, slots=True)
class VerbositySettings:
    workstream_blurb_max_sentences: int | None
    workstream_blurb_max_words: int | None
    exec_bullet_max_words: int | None
    exec_max_bullets: int | None
    scorecard_summary_max_sentences: int | None
    exec_summary_max_words: int | None = None
    exec_summary_max_words_by_edition: EditionVerbosityLimits = EditionVerbosityLimits()
    workstream_blurb_max_words_by_edition: EditionVerbosityLimits = EditionVerbosityLimits()
    workstream_blurb_max_paragraphs: int | None = None

    def exec_summary_max_words_for(self, edition_type: EditionType | None, *, default: int | None = None) -> int | None:
        edition_limit = self.exec_summary_max_words_by_edition.for_edition(edition_type)
        if edition_limit is not None:
            return edition_limit
        if self.exec_summary_max_words is not None:
            return self.exec_summary_max_words
        return default

    def workstream_blurb_max_words_for(self, edition_type: EditionType | None) -> int | None:
        edition_limit = self.workstream_blurb_max_words_by_edition.for_edition(edition_type)
        if edition_limit is not None:
            return edition_limit
        return self.workstream_blurb_max_words


@dataclass(frozen=True, slots=True)
class VoiceContractSettings:
    applies_to_editions: tuple[str, ...] = ()
    program_tokens: tuple[str, ...] = ()
    abstract_phrases: tuple[str, ...] = ()
    synthetic_delta_prefixes: tuple[str, ...] = ()
    decision_lead_terms: tuple[str, ...] = ()
    static_concrete_terms: tuple[str, ...] = ()
    exec_summary_bucket_prefixes: tuple[str, ...] = ()
    objective_preamble_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EditorialRules:
    schema_version: str
    stale_warn_days: int
    stale_block_days: int
    banned_phrases: tuple[str, ...]
    banned_openings: tuple[str, ...]
    verbosity: VerbositySettings
    voice_contract: VoiceContractSettings | None = None
    structural_rules: tuple[StructuralRule, ...] = ()
    text_processing: TextProcessingSettings = TextProcessingSettings()


@dataclass(frozen=True, slots=True)
class ReportBundle:
    config: ReportConfig
    editorial_rules: EditorialRules
    review: ReviewSettings
    program_context: NarrativeProgramContext | None
    program: "Program"
    template_contract: TemplateContract | None
    slice_contracts: tuple[SliceContract, ...] | None
    program_id: str | None = None
    chapter_namespace: str | None = None
    chapter_contract: ChapterContract | None = None
    persona_registry: PersonaRegistry | None = None


@dataclass(frozen=True, slots=True)
class BundleLoadResult:
    bundle: ReportBundle
    mode: str


def discover_report_editions(
    reports_root: Path = REPORTS_ROOT,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, ...]:
    del reports_root
    del editions_root
    return tuple(
        sorted(
            {
                *(
                    edition_path.stem
                    for edition_path in programs_root.glob("*/editions/*.yaml")
                    if not _is_example_edition_id(edition_path.stem)
                ),
                *(
                    edition_path.stem
                    for edition_path in programs_root.glob("_templates/*/editions/*.yaml")
                    if not _is_example_edition_id(edition_path.stem)
                ),
            }
        )
    )


def _load_required_v2_bundle(
    edition_name: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> Any:
    from src.core.config_loader_v2 import load_edition_bundle

    bundle = load_edition_bundle(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if bundle is None:
        raise FileNotFoundError(
            f"Edition '{edition_name}' was not found under {programs_root}. "
            "The V1 reports/ fallback was removed in V2.3."
        )
    return bundle


def _build_v2_report_bundle(v2_bundle: Any) -> ReportBundle:
    config = _parse_report_config(_resolve_env_values(v2_bundle.config_document))
    editorial_rules = load_editorial_rules(v2_bundle.editorial_rules_path)
    persona_registry = load_persona_registry(v2_bundle.resolved.paths.program_dir / "knowledge" / "personas.yaml")
    review = load_review_config(v2_bundle.review_path)
    template_contract = load_template_contract(v2_bundle.template_contract_path) if v2_bundle.template_contract_path.exists() else None
    slice_contracts = load_slice_contract(v2_bundle.slice_contract_path) if v2_bundle.slice_contract_path.exists() else None
    _validate_slice_contract_coverage(config, slice_contracts)
    program_context = _parse_program_context_document(
        v2_bundle.program_context_document,
        v2_bundle.resolved.paths.program_dir / "program.yaml",
    )
    chapter_namespace = v2_bundle.resolved.program.chapter_namespace or v2_bundle.resolved.program.id
    chapter_contract = None
    if v2_bundle.chapter_contract_path.exists():
        chapter_contract = load_chapter_contract(
            v2_bundle.chapter_contract_path,
            scorecards=tuple(
                (scorecard.name, tuple(dimension.name for dimension in scorecard.dimensions))
                for scorecard in config.scorecards
            ),
            chapter_namespace=chapter_namespace,
        )
    if config.layout_mode == "continuity" and chapter_contract is None:
        raise ConfigError(
            f"layout_mode=continuity requires chapter_contract.yaml under {v2_bundle.resolved.paths.program_dir}"
        )
    return ReportBundle(
        config=config,
        editorial_rules=editorial_rules,
        review=review,
        program_context=program_context,
        program=v2_bundle.resolved.program,
        template_contract=template_contract,
        slice_contracts=slice_contracts,
        program_id=v2_bundle.resolved.program.id,
        chapter_namespace=chapter_namespace,
        chapter_contract=chapter_contract,
        persona_registry=persona_registry,
    )


def load_report_bundle(
    edition_name: str,
    reports_root: Path = REPORTS_ROOT,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ReportBundle:
    del reports_root
    return _build_v2_report_bundle(
        _load_required_v2_bundle(
            edition_name,
            editions_root=editions_root,
            programs_root=programs_root,
        )
    )


def load_bundle_with_mode(
    edition_name: str,
    reports_root: Path = REPORTS_ROOT,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> BundleLoadResult:
    del reports_root
    v2_bundle = _load_required_v2_bundle(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    return BundleLoadResult(
        bundle=_build_v2_report_bundle(v2_bundle),
        mode="v2",
    )


def load_bundle(
    edition_name: str,
    reports_root: Path = REPORTS_ROOT,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ReportBundle:
    return load_bundle_with_mode(
        edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
    ).bundle


def _validate_slice_contract_coverage(
    config: ReportConfig,
    slice_contracts: tuple[SliceContract, ...] | None,
) -> None:
    if not slice_contracts:
        return
    expected = {
        (scorecard.name, dimension.name)
        for scorecard in config.scorecards
        for dimension in scorecard.dimensions
    }
    actual = {contract.lookup_key for contract in slice_contracts}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if not missing and not unexpected:
        return
    problems: list[str] = []
    if missing:
        problems.append(
            "missing slice contracts for "
            + ", ".join(f"{scorecard_name} / {dimension_name}" for scorecard_name, dimension_name in missing)
        )
    if unexpected:
        problems.append(
            "unexpected slice contracts for "
            + ", ".join(f"{scorecard_name} / {dimension_name}" for scorecard_name, dimension_name in unexpected)
        )
    raise ConfigError("; ".join(problems))


def _validate_slice_telemetry_contracts(
    config: ReportConfig,
    slice_contracts: tuple[SliceContract, ...] | None,
) -> None:
    if not slice_contracts:
        return
    queries_by_id = {query.id: query for query in config.kusto.queries}
    problems: list[str] = []
    for contract in slice_contracts:
        telemetry = contract.source_contract.telemetry
        if contract.source_of_truth == "manual_only" and contract.assignment_mode != "manual_only":
            problems.append(f"slice {contract.id} is manual_only but assignment_mode is {contract.assignment_mode}")
        if contract.source_of_truth in {"telemetry_primary", "hybrid"}:
            if telemetry is None:
                problems.append(f"slice {contract.id} requires source_contract.telemetry")
                continue
            linked_query = queries_by_id.get(telemetry.query_id)
            if linked_query is None:
                problems.append(f"slice {contract.id} references unknown kusto query {telemetry.query_id}")
                continue
            if not linked_query.kusto_section_validates_slice:
                problems.append(
                    f"slice {contract.id} references kusto query {telemetry.query_id} but the query is not marked slice-validating"
                )
        elif telemetry is not None and telemetry.query_id not in queries_by_id:
            problems.append(f"slice {contract.id} references unknown kusto query {telemetry.query_id}")
    if problems:
        raise ConfigError("; ".join(problems))
    _validate_slice_telemetry_contracts(config, slice_contracts)


def load_report_config(
    edition_name: str,
    reports_root: Path = REPORTS_ROOT,
    *,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> ReportConfig:
    del reports_root
    v2_bundle = _load_required_v2_bundle(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    return _parse_report_config(_resolve_env_values(v2_bundle.config_document))


def load_editorial_rules(path: Path) -> EditorialRules:
    raw_rules = load_yaml_mapping(path)
    if raw_rules.get("schema_version") != "1.0":
        raise ConfigError(f"Unsupported editorial rules schema version in {path}")
    verbosity = raw_rules.get("verbosity", {})
    workstream_blurb_max_words, workstream_blurb_max_words_by_edition = _parse_verbosity_word_limit(
        verbosity.get("workstream_blurb_max_words"),
        field_name="verbosity.workstream_blurb_max_words",
    )
    exec_summary_max_words, exec_summary_max_words_by_edition = _parse_verbosity_word_limit(
        verbosity.get("exec_summary_max_words"),
        field_name="verbosity.exec_summary_max_words",
    )
    voice_contract = _parse_voice_contract(raw_rules.get("voice_contract"), path=path)
    structural_rules = _parse_structural_rules(raw_rules.get("structural_rules"), path=path)
    text_processing = _parse_text_processing(raw_rules.get("text_processing"), path=path)
    return EditorialRules(
        schema_version=raw_rules["schema_version"],
        stale_warn_days=raw_rules["stale_warn_days"],
        stale_block_days=raw_rules["stale_block_days"],
        banned_phrases=tuple(raw_rules.get("banned_phrases", [])),
        banned_openings=tuple(raw_rules.get("banned_openings", [])),
        verbosity=VerbositySettings(
            workstream_blurb_max_sentences=verbosity.get("workstream_blurb_max_sentences"),
            workstream_blurb_max_words=workstream_blurb_max_words,
            exec_bullet_max_words=verbosity.get("exec_bullet_max_words"),
            exec_max_bullets=verbosity.get("exec_max_bullets"),
            scorecard_summary_max_sentences=verbosity.get("scorecard_summary_max_sentences"),
            exec_summary_max_words=exec_summary_max_words,
            exec_summary_max_words_by_edition=exec_summary_max_words_by_edition,
            workstream_blurb_max_words_by_edition=workstream_blurb_max_words_by_edition,
            workstream_blurb_max_paragraphs=verbosity.get("workstream_blurb_max_paragraphs"),
        ),
        voice_contract=voice_contract,
        structural_rules=structural_rules,
        text_processing=text_processing,
    )


def load_persona_registry(path: Path) -> PersonaRegistry | None:
    if not path.exists():
        return None
    raw_registry = load_yaml_mapping(path)
    if not isinstance(raw_registry, dict):
        raise PersonaSchemaError(f"{path} must contain a mapping")
    _validate_persona_document_shape(raw_registry, path=path)
    enforcement_raw = raw_registry.get("enforcement") or {}
    if not isinstance(enforcement_raw, dict):
        raise PersonaSchemaError(f"enforcement must be a mapping in {path}")
    enforcement = PersonaEnforcementConfig(
        enabled=bool(enforcement_raw.get("enabled", True)),
        mode=str(enforcement_raw.get("mode", "enforce")),
        staleness_threshold_days=int(enforcement_raw.get("staleness_threshold_days", 90)),
    )
    personas_raw = raw_registry.get("personas") or []
    personas: list[PersonaDefinition] = []
    seen_persona_ids: set[str] = set()
    seen_check_ids: set[str] = set()
    for raw_persona in personas_raw:
        if not isinstance(raw_persona, dict):
            raise PersonaSchemaError(f"Each persona entry must be a mapping in {path}")
        persona_id = str(raw_persona.get("id", "")).strip()
        if not _PERSONA_ID_RE.match(persona_id):
            raise PersonaSchemaError(f"Invalid persona id {persona_id!r} in {path}")
        if persona_id in seen_persona_ids:
            raise PersonaSchemaError(f"Duplicate persona id {persona_id!r} in {path}")
        seen_persona_ids.add(persona_id)
        checks: list[PersonaCheck] = []
        for raw_check in raw_persona.get("checks") or []:
            if not isinstance(raw_check, dict):
                raise PersonaSchemaError(f"Each check for persona {persona_id} must be a mapping in {path}")
            check = _parse_persona_check(raw_check, persona_id=persona_id, path=path)
            if check.id in seen_check_ids:
                raise PersonaSchemaError(f"Duplicate persona check id {check.id!r} in {path}")
            seen_check_ids.add(check.id)
            checks.append(check)
        _validate_persona_dependencies(persona_id=persona_id, checks=tuple(checks), path=path)
        personas.append(
            PersonaDefinition(
                id=persona_id,
                priority=str(raw_persona.get("priority", "normal")),
                role=_optional_config_string(raw_persona.get("role")),
                owner=_optional_config_string(raw_persona.get("owner")),
                frame=_optional_config_string(raw_persona.get("frame")),
                always_active=bool(raw_persona.get("always_active", False)),
                checks=tuple(checks),
            )
        )
    return PersonaRegistry(
        schema_version=str(raw_registry.get("schema_version")),
        enforcement=enforcement,
        personas=tuple(personas),
    )


def _parse_voice_contract(value: Any, *, path: Path) -> VoiceContractSettings | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"voice_contract must be a mapping in {path}")
    return VoiceContractSettings(
        applies_to_editions=_parse_string_tuple(value.get("applies_to_editions"), field_name="voice_contract.applies_to_editions", path=path),
        program_tokens=_parse_string_tuple(value.get("program_tokens"), field_name="voice_contract.program_tokens", path=path),
        abstract_phrases=_parse_string_tuple(value.get("abstract_phrases"), field_name="voice_contract.abstract_phrases", path=path),
        synthetic_delta_prefixes=tuple(
            entry.upper()
            for entry in _parse_string_tuple(value.get("synthetic_delta_prefixes"), field_name="voice_contract.synthetic_delta_prefixes", path=path)
        ),
        decision_lead_terms=tuple(
            entry.lower()
            for entry in _parse_string_tuple(value.get("decision_lead_terms"), field_name="voice_contract.decision_lead_terms", path=path)
        ),
        static_concrete_terms=tuple(
            entry.lower()
            for entry in _parse_string_tuple(value.get("static_concrete_terms"), field_name="voice_contract.static_concrete_terms", path=path)
        ),
        exec_summary_bucket_prefixes=tuple(
            entry.lower()
            for entry in _parse_string_tuple(value.get("exec_summary_bucket_prefixes"), field_name="voice_contract.exec_summary_bucket_prefixes", path=path)
        ),
        objective_preamble_prefixes=tuple(
            entry.lower()
            for entry in _parse_string_tuple(value.get("objective_preamble_prefixes"), field_name="voice_contract.objective_preamble_prefixes", path=path)
        ),
    )


def _parse_structural_rules(value: Any, *, path: Path) -> tuple[StructuralRule, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"structural_rules must be a list in {path}")
    rules: list[StructuralRule] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError(f"structural_rules entries must be mappings in {path}")
        rule_id = str(entry.get("id", "")).strip()
        if not _PERSONA_ID_RE.match(rule_id):
            raise ConfigError(f"Invalid structural rule id {rule_id!r} in {path}")
        if rule_id in seen:
            raise ConfigError(f"Duplicate structural rule id {rule_id!r} in {path}")
        seen.add(rule_id)
        regex_absent = _optional_config_string(entry.get("regex_absent"))
        regex_present = _optional_config_string(entry.get("regex_present"))
        if bool(regex_absent) == bool(regex_present):
            raise ConfigError(f"structural rule {rule_id!r} must define exactly one of regex_absent or regex_present")
        severity = str(entry.get("severity", "warn")).strip().lower()
        if severity not in {"warn", "block"}:
            raise ConfigError(f"structural rule {rule_id!r} severity must be warn or block")
        scopes = entry.get("scope") or ()
        if not isinstance(scopes, list) or not all(isinstance(scope, str) and scope.strip() for scope in scopes):
            raise ConfigError(f"structural rule {rule_id!r} scope must be a non-empty list of strings")
        rules.append(
            StructuralRule(
                id=rule_id,
                description=_optional_config_string(entry.get("description")),
                regex_absent=regex_absent,
                regex_present=regex_present,
                scope=tuple(str(scope).strip() for scope in scopes),
                severity=severity,
                autofix_hint=_optional_config_string(entry.get("autofix_hint")),
            )
        )
    return tuple(rules)


def _parse_text_processing(value: Any, *, path: Path) -> TextProcessingSettings:
    if value in (None, ""):
        return TextProcessingSettings()
    if not isinstance(value, dict):
        raise ConfigError(f"text_processing must be a mapping in {path}")
    abbreviations = value.get("abbreviations")
    if abbreviations is None:
        return TextProcessingSettings()
    if not isinstance(abbreviations, list) or not all(isinstance(entry, str) and entry.strip() for entry in abbreviations):
        raise ConfigError(f"text_processing.abbreviations must be a list of strings in {path}")
    return TextProcessingSettings(abbreviations=tuple(str(entry).strip() for entry in abbreviations))


def _validate_persona_document_shape(raw_registry: dict[str, Any], *, path: Path) -> None:
    schema_path = CORE_SCHEMAS_ROOT / "personas.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(raw_registry), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "<root>"
        raise PersonaSchemaError(f"Invalid personas.yaml at {location}: {first.message}")


def _parse_persona_check(raw_check: dict[str, Any], *, persona_id: str, path: Path) -> PersonaCheck:
    check_id = str(raw_check.get("id", "")).strip()
    check_type = str(raw_check.get("type", "")).strip()
    severity = str(raw_check.get("severity", "warn")).strip().lower()
    quarantine_reason = _persona_check_quarantine_reason(raw_check, check_id=check_id, check_type=check_type, path=path)
    return PersonaCheck(
        id=check_id,
        type=check_type,
        scope=str(raw_check.get("scope", "")).strip(),
        message=str(raw_check.get("message", "")).strip(),
        severity=severity if severity in {"warn", "block"} else "warn",
        keywords=tuple(str(entry).strip() for entry in (raw_check.get("keywords") or ()) if str(entry).strip()),
        pattern=_optional_config_string(raw_check.get("pattern")),
        regex_flags=_optional_config_string(raw_check.get("regex_flags")),
        threshold=_optional_int(raw_check.get("threshold")),
        element=_optional_config_string(raw_check.get("element")),
        rule_ref=_optional_config_string(raw_check.get("rule_ref")),
        enforce_after=_optional_date_string(raw_check.get("enforce_after"), field_name=f"{persona_id}.{check_id}.enforce_after"),
        updated_at=_optional_date_string(raw_check.get("updated_at"), field_name=f"{persona_id}.{check_id}.updated_at"),
        phase=_optional_config_string(raw_check.get("phase")),
        requires=tuple(str(entry).strip() for entry in (raw_check.get("requires") or ()) if str(entry).strip()),
        strict_scope=bool(raw_check.get("strict_scope", False)),
        quarantine_reason=quarantine_reason,
        min=_optional_int(raw_check.get("min")),
        max=_optional_int(raw_check.get("max")),
        source_scope=_optional_config_string(raw_check.get("source_scope")),
        source_extract=_optional_config_string(raw_check.get("source_extract")),
        target_scope=_optional_config_string(raw_check.get("target_scope")),
        require_all_in_target=bool(raw_check.get("require_all_in_target", False)),
        require_none_in_target=bool(raw_check.get("require_none_in_target", False)),
        baseline_scope=_optional_config_string(raw_check.get("baseline_scope")),
        check_attributes=tuple(
            str(entry).strip() for entry in (raw_check.get("check_attributes") or ()) if str(entry).strip()
        ),
        tolerance=_parse_persona_tolerance(raw_check.get("tolerance")),
        risk_keyword_map=_parse_risk_keyword_map(raw_check.get("risk_keyword_map")),
        source_field=_optional_config_string(raw_check.get("source_field")),
        when_source_present=_parse_when_source_present(raw_check.get("when_source_present")),
        extract_numerator_from=_optional_config_string(raw_check.get("extract_numerator_from")),
        extract_denominator_from=_optional_config_string(raw_check.get("extract_denominator_from")),
        count_tolerance=_optional_int(raw_check.get("count_tolerance")) or 0,
        rules=_parse_section_structure_rules(raw_check.get("rules")),
    )


def _persona_check_quarantine_reason(raw_check: dict[str, Any], *, check_id: str, check_type: str, path: Path) -> str | None:
    if not _PERSONA_ID_RE.match(check_id):
        return f"invalid check id {check_id!r}"
    if check_type in {"keyword_present", "keyword_absent"} and not raw_check.get("keywords"):
        return "keyword check requires non-empty keywords"
    if check_type in {"regex_present", "regex_absent"}:
        pattern = raw_check.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return "regex check requires pattern"
        if len(pattern) > 500:
            return "regex pattern exceeds 500 characters"
        if _has_nested_quantifier(pattern):
            return "regex pattern contains nested quantifier"
        try:
            re.compile(pattern, flags=_regex_flags(raw_check.get("regex_flags")))
        except re.error as exc:
            return f"invalid regex pattern: {exc}"
    if check_type == "sentence_length_max" and _optional_int(raw_check.get("threshold")) is None:
        return "sentence_length_max requires threshold"
    if check_type == "structure_present" and not raw_check.get("element"):
        return "structure_present requires element"
    if check_type == "delegate_to_rule" and not raw_check.get("rule_ref"):
        return "delegate_to_rule requires rule_ref"
    if check_type == "format_matches" and not _optional_config_string(raw_check.get("pattern")):
        return "format_matches requires pattern"
    if check_type == "cross_scope_consistency":
        if not _optional_config_string(raw_check.get("source_scope")):
            return "cross_scope_consistency requires source_scope"
        if not _optional_config_string(raw_check.get("source_extract")):
            return "cross_scope_consistency requires source_extract"
        if not _optional_config_string(raw_check.get("target_scope")) and not _optional_config_string(raw_check.get("scope")):
            return "cross_scope_consistency requires target_scope"
    if check_type == "published_baseline_match" and not raw_check.get("check_attributes"):
        return "published_baseline_match requires check_attributes"
    if check_type == "terminology_consistency":
        if not _optional_config_string(raw_check.get("source_field")):
            return "terminology_consistency requires source_field"
        if not isinstance(raw_check.get("when_source_present"), dict):
            return "terminology_consistency requires when_source_present"
    if check_type == "count_range":
        if not _optional_config_string(raw_check.get("pattern")):
            return "count_range requires pattern"
        if not _optional_config_string(raw_check.get("extract_numerator_from")):
            return "count_range requires extract_numerator_from"
        if not _optional_config_string(raw_check.get("extract_denominator_from")):
            return "count_range requires extract_denominator_from"
    if check_type == "section_structure" and not raw_check.get("rules"):
        return "section_structure requires rules"
    return None


def _parse_persona_tolerance(value: Any) -> tuple[tuple[str, str], ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, dict):
        return ()
    parsed: list[tuple[str, str]] = []
    for key, raw in value.items():
        name = str(key).strip()
        if not name:
            continue
        parsed.append((name, str(raw).strip()))
    return tuple(parsed)


def _parse_risk_keyword_map(value: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, dict):
        return ()
    parsed: list[tuple[str, tuple[str, ...]]] = []
    for key, raw in value.items():
        keywords = tuple(str(entry).strip() for entry in (raw or ()) if str(entry).strip())
        parsed.append((str(key).strip(), keywords))
    return tuple(parsed)


def _parse_when_source_present(value: Any) -> WhenSourcePresent | None:
    if value in (None, "") or not isinstance(value, dict):
        return None
    forbidden = tuple(str(entry).strip() for entry in (value.get("forbidden_patterns") or ()) if str(entry).strip())
    required = _optional_config_string(value.get("required_pattern"))
    return WhenSourcePresent(
        forbidden_patterns=forbidden,
        required_pattern=required,
    )


def _parse_section_structure_rules(value: Any) -> tuple[SectionSubRule, ...]:
    if value in (None, "") or not isinstance(value, list):
        return ()
    parsed: list[SectionSubRule] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        parsed.append(
            SectionSubRule(
                id=str(entry.get("id", "")).strip(),
                pattern=str(entry.get("pattern", "")).strip(),
                message=str(entry.get("message", "")).strip(),
                require=bool(entry.get("require", True)),
                min_matching_lines=_optional_int(entry.get("min_matching_lines")) or 0,
            )
        )
    return tuple(rule for rule in parsed if rule.id and rule.pattern and rule.message)


def _validate_persona_dependencies(*, persona_id: str, checks: tuple[PersonaCheck, ...], path: Path) -> None:
    check_ids = {check.id for check in checks}
    for check in checks:
        for dependency in check.requires:
            if dependency not in check_ids:
                raise PersonaSchemaError(f"Persona {persona_id} check {check.id!r} requires unknown check {dependency!r} in {path}")
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {check.id: check for check in checks}

    def visit(check_id: str) -> None:
        if check_id in visited:
            return
        if check_id in visiting:
            raise PersonaSchemaError(f"Persona {persona_id} has a requires cycle involving {check_id!r} in {path}")
        visiting.add(check_id)
        for dependency in by_id[check_id].requires:
            visit(dependency)
        visiting.remove(check_id)
        visited.add(check_id)

    for check_id in check_ids:
        visit(check_id)


def _regex_flags(value: Any) -> int:
    if value in (None, ""):
        return 0
    flags = 0
    for flag in str(value).split("|"):
        normalized = flag.strip().upper()
        if normalized == "IGNORECASE":
            flags |= re.IGNORECASE
        elif normalized == "MULTILINE":
            flags |= re.MULTILINE
        elif normalized == "DOTALL":
            flags |= re.DOTALL
        else:
            raise PersonaSchemaError(f"Unsupported regex flag {flag!r}")
    return flags


def _has_nested_quantifier(pattern: str) -> bool:
    return bool(re.search(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*[+*?{]", pattern))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_date_string(value: Any, *, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    normalized = str(value).strip()
    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise PersonaSchemaError(f"{field_name} must be YYYY-MM-DD") from exc
    return normalized


def _optional_config_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    return normalized or None


def _parse_verbosity_word_limit(value: Any, *, field_name: str) -> tuple[int | None, EditionVerbosityLimits]:
    if value is None:
        return None, EditionVerbosityLimits()
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer or mapping")
    if isinstance(value, int):
        return value, EditionVerbosityLimits()
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be an integer or mapping")

    default_value = _parse_optional_int(value.get("default"), field_name=f"{field_name}.default")
    return default_value, EditionVerbosityLimits(
        detailed=_parse_optional_int(value.get("detailed"), field_name=f"{field_name}.detailed"),
        focused=_parse_optional_int(value.get("focused"), field_name=f"{field_name}.focused"),
        condensed=_parse_optional_int(value.get("condensed"), field_name=f"{field_name}.condensed"),
        narrative=_parse_optional_int(value.get("narrative"), field_name=f"{field_name}.narrative"),
        deck=_parse_optional_int(value.get("deck"), field_name=f"{field_name}.deck"),
    )


def _parse_optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field_name} must be an integer")
    return value


def _parse_string_tuple(value: Any, *, field_name: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ConfigError(f"{field_name} must be a list of strings in {path}")
    return tuple(entry.strip() for entry in value if entry.strip())


def load_review_config(path: Path) -> ReviewSettings:
    raw_review = load_yaml_mapping(path)
    raw_reviewers = raw_review.get("reviewers", [])
    if not isinstance(raw_reviewers, list):
        raise ConfigError(f"reviewers must be a list in {path}")

    reviewers: list[ReviewerSettings] = []
    for reviewer in raw_reviewers:
        if not isinstance(reviewer, dict):
            raise ConfigError(f"Each reviewer entry must be a mapping in {path}")
        name = reviewer.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Reviewer name is required in {path}")
        sections = reviewer.get("sections", [])
        if not isinstance(sections, list) or not all(isinstance(section, str) for section in sections):
            raise ConfigError(f"Reviewer sections must be a list of strings in {path}")
        reviewers.append(
            ReviewerSettings(
                name=name,
                sections=tuple(section for section in sections if section.strip()),
            )
        )

    return ReviewSettings(
        reviewers=tuple(reviewers),
        required=bool(raw_review.get("required", False)),
    )


def _parse_leadership_personas(raw: Any) -> tuple[LeadershipPersona, ...]:
    """Parse the ``leadership_personas`` mapping from ``program.yaml``."""
    if not isinstance(raw, dict):
        return ()
    result: list[LeadershipPersona] = []
    for persona_id, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        raw_rules = spec.get("voice_rules")
        voice_rules: PersonaVoiceRules | None = None
        if isinstance(raw_rules, dict):
            voice_rules = PersonaVoiceRules(
                banned_patterns=tuple(
                    str(entry).lower()
                    for entry in (raw_rules.get("banned_patterns") or [])
                    if isinstance(entry, str) and str(entry).strip()
                ),
                required_signals=tuple(
                    str(entry).lower()
                    for entry in (raw_rules.get("required_signals") or [])
                    if isinstance(entry, str) and str(entry).strip()
                ),
            )
        result.append(
            LeadershipPersona(
                persona_id=str(persona_id),
                role=spec.get("role") or None,
                question_style=spec.get("question_style") or None,
                typical_questions=tuple(
                    q for q in (spec.get("typical_questions") or []) if isinstance(q, str) and q.strip()
                ),
                cares_about=spec.get("cares_about") or None,
                pet_peeves=spec.get("pet_peeves") or None,
                communication_bar=spec.get("communication_bar") or None,
                voice_rules=voice_rules,
            )
        )
    return tuple(result)


def load_program_context(path: Path) -> NarrativeProgramContext:
    raw_context = load_yaml_mapping(path)
    return _parse_program_context_document(raw_context, path)


def _parse_program_context_document(raw_context: dict[str, Any], path: Path) -> NarrativeProgramContext:
    raw_program = raw_context.get("program", {})
    if not isinstance(raw_program, dict):
        raw_program = {}
    program_name = raw_context.get("program_name") or raw_program.get("name")
    objective = raw_context.get("objective") or raw_program.get("objective")
    mission = raw_context.get("mission") or raw_program.get("why_it_matters")
    if raw_context.get("schema_version") != "1.0":
        raise ConfigError(f"Unsupported program context schema version in {path}")
    if not program_name:
        raise ConfigError(f"program_name is required in {path}")
    if not objective and not mission:
        raise ConfigError(f"Either objective or mission is required in {path}")
    sub_programs = tuple(
        ProgramSubProgram(
            id=sub_program["id"],
            name=sub_program["name"],
            aliases=tuple(sub_program.get("aliases", [])),
            summary=sub_program.get("summary"),
            status=sub_program.get("status"),
            primary_area_paths=tuple(sub_program.get("primary_area_paths", [])),
            why_distinct=sub_program.get("why_distinct"),
        )
        for sub_program in raw_context.get("sub_programs", [])
        if isinstance(sub_program, dict)
        and isinstance(sub_program.get("id"), str)
        and isinstance(sub_program.get("name"), str)
    )
    workstreams = tuple(
        ProgramWorkstream(
            name=workstream["name"],
            aliases=tuple(workstream.get("aliases", [])),
            area_paths=tuple(workstream.get("area_paths", [])),
            dri_email=workstream.get("dri_email"),
            alternate_owner=workstream.get("alternate_owner"),
            description=workstream.get("description"),
            why_it_matters=workstream.get("why_it_matters"),
            history_summary=workstream.get("history_summary"),
            leadership_sensitivity=workstream.get("leadership_sensitivity"),
            current_blocker=workstream.get("current_blocker"),
        )
        for workstream in raw_context.get("workstreams", [])
    )
    raw_people = raw_context.get("people", [])
    people_payload = raw_people if isinstance(raw_people, list) else raw_people.get("people", []) if isinstance(raw_people, dict) else []
    leadership_payload = raw_context.get("leadership_readers")
    if leadership_payload is None and isinstance(raw_people, dict):
        leadership_payload = raw_people.get("leadership_readers", [])
    workstream_owner_payload = raw_context.get("workstream_owners")
    if workstream_owner_payload is None and isinstance(raw_people, dict):
        workstream_owner_payload = raw_people.get("workstream_owners", [])
    raw_dependency_chain = raw_context.get("key_dependency_chain")
    if raw_dependency_chain is None:
        raw_dependency_chain = raw_program.get("key_dependency_chain", [])
    people = tuple(
        ProgramPerson(
            email=person["email"],
            display_name=person.get("display_name"),
            role=person.get("role"),
            workstreams=tuple(person.get("workstreams", [])),
        )
        for person in people_payload
    )
    leadership_readers = tuple(
        LeadershipReader(
            name=reader["name"],
            role=reader.get("role"),
            cares_about=tuple(reader.get("cares_about", [])),
            prefers=reader.get("prefers"),
            pet_peeves=tuple(reader.get("pet_peeves", [])),
        )
        for reader in (leadership_payload if isinstance(leadership_payload, list) else [])
        if isinstance(reader, dict) and isinstance(reader.get("name"), str)
    )
    workstream_owners = tuple(
        WorkstreamOwnerProfile(
            name=owner["name"],
            areas=tuple(owner.get("areas", [])),
            style_note=owner.get("style_note"),
            timezone=owner.get("timezone"),
            alternate=owner.get("alternate"),
        )
        for owner in (workstream_owner_payload if isinstance(workstream_owner_payload, list) else [])
        if isinstance(owner, dict) and isinstance(owner.get("name"), str)
    )
    key_dependency_chain = tuple(
        ProgramDependency(
            source=str(dependency.get("from") or dependency.get("from_item")),
            target=str(dependency.get("to") or dependency.get("to_item")),
            impact=dependency["impact"],
        )
        for dependency in (raw_dependency_chain if isinstance(raw_dependency_chain, list) else [])
        if isinstance(dependency, dict)
        and isinstance(dependency.get("from") or dependency.get("from_item"), str)
        and isinstance(dependency.get("to") or dependency.get("to_item"), str)
        and isinstance(dependency.get("impact"), str)
    )
    recurring_themes = tuple(
        theme.strip()
        for theme in raw_context.get("recurring_themes", [])
        if isinstance(theme, str) and theme.strip()
    )
    raw_writing_style = raw_context.get("writing_style", {})
    writing_style = None
    if isinstance(raw_writing_style, dict) and any(
        raw_writing_style.get(key) for key in ("voice", "structure", "risk_framing", "preferred_patterns")
    ):
        writing_style = WritingStyle(
            voice=raw_writing_style.get("voice"),
            structure=raw_writing_style.get("structure"),
            risk_framing={
                str(key): str(value)
                for key, value in raw_writing_style.get("risk_framing", {}).items()
                if isinstance(key, str) and isinstance(value, str)
            },
            preferred_patterns=tuple(
                pattern
                for pattern in raw_writing_style.get("preferred_patterns", [])
                if isinstance(pattern, str) and pattern.strip()
            ),
        )
    raw_tone_calibration = raw_context.get("tone_calibration", {})
    tone_calibration = None
    if isinstance(raw_tone_calibration, dict) and (
        raw_tone_calibration.get("overall") or raw_tone_calibration.get("per_theme_override")
    ):
        tone_calibration = ToneCalibration(
            overall=raw_tone_calibration.get("overall"),
            per_theme_override={
                str(key): str(value)
                for key, value in raw_tone_calibration.get("per_theme_override", {}).items()
                if isinstance(key, str) and isinstance(value, str)
            },
        )
    leadership_personas = _parse_leadership_personas(
        raw_context.get("leadership_personas") or raw_program.get("leadership_personas")
    )
    return NarrativeProgramContext(
        schema_version=raw_context["schema_version"],
        program_name=program_name,
        objective=objective,
        mission=mission,
        pillars=tuple(raw_context.get("pillars", [])),
        glossary=dict(raw_context.get("glossary", {})),
        sub_programs=sub_programs,
        workstreams=workstreams,
        people=people,
        leadership_readers=leadership_readers,
        workstream_owners=workstream_owners,
        recurring_themes=recurring_themes,
        writing_style=writing_style,
        tone_calibration=tone_calibration,
        current_phase=raw_context.get("current_phase") or raw_program.get("current_phase"),
        key_dependency_chain=key_dependency_chain,
        leadership_personas=leadership_personas,
    )


def _validate_with_schema(document: dict[str, Any], schema_path: Path) -> None:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda err: list(err.path))
    if not errors:
        return
    error_messages = []
    for error in errors:
        path = ".".join(str(part) for part in error.path) or "<root>"
        error_messages.append(f"{path}: {error.message}")
    joined = "; ".join(error_messages)
    raise ConfigError(f"Config validation failed: {joined}")


def _resolve_env_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_values(item) for item in value]
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(_replace_env_match, value)
    return value


def _replace_env_match(match: re.Match[str]) -> str:
    env_name = match.group(1)
    return os.environ.get(env_name, match.group(0))


def _parse_report_config(raw_config: dict[str, Any]) -> ReportConfig:
    edition = raw_config["edition"]
    author = raw_config.get("author", {})
    distribution = raw_config.get("distribution", {})
    ado = raw_config["ado"]
    ai = raw_config.get("ai", {})
    kusto = raw_config.get("kusto", {})
    m365 = raw_config.get("m365", {})
    workiq = m365.get("workiq", {}) if isinstance(m365, dict) else {}
    bluebird = m365.get("bluebird", {}) if isinstance(m365, dict) else {}
    offline = m365.get("offline", {}) if isinstance(m365, dict) else {}
    archive = raw_config["archive"]
    logging = raw_config.get("logging", {})
    resolved_ado_timeout_seconds = int(raw_config.get("ado_fetch_timeout_seconds") or ado.get("api_timeout_seconds") or 45)
    layout_mode = _parse_layout_mode(raw_config.get("layout_mode"))
    cadence_note = _parse_cadence_note(raw_config.get("cadence_note"))
    if layout_mode == "continuity" and cadence_note is None:
        raise ConfigError("layout_mode=continuity requires cadence_note in config.yaml")

    scorecards = tuple(
        ScorecardSettings(
            name=scorecard["name"],
            dimensions=tuple(
                ScorecardDimensionSettings(
                    name=dimension["name"],
                    description=dimension.get("description"),
                    ado_filter=dimension.get("ado_filter", ""),
                    linked_scorecard_name=dimension.get("linked_scorecard"),
                    linked_dimension_name=dimension.get("linked_dimension"),
                    dfd_proximity_sensitive=bool(dimension.get("dfd_proximity_sensitive", False)),
                )
                for dimension in scorecard["dimensions"]
            ),
        )
        for scorecard in raw_config["scorecards"]
    )

    return ReportConfig(
        schema_version=raw_config["schema_version"],
        edition=EditionSettings(
            name=edition["name"],
            type=edition["type"],
            title=edition["title"],
            cadence=edition["cadence"],
            send_day=edition.get("send_day"),
            send_time_local=edition.get("send_time_local"),
            timezone=edition.get("timezone"),
        ),
        author=AuthorSettings(
            display_name=author.get("display_name", ""),
            email=author.get("email", ""),
        ),
        distribution=DistributionSettings(
            to=tuple(distribution.get("to", [])),
            cc=tuple(distribution.get("cc", [])),
            channels=tuple(distribution.get("channels", [])),
        ),
        ado=ADOSettings(
            organization=ado["organization"],
            project=ado["project"],
            area_paths=tuple(ado["area_paths"]),
            work_item_types=tuple(ado["work_item_types"]),
            excluded_states=tuple(ado.get("excluded_states", [])),
            date_window_days=ado["date_window_days"],
            api_timeout_seconds=resolved_ado_timeout_seconds,
            proposal_ttl_hours=int(ado.get("proposal_ttl_hours", 72) or 72),
        ),
        scorecards=scorecards,
        ai=AISettings(
            enabled=bool(ai.get("enabled", False)),
            budget_usd_per_run=float(ai.get("budget_usd_per_run", 0.5)),
            blurb_deployment=ai.get("blurb_deployment"),
            blurb_backup_deployment=ai.get("blurb_backup_deployment"),
            exec_summary_deployment=ai.get("exec_summary_deployment"),
            exec_summary_backup_deployment=ai.get("exec_summary_backup_deployment"),
            temperature=ai.get("temperature"),
        ),
        kusto=KustoSettings(
            enabled=bool(kusto.get("enabled", False)),
            queries=tuple(
                KustoQuerySettings(
                    id=query["id"],
                    cluster=query["cluster"],
                    database=query["database"],
                    kql=query["kql"],
                    section=query["section"],
                    render_as=query["render_as"],
                    confidence=str(query.get("confidence", "medium")),
                    kusto_section_validates_slice=bool(query.get("kusto_section_validates_slice", False)),
                    caveats=tuple(query.get("caveats", [])),
                    reference_url=query.get("reference_url"),
                )
                for query in kusto.get("queries", [])
            ),
        ),
        m365=M365Settings(
            enabled=bool(m365.get("enabled", False)) if isinstance(m365, dict) else False,
            prefer_agency=bool(m365.get("prefer_agency", True)) if isinstance(m365, dict) else True,
            workiq=M365WorkIQSettings(
                newsletter_search=workiq.get("newsletter_search") if isinstance(workiq.get("newsletter_search"), str) else None,
                feedback_search=workiq.get("feedback_search") if isinstance(workiq.get("feedback_search"), str) else None,
                teams_search=workiq.get("teams_search") if isinstance(workiq.get("teams_search"), str) else None,
            ),
            bluebird=M365BluebirdSettings(
                teams_channels=tuple(
                    channel
                    for channel in bluebird.get("teams_channels", [])
                    if isinstance(channel, str) and channel.strip()
                ),
                lookback_days=int(bluebird.get("lookback_days", 7)),
            ),
            offline=M365OfflineSettings(
                newsletter_dir=offline.get("newsletter_dir") if isinstance(offline.get("newsletter_dir"), str) else None,
                transcript_dir=offline.get("transcript_dir") if isinstance(offline.get("transcript_dir"), str) else None,
            ),
            teams_incoming_webhook_url=(
                str(m365.get("teams_incoming_webhook_url", "")).strip()
                if isinstance(m365, dict)
                and isinstance(m365.get("teams_incoming_webhook_url"), str)
                and str(m365.get("teams_incoming_webhook_url", "")).strip()
                else None
            ),
            artifact_base_url=(
                str(m365.get("artifact_base_url", "")).strip()
                if isinstance(m365, dict) and isinstance(m365.get("artifact_base_url"), str) and str(m365.get("artifact_base_url", "")).strip()
                else None
            ),
            sharepoint=_parse_sharepoint_config(m365.get("sharepoint") if isinstance(m365, dict) else None),
        ),
        archive=ArchiveSettings(root=archive["root"]),
        logging=LoggingSettings(
            level=str(logging.get("level", "INFO")),
            json=bool(logging.get("json", False)),
        ),
        layout_mode=layout_mode,
        cadence_note=cadence_note,
        scorecard_sort=_parse_scorecard_sort(raw_config.get("scorecard_sort")),
        scorecard_plain_text_only=bool(raw_config.get("scorecard_plain_text_only", False)),
        brand_name=_optional_string(raw_config.get("brand_name")),
        brand_header_url=_optional_string(raw_config.get("brand_header_url")),
        ado_fetch_timeout_seconds=resolved_ado_timeout_seconds,
        forecast_enabled=bool(raw_config.get("forecast_enabled", False)),
        mobile_safe_scorecards=_parse_mobile_safe_scorecards(raw_config.get("mobile_safe_scorecards")),
        type_scale_v2=bool(raw_config.get("type_scale_v2", False)),
        calibration_pilot=bool(raw_config.get("calibration_pilot", False)),
    )


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _parse_sharepoint_config(sp_dict: dict[str, Any] | None) -> "SharePointConfig | None":
    """SP2-3: Parse m365.sharepoint YAML section into SharePointConfig.

    current_deck_date is explicitly excluded from **lt before unpacking to prevent
    TypeError: duplicate keyword argument when the key appears twice.
    date.fromisoformat raises ValueError on malformed input — intentional.
    """
    if sp_dict is None or not isinstance(sp_dict, dict):
        return None
    lt_deck = None
    if "lt_deck" in sp_dict and isinstance(sp_dict["lt_deck"], dict):
        lt_raw = sp_dict["lt_deck"]
        lt_kwargs = {k: v for k, v in lt_raw.items() if k != "current_deck_date"}
        if "current_deck_date" in lt_raw and lt_raw["current_deck_date"] is not None:
            lt_kwargs["current_deck_date"] = date.fromisoformat(str(lt_raw["current_deck_date"]))
        lt_deck = SharePointLtDeckConfig(**lt_kwargs)
    rest = {k: v for k, v in sp_dict.items() if k != "lt_deck"}
    return SharePointConfig(lt_deck=lt_deck, **rest)


def _parse_layout_mode(value: Any) -> str:
    if value in (None, ""):
        return "dashboard"
    normalized = str(value).strip().lower()
    if normalized in {"dashboard", "continuity"}:
        return normalized
    raise ConfigError(f"Unsupported layout_mode value in config.yaml: {value!r}")


def _parse_scorecard_sort(value: Any) -> str:
    if value in (None, ""):
        return "risk_desc"
    normalized = str(value).strip().lower()
    if normalized in {"risk_desc", "fixed"}:
        return normalized
    raise ConfigError(f"Unsupported scorecard_sort value in config.yaml: {value!r}")


def _parse_cadence_note(value: Any) -> CadenceNoteSettings | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ConfigError("cadence_note must be a mapping in config.yaml")
    detailed = value.get("detailed")
    focused = value.get("focused")
    if not isinstance(detailed, str) or not detailed.strip():
        raise ConfigError("cadence_note.detailed is required in config.yaml")
    if not isinstance(focused, str) or not focused.strip():
        raise ConfigError("cadence_note.focused is required in config.yaml")
    return CadenceNoteSettings(
        detailed=detailed.strip(),
        focused=focused.strip(),
        first_issue_override=_optional_string(value.get("first_issue_override")),
    )


def _parse_mobile_safe_scorecards(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in {"auto", "row"}:
        return normalized
    raise ConfigError(f"Unsupported mobile_safe_scorecards value in config.yaml: {value!r}")
