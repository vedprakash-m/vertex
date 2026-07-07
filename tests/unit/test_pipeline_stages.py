from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.ai.ai_stage import AIStage
from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands import report as report_module
from src.commands import report_deck as report_deck_module
from src.commands.report import generate_report_draft
from src.core.action_tracker import append_action
from src.core.exceptions import ConfigError, QueryTimeoutError
from src.core.models import Confidence, EditionType, RiskLevel
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Dependency, DependencyScheduleStatus, DependencyStatus
from src.core.fact_sor_state import save_fact_sor_state
from src.core.models_v2 import DependencyType, Milestone, MilestoneAssessment, MilestoneStatus, SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus, Signal, SignalReviewDecision, TrajectoryPoint
from src.core.pipeline import StageContext
from src.core.section_proposal_store import append_proposal
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.stages import action_stage as action_stage_module
from src.core.stages.action_stage import ActionStage
from src.core.stages.compute_stage import ComputeStage
from src.core.stages.fetch_stage import FetchStage
from src.core.stages import milestone_stage as milestone_stage_module
from src.core.stages.milestone_stage import MilestoneStage
from src.core.stages.narrative_stage import NarrativeStage
from src.core.stages import render_stage as render_stage_module
from src.core.stages.render_stage import RenderStage
from src.core.stages import risk_stage as risk_stage_module
from src.core.stages.risk_stage import RiskStage
from src.core.stages.resolution_stage import ResolutionStage
from src.core.stages import validation_stage as validation_stage_module
from src.core.stages.validation_stage import ValidationStage
from tests.support.ado_cassettes import load_cassette_work_items
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_report import _sample_items


EDITION_NAME = "acme_weekly"


def _rampp1_items(as_of: datetime) -> tuple:
    from dataclasses import replace
    return tuple(replace(item, tags=[*item.tags, "RAMPP1"]) for item in _sample_items(as_of))


def test_resolution_stage_populates_report_context(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    ctx = ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            as_of=as_of,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )
    )

    assert ctx.bundle is not None
    assert ctx.archive_index is not None
    assert ctx.reports_root == reports_root
    assert ctx.archive_root == archive_root
    assert ctx.output_root is None
    assert ctx.programs_root == tmp_path / "programs"
    assert ctx.resolved_issue_number == 1
    assert ctx.data_as_of == as_of
    assert ctx.resolved_edition_type == EditionType.DETAILED


def test_fetch_stage_offline_uses_cached_snapshot_without_live_loader(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    seeded_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    def _unexpected_loader(bundle, timestamp):
        raise AssertionError("FetchStage offline path should not invoke the live work item loader")

    resolved_ctx = ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            offline=True,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            work_item_loader=_unexpected_loader,
        )
    )

    fetched_ctx = FetchStage().execute(resolved_ctx)
    assert fetched_ctx.offline_source_label == "cached draft Issue 001"
    assert fetched_ctx.data_as_of == seeded_artifacts.snapshot.ado_data_as_of
    # started_at is the wall-clock execution time, not the cached data timestamp
    assert fetched_ctx.started_at is not None
    assert tuple(item.id for item in fetched_ctx.items) == tuple(item.id for item in seeded_artifacts.report.items)


def test_fetch_stage_timeout_suggests_offline_when_cache_exists(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            work_item_loader=lambda bundle, timestamp: (_ for _ in ()).throw(QueryTimeoutError("boom")),
        )
    )

    with pytest.raises(QueryTimeoutError, match="Re-run with --offline to use cached data"):
        FetchStage().execute(resolved_ctx)


def test_compute_stage_populates_deterministic_report_state(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: (_rampp1_items(timestamp), 0),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)

    assert computed_ctx.eta_forecasts == {}
    assert computed_ctx.evidence_by_item is not None and computed_ctx.evidence_by_item
    assert computed_ctx.continuity_snapshot is None
    assert computed_ctx.deltas is not None
    assert computed_ctx.overrides_document is not None
    assert computed_ctx.override_snapshot is not None
    assert computed_ctx.overrides_path is not None and computed_ctx.overrides_path.exists()
    assert computed_ctx.scorecard_packets is not None
    assert computed_ctx.scorecards is not None and len(computed_ctx.scorecards) > 0
    assert computed_ctx.dimension_risks is not None and len(computed_ctx.dimension_risks) > 0
    assert computed_ctx.scorecard_deltas is not None
    assert computed_ctx.signal_context is not None
    assert computed_ctx.default_exec_summary is not None
    assert "current-state inventory" in computed_ctx.default_exec_summary


