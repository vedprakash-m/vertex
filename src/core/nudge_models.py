"""Nudge engine data models — config, candidate, rendering, and audit value objects.

All models are immutable. Registry types (WorkstreamStakeholder, WorkstreamRegistryEntry)
remain in workstream_registry.py and are imported here for convenience where needed.

Schema versions:
  nudge_state: "1.2" (current dict per-item with triggered_at/origin/run_id)
  nudge_audit: "1.0" (current); "1.1" adds comment_fetch totals + schema_version payload
  publication_index: "1.1" (current); adds content_hash + audience manifest
"""
from __future__ import annotations

import dataclasses
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal, Union


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUDGE_STATE_SCHEMA_VERSION = "1.2"
NUDGE_STATE_SCHEMA_VERSION_V12 = "1.2"
NUDGE_AUDIT_SCHEMA_VERSION = "1.0"
NUDGE_AUDIT_SCHEMA_VERSION_V11 = "1.1"
NUDGE_BATCH_SIZE = 200
NUDGE_COMMENT_FETCH_LIMIT_DEFAULT = 100
NUDGE_CANDIDATE_WORKERS_DEFAULT = 2
NUDGE_CANDIDATE_WORKERS_MAX = 3
NUDGE_TITLE_CACHE_MAX_ENTRIES = 1_000
NUDGE_AUDIT_MAX_BYTES = 10 * 1024 * 1024
NUDGE_STATE_LOCK_TIMEOUT_SECONDS = 5
NUDGE_DRAFT_RETAIN = 20
NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION = "1.1"
NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION_V11 = "1.1"

# Phase 4 numeric budgets (§6.5) — tune after baseline
NUDGE_MAX_COMMENT_CALLS = 250        # total direct calls per run
NUDGE_COMMENT_CONCURRENCY = 8        # max parallel calls
NUDGE_COMMENT_WALL_CLOCK_SECONDS = 180   # degrade after this
NUDGE_COMMENT_RETRY_MAX = 3
NUDGE_COMMENT_UNKNOWN_MAX_PCT = 10.0     # unknown-comment % threshold
NUDGE_COMMENT_ERROR_MAX_PCT = 5.0        # error % threshold

# Fact type names for event.nudge.* (§6.7)
NUDGE_FACT_GENERATED = "event.nudge.generated"
NUDGE_FACT_SENT_ATTESTED = "event.nudge.sent_attested"
NUDGE_FACT_EVALUATED = "event.nudge.evaluated"
NUDGE_FACT_WAIVER_CREATED = "event.nudge.waiver_created"


# ---------------------------------------------------------------------------
# Section configuration models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NudgeSectionCriteria:
    source: Literal["registry", "tag", "area_path"]
    tags: tuple[str, ...] = ()
    area_path_filter: tuple[str, ...] = ()
    required_tags: tuple[str, ...] = ()
    legacy_scope_override: bool = False


@dataclass(frozen=True, slots=True)
class WorkstreamHint:
    """Maps a set of ADO item IDs to a workstream for tag-sourced sections."""
    workstream_id: str
    ado_item_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class NudgeSectionSpec:
    id: str
    title: str
    criteria: NudgeSectionCriteria
    stale_business_days: int
    letter: str
    cooldown_days: int | None = None
    description: str = ""
    deadline: date | None = None
    stale_summary_threshold_days: int = 7
    # Phase 1/2 additive fields — all optional with safe defaults
    deadline_milestone_id: str | None = None
    requires_milestone: bool = False
    required: bool = False
    retire_when_milestone_done: str | None = None
    nudge_participating_lanes: tuple[str, ...] = ()
    # Workstream classification hints for tag/area_path sourced sections.
    # Maps item IDs to their workstream so they appear grouped with owners.
    workstream_hints: tuple[WorkstreamHint, ...] = ()
    # Whether this section's items count toward the leadership rollup scorecard.
    # Default True; set False for sections (e.g. post-ramp/future-scoped work)
    # that shouldn't drive leader accountability yet.
    include_in_leadership_rollup: bool = True


