from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.commands import report_health
from src.commands.report_pipeline import assemble_stage
from src.core.models import ArchiveIndex, EditionType, RiskLevel
from src.core.pipeline import StageContext
from src.core.stages import compute_stage, render_stage, resolution_stage


AS_OF = datetime(2026, 10, 24, tzinfo=timezone.utc)


@pytest.mark.parametrize("program_backed", [False, True])
def test_compute_forecast_callback_reaches_real_selected_or_flat_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool,
) -> None:
    from src.commands import report_ai
    from src.commands.report_pipeline import assemble_stage
    from tests.unit.test_report_ai_calibration import _forecast_workspace

    root, reports, item = _forecast_workspace(tmp_path)
    support = Mock()
    support.load_eta_forecasts = assemble_stage._load_eta_forecasts
    support.build_continuity_deltas.side_effect = RuntimeError("Completed real forecast boundary")
    forecast = Mock(return_value={})
    monkeypatch.setattr(report_ai, "forecast_etas", forecast)
    ctx = StageContext(
        edition_name="acme_weekly", bundle=SimpleNamespace(config=SimpleNamespace(
            ado=SimpleNamespace(date_window_days=14),
        )), reports_root=reports, archive_root=tmp_path / "archive", programs_root=root,
        resolved_v2=SimpleNamespace() if program_backed else None, resolved_issue_number=7,
        resolved_edition_type=EditionType.DETAILED, data_as_of=AS_OF, stage_support=support, items=(item,),
    )
    with pytest.raises(RuntimeError, match="Completed real forecast boundary"):
        compute_stage.ComputeStage().execute(ctx)
    assert forecast.call_args.args[0][item.id][-1].target_date.day == (15 if program_backed else 25)
    assert forecast.call_args.kwargs["calibration_adjustments"] == {item.id: 0.2 if program_backed else -0.2}


@pytest.mark.parametrize("mode", ["omitted", "none", "explicit"])
@pytest.mark.parametrize("edition", [None, "fixture_weekly"])
def test_scorecard_wrapper_selected_root_and_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, edition: str | None,
) -> None:
    from src.commands.report_pipeline import assemble_stage as assembly
    from src.core.overrides_store import OverridesDocument
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "elsewhere" / "reports"
    expected = root if mode == "explicit" else reports.parent / "programs"
    resolver = Mock(return_value=SimpleNamespace(
        program=SimpleNamespace(id="fixture"), raw_program={"selected": True}, scorecards=(),
    ))
    monkeypatch.setattr(assembly, "resolve_edition", resolver)
    implementation = Mock(return_value=((), (), ()))
    monkeypatch.setattr(assembly, "_build_scorecard_data_impl", implementation)
    kwargs = {} if mode == "omitted" else {"programs_root": root if mode == "explicit" else None}
    assembly._build_scorecard_data(
        SimpleNamespace(), (), {}, {}, OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
        edition_name=edition, reports_root=reports, **kwargs,
    )
    assert implementation.call_args.kwargs["programs_root"] == expected
    if edition is None:
        resolver.assert_not_called()
        assert implementation.call_args.kwargs["program_id"] is None
    else:
        resolver.assert_called_once_with(edition, programs_root=expected)


@pytest.mark.parametrize("invalid", ["missing", "relative", "unknown_edition"])
def test_scorecard_wrapper_explicit_context_never_uses_inferred_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    from src.commands.report_pipeline import assemble_stage as assembly
    from src.core.exceptions import ConfigError
    from src.core.overrides_store import OverridesDocument
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    if invalid == "missing":
        root = tmp_path / "missing"
    elif invalid == "relative":
        root = Path("relative-home")
    edition = "missing_edition" if invalid == "unknown_edition" else "fixture_weekly"
    implementation = Mock(side_effect=AssertionError("Must reject before scorecard reads"))
    monkeypatch.setattr(assembly, "_build_scorecard_data_impl", implementation)
    with pytest.raises(ConfigError):
        assembly._build_scorecard_data(
            SimpleNamespace(), (), {}, {}, OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
            edition_name=edition, reports_root=tmp_path / "reports", programs_root=root,
        )
    implementation.assert_not_called()


