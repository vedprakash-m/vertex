from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Literal, Self, cast


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EnumParserMixin:
    _default_member_name: ClassVar[str | None] = None

    @classmethod
    def from_string(cls, value: str | None) -> Self:
        normalized = value.strip().lower() if value is not None else ""
        for member in cls:
            if member.value.lower() == normalized:
                return cast(Self, member)
        if cls._default_member_name is not None:
            return cast(Self, getattr(cls, cls._default_member_name))
        raise ValueError(f"Unsupported {cls.__name__} value: {value!r}")


class RiskLevel(EnumParserMixin, str, Enum):
    _default_member_name = "UNKNOWN"

    BLOCKED = "blocked"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    DONE = "done"
    UNKNOWN = "unknown"


class DeltaKind(EnumParserMixin, str, Enum):
    NEW = "new"
    CLOSED = "closed"
    RISK_UP = "risk_up"
    RISK_DOWN = "risk_down"
    ETA_CHANGED = "eta_changed"
    OWNER_CHANGED = "owner_changed"
    UNCHANGED = "unchanged"


class Confidence(EnumParserMixin, str, Enum):
    _default_member_name = "NONE"

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class AttributionTier(EnumParserMixin, str, Enum):
    TIER1 = "tier1"
    TIER2 = "tier2"
    TIER3 = "tier3"


class ReviewState(EnumParserMixin, str, Enum):
    PENDING = "pending"
    SENT = "sent"
    APPROVED = "approved"
    SKIPPED_NO_DELTA = "skipped_no_delta"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class EditionType(EnumParserMixin, str, Enum):
    DETAILED = "detailed"
    FOCUSED = "focused"
    CONDENSED = "condensed"
    NARRATIVE = "narrative"
    DECK = "deck"
    LOOKBACK = "lookback"


@dataclass(frozen=True, slots=True)
class Revision:
    work_item_id: int
    rev_number: int
    changed_by: str
    changed_by_email: str
    changed_date: datetime
    fields_changed: dict[str, tuple[str | None, str | None]]


@dataclass(frozen=True, slots=True)
class Comment:
    work_item_id: int
    comment_id: int
    created_by: str
    created_by_email: str
    created_date: datetime
    text: str


@dataclass(frozen=True, slots=True)
class ChildWorkItem:
    id: int
    type: str
    title: str
    state: str
    assigned_to: str | None
    assigned_to_email: str | None
    area_path: str
    iteration_path: str
    target_date: date | None
    risk_level: RiskLevel
    tags: tuple[str, ...] = ()
    risk_assessment: str | None = None
    risk_assessment_comment: str | None = None