# ---------------------------------------------------------------------------
# Governed waivers (Phase 2, §6.8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NudgeWaiver:
    """Operator-authored suppression for a specific item+owner pair."""
    work_item_id: int
    owner_alias: str
    reason: str
    created: date
    expires: date

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc).date() >= self.expires


# ---------------------------------------------------------------------------
# Action-due policy (Phase 1/2, §8.3 / D-2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExplicitActionDue:
    """Operator-authored explicit action-due date."""
    mode: Literal["explicit"] = field(default="explicit", init=False)
    date: date = field(default_factory=lambda: date.today())


@dataclass(frozen=True, slots=True)
class SendDateOffsetActionDue:
    """N business days before planned_send_at (default = generation date)."""
    mode: Literal["send_date_offset"] = field(default="send_date_offset", init=False)
    business_days: int = 3


@dataclass(frozen=True, slots=True)
class MilestoneRelativeActionDue:
    """N business days before a milestone target (Phase 3 only)."""
    mode: Literal["milestone_relative"] = field(default="milestone_relative", init=False)
    milestone_id: str = ""
    business_days_before: int = 3


ActionDuePolicy = Union[ExplicitActionDue, SendDateOffsetActionDue, MilestoneRelativeActionDue]


# ---------------------------------------------------------------------------
# Assessed deadline + resolved section (Phase 2, §8.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssessedDeadline:
    """Deadline with provenance and truth metadata."""
    date: date | None
    milestone_id: str | None
    truth_level: str | None          # TruthLevel value (str to avoid import cycle)
    disputed: bool
    stale: bool
    provisional_inputs: bool
    evidence: tuple[str, ...]
    authority: Literal["assessed", "operator_override", "none"]
    resolution_status: Literal["explicit", "resolved", "unconfirmed", "unavailable", "none"]


@dataclass(frozen=True, slots=True)
class ResolvedNudgeSection:
    """NudgeSectionSpec after milestone/deadline resolution."""
    spec: NudgeSectionSpec
    action_due_at: date | None
    target_date_ceiling: AssessedDeadline
    is_retired: bool = False


# ---------------------------------------------------------------------------
# Audience policy + manifest (Phase 2, §8.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NudgeAudiencePolicy:
    """Per-edition audience governance rules."""
    allowed_domains: tuple[str, ...] = ("microsoft.com",)
    max_recipients: int = 200
    opt_out: frozenset[str] = field(default_factory=frozenset)
    opt_out_fallback: Literal["escalate", "gap"] = "escalate"
    new_recipient_approval: bool = True
    unresolved_owner: Literal["drop", "fail"] = "drop"
    delivery_mode: Literal["to", "bcc"] = "to"


@dataclass(frozen=True, slots=True)
class NudgeAudienceManifest:
    """Per-run audience record (immutable, keyed to content_hash)."""
    to_aliases: tuple[str, ...]
    cc_aliases: tuple[str, ...]
    bcc_aliases: tuple[str, ...]
    added_since_last_attested: tuple[str, ...]
    removed_since_last_attested: tuple[str, ...]


# ---------------------------------------------------------------------------
# Lifecycle / outcome models (Phase 3, §8.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NudgePublishedItem:
    """Item snapshot included in a publication record."""
    work_item_id: int
    owner_alias: str | None
    ado_revision_baseline: int | None
    ado_target_date_baseline: str | None


@dataclass(frozen=True, slots=True)
class NudgePublicationRecord:
    """Immutable record of one attested (or reconstructed) send event. Natural key: run_id."""
    run_id: str
    program_id: str
    content_hash: str
    attested_at: datetime
    claimed_send_at: datetime
    actor: str
    origin: Literal["attested", "reconstructed"]
    items: tuple[NudgePublishedItem, ...]
    audience: NudgeAudienceManifest
    schema_version: str = NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION_V11


ClassificationT = Literal["updated", "unchanged", "closed", "regressed", "not_evaluated", "degraded"]


