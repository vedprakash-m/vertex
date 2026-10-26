from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.exceptions import ConfigError
from src.core.models import EditionType
from src.core.pipeline import StageContext
from src.core.program_fact_store import ProgramFactSnapshot
from src.core.stages import render_stage, sor_gated_load
from src.core.stages.render_stage import RenderStage


@pytest.fixture
def deck_context() -> StageContext:
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    snapshot = ProgramFactSnapshot("acme", as_of, ())
    support = MagicMock()
    support.is_continuity_layout.return_value = True
    support.build_workstream_data.return_value = ()
    bundle = MagicMock()
    bundle.template_contract = None
    return StageContext(
        edition_name="acme_weekly",
        resolved_edition_type=EditionType.DECK,
        resolved_issue_number=1,
        started_at=as_of,
        data_as_of=as_of,
        programs_root=Path("synthetic-programs"),
        reports_root=Path("synthetic-reports"),
        archive_root=Path("synthetic-archive"),
        narratives_dir=Path("synthetic-narratives"),
        bundle=bundle,
        resolved_v2=SimpleNamespace(
            program=SimpleNamespace(id="acme"),
            paths=SimpleNamespace(program_id="acme"),
            workstreams=(),
            raw_program={},
        ),
        archive_index=SimpleNamespace(issues=()),
        overrides_document=SimpleNamespace(removed_sections=(), show_orientation=False),
        top_items=(),
        auto_suggestions=(),
        continuity_chapters=(),
        exec_summary_text="Synthetic assessment",
        workstream_blurbs={},
        workstream_narrative_history={},
        ai_synthesis=SimpleNamespace(workstream_source_footnotes={}),
        render_exec_summary_text="Synthetic assessment",
        render_workstream_blurbs={},
        dimension_risks=(),
        scorecard_deltas=(),
        scorecards={},
        scorecard_packets={},
        evidence_by_item={},
        risk_assessments=(),
        risks=(),
        fact_snapshot=snapshot,
        stage_support=support,
    )


@pytest.fixture
def isolated_render() -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            sor_gated_load, "load_fact_sor_state", return_value=SimpleNamespace(family_modes={}),
        ))
        stack.enter_context(patch.object(
            sor_gated_load, "resolve_fact_sor_mode", return_value="primary",
        ))
        for name in (
            "load_latest_notification_state",
            "build_freshness_report",
            "get_review_status_path",
            "ReportData",
            "build_forecast_assessment",
            "build_default_registry",
            "build_theme_context",
            "build_quality_matrix",
            "build_remediation_report",
            "build_inline_citations",
            "EditionMeta",
            "RenderContext",
        ):
            stack.enter_context(patch.object(render_stage, name))
        stack.enter_context(patch.object(
            render_stage, "build_signal_store_for_program_id", return_value=MagicMock(),
        ))
        stack.enter_context(patch.object(
            render_stage, "build_kusto_sections", return_value=((), (), ()),
        ))
        stack.enter_context(patch.object(
            render_stage, "_apply_kusto_fallbacks", return_value=((), (), ()),
        ))
        stack.enter_context(patch.object(
            render_stage, "_route_chart_placements", return_value=((), None, ()),
        ))
        yield


@pytest.mark.parametrize("input_name", ["fact_snapshot", "as_of", "assessment_as_of", "edition_name"])
@pytest.mark.usefixtures("isolated_render")
def test_deck_reality_uses_report_retained_facts_and_assessment_clock(
    deck_context: StageContext, input_name: str,
) -> None:
    class ReachedDeckReality(Exception):
        pass

    with patch.object(render_stage.ProgramReality, "load", side_effect=ReachedDeckReality) as loader:
        with pytest.raises(ReachedDeckReality):
            RenderStage().execute(deck_context)
    if input_name == "fact_snapshot":
        assert loader.call_args.kwargs.get(input_name) is deck_context.fact_snapshot
    elif input_name in {"as_of", "assessment_as_of"}:
        assert loader.call_args.kwargs.get(input_name) == deck_context.data_as_of
    else:
        assert loader.call_args.kwargs.get(input_name) == deck_context.edition_name