@pytest.mark.parametrize("mode", ["omitted", "none", "explicit"])
@pytest.mark.parametrize("reports_present", [False, True])
def test_runtime_db_helper_selected_root_and_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, reports_present: bool,
) -> None:
    from src.commands.report_pipeline import assemble_stage as assembly

    root = tmp_path / "named-home"
    reports = tmp_path / "elsewhere" / "reports" if reports_present else None
    resolver = Mock(return_value=tmp_path / "selected-db")
    monkeypatch.setattr(assembly, "_resolve_fact_db_root", resolver)
    kwargs = {} if mode == "omitted" else {"programs_root": root if mode == "explicit" else None}
    result = assembly._runtime_db_root_for_reports(reports, **kwargs)
    if mode == "explicit" or reports_present:
        resolver.assert_called_once_with(root if mode == "explicit" else reports.parent / "programs")
        assert result == tmp_path / "selected-db"
    else:
        resolver.assert_not_called()
        assert result is None


@pytest.mark.parametrize("program_backed", [False, True])
def test_compute_stage_artifacts_keep_selected_or_flat_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool,
) -> None:
    from src.core.overrides_store import OverridesDocument, load_overrides, save_overrides
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "unrelated" / "reports"
    selected = root if program_backed else None
    save_overrides(
        "fixture_weekly", OverridesDocument(issue_number=7, top_3_now=(), scorecards=(), edition_intro="selected"),
        reports, programs_root=selected,
    )
    if program_backed:
        save_overrides(
            "fixture_weekly", OverridesDocument(issue_number=7, top_3_now=(), scorecards=(), edition_intro="decoy"),
            reports,
        )
    support = Mock()
    support.build_scorecard_data.return_value = ((), (), ())
    support.load_report_signal_context.side_effect = RuntimeError("Completed artifact boundary")
    ctx = StageContext(
        edition_name="fixture_weekly", bundle=SimpleNamespace(config=SimpleNamespace(
            scorecards=(), ado=SimpleNamespace(date_window_days=14),
        )), reports_root=reports, archive_root=tmp_path / "archive", programs_root=root,
        resolved_v2=SimpleNamespace() if program_backed else None, resolved_issue_number=7,
        resolved_edition_type=EditionType.DETAILED, data_as_of=AS_OF, stage_support=support,
    )
    with pytest.raises(RuntimeError, match="Completed artifact boundary"):
        compute_stage.ComputeStage().execute(ctx)
    assert support.build_scorecard_data.call_args.kwargs.get("programs_root") == selected
    assert support.build_override_snapshot.call_args.args[0].edition_intro == "selected"
    assert load_overrides("fixture_weekly", reports, issue_number=7, programs_root=selected).edition_intro == "selected"
    if program_backed:
        assert load_overrides("fixture_weekly", reports, issue_number=7).edition_intro == "decoy"


@pytest.mark.parametrize("program_backed", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_render_stage_review_artifacts_keep_selected_or_flat_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_backed: bool, existing: bool,
) -> None:
    from src.commands.report_review import _ensure_review_status
    from src.core.models import ArchiveIndex
    from src.core.overrides_store import OverridesDocument
    from src.core.review_status_store import get_review_status_path, load_review_status, reset_review_status
    from tests.unit.test_narrative_store import _selected_artifact_root

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "unrelated" / "reports"
    selected = root if program_backed else None
    if existing:
        reset_review_status("fixture_weekly", 7, ("exec_summary", "ws:prior"), reports, programs_root=selected)
    decoy = None
    if program_backed:
        _ensure_review_status("fixture_weekly", 99, ("decoy",), set(), reports)
        decoy = get_review_status_path("fixture_weekly", reports).read_bytes()
    support = Mock()
    support.ensure_review_status = _ensure_review_status
    support.is_continuity_layout.return_value = False
    support.skipped_review_sections.return_value = set()
    path_reader = Mock(wraps=get_review_status_path)
    monkeypatch.setattr(render_stage, "get_review_status_path", path_reader)
    monkeypatch.setattr(render_stage, "load_latest_notification_state", Mock(return_value=None))
    monkeypatch.setattr(render_stage, "build_freshness_report", Mock())
    monkeypatch.setattr(render_stage, "ReportData", Mock(side_effect=RuntimeError("Completed review boundary")))
    ctx = StageContext(
        edition_name="fixture_weekly", bundle=SimpleNamespace(
            editorial_rules=SimpleNamespace(stale_warn_days=7, stale_block_days=14), program_context=None,
        ), reports_root=reports, archive_root=tmp_path / "archive", programs_root=root,
        resolved_v2=SimpleNamespace() if program_backed else None, archive_index=ArchiveIndex(edition="fixture_weekly", issues=()),
        resolved_issue_number=7, resolved_edition_type=EditionType.DETAILED, started_at=AS_OF, data_as_of=AS_OF,
        overrides_document=OverridesDocument(issue_number=7, top_3_now=(), scorecards=()),
        top_items=(), auto_suggestions=(), continuity_chapters=(), narratives_dir=tmp_path / "narratives",
        exec_summary_text="", workstream_blurbs={"delivery": "selected"}, workstream_narrative_history={},
        ai_synthesis=SimpleNamespace(), render_exec_summary_text="", render_workstream_blurbs={},
        dimension_risks=(), scorecard_deltas=(), scorecards=(), scorecard_packets={}, evidence_by_item={},
        stage_support=support,
    )
    with pytest.raises(RuntimeError, match="Completed review boundary"):
        render_stage.RenderStage().execute(ctx)
    assert path_reader.call_args.kwargs.get("programs_root") == selected
    assert load_review_status("fixture_weekly", reports, programs_root=selected).issue_number == 7
    if program_backed:
        assert get_review_status_path("fixture_weekly", reports).read_bytes() == decoy


