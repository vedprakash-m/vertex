from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
from src.core.file_stores import FileTrajectoryStore
from src.core.models import Confidence, EditionType, RiskLevel
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Dependency, DependencyScheduleStatus, DependencyStatus
from src.core.fact_sor_state import save_fact_sor_state
from src.core.models_v2 import DependencyType, Milestone, MilestoneAssessment, MilestoneStatus, SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus, Signal, SignalReviewDecision, TrajectoryPoint
from src.core.pipeline import StageContext, capture_milestone_trajectory_snapshot
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
from tests.support.report_test_setup import disable_kusto_in_report_copy
from tests.unit.test_commands_report import _sample_items
from tests.unit.test_config_loader import _write_demo_v2_layout


EDITION_NAME = "acme_weekly"


def _stage_pipeline_workspace(workspace_root: Path) -> Path:
    editions_root, programs_root = _write_demo_v2_layout(workspace_root)
    program_dir = programs_root / "demo"
    program_dir = program_dir.rename(programs_root / "acme")
    program_path = program_dir / "program.yaml"
    program = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program.update(id="acme", name="Synthetic Acme", storage_backend="file")
    program["ado"]["area_paths"] = ["One\\Adventure\\Acme"]
    program["kusto"]["enabled"] = False
    program_path.write_text(yaml.safe_dump(program, sort_keys=False), encoding="utf-8")

    edition_path = editions_root / "demo_weekly.yaml"
    edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    edition.update(id=EDITION_NAME, program_id="acme", name="Synthetic Acme Issue {issue_number}")
    canonical_editions = program_dir / "editions"
    canonical_editions.mkdir()
    (canonical_editions / f"{EDITION_NAME}.yaml").write_text(
        yaml.safe_dump(edition, sort_keys=False), encoding="utf-8",
    )
    edition_path.unlink()

    documents = {
        "workstreams.yaml": {
            "schema_version": "2.0",
            "workstreams": [
                {
                    "id": "deployment_readiness",
                    "name": "Deployment Readiness",
                    "area_paths": ["One\\Adventure\\Acme\\Deployment"],
                    "dri_email": "demo@example.com",
                },
                {
                    "id": "networking",
                    "name": "Networking",
                    "area_paths": ["One\\Adventure\\Contoso\\Networking"],
                    "dri_email": "demo@example.com",
                },
            ],
        },
        "scorecards.yaml": {
            "schema_version": "2.0",
            "scorecards": [
                {
                    "name": "Synthetic Delivery",
                    "dimensions": [
                        {
                            "name": "Deployment",
                            "workstream_id": "deployment_readiness",
                            "ado_filter": "area_path contains 'Acme'",
                        },
                    ],
                },
            ],
        },
        "milestones.yaml": {
            "schema_version": "1.0",
            "milestones": [
                {
                    "id": "m3-code-complete",
                    "name": "Synthetic code complete",
                    "target_date": "2026-05-18",
                    "owner_alias": "demo",
                    "status": "on_track",
                    "exit_criteria": ["Code complete"],
                    "linked_workstream_ids": ["deployment_readiness"],
                    "linked_work_item_ids": [900001],
                },
            ],
        },
        Path("knowledge") / "golden_queries.yaml": {"schema_version": "1.0", "queries": []},
    }
    for name, document in documents.items():
        (program_dir / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    reports_root = workspace_root / "reports"
    reports_root.mkdir()
    return reports_root


def _exampletag1_items(as_of: datetime) -> tuple:
    from dataclasses import replace
    return tuple(replace(item, tags=[*item.tags, "EXAMPLETAG1"]) for item in _sample_items(as_of))


@pytest.mark.parametrize("program_backed", [False, True])
def test_lookback_stage_forwards_selected_program_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool,
) -> None:
    from unittest.mock import Mock

    root = tmp_path / "named-home"
    generator = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(report_module, "_generate_lookback_draft", generator)
    ctx = StageContext(
        edition_name=EDITION_NAME, bundle=SimpleNamespace(), archive_index=SimpleNamespace(),
        resolved_issue_number=7, resolved_edition_type=EditionType.LOOKBACK,
        started_at=datetime(2026, 5, 5, tzinfo=timezone.utc), data_as_of=datetime(2026, 5, 5, tzinfo=timezone.utc),
        reports_root=tmp_path / "reports", archive_root=tmp_path / "archive", programs_root=root,
        resolved_v2=SimpleNamespace() if program_backed else None,
    )
    result = report_module._LookbackStage().execute(ctx)
    assert result.artifacts is generator.return_value
    assert generator.call_args.kwargs.get("programs_root") == (root if program_backed else None)