def test_narrative_stage_populates_editorial_state(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    risk_ctx = RiskStage().execute(milestone_ctx)
    narrative_ctx = NarrativeStage().execute(risk_ctx)

    assert milestone_ctx.milestones is not None
    assert milestone_ctx.milestone_assessments is not None
    assert risk_ctx.risks is not None
    assert risk_ctx.risks == ()
    assert risk_ctx.stale_risk_ids == ()

    assert narrative_ctx.top_items is not None
    assert narrative_ctx.auto_suggestions is not None
    assert narrative_ctx.continuity_chapters is not None
    assert narrative_ctx.narratives_dir is not None and narrative_ctx.narratives_dir.exists()
    assert (narrative_ctx.narratives_dir / "exec_summary.md").exists()
    assert narrative_ctx.loaded_exec_summary_text is not None
    assert narrative_ctx.exec_summary_text is not None and narrative_ctx.exec_summary_text.strip()
    assert narrative_ctx.workstream_blurbs is not None
    assert narrative_ctx.workstream_narrative_history is not None


def test_action_stage_populates_action_state(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    append_action(
        "acme",
        ActionItem(
            id="action-acme-1",
            program_id="acme",
            text="Follow up with the firmware team",
            owner_alias="owner",
            due_date=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc).date(),
            status=ActionStatus.OPEN,
            source_signal_id="signal-1",
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="acme",
            created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=tmp_path / "programs",
    )

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    risk_ctx = RiskStage().execute(milestone_ctx)
    action_ctx = ActionStage().execute(risk_ctx)

    assert action_ctx.actions is not None
    assert "action-acme-1" in {action.id for action in action_ctx.actions}
    assert action_ctx.overdue_action_ids == ("action-acme-1",)


def test_action_stage_current_loader_uses_program_facts(monkeypatch, tmp_path: Path) -> None:
    snapshot = object()
    action = ActionItem(
        id="action-acme-1",
        program_id="acme",
        text="Follow up with the firmware team",
        owner_alias="owner",
        due_date=date(2026, 5, 1),
        status=ActionStatus.OPEN,
        source_signal_id="signal-1",
        source_type=ActionSourceType.SIGNAL,
        linked_work_item_ids=(1001,),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id="acme",
        created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )
    captured: list[tuple[str, tuple[str, ...]]] = []

    monkeypatch.setattr(
        action_stage_module,
        "load_program_facts",
        lambda program_id, *, programs_root, fact_types: captured.append((program_id, fact_types)) or snapshot,
    )
    monkeypatch.setattr(
        action_stage_module,
        "project_action_items",
        lambda loaded_snapshot: (action,) if loaded_snapshot is snapshot else (),
    )

    actions = action_stage_module._load_current_actions("acme", programs_root=tmp_path / "programs")

    assert actions == (action,)
    assert captured == [("acme", ("action.item",))]


def test_milestone_stage_current_loaders_use_program_facts(monkeypatch, tmp_path: Path) -> None:
    milestone_snapshot = object()
    dependency_snapshot = object()
    milestone = Milestone(
        id="ms-1",
        program_id="acme",
        name="Ramp readiness",
        target_date=date(2026, 5, 15),
        owner_alias="owner",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=(),
        linked_workstream_ids=("velocity",),
        linked_work_item_ids=(1001,),
        notes=None,
    )
    dependency = Dependency(
        id="dep-1",
        from_program_id="acme",
        from_workstream_id="velocity",
        from_item_id=1001,
        from_milestone_id=None,
        to_program_id="fabrikam",
        to_workstream_id="buildouts",
        to_item_id=None,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Fabrikam buildout planning depends on Acme readiness.",
        mitigation=None,
        status=DependencyStatus.BROKEN,
        owner_alias="owner",
        resolution_path=None,
        planned_resolution_date=None,
        schedule_status=DependencyScheduleStatus.BLOCKED,
    )
    captured: list[tuple[str, tuple[str, ...]]] = []

    def _load_program_facts(program_id: str, *, programs_root, fact_types: tuple[str, ...]):
        captured.append((program_id, fact_types))
        return {
            ("milestone.entry",): milestone_snapshot,
            ("dependency.link",): dependency_snapshot,
        }[fact_types]

    monkeypatch.setattr(milestone_stage_module, "load_program_facts", _load_program_facts)
    monkeypatch.setattr(
        milestone_stage_module,
        "project_milestones",
        lambda snapshot: (milestone,) if snapshot is milestone_snapshot else (),
    )
    monkeypatch.setattr(
        milestone_stage_module,
        "project_dependencies",
        lambda snapshot: (dependency,) if snapshot is dependency_snapshot else (),
    )

    milestones = milestone_stage_module._load_current_milestones("acme", programs_root=tmp_path / "programs")
    dependencies = milestone_stage_module._load_current_dependencies("acme", programs_root=tmp_path / "programs")

    assert milestones == (milestone,)
    assert dependencies == (dependency,)
    assert captured == [
        ("acme", ("milestone.entry",)),
        ("acme", ("dependency.link",)),
    ]


def test_risk_stage_current_loader_uses_program_facts(monkeypatch, tmp_path: Path) -> None:
    snapshot = object()
    captured: list[tuple[str, tuple[str, ...]]] = []

    monkeypatch.setattr(
        risk_stage_module,
        "load_program_facts",
        lambda program_id, *, programs_root, fact_types: captured.append((program_id, fact_types)) or snapshot,
    )
    monkeypatch.setattr(
        risk_stage_module,
        "project_risk_entries",
        lambda loaded_snapshot: ("risk-1",) if loaded_snapshot is snapshot else (),
    )

    risks = risk_stage_module._load_current_risks("acme", programs_root=tmp_path / "programs")

    assert risks == ("risk-1",)
    assert captured == [("acme", ("risk.entry",))]


def test_render_stage_current_loader_uses_program_facts(monkeypatch, tmp_path: Path) -> None:
    snapshot = object()
    captured: list[tuple[str, tuple[str, ...]]] = []

    monkeypatch.setattr(
        render_stage_module,
        "load_program_facts",
        lambda program_id, *, programs_root, fact_types: captured.append((program_id, fact_types)) or snapshot,
    )
    monkeypatch.setattr(
        render_stage_module,
        "project_risk_entries",
        lambda loaded_snapshot: ("risk-1",) if loaded_snapshot is snapshot else (),
    )

    risks = render_stage_module._load_current_risks("acme", programs_root=tmp_path / "programs")

    assert risks == ("risk-1",)
    assert captured == [("acme", ("risk.entry",))]


def test_ai_stage_populates_render_narratives(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    narrative_ctx = NarrativeStage().execute(milestone_ctx)
    ai_ctx = AIStage().execute(narrative_ctx)

    assert ai_ctx.ai_synthesis is not None
    assert ai_ctx.render_exec_summary_text is not None and ai_ctx.render_exec_summary_text.strip()
    assert ai_ctx.render_workstream_blurbs is not None


def test_ai_stage_disabled_mode_preserves_existing_narratives(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    narrative_ctx = NarrativeStage().execute(milestone_ctx)
    set_ai_mode(AIMode.DISABLED)
    try:
        ai_ctx = AIStage().execute(narrative_ctx)
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert ai_ctx.ai_synthesis is not None
    assert ai_ctx.ai_synthesis.ai_calls == 0
    assert ai_ctx.ai_synthesis.ai_cost_usd == 0.0
    assert ai_ctx.render_exec_summary_text == narrative_ctx.exec_summary_text
    assert ai_ctx.render_workstream_blurbs == narrative_ctx.workstream_blurbs


def test_render_stage_populates_render_outputs(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    narrative_ctx = NarrativeStage().execute(milestone_ctx)
    ai_ctx = AIStage().execute(narrative_ctx)
    render_ctx = RenderStage().execute(ai_ctx)

    assert render_ctx.render_state is not None
    assert render_ctx.render_state.review_status_path.exists()
    assert render_ctx.render_state.html_body
    assert render_ctx.render_state.markdown_body
    assert render_ctx.render_state.report.manifest_id
    assert render_ctx.render_state.snapshot.items
    assert render_ctx.render_state.rendered_strings["html"] == render_ctx.render_state.html_body


def test_render_stage_deck_reads_sqlite_backed_icm_signals(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    programs_root = tmp_path / "programs"
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=EditionType.DECK,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    SQLiteSignalStore(programs_root=programs_root).append(
        Signal(
            id="sig-render-deck-icm-1",
            timestamp=datetime(2026, 5, 5, 17, 45, tzinfo=timezone.utc),
            source="icm/incident",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Sev2 incident active for deployment readiness.",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"severity": 2},
        )
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    narrative_ctx = NarrativeStage().execute(milestone_ctx)
    ai_ctx = AIStage().execute(narrative_ctx)
    render_ctx = RenderStage().execute(ai_ctx)

    assert render_ctx.render_state is not None
    assert "IcM 12345: Sev2 incident active for deployment readiness. — icm incident | BLOCK | high confidence | workstream deployment_readiness" in render_ctx.render_state.markdown_body


def test_validation_stage_populates_manifest_and_warnings(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    narrative_ctx = NarrativeStage().execute(milestone_ctx)
    ai_ctx = AIStage().execute(narrative_ctx)
    render_ctx = RenderStage().execute(ai_ctx)
    validated_ctx = ValidationStage().execute(render_ctx)

    assert validated_ctx.validation_state is not None
    assert validated_ctx.validation_state.manifest.qg_results
    assert "milestone_assessments" in validated_ctx.validation_state.manifest.metadata
    # Data-dependent: tracks live milestones.yaml target_date (may drift).
    assert validated_ctx.validation_state.manifest.metadata["milestone_assessments"][0]["target_date"] in {"2026-05-18", "2026-05-29", "2026-06-22"}
    assert "completion_date" in validated_ctx.validation_state.manifest.metadata["milestone_assessments"][0]
    ai_safety = validated_ctx.validation_state.manifest.metadata["ai_safety"]
    assert ai_safety["enabled"] == validated_ctx.bundle.config.ai.enabled
    assert ai_safety["budget_usd"] == pytest.approx(0.5)
    assert ai_safety["spent_usd"] == pytest.approx(validated_ctx.validation_state.manifest.ai_cost_usd)
    assert ai_safety["ai_calls"] == validated_ctx.validation_state.manifest.ai_calls
    assert ai_safety["within_budget"] is True
    assert validated_ctx.validation_state.report.manifest_id
    assert validated_ctx.validation_state.report.hygiene_warnings is not None
    assert validated_ctx.validation_state.warnings is not None
    assert validated_ctx.validation_state.exit_code in {0, 2, 3}


def test_milestone_stage_soft_fails_for_malformed_config(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    milestone_path = tmp_path / "programs" / "acme" / "milestones.yaml"
    milestone_path.write_text('schema_version: "1.0"\nmilestones: invalid\n', encoding="utf-8")

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)

    assert milestone_ctx.milestones == ()
    assert milestone_ctx.milestone_assessments == ()
    assert milestone_ctx.milestone_warnings


def test_output_stage_populates_artifacts(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    narrative_ctx = NarrativeStage().execute(milestone_ctx)
    ai_ctx = AIStage().execute(narrative_ctx)
    render_ctx = RenderStage().execute(ai_ctx)
    validated_ctx = ValidationStage().execute(render_ctx)
    output_ctx = report_module._OutputStage().execute(validated_ctx)

    assert output_ctx.artifacts is not None
    assert output_ctx.artifacts.manifest.qg_results
    assert output_ctx.artifacts.html_path is not None and output_ctx.artifacts.html_path.exists()
    assert output_ctx.artifacts.md_path is not None and output_ctx.artifacts.md_path.exists()
    assert output_ctx.artifacts.manifest_path is not None and output_ctx.artifacts.manifest_path.exists()
    assert output_ctx.artifacts.snapshot_path is not None and output_ctx.artifacts.snapshot_path.exists()


def test_validation_stage_load_gate_signals_reads_sqlite_backed_reviews(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    signal_store.append(
        Signal(
            id="sqlite-approved-1",
            timestamp=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Deployment telemetry remains blocked.",
            raw_ref="WI:900001",
            confidence=Confidence.HIGH,
            thread_id=None,
        )
    )
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="sqlite-approved-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    )

    ctx = StageContext(
        bundle=SimpleNamespace(config=SimpleNamespace(ado=SimpleNamespace(date_window_days=14))),
        resolved_v2=SimpleNamespace(program=SimpleNamespace(id="acme")),
        programs_root=programs_root,
        data_as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
    )

    journal_signals, approved_signals = validation_stage_module._load_gate_signals(ctx)

    assert [signal.id for signal in journal_signals] == ["sqlite-approved-1"]
    assert [signal.id for signal in approved_signals] == ["sqlite-approved-1"]


def test_validation_stage_passes_stale_claim_ids_to_phase_1b(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    programs_root = reports_root.parent / "programs"

    append_proposal(
        SectionRevisionProposal(
            proposal_id="proposal-stale-claim-validation",
            edition_id=EDITION_NAME,
            issue_number=1,
            section_id="ws_networking",
            current_text="Current networking narrative.",
            proposed_text="Current networking narrative.",
            evidence_brief=SectionEvidenceBrief(
                section_id="ws_networking",
                ado_delta_summary="No material changes.",
                new_items=(),
                closed_items=(),
                risk_changed_items=(),
                eta_changed_items=(),
                top_signals=(),
                kpi_summary=None,
                stale_claims=("claim-stale-validation-1",),
                vitality_summary="Stable",
                confidence=Confidence.MEDIUM,
            ),
            status=SectionRevisionStatus.ACCEPTED,
            generated_at=datetime(2026, 5, 5, 18, 1, tzinfo=timezone.utc),
            resolved_at=datetime(2026, 5, 5, 18, 6, tzinfo=timezone.utc),
            accepted_text="Current networking narrative.",
            source_hash="sha256:test-validation-stale-claim",
        ),
        "acme",
        1,
        programs_root=programs_root,
    )

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)
    narrative_ctx = NarrativeStage().execute(milestone_ctx)
    ai_ctx = AIStage().execute(narrative_ctx)
    render_ctx = RenderStage().execute(ai_ctx)

    original_phase_1b = validation_stage_module.evaluate_phase_1b_gates
    captured: dict[str, object] = {}

    def _capture_phase_1b(**kwargs):
        captured["stale_claim_ids"] = kwargs["stale_claim_ids"]
        return original_phase_1b(**kwargs)

    monkeypatch.setattr("src.core.stages.validation_stage.evaluate_phase_1b_gates", _capture_phase_1b)

    ValidationStage().execute(render_ctx)

    assert captured["stale_claim_ids"] == ("claim-stale-validation-1",)


def test_milestone_stage_reads_sqlite_backed_trajectories(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    programs_root = reports_root.parent / "programs"
    _patch_m3_linked_wi(programs_root, work_item_id=900001)
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 4),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 28),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )

    request_ctx = report_module._build_stage_request_context(
        edition_name=EDITION_NAME,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=False,
        offline=False,
        diff_mode=False,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override=None,
        lookback_range=None,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=None,
        section_filter_ids=(),
        open_browser=False,
    )

    resolved_ctx = ResolutionStage().execute(request_ctx)
    fetched_ctx = FetchStage().execute(resolved_ctx)
    computed_ctx = ComputeStage().execute(fetched_ctx)
    milestone_ctx = MilestoneStage().execute(computed_ctx)

    assert milestone_ctx.milestone_assessments is not None
    assessment = next(item for item in milestone_ctx.milestone_assessments if item.milestone_id == "m3-code-complete")
    assert "Linked work item #900001 trajectory now points past milestone target (2026-05-28)." in assessment.blocked_criteria


def test_milestone_stage_uses_injected_reality_for_family_sor_flip(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"workitem.state": "shadow"},
        recorded_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    milestone = Milestone(
        id="m-reality",
        program_id="nova",
        name="Reality-backed milestone",
        target_date=date(2026, 7, 31),
        owner_alias="operator",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=(),
        linked_workstream_ids=(),
        linked_work_item_ids=(),
    )
    calls: list[dict[str, object]] = []

    class _Reality:
        def milestones(self):
            return (
                SimpleNamespace(
                    record=milestone,
                    lineage=SimpleNamespace(
                        source_document_key="email:sha256:milestone-source",
                        approval_event_id="evt-approval-1",
                    ),
                ),
            )

    def _load_program_reality(program_id: str, **kwargs):
        calls.append({"program_id": program_id, **kwargs})
        return _Reality()

    def _legacy_milestones(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("family shadow mode must not use the legacy milestone path")

    monkeypatch.setattr(milestone_stage_module, "_load_current_milestones", _legacy_milestones)
    monkeypatch.setattr(milestone_stage_module, "_load_current_dependencies", lambda *_a, **_kw: ())
    monkeypatch.setattr(
        milestone_stage_module,
        "assess_milestone_health",
        lambda *_a, **_kw: MilestoneAssessment(
            milestone_id="m-reality",
            computed_health=MilestoneStatus.ON_TRACK,
            blocked_criteria=(),
            slip_probability=0.0,
            critical_path=False,
            confidence=Confidence.HIGH,
            reasoning="test",
        ),
    )
    monkeypatch.setattr(
        milestone_stage_module,
        "build_critical_path",
        lambda *_a, **_kw: (),
    )

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
        data_as_of=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=_load_program_reality),
    )

    result = MilestoneStage().execute(ctx)

    assert [call["program_id"] for call in calls] == ["nova"]
    assert calls[0]["as_of"] == ctx.data_as_of
    assert calls[0]["edition_name"] == "nova_weekly"
    assert calls[0]["archive_root"] == tmp_path / "archive"
    assert result.milestones == (milestone,)
    assert result.milestone_lineage == {
        "m-reality": {
            "source_document_key": "email:sha256:milestone-source",
            "approval_event_id": "evt-approval-1",
        }
    }
    assert result.milestone_assessments[0].milestone_id == "m-reality"
    assert result.milestone_warnings == ()


def test_milestone_stage_requires_audited_rollback_flag_for_reality_failure(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"workitem.state": "primary"},
        recorded_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )

    def _broken_reality(*_args, **_kwargs):
        raise RuntimeError("facade unavailable")

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
        data_as_of=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=_broken_reality),
    )

    with pytest.raises(ConfigError, match="audited legacy rollback"):
        MilestoneStage().execute(ctx)


def test_milestone_stage_audited_rollback_warns_and_uses_legacy(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"workitem.state": "primary"},
        recorded_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    milestone = Milestone(
        id="m-legacy",
        program_id="nova",
        name="Legacy rollback milestone",
        target_date=date(2026, 7, 31),
        owner_alias="operator",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=(),
        linked_workstream_ids=(),
        linked_work_item_ids=(),
    )

    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    monkeypatch.setattr(milestone_stage_module, "_load_current_milestones", lambda *_a, **_kw: (milestone,))
    monkeypatch.setattr(milestone_stage_module, "_load_current_dependencies", lambda *_a, **_kw: ())
    monkeypatch.setattr(
        milestone_stage_module,
        "assess_milestone_health",
        lambda *_a, **_kw: MilestoneAssessment(
            milestone_id="m-legacy",
            computed_health=MilestoneStatus.ON_TRACK,
            blocked_criteria=(),
            slip_probability=0.0,
            critical_path=False,
            confidence=Confidence.HIGH,
            reasoning="test",
        ),
    )
    monkeypatch.setattr(milestone_stage_module, "build_critical_path", lambda *_a, **_kw: ())

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
        data_as_of=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("facade unavailable"))),
    )

    result = MilestoneStage().execute(ctx)

    assert result.milestones == (milestone,)
    assert result.milestone_lineage == {}
    assert len(result.milestone_warnings) == 1
    assert "degraded to legacy milestone source via audited rollback flag" in result.milestone_warnings[0]


def test_report_milestone_rows_carry_activation_lineage(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "nova"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "nova",
                "name": "NOVA",
                "storage_backend": "sqlite",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    rows = report_deck_module._build_report_milestone_rows(
        (
            Milestone(
                id="m-source",
                program_id="nova",
                name="Source-backed milestone",
                target_date=date(2026, 7, 31),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=(),
                linked_workstream_ids=(),
                linked_work_item_ids=(),
            ),
        ),
        (
            MilestoneAssessment(
                milestone_id="m-source",
                computed_health=MilestoneStatus.COMPLETED,
                blocked_criteria=(),
                slip_probability=0.0,
                critical_path=False,
                confidence=Confidence.HIGH,
                reasoning="test",
            ),
        ),
        items=(),
        program_id="nova",
        programs_root=programs_root,
        as_of=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        milestone_lineage={
            "m-source": {
                "source_document_key": "email:sha256:milestone-source",
                "approval_event_id": "evt-approval-1",
            }
        },
    )

    assert len(rows) == 1
    assert rows[0].source_document_key == "email:sha256:milestone-source"
    assert rows[0].approval_event_id == "evt-approval-1"


def _patch_m3_linked_wi(programs_root: Path, *, work_item_id: int = 900001) -> None:
    """Patch m3-code-complete in the test workspace to reference the given WI (test fixture only)."""
    milestones_path = programs_root / "acme" / "milestones.yaml"
    if not milestones_path.exists():
        return
    data = yaml.safe_load(milestones_path.read_text(encoding="utf-8")) or {}
    for m in data.get("milestones", []):
        if m.get("id") == "m3-code-complete":
            m["linked_work_item_ids"] = [work_item_id]
    milestones_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _set_v2_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    program_document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

