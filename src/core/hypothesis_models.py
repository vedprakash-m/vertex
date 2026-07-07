from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal

from src.core.metric_models import MetricQualityState, ObservationWindow
from src.core.models import EnumParserMixin
from src.core.source_models import SourceRef


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AssertionOperator(EnumParserMixin, str, Enum):
    GTE = ">="
    LTE = "<="
    EQ = "=="
    NEQ = "!="
    BETWEEN = "between"
    PCT_IMPROVEMENT = "pct_improvement"
    PCT_REGRESSION = "pct_regression"
    FORECAST_GTE = "forecast_gte"
    FORECAST_LTE = "forecast_lte"
    BURN_RATE_GTE = "burn_rate_gte"
    BURN_RATE_LTE = "burn_rate_lte"


class CompositeAssertionOperator(EnumParserMixin, str, Enum):
    AND = "and"
    OR = "or"


class HypothesisStatus(EnumParserMixin, str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CHALLENGED = "challenged"
    INVALIDATED = "invalidated"
    STALE = "stale"
    SUPERSEDED = "superseded"


class HypothesisKind(EnumParserMixin, str, Enum):
    SCALAR_FACT = "scalar_fact"
    TREND = "trend"
    DELIVERY_DATE = "delivery_date"


class ChallengeKind(EnumParserMixin, str, Enum):
    THRESHOLD_BREACH = "threshold_breach"
    DELIVERY_DATE = "delivery_date"
    STALENESS = "staleness"
    MANUAL = "manual"
    DATA_LOSS = "data_loss"
    SOURCE_DEGRADED = "source_degraded"
    ZERO_ROWS_BREACH = "zero_rows_breach"
    DEPENDENCY_CASCADE = "dependency_cascade"


class ChallengeSeverity(EnumParserMixin, str, Enum):
    INFO = "info"
    WARN = "warn"
    ALERT = "alert"


class ChallengeState(EnumParserMixin, str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    REOPENED = "reopened"
    SNOOZED = "snoozed"


@dataclass(frozen=True, slots=True)
class TelemetryAssertion:
    id: str
    program_id: str
    metric_id: str
    window: ObservationWindow
    operator: AssertionOperator
    threshold: float
    tolerance_rel: float = 0.10
    tolerance_abs: float | None = None
    baseline_value: float | None = None
    baseline_captured_at: datetime | None = None
    sustain_min_observations: int = 3
    cooldown_hours: int = 24
    severity_override: Literal["info", "warn", "alert"] | None = None
    description: str = ""
    linked_hypothesis_id: str | None = None
    linked_claim_id: str | None = None
    linked_assumption_id: str | None = None
    re_evaluate_by: date | None = None
    valid_from: datetime = field(default_factory=_utc_now)
    valid_until: datetime | None = None
    policy_version: int = 1
    created_by: str = "vertex/system"
    threshold_upper: float | None = None


@dataclass(frozen=True, slots=True)
class CompositeAssertion:
    id: str
    program_id: str
    operator: CompositeAssertionOperator
    child_assertion_ids: tuple[str, ...]
    description: str = ""
    linked_hypothesis_id: str | None = None
    valid_from: datetime = field(default_factory=_utc_now)
    valid_until: datetime | None = None
    policy_version: int = 1
    created_by: str = "vertex/system"


@dataclass(frozen=True, slots=True)
class Hypothesis:
    id: str
    short_id: str
    program_id: str
    kind: HypothesisKind
    statement: str
    expected_value: float | str | None
    as_of_date: date
    telemetry_assertion_id: str | None
    source_refs: tuple[SourceRef, ...] = ()
    workstream_id: str | None = None
    proposed_by: str = ""
    proposed_at: datetime | None = None
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    sensitivity_label: Literal["public", "internal", "confidential", "secret"] = "internal"
    depends_on: tuple[str, ...] = ()
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    review_due: date | None = None
    superseded_by: str | None = None
    supersedes_id: str | None = None
    rejection_reason: str | None = None
    linked_claim_id: str | None = None
    linked_assumption_id: str | None = None
    linked_ado_item_id: int | None = None
    linked_doc_section_id: str | None = None
    expected_value_frozen_at: datetime | None = None
    expires_at: datetime | None = None
    policy_version: int = 1
    composite_assertion_id: str | None = None


@dataclass(frozen=True, slots=True)
class HypothesisAnnotation:
    id: str
    program_id: str
    hypothesis_id: str
    kind: Literal["pdf", "markdown", "url", "file"]
    title: str
    locator: str
    locator_kind: Literal["url", "repo_path", "local_path"]
    media_type: str | None = None
    sha256: str | None = None
    note: str | None = None
    tags: tuple[str, ...] = ()
    source_ref: SourceRef | None = None
    added_by: str = "vertex/system"
    added_at: datetime = field(default_factory=_utc_now)
    archived_at: datetime | None = None
    archived_by: str | None = None
    archive_reason: str | None = None
    record_type: Literal["hypothesis_annotation"] = "hypothesis_annotation"


@dataclass(frozen=True, slots=True)
class HypothesisEvent:
    id: str
    hypothesis_id: str
    program_id: str
    event_type: Literal[
        "proposed",
        "confirmed",
        "rejected",
        "challenged",
        "invalidated",
        "stale",
        "superseded",
        "reinstated",
        "re_evaluated",
    ]
    from_status: HypothesisStatus | None
    to_status: HypothesisStatus
    occurred_at: datetime
    actor: str
    note: str | None = None
    evidence_refs: tuple[SourceRef, ...] = ()
    challenge_id: str | None = None


@dataclass(frozen=True, slots=True)
class MetricFreshnessEntry:
    metric_id: str
    last_observed_at: datetime | None
    quality_state: MetricQualityState
    hours_since_last_observation: float | None


@dataclass(frozen=True, slots=True)
class StaleHypothesisEntry:
    hypothesis_id: str
    confirmed_at: datetime
    days_since_confirmation: int
    last_observation_at: datetime | None
    staleness_reason: Literal["no_observations", "source_degraded", "review_due_passed"]


@dataclass(frozen=True, slots=True)
class RecoveredHypothesis:
    hypothesis_id: str
    short_id: str
    recovered_at: datetime
    days_challenged: int


@dataclass(frozen=True, slots=True)
class RealityBaseline:
    program_id: str
    as_of: datetime
    confirmed_hypothesis_ids: tuple[str, ...]
    challenged_hypothesis_ids: tuple[str, ...]
    stale_entries: tuple[StaleHypothesisEntry, ...]
    recovered_entries: tuple[RecoveredHypothesis, ...]
    freshness_summary: tuple[MetricFreshnessEntry, ...]
    digest_sha256: str
    policy_version: int


@dataclass(frozen=True, slots=True)
class RealityChallenge:
    id: str
    program_id: str
    hypothesis_id: str
    assertion_id: str | None
    observation_id: str | None
    challenge_kind: ChallengeKind
    observed_value: float | None
    expected_value: float | None
    delta_magnitude: float | None
    severity: ChallengeSeverity
    source: str
    detected_at: datetime
    note: str | None = None
    evidence_url: str | None = None
    ado_current_target: str | None = None
    snoozed_until: datetime | None = None
    snooze_reason: str | None = None
    current_state: ChallengeState = ChallengeState.OPEN
    state_changed_at: datetime | None = None
    state_actor: str | None = None
    state_reason: str | None = None
    last_event_at: datetime = field(default_factory=_utc_now)
    policy_version: int = 1
    composite_assertion_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssertionEvaluation:
    id: str
    program_id: str
    hypothesis_id: str
    assertion_id: str | None
    observation_id: str | None
    evaluated_at: datetime
    violated: bool
    value_num: float | None
    expected_value: float | None
    quality_state: MetricQualityState | None
    note: str | None = None
    composite_assertion_id: str | None = None


@dataclass(frozen=True, slots=True)
class SuppressionSummary:
    maintenance_window_id: str
    title: str
    suppressed_count: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class DigestDelta:
    since: datetime
    to: datetime
    challenges_opened: int
    challenges_resolved: int
    challenges_dismissed: int
    challenges_snoozed: int
    hypotheses_proposed: int
    hypotheses_confirmed: int
    hypotheses_recovered: int
    hypotheses_superseded: int


@dataclass(frozen=True, slots=True)
class RealityDigestModel:
    program_id: str
    as_of: datetime
    health: Literal["green", "amber", "red", "uninitialized"]
    confirmed_count: int
    challenged_count: int
    stale_count: int
    proposed_count: int
    recovered_count: int
    source_freshness: tuple[MetricFreshnessEntry, ...]
    open_challenges: tuple[RealityChallenge, ...]
    stale_entries: tuple[StaleHypothesisEntry, ...]
    recovered_entries: tuple[RecoveredHypothesis, ...]
    suppressed_during_maintenance: tuple[SuppressionSummary, ...]
    cache_built_at: datetime
    policy_version: int
    delta_since_last_digest: DigestDelta | None = None