@pytest.mark.parametrize("program_backed", [False, True])
def test_output_stage_fact_pin_uses_selected_program_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool,
) -> None:
    from unittest.mock import Mock

    root = tmp_path / "named-home"
    pin = Mock(side_effect=RuntimeError("Selected output pin boundary"))
    monkeypatch.setattr(report_module, "_pin_program_fact_snapshot", pin)
    monkeypatch.setattr(report_module, "_runtime_db_root_for_reports", Mock(wraps=report_module._runtime_db_root_for_reports))
    ctx = StageContext(
        edition_name=EDITION_NAME, reports_root=tmp_path / "elsewhere" / "reports",
        archive_root=tmp_path / "archive", editions_root=tmp_path / "editions", programs_root=root,
        resolved_v2=SimpleNamespace(program=SimpleNamespace(id="fixture")) if program_backed else None,
        fact_snapshot_pin=SimpleNamespace() if program_backed else None, resolved_issue_number=7,
        resolved_edition_type=EditionType.DETAILED, started_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        data_as_of=datetime(2026, 5, 5, tzinfo=timezone.utc), overrides_path=tmp_path / "overrides.yaml",
        narratives_dir=tmp_path / "narratives", override_snapshot={}, render_exec_summary_text="",
        render_workstream_blurbs={}, render_state=SimpleNamespace(), validation_state=SimpleNamespace(),
    )
    with pytest.raises(RuntimeError, match="Selected output pin boundary"):
        report_module._OutputStage().execute(ctx)
    reader = report_module._runtime_db_root_for_reports
    assert reader.call_args.kwargs.get("programs_root") == (root if program_backed else None)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("program_backed", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_dormant_assembly_artifacts_respect_context_without_reactivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool, missing: bool,
) -> None:
    from dataclasses import replace
    from unittest.mock import Mock
    from src.commands.report_pipeline import assemble_stage as assembly
    from src.core.narrative_store import write_narrative_section
    from src.core.overrides_store import OverridesDocument, save_overrides

    reports = _stage_pipeline_workspace(tmp_path)
    request = report_module._build_stage_request_context(
        edition_name=EDITION_NAME, issue_number=7, reseed=False, no_seed=True, dry_run=True,
        offline=False, diff_mode=False, as_of=datetime(2026, 5, 5, tzinfo=timezone.utc),
        edition_type_override=None, lookback_range=None, section_filter_ids=(),
        reports_root=reports, archive_root=tmp_path / "archive", programs_root=tmp_path / "programs",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=None, open_browser=False,
    )
    ctx = ResolutionStage().execute(request)
    for stage in (FetchStage(), ComputeStage(), NarrativeStage(), AIStage(), RenderStage(), ValidationStage()):
        ctx = stage.execute(ctx)
    root = (tmp_path / "programs").rename(tmp_path / "named-home")
    selected = root if program_backed else None
    reports = tmp_path / "elsewhere" / "reports"
    if not program_backed:
        import shutil
        shutil.copytree(root, reports.parent / "programs")
    save_overrides(
        EDITION_NAME, OverridesDocument(issue_number=7, top_3_now=(), scorecards=(), edition_intro="selected"),
        reports, programs_root=selected,
    )
    write_narrative_section(EDITION_NAME, 7, "exec_summary", "selected summary", reports, programs_root=selected)
    if program_backed:
        write_narrative_section(EDITION_NAME, 7, "exec_summary", "decoy", reports)
        decoy = reports / EDITION_NAME / "narratives" / "issue_007" / "exec_summary.md"
        decoy_bytes = decoy.read_bytes()
    ctx = replace(
        ctx, programs_root=root, reports_root=reports, resolved_v2=ctx.resolved_v2 if program_backed else None,
        default_exec_summary=None if missing else ctx.default_exec_summary,
        evidence_by_item=None if missing else ctx.evidence_by_item,
        narratives_dir=None if missing else ctx.narratives_dir,
    )
    readers = {}
    for name in ("load_overrides", "get_overrides_path", "load_narratives", "merge_narratives", "build_workstream_narrative_history"):
        readers[name] = Mock(wraps=getattr(assembly, name))
        monkeypatch.setattr(assembly, name, readers[name])
    monkeypatch.setattr(assembly, "_load_report_signal_context", Mock(return_value=ctx.signal_context))
    forecast = Mock(wraps=assembly._load_eta_forecasts)
    monkeypatch.setattr(assembly, "_load_eta_forecasts", forecast)
    pin = Mock(side_effect=RuntimeError("Dormant assembly output boundary"))
    monkeypatch.setattr(assembly, "_pin_program_fact_snapshot", pin)
    runtime = Mock(wraps=assembly._runtime_db_root_for_reports)
    monkeypatch.setattr(assembly, "_runtime_db_root_for_reports", runtime)
    with pytest.raises(RuntimeError, match="Dormant assembly output boundary"):
        assembly._generate_report_draft_from_context(ctx)
    assert runtime.call_args.kwargs.get("programs_root") == selected
    if missing:
        assert forecast.called
        assert forecast.call_args.kwargs.get("programs_root") == selected
    else:
        forecast.assert_not_called()
    for reader in readers.values():
        if missing:
            assert reader.called
            assert all(call.kwargs.get("programs_root") == selected for call in reader.call_args_list)
        else:
            reader.assert_not_called()
    if program_backed:
        assert decoy.read_bytes() == decoy_bytes
    assert [stage.name() for stage in report_module._report_pipeline_stages(EditionType.DETAILED)][-1] == "output"


