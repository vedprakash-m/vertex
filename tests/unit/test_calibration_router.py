from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

from src.core.calibration_engine import CalibrationRollup
from src.core.feedback.calibration_router import load_forecast_calibration_modifier, refresh_forecast_calibration
from src.core.models import Confidence
from src.core.models_v2 import WorkstreamCalibration


def test_refresh_forecast_calibration_writes_yaml_and_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    modifier, path = refresh_forecast_calibration(
        "acme",
        workstream_rows=(
            WorkstreamCalibration(workstream_id="deployment", met=2, contradicted=3, stale=1),
            WorkstreamCalibration(workstream_id="repair", met=5, contradicted=0, stale=0),
        ),
        dri_rows=(
            CalibrationRollup(subject_id="alex", met=2, contradicted=3, stale=1),
            CalibrationRollup(subject_id="jamie", met=5, contradicted=0, stale=0),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        since=date(2026, 1, 5),
        programs_root=programs_root,
    )

    assert path is not None
    assert path.exists()
    assert modifier.workstream_modifiers["deployment"] == 0.17
    assert modifier.dri_modifiers["alex"] == 0.17
    assert modifier.confidence == Confidence.HIGH

    loaded = load_forecast_calibration_modifier("acme", programs_root=programs_root)
    assert loaded is not None
    assert loaded.workstream_modifiers == modifier.workstream_modifiers
    assert loaded.dri_modifiers == modifier.dri_modifiers
    assert loaded.confidence == modifier.confidence

    audit_path = programs_root / "acme" / "_feedback" / "_audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries[-1]["module"] == "calibration_router"


def test_refresh_forecast_calibration_dry_run_skips_write(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    modifier, path = refresh_forecast_calibration(
        "acme",
        workstream_rows=(WorkstreamCalibration(workstream_id="deployment", met=5, contradicted=0, stale=0),),
        dri_rows=(CalibrationRollup(subject_id="alex", met=5, contradicted=0, stale=0),),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
        dry_run=True,
    )

    assert path is None
    assert modifier.workstream_modifiers["deployment"] == 0.0
    assert not any(programs_root.rglob("forecast_calibration.yaml"))