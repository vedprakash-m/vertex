from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from time import perf_counter

from src.commands.gather_pipeline.models import (
    BackgroundSynthesisTrigger,
    ProjectionStageInput,
    ProjectionStageResult,
)
from src.core.ai_proposal_store import load_ai_proposals
from src.core.dependency_scout import (
    load_dependency_proposals,
    merge_dependency_proposals,
    save_dependency_proposals,
    scout_dependency_proposals,
)
from src.core.leakage_detector import LeakageReport, detect_leakage, load_approved_workiq_signals
from src.core.models import SnapshotItem, WorkItem
from src.core.models_v2 import AIProposalStatus, Program, TrajectoryPoint, VitalityAggregate, VitalityScore, Workstream
from src.core.program_fact_store import load_program_facts, project_dependencies
from src.core.trajectory_analyzer import count_eta_slips
from src.core.vitality_reporting import parse_vitality_archive_entry, vitality_settings_from_program
from src.core.yaml_utils import load_yaml_mapping
from src.core.store_factory import build_trajectory_store


_BACKGROUND_LEAKAGE_RATIO_THRESHOLD = 0.5


def run_projection_stage(stage_input: ProjectionStageInput) -> ProjectionStageResult:
    trajectory_started_at = perf_counter()
    trajectory_updates = 0
    if not stage_input.dry_run:
        for item in stage_input.items:
            point = trajectory_point_from_item(item, stage_input.as_of)
            if stage_input.trajectory_store.append(stage_input.program_id, item.id, point):
                trajectory_updates += 1
    trajectory_elapsed_seconds = perf_counter() - trajectory_started_at
    trajectory_detail = f"updates={trajectory_updates}, items={len(stage_input.items)}"

    trajectories_by_item: dict[int, tuple[TrajectoryPoint, ...]] = {}
    needs_item_trajectories = stage_input.include_dependency_scout or background_synthesis_enabled(stage_input.program)
    if needs_item_trajectories:
        storage_backend = stage_input.program.storage_backend or "file"
        trajectories_by_item = load_item_trajectories(
            stage_input.program_id,
            items=stage_input.items,
            programs_root=stage_input.programs_root,
            storage_backend=storage_backend,
        )

    dependency_elapsed_seconds: float | None = None
    dependency_detail: str | None = None
    dependency_proposals_refreshed = 0
    if stage_input.include_dependency_scout:
        dependencies_started_at = perf_counter()
        if not stage_input.dry_run:
            dependency_proposals_refreshed = refresh_dependency_scout_state(
                stage_input.program_id,
                items=stage_input.items,
                workstreams=stage_input.workstreams,
                signal_store=stage_input.signal_store,
                as_of=stage_input.as_of,
                programs_root=stage_input.programs_root,
                trajectories_by_item=trajectories_by_item,
            )
        dependency_elapsed_seconds = perf_counter() - dependencies_started_at
        dependency_detail = f"refreshed={dependency_proposals_refreshed}"

    synthesis_elapsed_seconds: float | None = None
    synthesis_detail: str | None = None
    background_proposals = 0
    if background_synthesis_enabled(stage_input.program):
        synthesis_started_at = perf_counter()
        if not stage_input.dry_run:
            for trigger in evaluate_background_synthesis_triggers(
                stage_input.program_id,
                program=stage_input.program,
                workstreams=stage_input.workstreams,
                items=stage_input.items,
                as_of=stage_input.as_of,
                programs_root=stage_input.programs_root,
                resolve_workstream_id=stage_input.resolve_workstream_id,
                trajectories_by_item=trajectories_by_item,
            ):
                if (stage_input.background_synthesis_runner or run_background_synthesis)(
                    stage_input.program_id,
                    trigger.workstream_id,
                    stage_input.programs_root,
                    stage_input.as_of,
                ):
                    background_proposals += 1
        synthesis_elapsed_seconds = perf_counter() - synthesis_started_at
        synthesis_detail = f"proposals={background_proposals}"

    return ProjectionStageResult(
        trajectory_updates=trajectory_updates,
        dependency_proposals_refreshed=dependency_proposals_refreshed,
        background_proposals=background_proposals,
        trajectory_detail=trajectory_detail,
        dependency_detail=dependency_detail,
        synthesis_detail=synthesis_detail,
        trajectory_elapsed_seconds=trajectory_elapsed_seconds,
        dependency_elapsed_seconds=dependency_elapsed_seconds,
        synthesis_elapsed_seconds=synthesis_elapsed_seconds,
    )


def background_synthesis_enabled(program: Program) -> bool:
    return bool(program.ai is not None and program.ai.enabled)


