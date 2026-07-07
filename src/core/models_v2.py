from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from src.core.hypothesis_models import (
    AssertionEvaluation,
    AssertionOperator,
    ChallengeKind,
    ChallengeSeverity,
    ChallengeState,
    CompositeAssertion,
    CompositeAssertionOperator,
    DigestDelta,
    Hypothesis,
    HypothesisEvent,
    HypothesisKind,
    HypothesisStatus,
    MetricFreshnessEntry,
    RealityBaseline,
    RealityChallenge,
    RealityDigestModel,
    RecoveredHypothesis,
    StaleHypothesisEntry,
    SuppressionSummary,
    TelemetryAssertion,
)
from src.core.metric_models import (
    MetricAggregation,
    MetricDefinition,
    MetricObservation,
    MetricQualityState,
    MetricSourceBinding,
    ObservationWindow,
)
from src.core.chronicle import ProgramEvent
from src.core.external_dependency import ExternalDependency
from src.core.models import Confidence, EnumParserMixin, RiskLevel
from src.core.source_models import AssumptionEvent, IngestionRun, MaintenanceWindow, MetricBindingHealth, SourceKind, SourceRef

if TYPE_CHECKING:
    # Avoid runtime import cycles: program_reality imports models_v2, and
    # evidence_models imports models (not models_v2) but we keep both under
    # TYPE_CHECKING so the bundle's annotations resolve for type-checkers only.
    from src.core.evidence_models import WorkstreamEvidence
    from src.core.program_reality import RealityConflict


DateValue = date


@dataclass(frozen=True, slots=True)
class PersonDirectory:
    alias: str
    email: str | None = None
    display_name: str | None = None
    title: str | None = None
    team_ids: tuple[str, ...] = ()
    org_chain: tuple[str, ...] = ()
    exempt_from_vitality: bool = False


@dataclass(frozen=True, slots=True)
class PersonProfile:
    alias: str
    comm_style: str | None = None
    cares_about: tuple[str, ...] = ()
    pet_peeves: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Team:
    id: str
    name: str
    area_paths: tuple[str, ...] = ()
    programs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Product:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    related_teams: tuple[str, ...] = ()
    description: str | None = None


@dataclass(frozen=True, slots=True)
class EngMsPage:
    id: str
    title: str
    url: str
    workstream_ids: tuple[str, ...] = ()
    program_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    description: str | None = None
    source_subtype: Literal["lt_deck", "ref_doc"] | None = None  # SP2-2: document subtype for change detection routing
    cadence_days: int | None = None  # SP2-2: expected update frequency in days


@dataclass(frozen=True, slots=True)
class SkippedIssueEntry:
    edition_id: str
    issue_number: int
    generated_at: datetime
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorConfig:
    display_name: str
    email: str


@dataclass(frozen=True, slots=True)
class DistributionConfig:
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ADOConfig:
    organization: str
    project: str
    area_paths: tuple[str, ...]
    work_item_types: tuple[str, ...]
    excluded_states: tuple[str, ...]
    date_window_days: int
    api_timeout_seconds: int = 30
    proposal_ttl_hours: int = 72


@dataclass(frozen=True, slots=True)
class AIConfig:
    enabled: bool
    budget_usd_per_run: float
    blurb_deployment: str | None = None
    blurb_backup_deployment: str | None = None
    exec_summary_deployment: str | None = None
    exec_summary_backup_deployment: str | None = None
    temperature: float | None = None
    requests_per_minute: int | None = None


@dataclass(frozen=True, slots=True)
class AttachmentConfig:
    """Chart placement configuration. Used by KustoQuery and KustoQuerySettings."""
    target: str  # "exec_summary" or "workstream:<section_id>"
    position: Literal["after"] = "after"
    fallback: Literal["standalone", "suppress"] = "standalone"


@dataclass(frozen=True, slots=True)
class KustoQuery:
    id: str
    cluster: str
    database: str
    kql: str
    section: str
    render_as: str
    confidence: str
    reference_url: str | None = None
    caveats: tuple[str, ...] = ()
    kusto_section_validates_slice: bool = False
    fallback_kql: str | None = None  # FR-SG-31: secondary KQL if primary returns ZERO_ROWS or QUERY_FAILED
    program_ids: tuple[str, ...] = ()
    workstream_ids: tuple[str, ...] = ()
    validated: bool = False
    refresh_on_gather: bool = False
    label: str | None = None
    result_column: str | None = None
    catalog_source: dict[str, str] | None = None
    validated_at: datetime | None = None
    owner_alias: str | None = None
    expected_cardinality: str = "zero_ok"
    kusto_no_safety: bool = False
    last_cycle_succeeded: bool | None = None
    metric_id: str | None = None
    assertion_ids: tuple[str, ...] = ()
    engine: str = "kusto"
    wiql: str | None = None
    # Chart pipeline fields (R3)
    chart_renderer_id: str | None = None
    chart_config: dict[str, Any] | None = None
    attachment: AttachmentConfig | None = None
    chart_cache_ttl_hours: int = 26
    chart_blocks_publish: bool = False
    fallback_on_empty_rows: bool = False
    chapter: str | None = None
    timeout_seconds: int | None = None  # per-query override; None = KustoClient default (120s)


