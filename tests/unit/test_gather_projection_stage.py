from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands.gather_pipeline.models import ProjectionStageInput
from src.commands.gather_pipeline.projection_stage import run_projection_stage
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, AIConfig, Program


def _demo_program(*, ai_enabled: bool = False) -> Program:
    return Program(
        schema_version="2.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        ai=AIConfig(enabled=True, budget_usd_per_run=1.0) if ai_enabled else None,
    )


def _demo_item(current_time: datetime) -> WorkItem:
    return WorkItem(
        id=101,
        type="Feature",
        title="Demo",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=["RAMPP1"],
        custom_fields={},
        fetched_at=current_time,
    )


def test_run_projection_stage_dry_run_skips_trajectory_writes(tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    trajectory_store = SimpleNamespace(
        append=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("trajectory write during dry run")),
    )

    result = run_projection_stage(
        ProjectionStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            items=(_demo_item(current_time),),
            signal_store=SimpleNamespace(),
            trajectory_store=trajectory_store,
            as_of=current_time,
            programs_root=tmp_path,
            include_dependency_scout=False,
            background_synthesis_runner=None,
            resolve_workstream_id=lambda area_path, workstreams: None,
            dry_run=True,
        )
    )

    assert result.trajectory_updates == 0
    assert result.dependency_proposals_refreshed == 0
    assert result.background_proposals == 0


def test_run_projection_stage_skips_dependency_refresh_when_disabled(monkeypatch, tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.refresh_dependency_scout_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dependency refresh called")),
    )

    result = run_projection_stage(
        ProjectionStageInput(
            program=_demo_program(),
            program_id="demo",
            workstreams=(),
            items=(_demo_item(current_time),),
            signal_store=SimpleNamespace(),
            trajectory_store=SimpleNamespace(append=lambda *args, **kwargs: False),
            as_of=current_time,
            programs_root=tmp_path,
            include_dependency_scout=False,
            background_synthesis_runner=None,
            resolve_workstream_id=lambda area_path, workstreams: None,
        )
    )

    assert result.dependency_proposals_refreshed == 0
    assert result.dependency_detail is None


def test_run_projection_stage_skips_synthesis_when_ai_disabled(monkeypatch, tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.evaluate_background_synthesis_triggers",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("synthesis triggers evaluated")),
    )

    result = run_projection_stage(
        ProjectionStageInput(
            program=_demo_program(ai_enabled=False),
            program_id="demo",
            workstreams=(),
            items=(_demo_item(current_time),),
            signal_store=SimpleNamespace(),
            trajectory_store=SimpleNamespace(append=lambda *args, **kwargs: False),
            as_of=current_time,
            programs_root=tmp_path,
            include_dependency_scout=False,
            background_synthesis_runner=None,
            resolve_workstream_id=lambda area_path, workstreams: None,
        )
    )

    assert result.background_proposals == 0
    assert result.synthesis_detail is None


def test_run_projection_stage_uses_injected_synthesis_runner(monkeypatch, tmp_path: Path) -> None:
    current_time = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "src.commands.gather_pipeline.projection_stage.evaluate_background_synthesis_triggers",
        lambda *args, **kwargs: (
            SimpleNamespace(workstream_id="demo.slice", reasons=("low vitality",)),
        ),
    )
    calls: list[tuple[str, str]] = []

    def _runner(program_id: str, workstream_id: str, programs_root: Path, as_of: datetime) -> bool:
        calls.append((program_id, workstream_id))
        return True

    result = run_projection_stage(
        ProjectionStageInput(
            program=_demo_program(ai_enabled=True),
            program_id="demo",
            workstreams=(),
            items=(_demo_item(current_time),),
            signal_store=SimpleNamespace(),
            trajectory_store=SimpleNamespace(append=lambda *args, **kwargs: False),
            as_of=current_time,
            programs_root=tmp_path,
            include_dependency_scout=False,
            background_synthesis_runner=_runner,
            resolve_workstream_id=lambda area_path, workstreams: None,
        )
    )

    assert calls == [("demo", "demo.slice")]
    assert result.background_proposals == 1