@pytest.mark.parametrize("program_backed", [False, True])
@pytest.mark.parametrize("seed_mode", ["seed", "no_seed", "sentinel"])
def test_narrative_stage_selected_artifacts_and_legacy_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool, seed_mode: str,
) -> None:
    from unittest.mock import Mock
    from src.core import narrative_store
    from src.core.stages import narrative_stage
    from src.core.overrides_store import OverridesDocument
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    reports, archive = tmp_path / "elsewhere" / "reports", tmp_path / "archive"
    selected = root if program_backed else None
    for issue in (1, 2):
        narrative_store.write_narrative_section("fixture_weekly", issue, "delivery", f"prior {issue}", reports, programs_root=selected)
        narrative_store.archive_narratives("fixture_weekly", issue, archive, reports, programs_root=selected)
    if program_backed:
        narrative_store.write_narrative_section("fixture_weekly", 3, "exec_summary", "decoy", reports)
        decoy = reports / "fixture_weekly" / "narratives" / "issue_003" / "exec_summary.md"
        decoy_bytes = decoy.read_bytes()
    if seed_mode == "sentinel":
        target = narrative_store.get_narratives_dir("fixture_weekly", 3, reports, programs_root=selected)
        target.mkdir(parents=True)
        (target / narrative_store.NO_SEED_SENTINEL_FILENAME).touch()
    monkeypatch.setattr(
        narrative_store, "read_archive_index",
        lambda *_args, **_kwargs: SimpleNamespace(issues=tuple(
            SimpleNamespace(kind="confirmed", issue_number=i) for i in (1, 2)
        )),
    )
    sync = Mock(wraps=narrative_stage.sync_published_baseline_to_target)
    monkeypatch.setattr(narrative_stage, "sync_published_baseline_to_target", sync)
    support = Mock()
    support.build_top_items.return_value = ()
    support.build_auto_suggested_top_items.return_value = ()
    support.is_continuity_layout.return_value = False
    support.visible_continuity_chapters.return_value = ()
    support.build_workstream_templates.return_value = {"ws_delivery.md": "fresh"}
    support.build_exec_summary_template.return_value = "fresh summary"
    support.visible_detail_section_ids.return_value = {"delivery"}
    support.active_workstream_blurbs.side_effect = lambda narratives, _ids: {"delivery": narratives["ws_delivery.md"].strip()}
    ctx = StageContext(
        edition_name="fixture_weekly", reports_root=reports, archive_root=archive, programs_root=root,
        resolved_v2=SimpleNamespace() if program_backed else None,
        bundle=SimpleNamespace(
            chapter_contract=None, slice_contracts=None, config=SimpleNamespace(layout_mode="default"),
            program_context=SimpleNamespace(workstreams=(SimpleNamespace(name="Delivery"),)),
        ), resolved_issue_number=3, trusted_baseline_issue_number=2, no_seed=seed_mode == "no_seed",
        resolved_edition_type=EditionType.DETAILED,
        overrides_document=OverridesDocument(issue_number=3, top_3_now=(), scorecards=()),
        scorecards=(), scorecard_packets={}, scorecard_deltas=(), deltas=SimpleNamespace(),
        default_exec_summary="default", stage_support=support,
    )
    result = NarrativeStage().execute(ctx)
    target = narrative_store.get_narratives_dir("fixture_weekly", 3, reports, programs_root=selected)
    assert result.narratives_dir == target
    assert result.workstream_narrative_history["Delivery"][1:] == ("prior 2", "prior 1")
    if seed_mode == "seed":
        assert result.narrative_seeding is not None
        assert "prior 2" in result.loaded_narratives["ws_delivery.md"]
        assert sync.call_args.kwargs.get("programs_root") == selected
    else:
        assert result.narrative_seeding is None
        assert result.loaded_narratives["ws_delivery.md"] == "fresh\n"
        sync.assert_not_called()
    if program_backed:
        assert decoy.read_bytes() == decoy_bytes


@pytest.mark.parametrize("program_backed", [False, True])
def test_resolution_stage_reseed_forwards_artifact_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool,
) -> None:
    from unittest.mock import Mock
    from src.core.stages import resolution_stage
    from src.core.overrides_store import OverridesDocument, save_overrides, get_overrides_path
    from src.core.narrative_store import write_narrative_section, get_narratives_dir

    reports = _stage_pipeline_workspace(tmp_path)
    root, archive = tmp_path / "programs", tmp_path / "archive"
    selected = root if program_backed else None
    write_narrative_section(EDITION_NAME, 2, "exec_summary", "discard", reports, programs_root=selected)
    save_overrides(EDITION_NAME, OverridesDocument(issue_number=1, top_3_now=(), scorecards=(), edition_intro="prior"), reports)
    save_overrides(EDITION_NAME, OverridesDocument(issue_number=2, top_3_now=(), scorecards=()), reports)
    narrative_delete = Mock(wraps=resolution_stage.delete_narratives_dir)
    override_delete = Mock(wraps=resolution_stage.delete_seedable_overrides)
    seed = Mock(wraps=resolution_stage.seed_overrides_from_prior)
    monkeypatch.setattr(resolution_stage, "delete_narratives_dir", narrative_delete)
    monkeypatch.setattr(resolution_stage, "delete_seedable_overrides", override_delete)
    monkeypatch.setattr(resolution_stage, "seed_overrides_from_prior", seed)
    monkeypatch.setattr(resolution_stage, "load_trusted_baseline_issue", lambda *_args, **_kwargs: 1)
    if not program_backed:
        monkeypatch.setattr(resolution_stage, "resolve_edition", lambda *_args, **_kwargs: None)
    result = ResolutionStage().execute(StageContext(
        edition_name=EDITION_NAME, issue_number=2, reseed=True, reports_root=reports, archive_root=archive,
        programs_root=root, as_of=datetime(2026, 5, 5, tzinfo=timezone.utc),
    ))
    for call in (narrative_delete, override_delete, seed):
        assert call.call_args.kwargs.get("programs_root") == selected
    assert not get_narratives_dir(EDITION_NAME, 2, reports, programs_root=selected).exists()
    assert get_overrides_path(EDITION_NAME, reports, issue_number=2, programs_root=selected).exists()
    assert result.overrides_seeding.seeded