@dataclass(frozen=True, slots=True)
class KustoConfig:
    enabled: bool
    queries: tuple[KustoQuery, ...] = ()


# WorkIQ structured-retrieval configuration (vertex-tech-spec §13.1.1). Lives on
# ``M365Config`` (parsed from the ``m365.retrieval`` block) and NOT under the
# ``m365.workiq`` mapping — ``edition_resolver._parse_m365`` coerces every scalar
# under ``m365.workiq`` into a bogus query name, so a sibling key is mandatory.
# Only preview-enumeration fields live here; richer body-evidence controls are
# governed separately.
WORKIQ_DISCOVERY_MODE_LEGACY = "legacy_nl"
WORKIQ_DISCOVERY_MODE_STRUCTURED = "structured_json"
WORKIQ_DISCOVERY_MODES = frozenset({WORKIQ_DISCOVERY_MODE_LEGACY, WORKIQ_DISCOVERY_MODE_STRUCTURED})


@dataclass(frozen=True, slots=True)
class WorkIQRetrievalConfig:
    discovery_mode: str = WORKIQ_DISCOVERY_MODE_LEGACY
    discovery_union_runs: int = 1
    discovery_lookback_days: int = 14
    per_thread_extraction: bool = False
    per_thread_top_k: int = 3
    per_thread_one_hop: bool = False
    max_calls_per_cycle: int = 40
    max_wall_clock_seconds: int = 600


# --- REV (Program-Context Intelligence) retrieval profile ---
# specs/program-context-intelligence.md §5.1. Lives on ``M365Config`` (parsed
# from the ``m365.rev`` block) and governs the capability-port pipeline. The
# default profile is ``legacy_nl`` so existing behavior is unchanged until an
# operator explicitly opts into ``rev_verified`` (rollout states §7).
REV_PROFILE_LEGACY_NL = "legacy_nl"
REV_PROFILE_SEARCH_HYDRATE = "search_hydrate"
REV_PROFILE_REV_VERIFIED = "rev_verified"
REV_PROFILES = frozenset({REV_PROFILE_LEGACY_NL, REV_PROFILE_SEARCH_HYDRATE, REV_PROFILE_REV_VERIFIED})

REV_AUTH_SCOPE_PERSONAL_COMMS_MAIL = "personal_comms_mail"
REV_AUTH_SCOPE_PERSONAL_COMMS = "personal_comms"
REV_AUTH_SCOPE_FULL = "full"
REV_AUTH_SCOPE_TIERS = frozenset({REV_AUTH_SCOPE_PERSONAL_COMMS_MAIL, REV_AUTH_SCOPE_PERSONAL_COMMS, REV_AUTH_SCOPE_FULL})

REV_EVIDENCE_METADATA_ONLY = "metadata_only"
REV_EVIDENCE_EXCERPT_VAULTED = "excerpt_vaulted"
REV_EVIDENCE_POLICIES = frozenset({REV_EVIDENCE_METADATA_ONLY, REV_EVIDENCE_EXCERPT_VAULTED})

REV_HYDRATION_DROP = "drop"
REV_HYDRATION_METADATA_ONLY_FLAGGED = "metadata_only_flagged"
REV_HYDRATION_POLICIES = frozenset({REV_HYDRATION_DROP, REV_HYDRATION_METADATA_ONLY_FLAGGED})

REV_STRUCTURED_OUTPUTS_PROBE = "probe"
REV_STRUCTURED_OUTPUTS_JSON_OBJECT = "json_object"
REV_STRUCTURED_OUTPUTS_OFF = "off"
REV_STRUCTURED_OUTPUTS_MODES = frozenset({REV_STRUCTURED_OUTPUTS_PROBE, REV_STRUCTURED_OUTPUTS_JSON_OBJECT, REV_STRUCTURED_OUTPUTS_OFF})

REV_GROUNDEDNESS_OFF = "off"
REV_GROUNDEDNESS_ADVISORY = "advisory"
REV_GROUNDEDNESS_GATE = "gate"
REV_GROUNDEDNESS_MODES = frozenset({REV_GROUNDEDNESS_OFF, REV_GROUNDEDNESS_ADVISORY, REV_GROUNDEDNESS_GATE})


@dataclass(frozen=True, slots=True)
class RevBudgets:
    max_search_requests_total_per_cycle: int = 60
    max_search_requests_per_entity_per_cycle: int = 20
    max_hydrated_bytes_per_cycle: int = 10_485_760
    max_hydrated_bytes_per_item: int = 1_048_576
    max_chunk_count_per_cycle: int = 200
    max_chunk_count_per_item: int = 40
    max_model_tokens_in_per_cycle: int = 500_000
    max_model_tokens_out_per_cycle: int = 50_000
    max_content_safety_requests_per_cycle: int = 200
    max_monetized_spend_per_cycle_usd: float = 2.00
    max_wall_clock_seconds: int = 600
    concurrency_per_provider: int = 4
    fleet_concurrency_cap: int = 12
    per_lane_share: str = "equal"


