from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

from src.core.models import WorkItem
from src.core.models_v2 import Signal, TrajectoryPoint


class RegistrationStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    EXPIRED = "expired"
    RETIRED = "retired"
    SUPPRESSED = "suppressed"


class DiscoveryCompleteness(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    PARTIAL = "partial"


class HydrationMode(str, Enum):
    FULL = "full"
    FRESHNESS_ONLY = "freshness_only"


class RelationKind(str, Enum):
    """Typed ADO work-item relation edge category (ADF-F04 / Section 8.4.3).

    Mirrors the ADO ``System.LinkTypes.*`` family plus artifact/external links,
    reduced to the Section 8.4.3 edge kinds. ``UNKNOWN`` preserves unrecognized
    relation type names rather than dropping them (honest degradation).
    """

    HIERARCHY_PARENT = "hierarchy_parent"
    HIERARCHY_CHILD = "hierarchy_child"
    RELATED = "related"
    PREDECESSOR = "predecessor"
    SUCCESSOR = "successor"
    ARTIFACT_LINK = "artifact_link"
    EXTERNAL_LINK = "external_link"
    UNKNOWN = "unknown"


class RelationTargetKind(str, Enum):
    """What the far end of a relation points at (Section 8.4.3 typed edges)."""

    WORK_ITEM = "work_item"
    ARTIFACT = "artifact"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class WorkItemRelation:
    """One typed directed edge between a source work item and a target (ADF-W4.1).

    Direction is explicit (``forward`` = source -> target as ADO reports it;
    ``reverse`` = the edge was expressed from the target's perspective and
    flipped at parse time). ``depth`` is populated only by budgeted traversal,
    not by the raw parse.
    """

    source_work_item_id: int
    relation_kind: RelationKind
    target_kind: RelationTargetKind
    target_id: str
    target_type: str | None
    target_title: str | None
    direction: str  # "forward" | "reverse"
    rel_type_name: str  # raw ADO ``rel`` attribute, preserved for fidelity
    depth: int = 1


class ScopeStatusKind(str, Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TagExpression:
    all_of: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    channel: str
    discovery_modes: tuple[DiscoveryCompleteness, ...]
    hydration_modes: tuple[HydrationMode, ...]
    supports_since: bool
    max_batch_size: int
    rate_limit_rpm: int | None
    retry_max_attempts: int
    retry_backoff_seconds: float
    privacy_class: str
    timeout_seconds: int
    write_propose: bool = False
    write_apply: bool = False
    supports_comments: bool = False
    supports_attachments: bool = False
    supports_replay: bool = False
    supports_degradation: bool = False
    # ADF-F03 / Section 8.3.4 additive UIL capability descriptors. All default
    # safely off so existing producers/consumers compile unchanged. These extend
    # the existing capability contract rather than introducing a parallel
    # ``SourceConnector`` abstraction (audit reconciliation row).
    supports_pagination: bool = False
    supports_relations: bool = False
    supports_full_content: bool = False
    supports_durable_identity: bool = False
    supports_cancellation: bool = False
    max_page_size: int | None = None
    completeness_modes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunContext:
    dry_run: bool = False
    force_discovery: bool = False
    accept_shrinkage: bool = False


@dataclass(frozen=True, slots=True)
class PaginationOutcome:
    """ADF-W2.1 (Section 8.4.2): result of a multi-page provider fetch.

    ``is_truncated`` is True only when the fetch stopped because it hit its
    own safety cap (``max_pages``) while the provider still had more data
    to give (the last page returned a full page, or a continuation token
    was still present) -- never for a fetch that ended because the provider
    signaled "no more data". Reaching a cap must be a surfaced, actionable
    finding, not just a log line (Section 8.4.2's explicit requirement).
    """

    total_fetched: int
    page_count: int
    is_truncated: bool


@dataclass(frozen=True, slots=True)
class IntegrationError:
    source: str
    stage: str
    message: str
    retryable: bool
    operator_action: str | None = None
    ref_id: str | None = None
    ref_kind: str | None = None
    severity: str = "error"


@dataclass(frozen=True, slots=True)
class ChannelRegistration:
    channel: str
    program_id: str
    provider_instance_id: str
    ref_id: str
    ref_kind: str
    status: RegistrationStatus
    first_discovered_at: datetime
    last_seen_at: datetime
    last_verified_at: datetime | None = None
    retired_at: datetime | None = None
    consecutive_hydration_failures: int = 0
    confidence: float = 1.0
    confidence_source: str = "manual_config"
    pm_confirmed: bool = False
    promoted: bool = False
    signal_yield_last_3: tuple[int, int, int] = (0, 0, 0)
    ref_title: str | None = None
    metadata: dict[str, str | int | float | bool | None] | None = None
    workstream_ids: tuple[str, ...] = ()
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistrationBinding:
    workstream_id: str | None
    scope_id: str
    source_type: str
    confidence: float
    confidence_source: str
    pm_confirmed: bool = False
    promoted: bool = False
    status: RegistrationStatus = RegistrationStatus.ACTIVE
    signal_yield_last_3: tuple[int, int, int] = (0, 0, 0)


@dataclass(frozen=True, slots=True)
class DiscoveredRef:
    registration: ChannelRegistration
    bindings: tuple[RegistrationBinding, ...]


@dataclass(frozen=True, slots=True)
class ScopeStatus:
    scope_id: str
    status: ScopeStatusKind
    completeness: DiscoveryCompleteness
    item_count: int
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeState:
    scope_id: str
    cursor_value: str | None = None
    watermark_at: datetime | None = None
    last_success_at: datetime | None = None
    tombstone_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    channel: str
    program_id: str
    discovered_refs: tuple[DiscoveredRef, ...]
    completeness: DiscoveryCompleteness
    scope_statuses: dict[str, ScopeStatus]
    scope_state_updates: dict[str, ScopeState]
    errors: tuple[IntegrationError, ...]
    computed_at: datetime
    provider_instance_id: str = "default"


@dataclass(frozen=True, slots=True)
class RegistryDelta:
    channel: str
    program_id: str
    computed_at: datetime
    previous_discovery_at: datetime | None
    completeness: DiscoveryCompleteness
    added: tuple[ChannelRegistration, ...]
    removed: tuple[ChannelRegistration, ...]
    updated: tuple[ChannelRegistration, ...]
    unchanged_count: int
    failed_scopes: dict[str, ScopeStatus]
    shrinkage_pct: float

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed and not self.updated

    @property
    def summary(self) -> str:
        return f"+{len(self.added)} -{len(self.removed)} ~{len(self.updated)} ={self.unchanged_count}"

    def is_shrinkage_guarded(self, *, threshold_pct: float = 0.30, floor: int = 5) -> bool:
        return self.shrinkage_pct >= threshold_pct and len(self.removed) >= floor


ResourceT = TypeVar("ResourceT")


@dataclass(frozen=True, slots=True)
class HydrationResult(Generic[ResourceT]):
    channel: str
    resources: ResourceT
    api_call_count: int
    errors: tuple[IntegrationError, ...]
    hydrated_ref_ids: tuple[tuple[str, str], ...]
    failed_ref_ids: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    channel: str
    signals: tuple[Signal, ...]
    trajectory_points: tuple[TrajectoryPoint, ...]
    side_artifacts: dict[str, str | int | float | bool | None]
    errors: tuple[IntegrationError, ...]


@dataclass(frozen=True, slots=True)
class SidecarResult:
    name: str
    signals: tuple[Signal, ...]
    errors: tuple[IntegrationError, ...]
    side_artifacts: dict[str, str | int | float | bool | None]


from src.core.ado_pr_client import PullRequestSummary


@dataclass(frozen=True, slots=True)
class ADOHydrationOutput:
    work_items: tuple[WorkItem, ...]
    freshness_items: tuple[WorkItem, ...] | None = None
    pull_requests: tuple[PullRequestSummary, ...] = ()
    # ADF-W4.1 (Section 8.4.3): typed directed edges + any traversal truncation.
    relations: tuple[WorkItemRelation, ...] = ()
    relation_truncation: PaginationOutcome | None = None


@dataclass(frozen=True, slots=True)
class MeetingEvent:
    event_id: str
    series_id: str | None
    thread_id: str | None
    title: str | None
    started_at: datetime
    ended_at: datetime | None
    organizer: str | None = None
    summary: str | None = None
    workstream_ids: tuple[str, ...] = ()
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    message_id: str
    thread_id: str
    sender: str | None
    sent_at: datetime
    text: str
    permalink: str | None = None
    workstream_ids: tuple[str, ...] = ()
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EmailMessage:
    message_id: str
    thread_id: str
    subject: str | None
    sent_at: datetime
    preview: str
    sender: str | None = None
    recipients: tuple[str, ...] = ()
    permalink: str | None = None
    workstream_ids: tuple[str, ...] = ()
    work_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class IncidentState:
    incident_id: str
    severity: int | None
    status: str
    owning_team: str | None
    title: str | None
    updated_at: datetime
    workstream_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TeamsHydrationOutput:
    meeting_events: tuple[MeetingEvent, ...] = ()
    thread_messages: tuple[ThreadMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class EmailHydrationOutput:
    messages: tuple[EmailMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class KustoResultSet:
    query_id: str
    rows: tuple[dict[str, str | int | float | bool | None], ...]
    observed_at: datetime
    workstream_ids: tuple[str, ...] = ()
    # ADF-F05 / Appendix A.10 additive metric-semantics fields. All defaulted so
    # existing producers/consumers compile unchanged; raw rows remain available
    # for entity extraction/charts. No parallel result type is authorized.
    metric_id: str | None = None
    result_column: str | None = None
    unit: str | None = None
    slo_target: float | None = None
    comparison: str | None = None  # one of >=, <=, ==, >, <
    observed_value: float | None = None
    is_breach: bool | None = None
    is_partial: bool = False
    row_count: int | None = None


@dataclass(frozen=True, slots=True)
class KustoHydrationOutput:
    result_sets: tuple[KustoResultSet, ...] = ()


@dataclass(frozen=True, slots=True)
class IcMHydrationOutput:
    incident_states: tuple[IncidentState, ...] = ()


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    channel: str
    enabled: bool
    discovery_threshold_hours: int
    ttl_days: int | None
    extra: dict[str, str | int | float | bool | None | list[str | int | float | bool | None]] | None = None


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    config: ChannelConfig
    discovery_provider: object
    hydration_provider: object
    signal_extractor: object
    discovery_config: object
    hydration_config: object


@dataclass(frozen=True, slots=True)
class RegistryFeedbackEvent:
    channel: str
    program_id: str
    ref_id: str
    ref_kind: str
    action: str
    pm_alias: str
    created_at: datetime
    provider_instance_id: str = "default"
    reason: str | None = None
    workstream_id: str | None = None
    prior_workstream_id: str | None = None
    series_id: str | None = None
    thread_id: str | None = None
    new_artifact_id: str | None = None
    detail_json: str | None = None