def test_resolution_stage_keeps_custom_root_admission_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock
    from src.core.stages import resolution_stage
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    reader = Mock(side_effect=AssertionError("Admission must precede bundle loading"))
    monkeypatch.setattr(resolution_stage, "load_bundle", reader)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(ConfigError, match="Conflicting report workspace roots"):
        ResolutionStage().execute(StageContext(
            edition_name="fixture_weekly", issue_number=7, reseed=True, programs_root=root,
            reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
        ))
    reader.assert_not_called()
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not (tmp_path / "reports").exists()
    assert not (tmp_path / "archive").exists()


def test_resolution_stage_populates_report_context(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
        work_item_loader=lambda bundle, timestamp: (_exampletag1_items(timestamp), 0),
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    if milestone_ctx.milestone_assessments:
        assert milestone_ctx.milestone_trajectory_snapshot is not None
        assert milestone_ctx.milestone_trajectory_snapshot["backend"] in {"file", "sqlite"}
        assert milestone_ctx.milestone_trajectory_snapshot["sha256"].startswith("sha256:")
    else:
        assert milestone_ctx.milestone_trajectory_snapshot is None
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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


@pytest.mark.parametrize("pipeline", ["staged", "legacy"])
@pytest.mark.parametrize("condition", ["offline", "runtime_disabled"])
def test_ai_enabled_report_skips_reality_summary_without_ai_synthesis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pipeline: str, condition: str,
) -> None:
    from dataclasses import replace
    from unittest.mock import Mock

    from src.commands.report_pipeline import assemble_stage as assembly
    from tests.unit.test_commands_report import _enable_v2_program_ai

    reports_root = _stage_pipeline_workspace(tmp_path)
    _enable_v2_program_ai(tmp_path / "programs")
    summary_builder = Mock(side_effect=AssertionError("AI summary must not be built"))
    monkeypatch.setattr("src.commands.report_ai.build_reality_assessment_summary", summary_builder)
    request = report_module._build_stage_request_context(
        edition_name=EDITION_NAME, issue_number=1, reseed=False, no_seed=False, dry_run=True,
        offline=False, diff_mode=False, as_of=datetime(2026, 5, 5, tzinfo=timezone.utc),
        edition_type_override=None, lookback_range=None, section_filter_ids=(),
        reports_root=reports_root, archive_root=tmp_path / "archive", programs_root=tmp_path / "programs",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=None, open_browser=False,
    )
    ctx = ResolutionStage().execute(request)
    for stage in (FetchStage(), ComputeStage(), MilestoneStage(), NarrativeStage()):
        ctx = stage.execute(ctx)
    ctx = replace(ctx, offline=condition == "offline")
    assert ctx.bundle.config.ai.enabled

    mode = AIMode.DISABLED if condition == "runtime_disabled" else AIMode.ACTIVE
    set_ai_mode(mode)
    try:
        if pipeline == "staged":
            draft_loader = Mock(wraps=ctx.stage_support.load_draft_ai_context)
            ctx = replace(ctx, stage_support=replace(ctx.stage_support, load_draft_ai_context=draft_loader))
            result = AIStage().execute(ctx).ai_synthesis
            draft_loader.assert_not_called()
        else:
            draft_loader = Mock(wraps=assembly._load_draft_ai_context)
            monkeypatch.setattr(assembly, "_load_draft_ai_context", draft_loader)
            captured = []

            class _StopAtRender:
                def execute(self, render_ctx):
                    captured.append(render_ctx.ai_synthesis)
                    raise RuntimeError("AI synthesis boundary")

            monkeypatch.setattr(assembly, "RenderStage", _StopAtRender)
            with pytest.raises(RuntimeError, match="AI synthesis boundary"):
                assembly._generate_report_draft_from_context(ctx)
            result = captured[0]
            assert draft_loader.call_count == (1 if condition == "runtime_disabled" else 0)
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert result.exec_summary_text == ctx.exec_summary_text
    assert result.workstream_blurbs == ctx.workstream_blurbs
    assert result.warnings == (
        ("AI synthesis disabled by --no-ai / AIMode.DISABLED.",)
        if condition == "runtime_disabled" else ()
    )
    summary_builder.assert_not_called()


def test_render_stage_populates_render_outputs(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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


def test_render_stage_deck_reuses_ctx_risks_when_available(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = _stage_pipeline_workspace(tmp_path)
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
        edition_type_override=EditionType.DECK,
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
    narrative_ctx = NarrativeStage().execute(risk_ctx)
    ai_ctx = AIStage().execute(narrative_ctx)

    monkeypatch.setattr(
        render_stage_module,
        "_load_current_risks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RenderStage should reuse ctx.risks")),
    )

    render_ctx = RenderStage().execute(ai_ctx)

    assert render_ctx.render_state is not None


def test_validation_stage_populates_manifest_and_warnings(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
    reports_root = _stage_pipeline_workspace(tmp_path)
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
        "contoso",
        mode="legacy",
        family_modes={"workitem.state": "shadow"},
        recorded_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    milestone = Milestone(
        id="m-reality",
        program_id="contoso",
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
                    truth_level=SimpleNamespace(value="source_validated"),
                    disputed=False,
                    stale=False,
                ),
            )

        def dependencies(self):
            return ()

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
        edition_name="example_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="contoso")),
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
        data_as_of=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=_load_program_reality),
    )

    result = MilestoneStage().execute(ctx)

    assert [call["program_id"] for call in calls] == ["contoso"]
    assert calls[0]["as_of"] == ctx.data_as_of
    assert calls[0]["edition_name"] == "example_weekly"
    assert calls[0]["archive_root"] == tmp_path / "archive"
    assert result.milestones == (milestone,)
    assert result.milestone_lineage == {
        "m-reality": {
            "source_document_key": "email:sha256:milestone-source",
            "approval_event_id": "evt-approval-1",
            "truth_level": "source_validated",
            "disputed": "false",
            "stale": "false",
        }
    }
    assert result.milestone_assessments[0].milestone_id == "m-reality"
    assert result.milestone_warnings == ()


