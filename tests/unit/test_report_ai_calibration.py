from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from src.commands.report_ai import _build_forecast_calibration_adjustments
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import ForecastCalibrationModifier, Workstream, WorkstreamSignalSources


def _forecast_workspace(tmp_path: Path) -> tuple[Path, Path, WorkItem]:
    import shutil
    from tests.unit.test_pipeline_stages import _stage_pipeline_workspace
    from tests.unit.test_commands_report import _sample_items
    from src.core.trajectory import append_trajectory_point
    from src.core.models_v2 import TrajectoryPoint
    from src.core.feedback.calibration_router import get_forecast_calibration_path

    _stage_pipeline_workspace(tmp_path)
    root = (tmp_path / "programs").rename(tmp_path / "named-home")
    reports = tmp_path / "elsewhere" / "reports"
    reports.mkdir(parents=True)
    decoy = reports.parent / "programs"
    shutil.copytree(root, decoy)
    item = replace(_sample_items(datetime(2026, 5, 5, tzinfo=timezone.utc))[0], assigned_to="Owner", assigned_to_email="owner@example.test")
    for selected, modifier, target_day in ((root, 0.2, 15), (decoy, -0.2, 25)):
        for day in (1, 3):
            append_trajectory_point(
                "acme", item.id, TrajectoryPoint(
                    date=date(2026, 5, day), state="Active", assigned_to="Owner",
                    target_date=date(2026, 5, target_day), risk_level=RiskLevel.LOW, area_path=item.area_path,
                ), programs_root=selected,
            )
        calibration = get_forecast_calibration_path("acme", programs_root=selected)
        calibration.parent.mkdir(parents=True)
        calibration.write_text(yaml.safe_dump({"dri_modifiers": {"owner": modifier}}), encoding="utf-8")
    return root, reports, item


@pytest.mark.parametrize("mode", ["omitted", "none", "explicit"])
@pytest.mark.parametrize("caller", ["helper", "wrapper"])
def test_forecast_helper_reads_selected_trajectories_and_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, caller: str,
) -> None:
    from src.commands import report_ai
    from src.commands.report_pipeline import assemble_stage

    root, reports, item = _forecast_workspace(tmp_path)
    forecast = Mock(return_value={item.id: "selected forecast"})
    monkeypatch.setattr(report_ai, "forecast_etas", forecast)
    kwargs = {} if mode == "omitted" else {"programs_root": root if mode == "explicit" else None}
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    loader = report_ai._load_eta_forecasts if caller == "helper" else assemble_stage._load_eta_forecasts
    if caller == "helper":
        kwargs["item_trajectory_points"] = report_ai._item_trajectory_points
    result = loader(
        edition_name="acme_weekly", items=(item,), as_of=datetime(2026, 5, 5, tzinfo=timezone.utc),
        reports_root=reports, **kwargs,
    )
    assert result == {item.id: "selected forecast"}
    points = forecast.call_args.args[0][item.id]
    assert points[-1].target_date == date(2026, 5, 15 if mode == "explicit" else 25)
    assert forecast.call_args.kwargs["calibration_adjustments"] == {item.id: 0.2 if mode == "explicit" else -0.2}
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("invalid", ["missing", "relative", "unknown_edition"])
def test_forecast_explicit_context_rejects_before_bundle_or_history_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    from src.commands import report_ai
    from src.core.exceptions import ConfigError

    root, reports, item = _forecast_workspace(tmp_path)
    if invalid == "missing":
        root = tmp_path / "missing"
    elif invalid == "relative":
        root = Path("relative-home")
    bundle = Mock(side_effect=AssertionError("No read before explicit admission"))
    history = Mock(side_effect=AssertionError("No trajectory fallback"))
    monkeypatch.setattr(report_ai, "load_bundle_with_mode", bundle)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(ConfigError):
        report_ai._load_eta_forecasts(
            edition_name="missing" if invalid == "unknown_edition" else "acme_weekly",
            items=(item,), as_of=datetime(2026, 5, 5, tzinfo=timezone.utc), reports_root=reports,
            item_trajectory_points=history, programs_root=root,
        )
    bundle.assert_not_called()
    history.assert_not_called()
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("branch", ["missing_loader", "non_v2", "unresolved", "empty"])
def test_forecast_omitted_root_preserves_empty_and_legacy_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str,
) -> None:
    from src.commands import report_ai

    monkeypatch.setattr(report_ai, "load_bundle_with_mode", Mock(return_value=SimpleNamespace(mode="v1" if branch == "non_v2" else "v2")))
    monkeypatch.setattr(report_ai, "resolve_edition", Mock(return_value=None if branch == "unresolved" else SimpleNamespace(program=SimpleNamespace(id="fixture"))))
    history = Mock(return_value=())
    kwargs = {} if branch == "missing_loader" else {"item_trajectory_points": history}
    if branch == "missing_loader":
        with pytest.raises(ValueError, match="trajectory loader"):
            report_ai._load_eta_forecasts(
                edition_name="fixture", items=(), as_of=datetime(2026, 5, 5, tzinfo=timezone.utc),
                reports_root=tmp_path / "reports", **kwargs,
            )
    else:
        assert report_ai._load_eta_forecasts(
            edition_name="fixture", items=(SimpleNamespace(id=7),), as_of=datetime(2026, 5, 5, tzinfo=timezone.utc),
            reports_root=tmp_path / "reports", **kwargs,
        ) == {}


def test_build_forecast_calibration_adjustments_combines_workstream_and_dri_modifiers() -> None:
    items = (
        WorkItem(
            id=1001,
            type="Feature",
            title="Scoped item",
            state="Active",
            assigned_to="Priya",
            assigned_to_email="priya@example.com",
            area_path="One\\Demo\\WS",
            iteration_path="Sprint 1",
            target_date=None,
            tags=[],
            risk_level=RiskLevel.MEDIUM,
            custom_fields={},
        ),
    )
    workstreams = (
        Workstream(
            id="ws_demo",
            name="Demo Workstream",
            area_paths=("One\\Demo\\WS",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )
    modifier = ForecastCalibrationModifier(
        workstream_modifiers={"ws_demo": 0.10},
        dri_modifiers={"priya": 0.05},
        confidence=Confidence.MEDIUM,
    )

    adjustments = _build_forecast_calibration_adjustments(
        items=items,
        workstreams=workstreams,
        calibration_modifier=modifier,
    )

    assert adjustments == {1001: 0.15}