@dataclass(slots=True)
class WorkItem:
    id: int
    type: str
    title: str
    state: str
    assigned_to: str | None
    assigned_to_email: str | None
    area_path: str
    iteration_path: str
    target_date: date | None
    risk_level: RiskLevel
    tags: list[str]
    custom_fields: dict[str, object]
    revisions: list[Revision] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=_utc_now)
    risk_assessment: str | None = None
    risk_assessment_comment: str | None = None
    child_items: tuple[ChildWorkItem, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """All justification for a single claim."""

    work_item_id: int
    revisions: tuple[Revision, ...]
    comments: tuple[Comment, ...]
    enrichments: tuple[Enrichment, ...]
    confidence: Confidence
    tier: AttributionTier
    summary_for_reviewer: str


@dataclass(frozen=True, slots=True)
class Enrichment:
    source: Literal["mail", "transcript", "teams_chat", "calendar", "local_kb"]
    source_id: str
    author: str
    timestamp: datetime
    excerpt: str
    permalink: str | None
    body_text: str | None = None


@dataclass(frozen=True, slots=True)
class ScorecardEvidencePacket:
    """Per-dimension aggregate evidence built by the scorecard engine."""

    dimension_name: str
    dimension_description: str
    total_items: int
    items_by_risk: dict[str, int]
    stale_items: tuple[int, ...]
    stale_count: int
    overdue_items: tuple[int, ...]
    overdue_count: int
    blocked_items: tuple[int, ...]
    blocked_count: int
    unowned_items: tuple[int, ...]
    unowned_count: int
    high_activity_items: tuple[int, ...]
    prior_confirmed_risk: RiskLevel | None
    author_risk: RiskLevel | None
    ado_query_url: str
    item_links: tuple[str, ...]
    item_ids: tuple[int, ...] = ()
    derived_risk: RiskLevel = RiskLevel.UNKNOWN
    next_target_date: date | None = None
    latest_target_date: date | None = None
    author_target_date: date | None = None
    streak_count: int = 0
    is_stale_dimension: bool = False
    is_ghost_narrative: bool = False
    dfd_annotation: str = ""
    escalation_badge: str = ""


@dataclass(frozen=True, slots=True)
class ItemDelta:
    work_item_id: int
    kind: DeltaKind
    field_changes: dict[str, tuple[str | None, str | None]]
    old_risk: RiskLevel | None
    new_risk: RiskLevel | None
    old_eta: date | None
    new_eta: date | None
    evidence: EvidencePacket


@dataclass(frozen=True, slots=True)
class DeltaSet:
    issue_number: int
    previous_issue_number: int | None
    new_items: tuple[ItemDelta, ...]
    closed_items: tuple[ItemDelta, ...]
    risk_changes: tuple[ItemDelta, ...]
    eta_changes: tuple[ItemDelta, ...]
    unchanged_count: int
    owner_changes: tuple[ItemDelta, ...] = ()


@dataclass(frozen=True, slots=True)
class DimensionRisk:
    """Rendered scorecard dimension using `overrides.yaml` when set, otherwise the derived issue risk."""

    name: str
    risk: RiskLevel
    summary: str
    evidence: EvidencePacket
    display_name: str | None = None
    derived_risk: RiskLevel = RiskLevel.UNKNOWN
    override_risk: RiskLevel | None = None
    vector_label: str | None = None
    risk_sparkline: str | None = None
    trend_label: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ScorecardDelta:
    dimension: str
    old_risk: RiskLevel
    new_risk: RiskLevel
    delta_kind: DeltaKind
    summary: str


@dataclass(frozen=True, slots=True)
class FreshnessItem:
    work_item_id: int
    rule_id: str
    severity: Literal["block", "warn", "info"]
    message: str
    suggested_fix: str | None
    action_label: str | None = None
    action_message: str | None = None


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    issue_number: int
    items: tuple[FreshnessItem, ...]
    blocks: int
    warns: int
    infos: int

    @property
    def is_clean(self) -> bool:
        return self.blocks == 0


@dataclass(frozen=True, slots=True)
class DRISummary:
    dri_email: str
    dri_name: str
    open_count: int
    overdue_count: int
    stale_count: int
    items: tuple[FreshnessItem, ...]


@dataclass(frozen=True, slots=True)
class ReportData:
    issue_number: int
    edition: EditionType
    generated_at: datetime
    ado_data_as_of: datetime
    program: ProgramContext
    items: tuple[WorkItem, ...]
    deltas: DeltaSet
    scorecard: tuple[DimensionRisk, ...]
    scorecard_deltas: tuple[ScorecardDelta, ...]
    exec_summary_text: str
    workstream_blurbs: dict[str, str]
    freshness: FreshnessReport
    hygiene_warnings: tuple[str, ...]
    review_status: ReviewStatus
    manifest_id: str


@dataclass(frozen=True, slots=True)
class ReviewSection:
    section_id: str
    state: ReviewState
    reviewer: str | None
    note: str | None
    updated_at: datetime | None
    manifest_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewStatus:
    issue_number: int
    sections: tuple[ReviewSection, ...]

    @property
    def all_approved(self) -> bool:
        return all(section.state in {ReviewState.APPROVED, ReviewState.SKIPPED_NO_DELTA} for section in self.sections)


@dataclass(frozen=True, slots=True)
class NotifyPreview:
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    html_body: str
    md_body: str
    attachments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    sent_at: datetime
    channel: Literal["email", "teams"]
    to: tuple[str, ...]
    subject: str
    message_id: str | None
    success: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class NotifiedWorkItemState:
    work_item_id: int
    dri_email: str
    notified_at: datetime


@dataclass(frozen=True, slots=True)
class PriorNotificationState:
    notified_at: datetime
    items: tuple[NotifiedWorkItemState, ...]


@dataclass(frozen=True, slots=True)
class SnapshotItem:
    """Minimal work-item record for confirmed snapshots without transient evidence payloads."""

    id: int
    type: str
    title: str
    state: str
    assigned_to: str | None
    area_path: str
    target_date: date | None
    risk_level: RiskLevel
    tags: list[str]


@dataclass(frozen=True, slots=True)
class ConfirmedDimension:
    """Per-dimension record in a confirmed snapshot with author-confirmed risk."""

    scorecard_name: str
    name: str
    risk: RiskLevel
    prior_risk: RiskLevel | None
    item_count: int
    ado_query_url: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    issue_number: int
    generated_at: datetime
    ado_data_as_of: datetime
    edition_type: EditionType
    items: tuple[SnapshotItem, ...]
    scorecards: tuple[ConfirmedDimension, ...]
    schema_version: str = "1.0"


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    issue_number: int
    generated_at: datetime
    kind: Literal["confirmed", "skipped"] = "confirmed"
    eml_path: str | None = None
    html_path: str | None = None
    md_path: str | None = None
    snapshot_path: str | None = None
    manifest_path: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArchiveIndex:
    edition: str
    issues: tuple[ArchiveEntry, ...]


@dataclass(frozen=True, slots=True)
class RunManifest:
    manifest_id: str
    issue_number: int
    edition: str
    started_at: datetime
    ended_at: datetime
    config_hash: str
    snapshot_hash: str
    html_hash: str
    md_hash: str
    ado_calls: int
    ai_calls: int
    ai_cost_usd: float
    freshness_summary: dict[str, int]
    qg_results: dict[str, bool]
    git_sha: str | None
    ai_cost_by_model: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Workstream:
    name: str
    aliases: tuple[str, ...]
    area_paths: tuple[str, ...]
    dri_email: str
    description: str


@dataclass(frozen=True, slots=True)
class PersonProfile:
    email: str
    display_name: str
    role: str
    workstreams: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgramContext:
    program_name: str
    mission: str
    pillars: tuple[str, ...]
    workstreams: tuple[Workstream, ...]
    glossary: dict[str, str]
    people: tuple[PersonProfile, ...]


@dataclass(frozen=True, slots=True)
class LearningEntry:
    created_at: datetime
    rule_kind: Literal[
        "banned_phrase",
        "preferred_phrase",
        "include_pattern",
        "exclude_pattern",
    ]
    pattern: str
    rationale: str
    source_issue: int