def test_compute_stage_forwards_archive_to_actual_signal_reader_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = Mock()
    support.build_scorecard_data.return_value = ((), (), ())
    support.load_report_signal_context.side_effect = RuntimeError("Selected signal boundary")
    monkeypatch.setattr(compute_stage, "load_overrides", Mock(return_value=None))
    monkeypatch.setattr(compute_stage, "save_overrides", Mock(return_value=tmp_path / "overrides.yaml"))
    bundle = SimpleNamespace(config=SimpleNamespace(
        scorecards=(), ado=SimpleNamespace(date_window_days=14),
    ))
    ctx = StageContext(
        edition_name="fixture", bundle=bundle, reports_root=tmp_path / "reports",
        archive_root=tmp_path / "archive", resolved_issue_number=1,
        resolved_edition_type=EditionType.DETAILED, data_as_of=AS_OF, stage_support=support,
    )
    with pytest.raises(RuntimeError, match="Selected signal boundary"):
        compute_stage.ComputeStage().execute(ctx)
    support.load_report_signal_context.assert_called_once_with(
        edition_name="fixture", bundle=bundle, items=(), as_of=AS_OF,
        previous_snapshot=None, reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("explicit", [False, True])
def test_health_reader_preserves_selected_and_omitted_archive_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    loader = Mock(side_effect=RuntimeError("Selected health boundary"))
    monkeypatch.setattr(report_health.ProgramReality, "load", loader)
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    with pytest.raises(RuntimeError, match="Selected health boundary"):
        report_health._build_health_summary(
            (), None, program_id="fixture", programs_root=tmp_path / "programs",
            as_of=AS_OF, **archive_options,
        )
    loader.assert_called_once_with("fixture", programs_root=tmp_path / "programs", **archive_options)
    assert not list(tmp_path.iterdir())


def test_health_with_injected_reality_does_not_reopen_a_default_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = Mock(side_effect=AssertionError("Use already selected reality"))
    monkeypatch.setattr(report_health.ProgramReality, "load", loader)
    reality = Mock()
    reality.risks.return_value = ()
    report_health._build_health_summary(
        (), None, program_id="fixture", programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive", reality=reality,
    )
    reality.risks.assert_called_once_with()
    loader.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_risk_reader_forwards_archive_without_changing_omitted_call_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    loader = Mock(side_effect=RuntimeError("Selected risk boundary"))
    monkeypatch.setattr(render_stage, "load_program_facts", loader)
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    with pytest.raises(RuntimeError, match="Selected risk boundary"):
        render_stage._load_current_risks("fixture", programs_root=tmp_path / "programs", **archive_options)
    loader.assert_called_once_with(
        "fixture", programs_root=tmp_path / "programs", fact_types=("risk.entry",), **archive_options,
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("explicit", [False, True])
def test_render_risk_fallback_preserves_selected_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    loader = Mock(return_value=())
    monkeypatch.setattr(render_stage, "_load_current_risks", loader)
    support = Mock()
    support.build_health_summary.side_effect = [
        ValueError("Synthetic risk assessment failure"), report_health._build_health_summary((), None),
    ]
    ctx = StageContext(
        programs_root=tmp_path / "programs", archive_root=tmp_path / "archive" if explicit else None,
        data_as_of=AS_OF, risk_assessments=(), resolved_v2=SimpleNamespace(program=SimpleNamespace(id="fixture")),
    )
    result = render_stage._build_health_summary_with_risk_fallback(ctx, support, (), None)
    archive_options = {"archive_root": tmp_path / "archive"} if explicit else {}
    loader.assert_called_once_with(
        "fixture", programs_root=tmp_path / "programs", **archive_options,
    )
    assert result.risk_render_warning == render_stage._RISK_RENDER_WARNING
    assert support.build_health_summary.call_count == 2
    assert not list(tmp_path.iterdir())


def test_render_stage_forwards_archive_to_health_boundary() -> None:
    tree = ast.parse(inspect.getsource(render_stage.RenderStage))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_build_health_summary_with_risk_fallback"
    ]
    assert len(calls) == 1
    assert any(
        keyword.arg == "archive_root" and isinstance(keyword.value, ast.Attribute)
        and isinstance(keyword.value.value, ast.Name) and keyword.value.value.id == "ctx"
        and keyword.value.attr == "archive_root"
        for keyword in calls[0].keywords
    )


def test_render_stage_deck_loads_reality_from_selected_archive() -> None:
    tree = ast.parse(inspect.getsource(render_stage.RenderStage))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "load" and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ProgramReality"
    ]
    assert len(calls) == 1
    assert any(
        keyword.arg == "archive_root" and isinstance(keyword.value, ast.Attribute)
        and isinstance(keyword.value.value, ast.Name) and keyword.value.value.id == "ctx"
        and keyword.value.attr == "archive_root"
        for keyword in calls[0].keywords
    )