@dataclass(frozen=True, slots=True)
class NudgeEvaluationObservation:
    """Immutable evaluation record. Natural key: run_id + work_item_id + evaluated_at."""
    run_id: str
    work_item_id: int
    evaluated_at: datetime
    ado_revision_current: int | None
    ado_target_date_current: str | None
    classification: ClassificationT
    degraded_reason: str | None
    evidence_ref: str | None
    confidence: float | None
    schema_version: str = NUDGE_AUDIT_SCHEMA_VERSION_V11


# ---------------------------------------------------------------------------
# Grouped configuration models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NudgeDeliveryConfig:
    recipient: str
    delivery_mode: Literal["broadcast", "per_workstream"]
    cadence_days: int
    include_workstream_owners: bool = True
    include_item_assignees: bool = True
    send_day: str = ""               # day of week: "monday", "friday", etc.
    # Stakeholder roles considered "workstream owner" for display and email expansion.
    # Matches against the `role` field in workstream_registry stakeholders.
    owner_roles: tuple[str, ...] = ("tpm_lead", "eng_lead")
    # When True: To = resolved `leadership_rollup` stakeholders (the publisher/author
    # is intentionally excluded); Cc = workstream owners (owner_roles) + item assignees.
    # When False (default), all resolved recipients (owners + assignees) go into a
    # single To list as before.
    to_leadership_rollup: bool = False
    # Standing Cc addresses always added on top of the resolved workstream owners/
    # item assignees when to_leadership_rollup=True (e.g. leadership stakeholders
    # not derivable from ADO/workstream data).
    additional_cc: tuple[str, ...] = ()
    #: specs/people.md §7.4/PPL-W5a.6: opt-in names into a program's
    #: audience_scopes.yaml (src/core/audience_scopes.py). Empty by
    #: default -- an edition with no opt-in resolves zero extra
    #: recipients, matching every existing nudge config's current
    #: (unaffected) behavior.
    audience_scope_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NudgeEvaluationConfig:
    comment_window_days: int
    status_keywords: tuple[str, ...]
    risk_on_track_values: tuple[str, ...]
    cooldown_days: int
    nudge_exempt_item_ids: frozenset[int]
    comment_fetch_limit: int = NUDGE_COMMENT_FETCH_LIMIT_DEFAULT
    nudge_waivers: tuple[NudgeWaiver, ...] = ()   # Phase 2 waivers (§6.8)
    action_due_policy: ActionDuePolicy | None = None  # Phase 1/2; None = no action-due prefix


@dataclass(frozen=True, slots=True)
class NudgePresentationConfig:
    brand_label: str
    email_subject_label: str
    template: str
    preheader: str
    compress_titles_with_ai: bool
    # Phase 1 additions
    context_subject_prefix: bool = False
    context_subject_prefix_template: str = "[Action DUE {due} EOD]"
    context_subject_overdue_template: str = ""  # empty = no OVERDUE prefix (demotivating); falls through to upcoming check
    context_subject_lookahead_days: int = 14
    audience_policy: NudgeAudiencePolicy | None = None  # Phase 2


@dataclass(frozen=True, slots=True)
class NudgeConfig:
    sections: tuple[NudgeSectionSpec, ...]
    delivery: NudgeDeliveryConfig
    evaluation: NudgeEvaluationConfig
    presentation: NudgePresentationConfig


# ---------------------------------------------------------------------------
# Audit models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NudgeAuditSection:
    section_id: str
    letter: str
    candidate_count: int
    candidate_item_ids: tuple[int, ...]
    item_ids: tuple[int, ...]
    item_count: int
    staleness_filtered: int
    exempt_filtered: int
    comment_fetch_skipped: int
    comment_fetch_errors: int
    query_error: bool
    error_details: str | None = None
    cross_section_dedup_filtered: int = 0


@dataclass(frozen=True, slots=True)
class NudgeAuditEvent:
    event_type: Literal[
        "nudge_generated", "dry_run", "no_items", "config_error", "auth_error",
        "query_error", "artifact_error", "state_error", "cooldown_reset",
        "nudge_marked_sent", "nudge_draft_approved", "nudge_imported_sent",
    ]
    schema_version: str
    run_id: str
    program_id: str
    triggered_at: datetime
    dry_run: bool
    sections: tuple[NudgeAuditSection, ...]
    total_items: int
    total_staleness_filtered: int
    total_exempt_filtered: int
    recipient: str | None
    optional_recipient_failures: tuple[str, ...]
    degraded_section_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    eml_paths: tuple[str, ...]
    error: str | None = None
    error_details: str | None = None
    # Phase 4 telemetry (§6.5) — always present, 0 when not applicable
    comment_fetch_skipped_total: int = 0
    comment_fetch_errors_total: int = 0
    total_waiver_filtered: int = 0   # Phase 2