def refresh_dependency_scout_state(
    program_id: str,
    *,
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
    signal_store,
    as_of: datetime,
    programs_root,
    trajectories_by_item: dict[int, tuple[TrajectoryPoint, ...]],
) -> int:
    existing_proposals = load_dependency_proposals(program_id, programs_root=programs_root)
    generated = scout_dependency_proposals(
        program_id=program_id,
        signals=signal_store.read(program_id),
        review_states=signal_store.read_reviews(program_id),
        snapshot_items=tuple(snapshot_item_from_work_item(item) for item in items),
        workstreams=workstreams,
        existing_dependencies=project_dependencies(
            load_program_facts(
                program_id,
                programs_root=programs_root,
                fact_types=("dependency.link",),
            )
        ),
        trajectories_by_item_id=trajectories_by_item,
        as_of=as_of,
    )
    save_dependency_proposals(
        program_id,
        merge_dependency_proposals(existing_proposals, generated),
        programs_root=programs_root,
        timestamp=as_of,
    )
    return len(generated)


def snapshot_item_from_work_item(item: WorkItem) -> SnapshotItem:
    return SnapshotItem(
        id=item.id,
        type=item.type,
        title=item.title,
        state=item.state,
        assigned_to=item.assigned_to,
        area_path=item.area_path,
        target_date=item.target_date,
        risk_level=item.risk_level,
        tags=list(item.tags),
    )


def evaluate_background_synthesis_triggers(
    program_id: str,
    *,
    program: Program,
    workstreams: tuple[Workstream, ...],
    items: tuple[WorkItem, ...],
    as_of: datetime,
    programs_root,
    resolve_workstream_id,
    trajectories_by_item: dict[int, tuple[TrajectoryPoint, ...]],
) -> tuple[BackgroundSynthesisTrigger, ...]:
    if not items:
        return ()

    raw_program = load_yaml_mapping(programs_root / program_id / "program.yaml")
    vitality_settings = vitality_settings_from_program(raw_program)
    leakage_report = detect_background_leakage(
        program_id,
        items=items,
        as_of=as_of,
        programs_root=programs_root,
        trajectories_by_item=trajectories_by_item,
    )
    leakage_ratio_by_workstream = build_leakage_ratio_by_workstream(
        items,
        workstreams=workstreams,
        leakage_report=leakage_report,
        resolve_workstream_id=resolve_workstream_id,
    )
    eta_slips_by_workstream = build_eta_slips_by_workstream(
        items,
        workstreams=workstreams,
        trajectories_by_item=trajectories_by_item,
        as_of=as_of.date(),
        resolve_workstream_id=resolve_workstream_id,
    )
    vitality_artifacts = generate_background_vitality_artifacts(
        program_id,
        as_of=as_of,
        programs_root=programs_root,
    )
    recent_vitality_scores = load_recent_workstream_vitality_scores(
        program_id,
        as_of=as_of,
        programs_root=programs_root,
        window_days=vitality_settings.nudge_stale_days,
    )
    vitality_aggregates = {aggregate.scope_id: aggregate for aggregate in vitality_artifacts.workstream_aggregates}

    triggers: list[BackgroundSynthesisTrigger] = []
    for workstream in workstreams:
        reasons: list[str] = []
        leakage_ratio = leakage_ratio_by_workstream.get(workstream.id, 0.0)
        eta_slips = eta_slips_by_workstream.get(workstream.id, 0)
        if leakage_ratio > _BACKGROUND_LEAKAGE_RATIO_THRESHOLD and eta_slips >= 2:
            reasons.append(f"leakage ratio {leakage_ratio:.2f} with {eta_slips} ETA slips")

        vitality_aggregate = vitality_aggregates.get(workstream.id)
        if vitality_aggregate is not None and has_sustained_low_vitality(
            workstream.id,
            vitality_aggregate,
            scores=vitality_artifacts.scored_items,
            recent_history_scores=recent_vitality_scores,
            threshold=vitality_settings.nudge_composite_threshold,
            stale_days=vitality_settings.nudge_stale_days,
        ):
            reasons.append(
                f"vitality {vitality_aggregate.composite_score} stayed below {vitality_settings.nudge_composite_threshold}"
            )

        if reasons:
            triggers.append(BackgroundSynthesisTrigger(workstream_id=workstream.id, reasons=tuple(reasons)))

    return tuple(triggers)


def load_item_trajectories(
    program_id: str,
    *,
    items: tuple[WorkItem, ...],
    programs_root,
    storage_backend: str = "file",
) -> dict[int, tuple[TrajectoryPoint, ...]]:
    trajectory_store = build_trajectory_store(storage_backend=storage_backend, programs_root=programs_root)
    return {
        item.id: trajectory_store.read(program_id, item.id)
        for item in items
    }


def detect_background_leakage(
    program_id: str,
    *,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    programs_root,
    trajectories_by_item: dict[int, tuple[TrajectoryPoint, ...]],
) -> LeakageReport:
    return detect_leakage(
        items,
        load_approved_workiq_signals(program_id, as_of=as_of, programs_root=programs_root),
        trajectory_loader=lambda work_item_id: trajectories_by_item.get(work_item_id, ()),
    )