def test_milestone_stage_requires_audited_rollback_flag_for_reality_failure(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "contoso",
        mode="legacy",
        family_modes={"workitem.state": "primary"},
        recorded_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )

    def _broken_reality(*_args, **_kwargs):
        raise RuntimeError("facade unavailable")

    ctx = StageContext(
        edition_name="example_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="contoso")),
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
        "contoso",
        mode="legacy",
        family_modes={"workitem.state": "primary"},
        recorded_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    milestone = Milestone(
        id="m-legacy",
        program_id="contoso",
        name="Legacy rollback milestone",
        target_date=date(2026, 7, 31),
        owner_alias="operator",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=(),
        linked_workstream_ids=(),
        linked_work_item_ids=(),
    )

    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    (programs_root / "contoso" / "milestones.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "1.0",
            "milestones": [{
                "id": milestone.id,
                "name": milestone.name,
                "target_date": milestone.target_date.isoformat(),
                "owner_alias": milestone.owner_alias,
                "status": milestone.status.value,
                "exit_criteria": [],
                "linked_workstream_ids": [],
                "linked_work_item_ids": [],
            }],
        }),
        encoding="utf-8",
    )
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
        edition_name="example_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="contoso")),
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


def test_report_draft_retains_authored_milestone_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from unittest.mock import Mock

    from src.commands.doctor_checks.storage_checks import _render_manifest_sor_consistency_check
    from src.core.manifest_writer import write_run_manifest
    from src.core.program_fact_store import (
        ProgramFactInput, ProgramFactStore, build_legacy_program_fact_snapshot,
    )

    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    authored = build_legacy_program_fact_snapshot(
        "acme", programs_root=programs_root, fact_types=("milestone.entry",),
    ).facts[0]
    payload = {**authored.payload, "name": "Stored conflicting milestone"}
    ProgramFactStore("acme").append_fact(ProgramFactInput(
        fact_type=authored.fact_type, entity_refs=authored.entity_refs, payload=payload,
        scope=authored.scope, natural_key=authored.natural_key,
    ))
    save_fact_sor_state(
        "acme", mode="legacy", family_modes={"workitem.state": "primary"},
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc), programs_root=programs_root,
    )
    loader = Mock(side_effect=RuntimeError("Synthetic reality failure"))
    monkeypatch.setattr(report_module, "_load_program_reality", loader)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
        archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        dry_run=True, work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert "Synthetic code complete" in artifacts.html_body
    assert "Stored conflicting milestone" not in artifacts.html_body
    assert any("audited rollback flag" in warning for warning in artifacts.warnings)
    observed = artifacts.manifest.metadata["program_verdict_observation"]
    assert [milestone.name for milestone in observed.inputs.milestones] == ["Synthetic code complete"]
    assert any("audited rollback flag" in warning for warning in observed.inputs.milestone_warnings)
    assert "milestone_input_degraded" in {gap.code for gap in observed.verdict.evidence_gaps}
    assert dict(observed.inputs.capture.effective_family_modes)["workitem.state"] == "primary"
    write_run_manifest(EDITION_NAME, artifacts.manifest.issue_number, artifacts.manifest, programs_root=programs_root)
    check = _render_manifest_sor_consistency_check(
        "acme", edition_name=EDITION_NAME, programs_root=programs_root,
        latest_confirmed_issue_number=artifacts.manifest.issue_number,
    )
    assert check.status == "warn"
    assert check.metadata["mismatches"] == {
        "milestone": {"manifest_recorded": "legacy", "current": "reality"},
        "dependency": {"manifest_recorded": "legacy", "current": "reality"},
    }
    assert artifacts.manifest.metadata["family_read_paths"]["milestone"] == "legacy"
    assert artifacts.manifest.metadata["family_read_paths"]["dependency"] == "legacy"
    loader.assert_called_once()


def test_report_draft_records_nonempty_authored_risk_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from unittest.mock import Mock

    from src.commands.doctor_checks.storage_checks import _render_manifest_sor_consistency_check
    from src.core.manifest_writer import write_run_manifest
    from src.core.program_fact_store import (
        ProgramFactInput, ProgramFactStore, build_legacy_program_fact_snapshot,
    )

    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", "1")
    risk_path = programs_root / "acme" / "risk_register.yaml"
    risk_path.write_text(yaml.safe_dump({
        "schema_version": "1.0",
        "risks": [{
            "id": "risk-1", "title": "Authored risk source", "description": "Synthetic risk",
            "probability": "likely", "impact": "high", "category": "schedule",
            "owner_alias": "operator", "status": "open", "identified_date": "2026-05-01",
        }],
    }), encoding="utf-8")
    authored = build_legacy_program_fact_snapshot(
        "acme", programs_root=programs_root, fact_types=("risk.entry",),
    ).facts[0]
    ProgramFactStore("acme").append_fact(ProgramFactInput(
        fact_type=authored.fact_type, entity_refs=authored.entity_refs,
        payload={**authored.payload, "title": "Stored conflicting risk"},
        scope=authored.scope, natural_key=authored.natural_key,
    ))
    save_fact_sor_state(
        "acme", mode="legacy", family_modes={"judgment": "primary"},
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc), programs_root=programs_root,
    )
    loader = Mock(side_effect=RuntimeError("Synthetic reality failure"))
    monkeypatch.setattr(report_module, "_load_program_reality", loader)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
        archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        dry_run=True, work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert "Authored risk source" in artifacts.html_body
    assert "Stored conflicting risk" not in artifacts.html_body
    assert any("audited rollback flag" in warning for warning in artifacts.warnings)
    observed = artifacts.manifest.metadata["program_verdict_observation"]
    assert [risk.title for risk in observed.inputs.risks] == ["Authored risk source"]
    assert any("audited rollback flag" in warning for warning in observed.inputs.risk_warnings)
    assert "risk_input_degraded" in {gap.code for gap in observed.verdict.evidence_gaps}
    assert dict(observed.inputs.capture.effective_family_modes)["judgment"] == "primary"
    assert artifacts.manifest.metadata["family_read_paths"]["risk"] == "legacy"
    write_run_manifest(EDITION_NAME, artifacts.manifest.issue_number, artifacts.manifest, programs_root=programs_root)
    check = _render_manifest_sor_consistency_check(
        "acme", edition_name=EDITION_NAME, programs_root=programs_root,
        latest_confirmed_issue_number=artifacts.manifest.issue_number,
    )
    assert check.status == "warn"
    assert check.metadata["mismatches"] == {"risk": {"manifest_recorded": "legacy", "current": "reality"}}
    loader.assert_called_once()