# ---------------------------------------------------------------------------
# Transport / rendering models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedRecipient:
    alias: str
    email: str
    display_name: str


@dataclass(frozen=True, slots=True)
class NudgeCandidate:
    item: object  # WorkItem — avoid circular import; validated at call site
    workstream_id: str | None


@dataclass(frozen=True, slots=True)
class NudgeSectionFetchResult:
    section_id: str
    candidates: tuple[NudgeCandidate, ...]
    query_error: bool = False
    error_details: str | None = None
    # Registry-source only: count of scope-matching (required_tags) items per
    # workstream_id that were excluded from `candidates` solely because they are
    # in a terminal/closed state. Lets the report distinguish "no open items
    # because everything shipped" from "no items tracked at all" (a hygiene gap).
    closed_counts_by_workstream: tuple[tuple[str | None, int], ...] = ()


@dataclass(frozen=True, slots=True)
class FullHygieneRow:
    work_item_id: int
    title: str
    title_original: str
    item_url: str
    item_type: str
    owner_alias: str | None
    owner_email: str | None
    workstream_id: str | None
    workstream_name: str | None
    has_valid_target_date: bool
    has_committed: bool
    has_risk_assessment: bool
    risk_is_on_track: bool
    has_risk_reason: bool | None
    has_recent_comment: bool | None
    comment_has_status_keyword: bool | None
    is_ready: bool | None
    stale_business_days: int
    is_overdue: bool
    target_date: date | None


@dataclass(frozen=True, slots=True)
class FullHygieneWorkstreamGroup:
    workstream_id: str | None
    workstream_name: str
    workstream_owners: tuple[ResolvedRecipient, ...]
    rows: tuple[FullHygieneRow, ...]
    leadership_rollup: ResolvedRecipient | None = None
    # Registry-source only: scope-matching items excluded because they're closed/
    # terminal. Distinguishes "0 open, N closed" (fully delivered) from "0 open,
    # 0 closed" (nothing tracked at all — a hygiene gap, not a clean pass).
    closed_count: int = 0


@dataclass(frozen=True, slots=True)
class FullHygieneLeaderPortfolio:
    """One accountable leader's workstream groups within a section body.

    Buckets FullHygieneWorkstreamGroup entries that share the same
    leadership_rollup leader so the report can render active work in full
    detail while collapsing a leader's fully-quiet portfolio (or the quiet
    remainder of a partially-active one) into a single compact line instead
    of one alarm-style banner per empty workstream. Groups with no
    leadership_rollup leader (e.g. the "Unclassified" catch-all) each get
    their own single-group portfolio with ``leader=None``, unchanged from
    today's per-group rendering.
    """
    leader: ResolvedRecipient | None
    active_groups: tuple[FullHygieneWorkstreamGroup, ...]
    empty_groups: tuple[FullHygieneWorkstreamGroup, ...]

    @property
    def is_fully_empty(self) -> bool:
        return not self.active_groups