def build_leakage_ratio_by_workstream(
    items: tuple[WorkItem, ...],
    *,
    workstreams: tuple[Workstream, ...],
    leakage_report: LeakageReport,
    resolve_workstream_id,
) -> dict[str, float]:
    signal_counts: dict[str, int] = {}
    leakage_counts: dict[str, int] = {}
    for item in items:
        workstream_id = resolve_workstream_id(item.area_path, workstreams)
        if workstream_id is None:
            continue
        signal_counts[workstream_id] = signal_counts.get(workstream_id, 0) + leakage_report.signal_counts_by_item.get(item.id, 0)
        leakage_counts[workstream_id] = leakage_counts.get(workstream_id, 0) + leakage_report.leakage_counts_by_item.get(item.id, 0)

    return {
        workstream_id: round(leakage_counts.get(workstream_id, 0) / signal_total, 2)
        for workstream_id, signal_total in signal_counts.items()
        if signal_total > 0
    }


def build_eta_slips_by_workstream(
    items: tuple[WorkItem, ...],
    *,
    workstreams: tuple[Workstream, ...],
    trajectories_by_item: dict[int, tuple[TrajectoryPoint, ...]],
    as_of: date,
    resolve_workstream_id,
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in items:
        workstream_id = resolve_workstream_id(item.area_path, workstreams)
        if workstream_id is None:
            continue
        totals[workstream_id] = totals.get(workstream_id, 0) + count_eta_slips(
            trajectories_by_item.get(item.id, ()),
            as_of=as_of,
        )
    return totals


def generate_background_vitality_artifacts(
    program_id: str,
    *,
    as_of: datetime,
    programs_root,
):
    # Transitional: this stage still defers to the vitality command module rather
    # than receiving a precomputed vitality snapshot from the coordinator.
    from src.commands.vitality import generate_vitality_report

    return generate_vitality_report(
        program_id,
        as_of=as_of,
        programs_root=programs_root,
    )


def load_recent_workstream_vitality_scores(
    program_id: str,
    *,
    as_of: datetime,
    programs_root,
    window_days: int,
) -> dict[str, tuple[int, ...]]:
    archive_root = programs_root / program_id / "archive"
    if not archive_root.exists():
        return {}

    window_start = as_of - timedelta(days=window_days)
    scores_by_workstream: dict[str, list[int]] = {}
    for edition_dir in archive_root.iterdir():
        if not edition_dir.is_dir():
            continue
        vitality_path = edition_dir / "vitality.json"
        if not vitality_path.exists():
            continue
        payload = json.loads(vitality_path.read_text(encoding="utf-8"))
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parsed = parse_vitality_archive_entry(entry)
            if parsed is None or parsed.confirmed_at < window_start:
                continue
            for workstream_id, aggregate in parsed.per_workstream.items():
                scores_by_workstream.setdefault(workstream_id, []).append(aggregate.score)

    return {
        workstream_id: tuple(scores)
        for workstream_id, scores in scores_by_workstream.items()
    }


def has_sustained_low_vitality(
    workstream_id: str,
    aggregate: VitalityAggregate,
    *,
    scores: tuple[VitalityScore, ...],
    recent_history_scores: dict[str, tuple[int, ...]],
    threshold: int,
    stale_days: int,
) -> bool:
    if aggregate.composite_score >= threshold:
        return False

    historical_scores = recent_history_scores.get(workstream_id, ())
    if any(score < threshold for score in historical_scores):
        return True

    current_scores = tuple(score for score in scores if score.workstream_id == workstream_id)
    if not current_scores:
        return False
    return all(score.freshness_days > stale_days for score in current_scores)


def run_background_synthesis(
    program_id: str,
    workstream_id: str,
    programs_root,
    as_of: datetime,
) -> bool:
    if load_ai_proposals(
        program_id,
        status=AIProposalStatus.PENDING,
        workstream_id=workstream_id,
        programs_root=programs_root,
    ):
        return False

    from src.ai.synthesizer import SynthesizerError
    from src.commands.synthesize import synthesize_workstream

    try:
        synthesize_workstream(
            workstream_id=workstream_id,
            program_id=program_id,
            reviewer="system",
            programs_root=programs_root,
            now_provider=lambda: as_of,
        )
    except SynthesizerError:
        return False
    return True


def trajectory_point_from_item(item: WorkItem, as_of: datetime) -> TrajectoryPoint:
    return TrajectoryPoint(
        date=as_of.astimezone(timezone.utc).date(),
        state=item.state,
        assigned_to=item.assigned_to_email or item.assigned_to,
        target_date=item.target_date,
        risk_level=item.risk_level,
        area_path=item.area_path,
        tags=tuple(item.tags),
        risk_assessment=item.risk_assessment,
        risk_assessment_comment=item.risk_assessment_comment,
    )
