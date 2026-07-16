from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.core.models import WorkItem
from src.core.models_v2 import ActionItem, IntegrationError, Program, Signal, Workstream


AIActionExtractor = Callable[[Program, tuple[Signal, ...]], tuple[ActionItem, ...]]
BackgroundSynthesisRunner = Callable[[str, str, Path, datetime], bool]
M365StageCallback = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class BackgroundSynthesisTrigger:
    workstream_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class M365PromotionCandidate:
    artifact_id: str
    display_name: str | None
    workstream_id: str
    confidence: float
    signal_yield_last_3: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class M365PromotionBlockedArtifact:
    artifact_id: str
    artifact_type: str
    display_name: str | None
    workstream_id: str
    blocker_reason: str


@dataclass(frozen=True, slots=True)
class GatherArtifacts:
    program_id: str
    scanned_items: int
    discovered_signals: int
    new_signals: int
    pending_review: int
    trajectory_updates: int
    auto_reviews_written: int
    ado_calls: int
    archived_journal_files: int = 0
    background_proposals: int = 0
    dependency_proposals_refreshed: int = 0
    integration_errors: tuple[IntegrationError, ...] = ()
    promotion_candidates: tuple[M365PromotionCandidate, ...] = ()
    promotion_blocked_artifacts: tuple[M365PromotionBlockedArtifact, ...] = ()
    chart_results: tuple[Any, ...] = ()

    @property
    def integration_error_count(self) -> int:
        return len(self.integration_errors)


@dataclass(frozen=True, slots=True)
class PersistenceStageInput:
    program: Program
    program_id: str
    workstreams: tuple[Workstream, ...]
    candidate_signals: tuple[Signal, ...]
    existing_signals: tuple[Signal, ...]
    signal_store: Any
    current_time: datetime
    programs_root: Path
    ai_action_extractor: AIActionExtractor | None
    dry_run: bool = False
    # ADF-W2.12: the shared per-gather-cycle correlation id (mirrors report.py's
    # own StageContext threading). "" means no correlation identity was
    # threaded -- every fact-write's trace-link call degrades to a no-op.
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class PersistenceStageResult:
    new_signals: tuple[Signal, ...]
    pending_review: int
    auto_reviews_written: int
    extracted_action_count: int


@dataclass(frozen=True, slots=True)
class ProjectionStageInput:
    program: Program
    program_id: str
    workstreams: tuple[Workstream, ...]
    items: tuple[WorkItem, ...]
    signal_store: Any
    trajectory_store: Any
    as_of: datetime
    programs_root: Path
    include_dependency_scout: bool
    background_synthesis_runner: BackgroundSynthesisRunner | None
    resolve_workstream_id: Callable[[str, tuple[Workstream, ...]], str | None]
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class ProjectionStageResult:
    trajectory_updates: int
    dependency_proposals_refreshed: int
    background_proposals: int
    trajectory_detail: str
    dependency_detail: str | None
    synthesis_detail: str | None
    trajectory_elapsed_seconds: float
    dependency_elapsed_seconds: float | None
    synthesis_elapsed_seconds: float | None


@dataclass(frozen=True, slots=True)
class M365DiscoveryStageInput:
    program: Program
    program_id: str
    workstreams: tuple[Workstream, ...]
    items: tuple[WorkItem, ...]
    workiq_signals: tuple[Signal, ...]
    gather_flags: dict[str, bool]
    integration_error_details: tuple[IntegrationError, ...]
    as_of: datetime
    previous_entry: dict[str, Any] | None
    programs_root: Path
    count_transcript_series_state: M365StageCallback
    count_chat_thread_state: M365StageCallback
    tracked_m365_artifact_ids: M365StageCallback
    observed_m365_thread_ids: M365StageCallback
    load_discovery_milestones: M365StageCallback
    build_workiq_query_plans: M365StageCallback
    build_m365_discovery_queries: M365StageCallback
    build_seeded_source_discovery_state: M365StageCallback
    build_adaptive_workiq_state: M365StageCallback


@dataclass(frozen=True, slots=True)
class M365DiscoveryStageResult:
    m365_discovery_state: dict[str, Any]
    promotion_candidates: tuple[M365PromotionCandidate, ...]
    promotion_blocked_artifacts: tuple[M365PromotionBlockedArtifact, ...]


@dataclass(frozen=True, slots=True)
class StateWriteStageInput:
    program_id: str
    gathered_at: datetime
    scanned_items: int
    discovered_signals: int
    new_signals: int
    pending_review: int
    trajectory_updates: int
    auto_reviews_written: int
    ado_calls: int
    archived_journal_files: int
    background_proposals: int
    dependency_proposals_refreshed: int
    integration_error_details: tuple[IntegrationError, ...]
    gather_flags: dict[str, bool]
    channels: dict[str, dict[str, Any]]
    m365_discovery: dict[str, Any]
    previous_gathered_at: datetime | None
    previous_query_states: dict[str, dict[str, Any]] | None
    previous_channels: dict[str, dict[str, Any]] | None
    previous_m365_discovery: dict[str, Any] | None
    query_states: dict[str, dict[str, Any]]
    programs_root: Path
    promotion_candidates: tuple[M365PromotionCandidate, ...]
    promotion_blocked_artifacts: tuple[M365PromotionBlockedArtifact, ...]
    chart_results: tuple[Any, ...]
    hypothesis_count: int
    # ADF-W2.12: see PersistenceStageInput.correlation_id.
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class StateWriteStageResult:
    artifacts: GatherArtifacts
    finalize_detail: str