@pytest.mark.parametrize("capture", ["missing", "foreign"])
@pytest.mark.usefixtures("isolated_render")
def test_deck_refuses_invalid_primary_capture_before_reality_or_renderer(
    deck_context: StageContext, capture: str,
) -> None:
    snapshot = (
        None if capture == "missing"
        else ProgramFactSnapshot("other", deck_context.data_as_of, ())
    )
    with (
        patch.object(render_stage.ProgramReality, "load") as loader,
        patch.object(render_stage, "DeckRenderer") as renderer,
    ):
        with pytest.raises(ConfigError, match="snapshot"):
            RenderStage().execute(replace(deck_context, fact_snapshot=snapshot))
    loader.assert_not_called()
    renderer.assert_not_called()


@pytest.mark.parametrize("mode,families", [
    ("legacy", {}), ("shadow", {}), ("primary", {"judgment": "shadow"}),
    ("primary", {"workitem.state": "legacy"}), ("shadow", {"judgment": "primary"}),
])
@pytest.mark.usefixtures("isolated_render")
def test_deck_ineligible_modes_do_not_substitute_retained_facts(
    deck_context: StageContext, mode: str, families: dict[str, str],
) -> None:
    class ReachedDeckReality(Exception):
        pass

    with (
        patch.object(sor_gated_load, "resolve_fact_sor_mode", return_value=mode),
        patch.object(
            sor_gated_load, "load_fact_sor_state",
            return_value=SimpleNamespace(family_modes=families),
        ),
        patch.object(render_stage.ProgramReality, "load", side_effect=ReachedDeckReality) as loader,
    ):
        with pytest.raises(ReachedDeckReality):
            RenderStage().execute(deck_context)
    assert loader.call_args.kwargs.get("fact_snapshot") is None
    assert loader.call_args.kwargs.get("as_of") is None
    assert loader.call_args.kwargs.get("assessment_as_of") == deck_context.data_as_of
    assert loader.call_args.kwargs["edition_name"] == deck_context.edition_name


def test_actual_primary_deck_renders_captured_risk_after_current_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ai.ai_stage import AIStage
    from src.commands import report as report_module
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
    from src.core.stages.compute_stage import ComputeStage
    from src.core.stages.fetch_stage import FetchStage
    from src.core.stages.milestone_stage import MilestoneStage
    from src.core.stages.narrative_stage import NarrativeStage
    from src.core.stages.resolution_stage import ResolutionStage
    from src.core.stages.risk_stage import RiskStage
    from tests.support.ado_cassettes import load_cassette_work_items
    from tests.unit.test_pipeline_stages import _stage_pipeline_workspace

    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    save_fact_sor_state("acme", mode="primary", recorded_at=as_of, programs_root=programs_root)
    store = ProgramFactStore("acme", db_root=tmp_path)
    risk = ProgramFactInput(
        fact_type="risk.entry", entity_refs=("RISK:r1",),
        payload={
            "id": "r1", "program_id": "acme", "title": "Captured deck risk",
            "description": "Synthetic retained evidence", "probability": "likely",
            "impact": "high", "category": "schedule", "owner_alias": "demo",
            "status": "open", "identified_date": "2026-05-01",
        },
        approval_event_id="approved-r1",
    )
    store.append_fact(risk, recorded_at=as_of)
    ctx = report_module._build_stage_request_context(
        edition_name="acme_weekly", issue_number=None, reseed=False, no_seed=False,
        dry_run=True, offline=False, diff_mode=False, as_of=as_of,
        edition_type_override=EditionType.DECK, lookback_range=None,
        reports_root=reports_root, archive_root=tmp_path / "archive",
        programs_root=programs_root,
        work_item_loader=lambda _bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None, section_filter_ids=(), open_browser=False,
    )
    ctx = ResolutionStage().execute(ctx)
    store.append_fact(
        replace(risk, payload={**risk.payload, "title": "Replacement deck risk"}),
        recorded_at=as_of,
    )
    for stage in (FetchStage(), ComputeStage(), MilestoneStage(), RiskStage(), NarrativeStage(), AIStage()):
        ctx = stage.execute(ctx)
    assert [risk.title for risk in ctx.risks] == ["Captured deck risk"]
    result = RenderStage().execute(ctx)
    assert "Captured deck risk" in result.render_state.markdown_body
    assert "Replacement deck risk" not in result.render_state.markdown_body


