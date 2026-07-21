from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.commands.gather_pipeline.finalization_stage import compute_and_persist_plane1_changes as _compute_and_persist_plane1_changes
from src.commands.gather_pipeline.models import GatherArtifacts, StateWriteStageInput, StateWriteStageResult
from src.core.plane1_changelog import (
    append_plane1_changes,
    build_plane1_snapshot,
    compute_plane1_changes,
    load_plane1_last_seen,
    shadow_write_plane1_snapshot,
    write_plane1_last_seen,
)
from src.core.program_fact_store import (
    load_program_facts,
    persist_program_fact_snapshot,
    project_assumptions,
    project_decision_entries,
    project_milestones,
    project_risk_entries,
    project_workstreams,
)
from src.core.gather_state_store import write_gather_state


def compute_and_persist_plane1_changes(
    program_id: str,
    programs_root: Path,
    gathered_at: datetime,
    *,
    correlation_id: str = "",
) -> None:
    _compute_and_persist_plane1_changes(
        program_id,
        programs_root,
        gathered_at,
        load_program_facts=load_program_facts,
        project_milestones=project_milestones,
        project_risk_entries=project_risk_entries,
        project_decision_entries=project_decision_entries,
        project_assumptions=project_assumptions,
        project_workstreams=project_workstreams,
        load_plane1_last_seen=load_plane1_last_seen,
        compute_plane1_changes=compute_plane1_changes,
        append_plane1_changes=append_plane1_changes,
        build_plane1_snapshot=build_plane1_snapshot,
        shadow_write_plane1_snapshot=shadow_write_plane1_snapshot,
        persist_program_fact_snapshot=persist_program_fact_snapshot,
        write_plane1_last_seen=write_plane1_last_seen,
        correlation_id=correlation_id,
    )


def run_state_write_stage(stage_input: StateWriteStageInput) -> StateWriteStageResult:
    compute_and_persist_plane1_changes(
        stage_input.program_id,
        stage_input.programs_root,
        stage_input.gathered_at,
        correlation_id=stage_input.correlation_id,
    )

    write_gather_state(
        stage_input.program_id,
        gathered_at=stage_input.gathered_at,
        scanned_items=stage_input.scanned_items,
        discovered_signals=stage_input.discovered_signals,
        new_signals=stage_input.new_signals,
        pending_review=stage_input.pending_review,
        trajectory_updates=stage_input.trajectory_updates,
        auto_reviews_written=stage_input.auto_reviews_written,
        ado_calls=stage_input.ado_calls,
        archived_journal_files=stage_input.archived_journal_files,
        background_proposals=stage_input.background_proposals,
        integration_errors=len(stage_input.integration_error_details),
        integration_error_details=stage_input.integration_error_details,
        gather_flags=stage_input.gather_flags,
        channels=stage_input.channels,
        m365_discovery=stage_input.m365_discovery,
        previous_gathered_at=stage_input.previous_gathered_at,
        previous_query_states=stage_input.previous_query_states,
        previous_channels=stage_input.previous_channels,
        previous_m365_discovery=stage_input.previous_m365_discovery,
        query_states=stage_input.query_states,
        programs_root=stage_input.programs_root,
    )

    artifacts = GatherArtifacts(
        program_id=stage_input.program_id,
        scanned_items=stage_input.scanned_items,
        discovered_signals=stage_input.discovered_signals,
        new_signals=stage_input.new_signals,
        pending_review=stage_input.pending_review,
        trajectory_updates=stage_input.trajectory_updates,
        auto_reviews_written=stage_input.auto_reviews_written,
        ado_calls=stage_input.ado_calls,
        archived_journal_files=stage_input.archived_journal_files,
        background_proposals=stage_input.background_proposals,
        dependency_proposals_refreshed=stage_input.dependency_proposals_refreshed,
        integration_errors=stage_input.integration_error_details,
        promotion_candidates=stage_input.promotion_candidates,
        promotion_blocked_artifacts=stage_input.promotion_blocked_artifacts,
        chart_results=stage_input.chart_results,
        ado_query_results=stage_input.ado_query_results,
        discovered_work_item_ids=stage_input.discovered_work_item_ids,
        hydrated_work_item_ids=stage_input.hydrated_work_item_ids,
        channel_outcomes=stage_input.channel_outcomes,
    )
    return StateWriteStageResult(
        artifacts=artifacts,
        finalize_detail=(
            f"signals={stage_input.discovered_signals}, "
            f"new={stage_input.new_signals}, "
            f"hypotheses={stage_input.hypothesis_count}, "
            f"ado_calls={stage_input.ado_calls}"
        ),
    )