def test_resolution_previous_snapshot_uses_selected_archive_not_decoy(tmp_path: Path) -> None:
    from tests.unit.test_archive_workspace_context import EDITION, _bytes, _seed, _workspace

    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected, 78)
    _seed(decoy, 77)
    decoy_before = _bytes(decoy)

    snapshot, issue_number = resolution_stage._resolve_previous_snapshot(
        edition_name=EDITION,
        issue_number=79,
        archive_index=ArchiveIndex(edition=EDITION, issues=()),
        archive_root=archive,
        editions_root=selected.parent / "editions",
        programs_root=selected,
    )

    assert snapshot is not None and snapshot.issue_number == issue_number == 78
    assert _bytes(decoy) == decoy_before


@pytest.mark.parametrize("selection", ["omitted", "none", "explicit"])
def test_healthy_streak_wrapper_preserves_optional_root_call_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    root = tmp_path / "programs"
    archive = tmp_path / "archive"
    reader = Mock(return_value=4)
    monkeypatch.setattr(assemble_stage, "get_all_green_streak", reader)
    options = {} if selection == "omitted" else {
        "programs_root": root if selection == "explicit" else None,
    }

    assert assemble_stage._compute_healthy_streak(
        "fixture", 9, (SimpleNamespace(risk=RiskLevel.LOW),), archive, **options,
    ) == 5

    expected = {"archive_root": archive, "before_issue_number": 9}
    if selection == "explicit":
        expected["programs_root"] = root
    reader.assert_called_once_with("fixture", **expected)


def test_healthy_streak_wrapper_reads_selected_archive_not_decoy(tmp_path: Path) -> None:
    from tests.unit.test_archive_workspace_context import EDITION, _bytes, _seed, _workspace

    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected, 78)
    _seed(decoy, 77)
    decoy_before = _bytes(decoy)

    streak = assemble_stage._compute_healthy_streak(
        EDITION, 79, (SimpleNamespace(risk=RiskLevel.LOW),), archive, programs_root=selected,
    )

    assert streak == 2
    assert _bytes(decoy) == decoy_before


def test_render_stage_forwards_selected_program_context_to_history_and_streak() -> None:
    tree = ast.parse(inspect.getsource(render_stage.RenderStage))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "read_vitality_history")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "compute_healthy_streak")
        )
    ]

    assert {
        call.func.id if isinstance(call.func, ast.Name) else call.func.attr
        for call in calls
    } == {"read_vitality_history", "compute_healthy_streak"}
    for call in calls:
        rendered = ast.unparse(call)
        assert "programs_root" in rendered
        assert "ctx.programs_root" in rendered
        assert "ctx.resolved_v2 is not None" in rendered


def test_confirmation_artifacts_forward_selected_program_context_to_healthy_streak() -> None:
    from src.commands import confirm

    tree = ast.parse(inspect.getsource(confirm._build_confirm_artifacts))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_compute_healthy_streak"
    ]

    assert len(calls) == 1
    rendered = ast.unparse(calls[0])
    assert "programs_root" in rendered
    assert "programs_root" in rendered and "program_id is not None" in rendered
