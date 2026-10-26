from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.commands import report_health
from src.core.models import EditionType
from src.core.pipeline import StageContext
from src.core.stages import compute_stage, render_stage


AS_OF = datetime(2026, 10, 24, tzinfo=timezone.utc)


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
