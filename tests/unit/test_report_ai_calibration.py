from __future__ import annotations

from src.commands.report_ai import _build_forecast_calibration_adjustments
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import ForecastCalibrationModifier, Workstream, WorkstreamSignalSources


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