@dataclass(frozen=True, slots=True)
class FullHygieneSection:
    section_id: str
    letter: str
    title: str
    description: str
    stale_threshold_days: int
    stale_summary_threshold_days: int
    deadline: date | None
    groups: tuple[FullHygieneWorkstreamGroup, ...]
    total_count: int
    stale_count: int
    ready_count: int
    unknown_ready_count: int
    no_date_count: int
    past_due_count: int
    beyond_deadline_count: int
    stale_summary_count: int
    comment_fetch_skipped: int
    comment_fetch_errors: int
    query_error: bool = False
    error_details: str | None = None
    deadline_uncertain: bool = False
    elevated_from_other_pools: int = 0
    include_in_leadership_rollup: bool = True
    # Program-configured required tags (NudgeSectionCriteria.required_tags) for
    # this section, if any. Drives scope-verification copy in the template so
    # wording never hardcodes one program's tag vocabulary (e.g. Armada's
    # "ArmadaM1") for programs that don't use required-tag scoping at all.
    scope_tags: tuple[str, ...] = ()
    # Program-configured selection tags (NudgeSectionCriteria.tags) for
    # source=tag sections -- the ADO tag(s) that already define this section's
    # membership. Distinct from scope_tags above (which is only for
    # source=registry sections and disallowed for source=tag per config
    # validation). Lets the template ask readers to keep tagging accurate for
    # whichever tag actually drives this section, per program.
    selection_tags: tuple[str, ...] = ()
    # groups bucketed by leadership_rollup leader, for body rendering. Derived
    # from `groups` via aggregate_groups_by_leader() below — lets a leader
    # whose whole portfolio has zero open items collapse to one line instead
    # of one banner per empty workstream.
    leader_portfolios: tuple[FullHygieneLeaderPortfolio, ...] = ()


def aggregate_groups_by_leader(
    groups: tuple[FullHygieneWorkstreamGroup, ...],
) -> tuple[FullHygieneLeaderPortfolio, ...]:
    """Bucket workstream groups by their leadership_rollup leader.

    Groups without a leadership_rollup leader (e.g. the "Unclassified"
    catch-all) each become their own single-group portfolio, unchanged from
    today's per-group rendering. Groups that share a leader are bucketed
    together so a leader whose entire portfolio has zero open items can
    collapse to one compact line instead of one alarm banner per empty
    workstream.
    """
    order: list[str] = []
    buckets: dict[str, list[FullHygieneWorkstreamGroup]] = defaultdict(list)
    leader_by_key: dict[str, ResolvedRecipient | None] = {}
    no_leader_counter = 0

    for group in groups:
        leader = group.leadership_rollup
        if leader is not None and leader.email:
            key = f"leader:{leader.email.strip().lower()}"
        else:
            no_leader_counter += 1
            key = f"none:{no_leader_counter}"
        if key not in leader_by_key:
            order.append(key)
            leader_by_key[key] = leader
        buckets[key].append(group)

    portfolios: list[FullHygieneLeaderPortfolio] = []
    for key in order:
        bucket = buckets[key]
        active = tuple(g for g in bucket if g.rows)
        empty = tuple(g for g in bucket if not g.rows)
        portfolios.append(FullHygieneLeaderPortfolio(
            leader=leader_by_key[key],
            active_groups=active,
            empty_groups=empty,
        ))
    return tuple(portfolios)


@dataclass(frozen=True, slots=True)
class FullHygieneArtifacts:
    run_id: str
    sections: tuple[FullHygieneSection, ...]
    recipient: ResolvedRecipient
    to_recipients: tuple[ResolvedRecipient, ...]
    generated_at: datetime
    eml_paths: tuple[object, ...]  # tuple[Path, ...]
    using_snapshot_fallback: bool
    ai_titles_compressed: int
    degraded_section_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LeadershipRollupRow:
    """Per-leader ADO hygiene compliance rollup for accountability reporting."""

    leader_alias: str
    leader_email: str | None
    leader_display_name: str
    workstream_names: tuple[str, ...]
    total_count: int
    compliant_count: int
    percent_compliant: int
    closed_count: int = 0


# ---------------------------------------------------------------------------
# Fleet summary model (used in fleet.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FleetNudgeSummary:
    config_status: Literal["ok", "warn", "fail"]
    last_generated_at: datetime | None
    section_count: int
    cooldown_item_count: int
    degraded_section_count: int
    core_gate_status: Literal["ok", "warn", "fail"]


# ---------------------------------------------------------------------------
# Audit serialization helpers
# ---------------------------------------------------------------------------


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Unsupported audit value: {type(value).__name__}")


def build_audit_line(event: NudgeAuditEvent) -> str:
    import json  # noqa: PLC0415
    return json.dumps(
        dataclasses.asdict(event),
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def make_run_id(now_utc: datetime) -> str:
    return f"nudge_{now_utc.strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