@dataclass(frozen=True, slots=True)
class RevRetrievalProfile:
    profile: str = REV_PROFILE_LEGACY_NL
    auth_scope_tier: str = REV_AUTH_SCOPE_PERSONAL_COMMS_MAIL
    fallback_policy: str = "fail_visible"
    evidence_policy: str = REV_EVIDENCE_EXCERPT_VAULTED
    hydration_fallback: str = REV_HYDRATION_DROP
    structured_outputs: str = REV_STRUCTURED_OUTPUTS_PROBE
    groundedness: str = REV_GROUNDEDNESS_OFF
    budgets: RevBudgets = field(default_factory=RevBudgets)
    # Policy versions encoded in cache keys (§5.10) — bump to invalidate caches.
    normalization_version: str = "norm.v1"
    scrubber_version: str = "scrub.v1"
    chunking_version: str = "chunk.v1"
    injection_policy_version: str = "injection.v1"
    extraction_policy_version: str = "extract.v1"
    content_safety_policy_version: str = "cs.v1"
    human_materiality_policy_version: str = "materiality.v1"
    orphan_ttl_days: int = 7
    rejected_review_retention_days: int = 30
    pending_grace_days: int = 14
    fact_bridge_enabled: bool = False

    @property
    def is_rev_verified(self) -> bool:
        return self.profile == REV_PROFILE_REV_VERIFIED

    @property
    def verification_gate_enabled(self) -> bool:
        """The triage-approve verification gate is active only under rev_verified.

        Under ``legacy_nl``/``search_hydrate`` and for pre-existing deterministic
        candidates the gate is a no-op — backward compatible with the 25
        existing ``CandidateEvent`` callsites and the current ``triage_approve``
        flow (§5.9, rollout states §7).
        """
        return self.is_rev_verified


@dataclass(frozen=True, slots=True)
class M365Config:
    enabled: bool
    prefer_agency: bool = True
    workiq_queries: dict[str, str] | None = None
    icm_incidents_url: str | None = None
    workiq_enrich_schedule: str | None = None
    retrieval: WorkIQRetrievalConfig | None = None
    rev: "RevRetrievalProfile | None" = None


@dataclass(frozen=True, slots=True)
class IntegrationError:
    source: str
    stage: str
    retryable: bool
    message: str
    operator_action: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyDependency:
    from_item: str
    to_item: str
    impact: str


class DependencyType(EnumParserMixin, str, Enum):
    BLOCKS = "blocks"
    INFORMS = "informs"
    SHARES_RESOURCE = "shares_resource"