def _save_legacy_assumption(programs_root: Path, as_of: datetime) -> None:
    from src.core.assumption_tracker import save_assumptions
    from src.core.models_v2 import Assumption, AssumptionStatus

    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-legacy",
                program_id="acme",
                text="Legacy authored deck assumption",
                validation_method="Synthetic review",
                validation_due=as_of.date(),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="demo",
                identified_date=as_of.date(),
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )


def test_reality_assessment_clock_keeps_current_legacy_facts_and_as_of_stays_historical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.program_reality import ProgramReality

    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    programs_root = tmp_path / "programs"
    _save_legacy_assumption(programs_root, as_of)
    assessed = ProgramReality.load(
        "acme", assessment_as_of=as_of, programs_root=programs_root, archive_root=tmp_path / "archive",
    )
    historical = ProgramReality.load(
        "acme", as_of=as_of, programs_root=programs_root, archive_root=tmp_path / "archive",
    )

    assert assessed.as_of == as_of
    assert [a.record.text for a in assessed.assumptions()] == ["Legacy authored deck assumption"]
    assert historical.as_of == as_of
    assert historical.assumptions() == ()


def test_actual_legacy_deck_renders_current_yaml_assumption_with_draft_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ai.ai_stage import AIStage
    from src.commands import report as report_module
    from src.core.stages.compute_stage import ComputeStage
    from src.core.stages.fetch_stage import FetchStage
    from src.core.stages.milestone_stage import MilestoneStage
    from src.core.stages.narrative_stage import NarrativeStage
    from src.core.stages.resolution_stage import ResolutionStage
    from src.core.stages.risk_stage import RiskStage
    from tests.support.ado_cassettes import load_cassette_work_items
    from tests.unit.test_pipeline_stages import _stage_pipeline_workspace

    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    _save_legacy_assumption(programs_root, as_of)
    ctx = report_module._build_stage_request_context(
        edition_name="acme_weekly", issue_number=None, reseed=False, no_seed=False,
        dry_run=True, offline=False, diff_mode=False, as_of=as_of,
        edition_type_override=EditionType.DECK, lookback_range=None,
        reports_root=reports_root, archive_root=tmp_path / "archive",
        programs_root=programs_root,
        work_item_loader=lambda _bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        kusto_query_executor=None, section_filter_ids=(), open_browser=False,
    )
    for stage in (
        ResolutionStage(), FetchStage(), ComputeStage(), MilestoneStage(),
        RiskStage(), NarrativeStage(), AIStage(),
    ):
        ctx = stage.execute(ctx)
    assert ctx.fact_snapshot is None or ctx.fact_snapshot.program_id == "acme"
    with patch.object(
        render_stage.ProgramReality, "load", wraps=render_stage.ProgramReality.load,
    ) as loader:
        result = RenderStage().execute(ctx)
    assert "Legacy authored deck assumption" in result.render_state.markdown_body
    assert loader.call_args.kwargs.get("assessment_as_of") == as_of


def test_reality_selects_facts_by_as_of_and_assesses_at_assessment_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
    from src.core.program_reality import ProgramReality

    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    assessment_as_of = as_of + timedelta(days=2)
    store = ProgramFactStore("acme", db_root=tmp_path)
    for risk_id, title, recorded_at in (
        ("r-before", "Historical risk", as_of - timedelta(hours=1)),
        ("r-after", "Later risk", as_of + timedelta(days=1)),
    ):
        store.append_fact(
            ProgramFactInput(
                fact_type="risk.entry", entity_refs=(f"RISK:{risk_id}",),
                payload={
                    "id": risk_id, "program_id": "acme", "title": title,
                    "description": "Synthetic", "probability": "likely",
                    "impact": "high", "category": "schedule", "owner_alias": "demo",
                    "status": "open", "identified_date": "2026-05-01",
                },
            ),
            recorded_at=recorded_at,
        )

    reality = ProgramReality.load(
        "acme", programs_root=tmp_path / "programs", archive_root=tmp_path / "archive",
        as_of=as_of, assessment_as_of=assessment_as_of,
    )

    assert reality.as_of == assessment_as_of
    assert [risk.record.title for risk in reality.risks()] == ["Historical risk"]