@pytest.mark.parametrize("comparison", ["empty", "nonempty", "unavailable", "invalid"])
def test_report_draft_reuses_successful_empty_risks_at_selected_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, comparison: str,
) -> None:
    from unittest.mock import Mock

    from src.commands import report_health
    from src.core.program_fact_store import ProgramFactStore
    from tests.unit.test_reality_pinned_facts import _risk

    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", raising=False)
    save_fact_sor_state(
        "acme", mode="legacy", family_modes={"judgment": "primary"},
        recorded_at=as_of, programs_root=programs_root,
    )
    ProgramFactStore("acme").append_fact(
        _risk("Risk recorded after the selected time"), recorded_at=as_of + timedelta(days=1),
    )
    risk_path = programs_root / "acme" / "risk_register.yaml"
    if comparison == "nonempty":
        risk_path.write_text(yaml.safe_dump({
            "schema_version": "1.0", "risks": [{
                "id": "authored-risk", "title": "Authored comparison risk", "description": "Synthetic risk",
                "probability": "likely", "impact": "high", "category": "schedule",
                "owner_alias": "operator", "status": "open", "identified_date": "2026-05-01",
            }],
        }), encoding="utf-8")
    elif comparison == "invalid":
        risk_path.write_text("schema_version: '2.0'\nrisks: []\n", encoding="utf-8")
    elif comparison == "unavailable":
        monkeypatch.setattr(
            risk_stage_module, "_load_legacy_risks",
            Mock(side_effect=ConfigError("Synthetic comparison unavailable")),
        )

    stage_loader = Mock(wraps=report_module._load_program_reality)
    monkeypatch.setattr(report_module, "_load_program_reality", stage_loader)
    health_loader = Mock(wraps=report_health.ProgramReality.load)
    monkeypatch.setattr(report_health, "ProgramReality", SimpleNamespace(load=health_loader))
    health_results = []

    def observe_health(*args, **kwargs):
        health = report_health._build_health_summary(*args, **kwargs)
        health_results.append(health)
        return health

    monkeypatch.setattr(report_module, "_build_health_summary", observe_health)
    def run_draft():
        return generate_report_draft(
            edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
            archive_root=archive_root, as_of=as_of, dry_run=True,
            work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
        )

    if comparison == "invalid":
        with pytest.raises(ConfigError, match="Unsupported risk register schema_version"):
            run_draft()
        assert len(health_results) == 1
        assert health_results[0].risk_register_summary is None
        health_loader.assert_not_called()
        return
    artifacts = run_draft()

    stage_loader.assert_called_once_with(
        "acme", programs_root=programs_root, as_of=as_of,
        edition_name=EDITION_NAME, archive_root=archive_root,
    )
    assert len(health_results) == 1
    assert health_results[0].risk_register_summary is None
    assert "Risk recorded after the selected time" not in artifacts.html_body
    observed = artifacts.manifest.metadata["program_verdict_observation"]
    assert observed.inputs.risks == ()
    assert artifacts.manifest.metadata["family_read_paths"]["risk"] == "reality"
    if comparison == "nonempty":
        assert any("cross-check" in warning for warning in observed.inputs.risk_warnings)
    elif comparison == "unavailable":
        assert any("cross-check unavailable" in warning for warning in observed.inputs.risk_warnings)
    else:
        assert observed.inputs.risk_warnings == ()
    health_loader.assert_not_called()