class DependencyStatus(EnumParserMixin, str, Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    BROKEN = "broken"


class DependencyScheduleStatus(EnumParserMixin, str, Enum):
    OK = "ok"
    AT_RISK = "at_risk"
    SLIPPED = "slipped"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class Dependency:
    id: str
    from_program_id: str
    from_workstream_id: str | None
    from_item_id: int | None
    from_milestone_id: str | None
    to_program_id: str
    to_workstream_id: str | None
    to_item_id: int | None
    to_milestone_id: str | None
    dependency_type: DependencyType
    risk_if_broken: str
    mitigation: str | None
    status: DependencyStatus
    owner_alias: str | None
    resolution_path: str | None = None
    planned_resolution_date: date | None = None
    schedule_status: DependencyScheduleStatus | None = None
    linked_risk_ids: tuple[str, ...] = ()


class RiskImpact(EnumParserMixin, str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskProbability(EnumParserMixin, str, Enum):
    VERY_LIKELY = "very_likely"
    LIKELY = "likely"
    POSSIBLE = "possible"
    UNLIKELY = "unlikely"


class RiskStatus(EnumParserMixin, str, Enum):
    OPEN = "open"
    MITIGATED = "mitigated"
    ACCEPTED = "accepted"
    CLOSED = "closed"
    ESCALATED = "escalated"


class RiskCategory(EnumParserMixin, str, Enum):
    TECHNICAL = "technical"
    SCHEDULE = "schedule"
    RESOURCE = "resource"
    DEPENDENCY = "dependency"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class RiskEntry:
    id: str
    program_id: str
    title: str
    description: str
    probability: RiskProbability
    impact: RiskImpact
    category: RiskCategory
    owner_alias: str
    mitigation_plan: str | None
    mitigation_due_date: date | None
    linked_workstream_ids: tuple[str, ...]
    linked_work_item_ids: tuple[int, ...]
    linked_milestone_ids: tuple[str, ...]
    linked_claim_ids: tuple[str, ...]
    linked_action_ids: tuple[str, ...]
    status: RiskStatus
    identified_date: date
    identified_in_vertex_issue: int | None
    last_reviewed_date: date | None
    entity_refs: tuple[str, ...]
    source_signal_ids: tuple[str, ...] = ()
    kind: str = "strategic"
    dimension_id: str | None = None
    fact_id: str | None = None
    last_validated_at: datetime | None = None


class ActionStatus(EnumParserMixin, str, Enum):
    PROPOSED = "proposed"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class ActionSourceType(EnumParserMixin, str, Enum):
    MEETING_TRANSCRIPT = "meeting_transcript"
    REVIEW_FEEDBACK = "review_feedback"
    TRIAGE = "triage"
    MANUAL = "manual"
    SIGNAL = "signal"


@dataclass(frozen=True, slots=True)
class ActionItem:
    id: str
    program_id: str
    text: str
    owner_alias: str
    due_date: date | None
    status: ActionStatus
    source_signal_id: str | None
    source_type: ActionSourceType
    linked_work_item_ids: tuple[int, ...]
    linked_claim_id: str | None
    linked_risk_id: str | None
    workstream_id: str | None
    created_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None
    fact_id: str | None = None
    last_validated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ActionStatusUpdate:
    action_id: str
    new_status: ActionStatus
    updated_at: datetime
    updated_by: str
    note: str | None = None
    record_type: Literal["status_update"] = "status_update"


class MilestoneStatus(EnumParserMixin, str, Enum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    MISSED = "missed"
    COMPLETED = "completed"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class Milestone:
    id: str
    program_id: str
    name: str
    target_date: date
    owner_alias: str
    status: MilestoneStatus
    exit_criteria: tuple[str, ...]
    linked_workstream_ids: tuple[str, ...]
    linked_work_item_ids: tuple[int, ...]
    notes: str | None = None
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class MilestoneAssessment:
    milestone_id: str
    computed_health: MilestoneStatus
    blocked_criteria: tuple[str, ...]
    slip_probability: float
    critical_path: bool
    confidence: Confidence
    reasoning: str
    completion_date: date | None = None


@dataclass(frozen=True, slots=True)
class WritingStyle:
    voice: str | None = None
    structure: str | None = None
    risk_framing: dict[str, str] | None = None
    preferred_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToneCalibration:
    overall: str | None = None
    per_theme_override: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class LeadershipReader:
    name: str
    role: str | None = None
    cares_about: tuple[str, ...] = ()
    prefers: str | None = None
    pet_peeves: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TeamsMeetingSeries:
    display_name: str
    series_id: str | None = None
    include_transcripts: bool = True
    work_item_ids: tuple[int, ...] = ()
    # P4-21: name-based transcript path. ``calendar_name`` is the exact Teams
    # calendar title matching WorkIQ's calendar index (used by
    # ``TranscriptReader.get_transcript_by_name`` when ``series_id`` is null).
    # ``vpn_required`` records whether the meeting is only discoverable on VPN.
    calendar_name: str | None = None
    vpn_required: bool = False


@dataclass(frozen=True, slots=True)
class TeamsChat:
    display_name: str
    thread_id: str | None = None
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EmailThreadSource:
    display_name: str
    thread_id: str
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DependencyADOQuery:
    label: str
    resolution_path: str
    area_path: str | None = None
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ADOCoverageRequirement:
    min_ado_count: int = 3
    required_work_item_types: tuple[str, ...] = ()
    suppress_coverage_alert: bool = False


@dataclass(frozen=True, slots=True)
class WorkstreamSignalSources:
    teams_meeting_series: tuple[TeamsMeetingSeries, ...] = ()
    teams_chats: tuple[TeamsChat, ...] = ()
    email_subject_filters: tuple[str, ...] = ()
    workiq_keywords: tuple[str, ...] = ()
    kusto_query_ids: tuple[str, ...] = ()
    ado_coverage: ADOCoverageRequirement | None = None
    workiq_exclude_keywords: tuple[str, ...] = ()
    # Optional FQ-01 lane overrides. ``None`` inherits the program-level
    # ``M365Config.retrieval`` value; explicit values keep noisy/quiet lanes
    # independently tunable without changing the safe platform default.
    workiq_discovery_mode: str | None = None
    workiq_discovery_union_runs: int | None = None
    workiq_discovery_lookback_days: int | None = None
    email_threads: tuple[EmailThreadSource, ...] = ()
    dependency_ado_queries: tuple[DependencyADOQuery, ...] = ()
    sharepoint_paths: tuple[str, ...] = ()  # SP2-1: engms_pages.yaml entry IDs routed to this workstream
    engms_paths: tuple[str, ...] = ()  # SP2-1: eng.ms page IDs (same format, different host)


@dataclass(frozen=True, slots=True)
class Workstream:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    area_paths: tuple[str, ...] = ()
    ado_team: str | None = None
    ado_pipeline_ids: tuple[str, ...] = ()
    ado_repository_ids: tuple[str, ...] = ()
    pm_owner: str | None = None
    eng_owner: str | None = None
    accountable_owner: str | None = None
    responsible_owners: tuple[str, ...] = ()
    dri_email: str | None = None
    alternate_owner: str | None = None
    always_notify: tuple[str, ...] = ()
    description: str | None = None
    why_it_matters: str | None = None
    history_summary: str | None = None
    leadership_sensitivity: str | None = None
    current_blocker: str | None = None
    ado_saved_query_ids: tuple[str, ...] = ()
    signal_sources: WorkstreamSignalSources | None = None
    accountable_email: str | None = None
    consulted_owners: tuple[str, ...] = ()
    informed_owners: tuple[str, ...] = ()
    last_reviewed_date: date | None = None
    owner_person_id: str | None = None
    status: str = "active"


@dataclass(frozen=True, slots=True)
class ScorecardDimension:
    name: str
    workstream_id: str
    description: str | None = None
    ado_filter: str | None = None
    slice_contract_ref: str | None = None
    linked_scorecard_name: str | None = None
    linked_dimension_name: str | None = None
    dfd_proximity_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class Scorecard:
    name: str
    dimensions: tuple[ScorecardDimension, ...]


@dataclass(frozen=True, slots=True)
class WorkstreamFilter:
    mode: Literal["all", "include", "exclude"] = "all"
    workstream_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Program:
    schema_version: str
    id: str
    name: str
    chapter_namespace: str | None = None
    maturity_level: int = 0
    objective: str | None = None
    mission: str | None = None
    current_phase: str | None = None
    pillars: tuple[str, ...] = ()
    glossary: dict[str, str] | None = None
    leadership_readers: tuple[LeadershipReader, ...] = ()
    writing_style: WritingStyle | None = None
    tone_calibration: ToneCalibration | None = None
    key_dependencies: tuple[LegacyDependency, ...] = ()
    author_defaults: AuthorConfig | None = None
    distribution_defaults: DistributionConfig | None = None
    ado: ADOConfig | None = None
    ai: AIConfig | None = None
    kusto: KustoConfig | None = None
    m365: M365Config | None = None
    source_confidence_order: tuple[str, ...] = ()
    storage_backend: str = "file"
    expected_gather_cadence_hours: float | None = None
    golden_queries: tuple[str, ...] = ()
    min_channel_completeness_pct: int = 80
    backfill_max_days: int = 14


@dataclass(frozen=True, slots=True)
class EditionConfig:
    schema_version: str
    id: str
    program_id: str
    name: str
    type: str
    altitude: str
    cadence: str
    send_day: str | None = None
    send_time_local: str | None = None
    timezone: str | None = None
    author: AuthorConfig | None = None
    distribution: DistributionConfig | None = None
    ado: dict[str, Any] | None = None
    ai: dict[str, Any] | None = None
    kusto: dict[str, Any] | None = None
    m365: dict[str, Any] | None = None
    workstream_filter: WorkstreamFilter | None = None
    brand_name: str | None = None
    brand_header_url: str | None = None
    scorecard_sort: str = "risk_desc"
    scorecard_plain_text_only: bool = False
    layout_mode: str = "dashboard"
    cadence_note: dict[str, str] | None = None
    ado_fetch_timeout_seconds: int | None = None
    forecast_enabled: bool = False
    mobile_safe_scorecards: str | None = None
    type_scale_v2: bool = False
    calibration_pilot: bool = False


@dataclass(frozen=True, slots=True)
class WorkstreamCalibration:
    workstream_id: str
    met: int
    contradicted: int
    stale: int

    @property
    def sample_size(self) -> int:
        return self.met + self.contradicted + self.stale

    @property
    def claim_accuracy(self) -> float | None:
        if self.sample_size < 5:
            return None
        return self.met / self.sample_size


@dataclass(frozen=True, slots=True)
class ForecastCalibrationModifier:
    workstream_modifiers: dict[str, float] = field(default_factory=dict)
    dri_modifiers: dict[str, float] = field(default_factory=dict)
    confidence: Confidence = Confidence.NONE


class DataSourceType(EnumParserMixin, str, Enum):
    ADO = "ado"
    WORKIQ = "workiq"
    KUSTO = "kusto"
    JOURNAL = "journal"


@dataclass(frozen=True, slots=True)
class ResolvedContradiction:
    winning_source: DataSourceType
    confidence: Confidence
    rationale: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Contradiction:
    field: str
    source_a: str
    source_b: str
    summary: str
    confidence: Confidence
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContradictionPacket:
    work_item_id: int
    workstream_id: str | None
    contradictions: tuple[Contradiction, ...]
    confidence: Confidence
    recommended_resolution: ResolvedContradiction | None
    generated_at: datetime


class ReviewPolicy(EnumParserMixin, str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"  # §22 E3: operator-authored Plane 1 changes bypass review queue
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class CatchupEvent:
    event_id: str
    program_id: str
    detected_at: datetime
    kind: str
    work_item_id: int | None
    workstream_id: str | None
    summary: str
    severity: Literal["info", "warn", "alert"]
    salience_score: float
    confidence: Confidence = Confidence.NONE
    signal_id: str | None = None


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    timestamp: datetime
    source: str
    program_id: str
    workstream_id: str | None
    entity_refs: tuple[str, ...]
    text: str
    raw_ref: str | None
    confidence: Confidence
    metadata: dict[str, Any] | None = None
    thread_id: str | None = None
    review_policy: ReviewPolicy | None = None


@dataclass(frozen=True, slots=True)
class SignalThreadLink:
    signal_id: str
    thread_id: str
    linked_at: datetime
    linked_by: str
    record_type: str = "thread_link"


@dataclass(frozen=True, slots=True)
class ADOFieldChangeMetadata:
    field: str
    prior: str | None
    current: str | None


@dataclass(frozen=True, slots=True)
class KustoMetadata:
    query_id: str
    event_timestamp: datetime | None
    validated: bool


@dataclass(frozen=True, slots=True)
class WorkIQMetadata:
    source_type: str
    message_id: str
    sender_alias: str | None = None
    entity_link_confidence: str | None = None


@dataclass(frozen=True, slots=True)
class IcMMetadata:
    incident_id: int
    severity: int
    owning_team: str | None = None


@dataclass(frozen=True, slots=True)
class IncidentEntry:
    program_id: str
    incident_id: str
    signal_id: str
    observed_at: datetime
    recorded_at: datetime
    belief_change_summary: str
    workstream_id: str | None
    owning_team: str | None = None
    severity: int | None = None
    source_path: str | None = None
    query_id: str | None = None
    linked_work_item_ids: tuple[int, ...] = ()
    ado_entity_refs: tuple[str, ...] = ()
    raw_ref: str | None = None
    confidence: Confidence = Confidence.NONE
    schema_version: str = "1.0"


@dataclass(frozen=True, slots=True)
class SignalReviewDecision:
    signal_id: str
    decision: Literal["approved", "dismissed", "deferred"]
    reviewed_at: datetime
    reviewed_by: str
    note: str | None = None
    record_type: Literal["review"] = "review"


@dataclass(frozen=True, slots=True)
class SignalUsageMarker:
    signal_id: str
    issue_number: int
    edition_id: str
    manifest_id: str
    used_at: datetime
    record_type: Literal["usage_marker"] = "usage_marker"


class AIProposalStatus(EnumParserMixin, str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class MetricEvidenceBrief:
    """FR-SG-15: Compact metric evidence snapshot for inclusion in section briefs."""
    observation_id: str
    metric_id: str
    value_summary: str      # e.g., "98.5%" or "87.5 ms"
    freshness_label: str    # e.g., "fresh", "1 day stale"
    quality_state: str      # MetricQualityState.value
    linked_claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class SectionEvidenceBrief:
    section_id: str
    ado_delta_summary: str
    new_items: tuple[int, ...]
    closed_items: tuple[int, ...]
    risk_changed_items: tuple[int, ...]
    eta_changed_items: tuple[int, ...]
    top_signals: tuple[str, ...]
    kpi_summary: str | None
    stale_claims: tuple[str, ...]
    vitality_summary: str
    confidence: Confidence
    # FR-SG-23: activity heuristic for section suppression
    activity_score: float = 0.0
    suppression_suggested: bool = False
    # FR-SG-15: evidence-backed metric observations
    metric_observations: tuple[MetricEvidenceBrief, ...] = ()


class SectionRevisionStatus(EnumParserMixin, str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    ACCEPTED_MODIFIED = "accepted_modified"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class SectionRevisionProposal:
    proposal_id: str
    edition_id: str
    issue_number: int
    section_id: str
    current_text: str
    proposed_text: str | None
    evidence_brief: SectionEvidenceBrief
    status: SectionRevisionStatus
    generated_at: datetime
    resolved_at: datetime | None = None
    accepted_text: str | None = None
    rejection_reason: str | None = None
    source_hash: str | None = None
    ai_model_used: str | None = None
    ai_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class WorkstreamSynthesis:
    workstream_id: str
    overall_assessment: str
    proposed_risk: RiskLevel
    confidence: Confidence
    key_findings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    open_questions: tuple[str, ...]
    recommended_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AIProposal:
    id: str
    workstream_id: str
    synthesis: WorkstreamSynthesis
    status: AIProposalStatus
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    edition_id: str | None = None
    issue_number: int | None = None


@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    date: DateValue
    state: str
    assigned_to: str | None
    target_date: DateValue | None
    risk_level: RiskLevel | None
    area_path: str
    tags: tuple[str, ...] = ()
    risk_assessment: str | None = None
    risk_assessment_comment: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimEntry:
    id: str
    program_id: str
    edition_id: str
    issue_number: int
    workstream_id: str | None
    text: str
    entity_refs: tuple[str, ...]
    claim_date: date
    owner_alias: str | None
    due_date: date | None
    status: Literal["open"] = "open"
    # FR-SG-12: evidence governance fields (with backward-compat defaults)
    # contradiction_status is separate from effective_status computed by assess_claim_entries();
    # it records whether a fact-layer check found the claim contradicted by ADO reality.
    contradiction_status: Literal["ok", "contradicted", "unresolved"] = "ok"
    source_confidence_tier: Literal["high", "medium", "low"] = "low"
    last_validated_date: date | None = None


@dataclass(frozen=True, slots=True)
class ResurfacingPolicy:
    watch_days: int = 7
    nudge_days: int = 14
    escalate_days: int = 21


@dataclass(frozen=True, slots=True)
class RaidChainLink:
    node_id: str
    node_type: Literal["risk", "action", "decision", "assumption"]
    title: str
    status: str
    hop: int


@dataclass(frozen=True, slots=True)
class DecisionAsk:
    id: str
    program_id: str
    edition_id: str
    issue_number: int
    text: str
    entity_refs: tuple[str, ...]
    ask_date: date
    owner_alias: str | None
    status: Literal["open", "resolved", "deferred"] = "open"
    resolution: str | None = None
    expiry_date: date | None = None
    resurfacing_policy: ResurfacingPolicy | None = None
    affected_milestone_ids: tuple[str, ...] = ()
    last_touched_at: datetime | None = None


class DecisionStatus(EnumParserMixin, str, Enum):
    PROPOSED = "proposed"
    DECIDED = "decided"
    SUPERSEDED = "superseded"
    REVERTED = "reverted"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    id: str
    program_id: str
    title: str
    context: str
    decision: str
    rationale: str | None
    alternatives_considered: tuple[str, ...]
    decided_by: str | None
    decision_date: date | None
    status: DecisionStatus
    superseded_by: str | None
    linked_claim_id: str | None
    linked_risk_id: str | None
    linked_action_ids: tuple[str, ...]
    workstream_id: str | None
    entity_refs: tuple[str, ...]
    review_by: date | None = None
    linked_milestone_ids: tuple[str, ...] = ()
    last_reviewed_date: date | None = None
    fact_id: str | None = None
    last_validated_at: datetime | None = None
    expected_outcome_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Judgment:
    """A scored dimension judgment written at confirm time (FR-SG-59)."""

    id: str
    program_id: str
    dimension: str
    risk_level: str
    edition_id: str
    issue_number: int
    justification: str
    decided_by: str
    decided_at: datetime
    review_by: date | None = None
    status: str = "active"
    superseded_by: str | None = None
    fact_id: str | None = None


class AssumptionStatus(EnumParserMixin, str, Enum):
    ACTIVE = "active"
    UNVALIDATED = "unvalidated"
    CONFIRMED = "confirmed"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class Assumption:
    id: str
    program_id: str
    text: str
    validation_method: str | None
    validation_due: date | None
    status: AssumptionStatus
    linked_risk_id: str | None
    linked_milestone_id: str | None
    owner_alias: str | None
    identified_date: date
    entity_refs: tuple[str, ...]
    resolved_date: date | None = None
    category: str | None = None
    linked_workstream_ids: tuple[str, ...] = ()
    linked_milestone_ids: tuple[str, ...] = ()
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class ClaimStatusUpdate:
    claim_id: str
    new_status: Literal["open", "met", "contradicted", "stale", "deferred", "resolved"]
    updated_at: datetime
    updated_by: str
    note: str | None = None
    record_type: Literal["status_update"] = "status_update"


@dataclass(frozen=True, slots=True)
class VitalityScore:
    work_item_id: int
    owner_alias: str | None
    workstream_id: str | None
    freshness_days: int
    freshness_grade: Literal["green", "amber", "red"]
    richness_score: int
    richness_missing: tuple[str, ...]
    leakage_events: int
    workiq_signal_count: int
    composite_score: int
    suggested_update: str | None


@dataclass(frozen=True, slots=True)
class VitalityAggregate:
    scope_id: str
    scope_type: Literal["owner", "workstream"]
    total_items: int
    fresh_items: int
    avg_richness: float
    total_leakage: int
    workiq_signal_count: int
    leakage_ratio: float
    composite_score: int
    trend: Literal["improving", "stable", "worsening"] | None = None


class SignalClass(EnumParserMixin, str, Enum):
    STATUS = "status"
    RCA = "rca"
    MITIGATION = "mitigation"
    DECISION = "decision"
    RISK = "risk"
    DEPENDENCY = "dependency"


@dataclass(frozen=True, slots=True)
class VitalityProgramSnapshot:
    scores: tuple[VitalityScore, ...]
    workstream_aggregates: tuple[VitalityAggregate, ...]
    total_items: int
    items_fresh: int
    updated_percentage: int
    freshness_average_days: float
    avg_richness: int
    leakage_events: int
    aggregate_score: int


@dataclass(frozen=True, slots=True)
class VitalityArchiveWorkstream:
    score: int
    items: int
    fresh: int


@dataclass(frozen=True, slots=True)
class VitalityArchiveEntry:
    issue_number: int
    confirmed_at: datetime
    aggregate_score: int
    items_total: int
    items_fresh: int
    avg_richness: int
    leakage_events: int
    per_workstream: dict[str, VitalityArchiveWorkstream]
    per_owner: dict[str, VitalityArchiveWorkstream] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HygieneItem:
    work_item_id: int
    title: str
    item_type: str
    freshness_business_days: int
    freshness_calendar_days: int
    missing_fields: tuple[str, ...]
    workstream_name: str | None
    workstream_id: str | None
    item_url: str
    is_child: bool
    parent_id: int | None


@dataclass(frozen=True, slots=True)
class HygieneCoverageAlert:
    recipient_alias: str
    recipient_email: str
    recipient_display_name: str
    total_items: int
    items_missing_multiple_fields: int
    required_fields: tuple[str, ...]
    observed_ratio: float
    threshold_ratio: float
    summary_text: str
    confidence: Confidence = Confidence.NONE


@dataclass(frozen=True, slots=True)
class HygieneNudgePreview:
    recipient_alias: str
    recipient_email: str
    recipient_display_name: str
    subject: str
    items: tuple[HygieneItem, ...]
    suppressed_items: tuple[str, ...]
    deadline_iso: str
    html_body: str
    md_body: str


@dataclass(frozen=True, slots=True)
class HygieneNudgeArtifacts:
    program_id: str
    owner_previews: tuple[HygieneNudgePreview, ...]
    coverage_alerts: tuple[HygieneCoverageAlert, ...]
    suppressed_owners: tuple[str, ...]
    unresolved_owners: tuple[str, ...]
    eml_paths: tuple[Path, ...]
    total_items_flagged: int
    total_children_scanned: int
    unmapped_items: tuple[HygieneItem, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkstreamEvidencePacket:
    """FR-SG-19: Comprehensive per-workstream evidence packet.

    Assembles ADO delta, MetricObservations, Decisions/Commitments,
    ExternalDependencies, and Chronicle events for a single workstream.
    """

    workstream_id: str
    section_brief: SectionEvidenceBrief
    top_decisions: tuple[DecisionEntry, ...]
    open_dependencies: tuple[ExternalDependency, ...]
    chronicle_events: tuple[ProgramEvent, ...]
    eta_summary: str | None
    timeline_credibility: float | None  # 0.0–1.0 from trajectory_analyzer; None if unavailable
    as_of: datetime


@dataclass(frozen=True, slots=True)
class WorkstreamEvidenceBundle:
    """P4-5 / P4-1: unified pre-synthesis evidence for one workstream (§7.7).

    Replaces the ADO-only ``evidence_by_item`` input to ``blurb_generator`` with a
    single stream that merges every approved source. P4-1 targets this interface from
    the start (initially M365-populated); P4-5 fills in the remaining sources and
    P4-10 fills in ``conflicts``. This is the canonical bundle — do not create a
    competing parallel model (it co-exists with, and may later wrap, the narrower
    ``WorkstreamEvidencePacket`` above).

    All signal fields hold *approved* journal Signals only (the §17.8 review gate is
    enforced upstream by ``signal_is_approved_for_evidence``). ``m365_evidence`` is
    gated separately via ``load_approved_evidence_by_lane`` (§17.8 Option A, P4-0).
    """

    lane_id: str
    ado_signals: tuple[Signal, ...] = ()           # formal ADO state (auto-approved)
    kusto_metrics: tuple[Signal, ...] = ()         # quantitative truth (auto-approved)
    icm_blockers: tuple[Signal, ...] = ()          # active incidents (auto-approved)
    ado_comments: tuple[Signal, ...] = ()          # engineer "why" narrative (source="ado/comment")
    reference_signals: tuple[Signal, ...] = ()     # approved eng.ms / SharePoint reference-doc change signals
    m365_evidence: WorkstreamEvidence | None = None  # M365 extraction (requires approval)
    conflicts: tuple[RealityConflict, ...] = ()    # cross-source disagreements (P4-10)
    as_of: datetime | None = None
    lookback_intelligence: tuple[str, ...] = ()    # P4-24: trajectory / retrospective context
    freshness_by_source: dict[str, datetime] = field(default_factory=dict)
    corroboration_notes: tuple[str, ...] = ()      # P4-27: cross-source agreement signals


@dataclass(frozen=True, slots=True)
class RiskDerivedLevel:
    """FR-SG-20: Strategic risk derivation result.

    Always a *proposal* requiring human confirmation; never auto-applied.
    """

    proposed_level: RiskLevel
    upgrade_reason: str | None  # populated when item_risk_mix or trajectory signals upgrade
    downgrade_reason: str | None  # populated when mitigation evidence supports downgrade
    is_proposal: bool = True  # always True — requires human confirmation before use


# ── Phase 2 stubs — deliverable and incident authority ───────────────────────
# These models support the `deliverable.entry` and `incident.entry` fact types
# that are KNOWN_UNPROJECTEABLE in v1 (S-2d / Q9).
# Phase 2: add bridge appender, family_map entry, and fact-projection logic.
# See: .archive/specs/consolidated.md §40 (deliv-incident-fu epic, local-only).

class DeliverableStatus(EnumParserMixin, str, Enum):
    OPEN = "open"
    AT_RISK = "at_risk"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DeliverableEntry:
    """Phase 2 model: `deliverable.entry` fact type.

    Not yet projectable in v1 (disposition=KNOWN_UNPROJECTEABLE in event_type_registry).
    Phase 2 gate: bridge appender + IcM/ADO deliverable source + fact-type schema.
    """

    id: str
    program_id: str
    title: str
    owner_alias: str | None
    due_date: date | None
    status: DeliverableStatus
    linked_milestone_ids: tuple[str, ...]
    linked_workstream_ids: tuple[str, ...]
    entity_refs: tuple[str, ...]
    identified_date: date
    description: str | None = None
    resolution_note: str | None = None
    resolved_date: date | None = None
    fact_id: str | None = None
    last_validated_at: datetime | None = None


class IncidentSeverity(EnumParserMixin, str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentStatus(EnumParserMixin, str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    POST_MORTEM = "post_mortem"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class IncidentFactEntry:
    """Phase 2 model: `incident.entry` fact type.

    Not yet projectable in v1 (disposition=KNOWN_UNPROJECTEABLE in event_type_registry).
    Phase 2 gate: IcM source wired (S-10a/S-10b) + bridge appender + fact-type schema.
    IcM authority family is already defined in source_authority.yaml.

    Named IncidentFactEntry to avoid shadowing the existing IncidentEntry (journal/signal model).
    """

    id: str
    program_id: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    owner_alias: str | None
    identified_date: date
    resolved_date: date | None
    linked_milestone_ids: tuple[str, ...]
    linked_workstream_ids: tuple[str, ...]
    entity_refs: tuple[str, ...]
    description: str | None = None
    icm_id: str | None = None       # IcM incident ID (populated when IcM source is wired)
    post_mortem_url: str | None = None
    fact_id: str | None = None
    last_validated_at: datetime | None = None
