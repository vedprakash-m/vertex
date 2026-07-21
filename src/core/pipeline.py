from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Protocol

from src.core.models import ArchiveEntry, ArchiveIndex, EditionType, Snapshot, WorkItem
from src.core.program_fact_store import ProgramFactSnapshot


@dataclass(frozen=True, slots=True)
class StageContext:
    """Immutable context accumulated through report pipeline stages."""

    edition_name: str = ""
    issue_number: int | None = None
    reseed: bool = False
    no_seed: bool = False
    dry_run: bool = False
    offline: bool = False
    diff_mode: bool = False
    as_of: datetime | None = None
    edition_type_override: str | None = None
    lookback_range: int | None = None
    section_filter_ids: tuple[str, ...] = ()
    reports_root: Path | None = None
    archive_root: Path | None = None
    output_root: Path | None = None
    work_item_loader: Any = None
    kusto_query_executor: Any = None
    open_browser: bool = False
    stage_support: Any = None

    started_at: datetime | None = None
    data_as_of: datetime | None = None
    repo_root: Path | None = None
    editions_root: Path | None = None
    programs_root: Path | None = None
    bundle: Any = None
    resolved_v2: Any = None
    archive_index: ArchiveIndex | None = None
    latest_confirmed_entry: ArchiveEntry | None = None
    resolved_issue_number: int | None = None
    previous_dry_run_state: dict[str, Any] | None = None
    previous_snapshot: Snapshot | None = None
    previous_issue_number: int | None = None
    trusted_baseline_issue_number: int | None = None
    resolved_edition_type: EditionType | None = None
    ado_calls: int | None = None
    offline_source_label: str | None = None
    items: tuple[WorkItem, ...] = ()
    eta_forecasts: Any = None
    evidence_by_item: dict[int, Any] | None = None
    continuity_snapshot: Snapshot | None = None
    continuity_previous_issue_number: int | None = None
    deltas: Any = None
    overrides_document: Any = None
    override_snapshot: dict[str, dict[str, dict[str, Any]]] | None = None
    top_3_now: tuple[str, ...] = ()
    overrides_path: Path | None = None
    overrides_seeding: Any = None
    scorecard_packets: Any = None
    scorecards: Any = None
    dimension_risks: Any = None
    scorecard_deltas: Any = None
    milestones: Any = None
    milestone_assessments: Any = None
    milestone_lineage: dict[str, dict[str, str | None]] | None = None
    milestone_warnings: tuple[str, ...] = ()
    risks: Any = None
    risk_assessments: Any = None
    risk_lineage: dict[str, dict[str, str | None]] | None = None
    stale_risk_ids: tuple[str, ...] = ()
    risk_warnings: tuple[str, ...] = ()
    actions: Any = None
    overdue_action_ids: tuple[str, ...] = ()
    action_warnings: tuple[str, ...] = ()
    signal_context: Any = None
    default_exec_summary: str | None = None
    top_items: Any = None
    auto_suggestions: Any = None
    continuity_chapters: Any = None
    narratives_dir: Path | None = None
    narrative_seeding: Any = None
    loaded_narratives: dict[str, str] | None = None
    visible_section_ids: Any = None
    section_roster_current_ids: tuple[str, ...] = ()
    loaded_exec_summary_text: str | None = None
    exec_summary_text: str | None = None
    workstream_blurbs: dict[str, str] | None = None
    workstream_narrative_history: Any = None
    ai_synthesis: Any = None
    render_exec_summary_text: str | None = None
    render_workstream_blurbs: dict[str, str] | None = None
    render_state: Any = None
    validation_state: Any = None
    artifacts: Any = None
    fact_snapshot: ProgramFactSnapshot | None = None
    # ADF-W2.12 (Section 8.2.6): cross-stage correlation identity, generated
    # once at report_command entry and threaded through every stage so each
    # artifact-producing stage can record a trace link sharing the same id.
    # Empty string = no correlation (the default -- existing construction call
    # sites and tests are unaffected; a stage that sees "" simply does not
    # record a trace link).
    correlation_id: str = ""
    workflow_id: str = ""
    # run_id distinguishes two runs that share a correlation_id (e.g. a retry
    # of the same logical run); generated alongside correlation_id at
    # report_command entry and threaded the same way.
    run_id: str = ""
    # D-17: identity of the committed gather run whose data underlies this
    # report invocation. Resolved once, immediately after ResolutionStage
    # (see _execute_report_pipeline in src/commands/report.py), from
    # resolve_latest_committed_manifest(); every downstream stage that builds
    # a RunManifest or DraftState reads these two fields rather than
    # re-resolving, so one report run always stamps one identical
    # gather_run_id/gather_run_hash pair everywhere. Distinct from the
    # unrelated pre-existing run_id/correlation_id fields above, which are
    # ADF-W2.12 observability trace-correlation ids, not gather-run lineage.
    gather_run_id: str | None = None
    gather_run_hash: str | None = None


class PipelineStage(Protocol):
    def execute(self, ctx: StageContext) -> StageContext: ...

    def name(self) -> str: ...


PipelineProgressCallback = Callable[[PipelineStage, int, int, StageContext, StageContext, float], None]


def run_pipeline(
    stages: tuple[PipelineStage, ...],
    ctx: StageContext,
    *,
    progress_callback: PipelineProgressCallback | None = None,
    start_index: int = 1,
    total_stages: int | None = None,
) -> StageContext:
    """Execute stages in order, returning the final immutable context."""

    current = ctx
    resolved_total = total_stages or len(stages)
    for offset, stage in enumerate(stages):
        stage_started = perf_counter()
        next_ctx = stage.execute(current)
        if progress_callback is not None:
            progress_callback(stage, start_index + offset, resolved_total, current, next_ctx, perf_counter() - stage_started)
        current = next_ctx
    return current