@pytest.mark.parametrize("change_after_selection", ["none", "add", "invalidate"])
def test_report_draft_reuses_successful_empty_legacy_risks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change_after_selection: str,
) -> None:
    from unittest.mock import Mock

    from src.commands import report_health
    from src.core import quality_gates

    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    save_fact_sor_state("acme", mode="legacy", recorded_at=as_of, programs_root=programs_root)
    original_risks = risk_stage_module._load_current_risks
    selections = []

    def select_then_maybe_add(*args, **kwargs):
        records = original_risks(*args, **kwargs)
        selections.append(records)
        if change_after_selection == "add":
            (programs_root / "acme" / "risk_register.yaml").write_text(yaml.safe_dump({
                "schema_version": "1.0", "risks": [{
                    "id": "late-risk", "title": "Risk added after successful legacy selection",
                    "description": "Synthetic risk", "probability": "likely", "impact": "high",
                    "category": "schedule", "owner_alias": "operator", "status": "open",
                    "identified_date": "2026-05-01",
                }],
            }), encoding="utf-8")
        elif change_after_selection == "invalidate":
            (programs_root / "acme" / "risk_register.yaml").write_text(
                "schema_version: '2.0'\nrisks: []\n", encoding="utf-8",
            )
        return records

    monkeypatch.setattr(risk_stage_module, "_load_current_risks", select_then_maybe_add)
    health_loader = Mock(wraps=report_health.ProgramReality.load)
    monkeypatch.setattr(report_health, "ProgramReality", SimpleNamespace(load=health_loader))
    gate_loader = Mock(wraps=quality_gates._load_current_risks)
    monkeypatch.setattr(quality_gates, "_load_current_risks", gate_loader)
    health_results = []

    def observe_health(*args, **kwargs):
        health = report_health._build_health_summary(*args, **kwargs)
        health_results.append(health)
        return health

    monkeypatch.setattr(report_module, "_build_health_summary", observe_health)
    def run_draft():
        return generate_report_draft(
            edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
            archive_root=archive_root, as_of=as_of, dry_run=True,
            work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
        )

    if change_after_selection == "invalidate":
        with pytest.raises(ConfigError, match="Unsupported risk register schema_version"):
            run_draft()
        assert selections == [()]
        assert len(health_results) == 1
        assert health_results[0].risk_register_summary is None
        health_loader.assert_not_called()
        gate_loader.assert_called_once_with("acme", programs_root=programs_root)
        return
    artifacts = run_draft()

    assert selections == [()]
    assert len(health_results) == 1
    assert health_results[0].risk_register_summary is None
    assert "Risk added after successful legacy selection" not in artifacts.html_body
    observed = artifacts.manifest.metadata["program_verdict_observation"]
    assert observed.inputs.risks == ()
    assert observed.inputs.risk_warnings == ()
    assert artifacts.manifest.metadata["family_read_paths"]["risk"] == "legacy"
    health_loader.assert_not_called()
    gate_loader.assert_called_once_with("acme", programs_root=programs_root)


@pytest.mark.parametrize("outcome", ["typed_loader_error", "typed_accessor_error", "empty_rollback", "legacy_error"])
def test_report_draft_keeps_existing_empty_risk_fallback_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str,
) -> None:
    from unittest.mock import Mock

    from src.commands import report_health
    from tests.unit.test_report_deck_reality_consolidation import _risk_assessment

    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", raising=False)
    family_modes = {} if outcome == "legacy_error" else {"judgment": "primary"}
    save_fact_sor_state("acme", mode="legacy", family_modes=family_modes, recorded_at=as_of, programs_root=programs_root)
    if outcome == "empty_rollback":
        monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", "1")
        stage_loader = Mock(side_effect=RuntimeError("Synthetic risk failure"))
    elif outcome == "typed_accessor_error":
        stage_loader = Mock(return_value=SimpleNamespace(risks=Mock(side_effect=ConfigError("Synthetic risk failure"))))
    else:
        stage_loader = Mock(side_effect=ConfigError("Synthetic risk failure"))
    if outcome == "legacy_error":
        monkeypatch.setattr(
            risk_stage_module, "_load_current_risks", Mock(side_effect=ConfigError("Synthetic legacy risk failure")),
        )
    monkeypatch.setattr(report_module, "_load_program_reality", stage_loader)
    assessment = _risk_assessment()
    health_loader = Mock(return_value=SimpleNamespace(risks=lambda: (assessment,)))
    monkeypatch.setattr(report_health, "ProgramReality", SimpleNamespace(load=health_loader))
    health_results = []

    def observe_health(*args, **kwargs):
        health = report_health._build_health_summary(*args, **kwargs)
        health_results.append(health)
        return health

    monkeypatch.setattr(report_module, "_build_health_summary", observe_health)
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
        archive_root=archive_root, as_of=as_of, dry_run=True,
        work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
    )

    assert health_results[0].risk_register_summary is not None
    observed = artifacts.manifest.metadata["program_verdict_observation"]
    assert observed.inputs.risks == ()
    if outcome.startswith("typed_"):
        assert any("risk skipped (reality)" in warning for warning in observed.inputs.risk_warnings)
    elif outcome == "empty_rollback":
        assert any("audited rollback flag" in warning for warning in observed.inputs.risk_warnings)
    else:
        assert observed.inputs.risk_warnings == ("risk skipped: Synthetic legacy risk failure",)
    health_loader.assert_called_once_with("acme", programs_root=programs_root, archive_root=archive_root)
    assert stage_loader.call_count == (0 if outcome == "legacy_error" else 1)


def test_report_draft_still_refuses_invalid_legacy_risk_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    save_fact_sor_state("acme", mode="legacy", recorded_at=as_of, programs_root=programs_root)
    (programs_root / "acme" / "risk_register.yaml").write_text(
        "schema_version: '2.0'\nrisks: []\n", encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unsupported risk register schema_version"):
        generate_report_draft(
            edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
            archive_root=tmp_path / "archive", as_of=as_of, dry_run=True,
            work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
        )


def test_report_still_refuses_fact_added_after_successful_empty_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from unittest.mock import Mock

    from src.commands import report_health
    from src.core.program_fact_store import ProgramFactStore
    from tests.unit.test_reality_pinned_facts import _risk

    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    save_fact_sor_state("acme", mode="primary", recorded_at=as_of, programs_root=programs_root)
    stage_calls = []
    original_loader = report_module._load_program_reality

    def add_fact_after_capture(*args, **kwargs):
        reality = original_loader(*args, **kwargs)
        if not stage_calls:
            ProgramFactStore("acme").append_fact(
                _risk("Risk added after capture"), recorded_at=datetime.now(timezone.utc),
            )
        stage_calls.append(kwargs)
        return reality

    monkeypatch.setattr(report_module, "_load_program_reality", add_fact_after_capture)
    health_loader = Mock(wraps=report_health.ProgramReality.load)
    monkeypatch.setattr(report_health, "ProgramReality", SimpleNamespace(load=health_loader))
    health_results = []

    def observe_health(*args, **kwargs):
        health = report_health._build_health_summary(*args, **kwargs)
        health_results.append(health)
        return health

    monkeypatch.setattr(report_module, "_build_health_summary", observe_health)
    with pytest.raises(ConfigError, match="snapshot changed during report generation"):
        generate_report_draft(
            edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
            archive_root=archive_root, as_of=as_of, dry_run=True,
            work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0), open_browser=False,
        )
    assert stage_calls and stage_calls[0]["fact_snapshot"].facts == ()
    assert stage_calls[0]["as_of"] == as_of
    assert stage_calls[0]["edition_name"] == EDITION_NAME
    assert health_results[0].risk_register_summary is None
    health_loader.assert_not_called()


@pytest.mark.parametrize("invalid", ["milestones.yaml", "dependencies.yaml"])
def test_report_draft_refuses_invalid_milestone_rollback_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid: str,
) -> None:
    from unittest.mock import Mock

    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic operator")
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    save_fact_sor_state(
        "acme", mode="legacy", family_modes={"workitem.state": "primary"},
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc), programs_root=programs_root,
    )
    path = programs_root / "acme" / invalid
    path.write_text("schema_version: '2.0'\n", encoding="utf-8")
    original = path.read_bytes()
    loader = Mock(side_effect=RuntimeError("Synthetic reality failure"))
    monkeypatch.setattr(report_module, "_load_program_reality", loader)

    with pytest.raises(ConfigError, match="schema_version"):
        generate_report_draft(
            edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
            archive_root=tmp_path / "archive", as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
            dry_run=True, work_item_loader=lambda _bundle, timestamp: (_sample_items(timestamp), 0),
            open_browser=False,
        )

    assert path.read_bytes() == original
    loader.assert_called_once()


def test_report_milestone_rows_carry_activation_lineage(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "contoso"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "contoso",
                "name": "CONTOSO",
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
                program_id="contoso",
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
        program_id="contoso",
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


def test_milestone_trajectory_snapshot_preserves_ordered_assessment_projection(tmp_path: Path) -> None:
    store = FileTrajectoryStore(programs_root=tmp_path)
    points = (
        TrajectoryPoint(
            date=date(2026, 5, 5), state="Active", assigned_to="first",
            target_date=date(2026, 6, 1), risk_level=RiskLevel.LOW, area_path="first-path",
        ),
        TrajectoryPoint(
            date=date(2026, 5, 5), state="Closed", assigned_to="second",
            target_date=None, risk_level=RiskLevel.HIGH, area_path="second-path",
        ),
    )
    snapshot = capture_milestone_trajectory_snapshot(
        "acme", store, {900002: (), 900001: points},
    )
    assert snapshot["backend"] == "file"
    assert [item["work_item_id"] for item in snapshot["items"]] == [900001, 900002]
    assert snapshot["items"][0]["points"] == [
        {"date": "2026-05-05", "state": "Active", "target_date": "2026-06-01"},
        {"date": "2026-05-05", "state": "Closed", "target_date": None},
    ]
    assert snapshot["items"][1]["points"] == []


@pytest.mark.parametrize("work_item_id", [0, -1, True])
def test_milestone_trajectory_snapshot_rejects_nonpositive_or_bool_ids(
    tmp_path: Path,
    work_item_id: int,
) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        capture_milestone_trajectory_snapshot(
            "acme", FileTrajectoryStore(programs_root=tmp_path), {work_item_id: ()},
        )


def test_milestone_trajectory_snapshot_rejects_unknown_store_type() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        capture_milestone_trajectory_snapshot("acme", object(), {})


def test_milestone_stage_empty_and_lookback_paths_do_not_read_trajectories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("No milestone assessment may read trajectory history.")

    monkeypatch.setattr(milestone_stage_module, "build_trajectory_store_for_program_id", forbidden)
    monkeypatch.setattr(milestone_stage_module, "retained_report_fact_snapshot", lambda *_args: None)
    monkeypatch.setattr(milestone_stage_module, "resolve_family_sor_mode", lambda *_args, **_kwargs: "legacy")
    monkeypatch.setattr(milestone_stage_module, "_load_current_milestones", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(milestone_stage_module, "_load_current_dependencies", lambda *_args, **_kwargs: ())
    empty = MilestoneStage().execute(
        StageContext(
            resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="acme")),
            programs_root=tmp_path,
            data_as_of=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        )
    )
    lookback = MilestoneStage().execute(
        StageContext(resolved_edition_type=EditionType.LOOKBACK),
    )
    unresolved = MilestoneStage().execute(StageContext())
    assert empty.milestone_assessments == ()
    assert empty.milestone_trajectory_snapshot is None
    assert lookback.milestone_assessments == ()
    assert lookback.milestone_trajectory_snapshot is None
    assert unresolved.milestone_assessments == ()
    assert unresolved.milestone_trajectory_snapshot is None
