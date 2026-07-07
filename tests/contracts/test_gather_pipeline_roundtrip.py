from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.commands import gather
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, Program


def _demo_program() -> Program:
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
    )


def _demo_item(now: datetime) -> WorkItem:
    return WorkItem(
        id=101,
        type="Feature",
        title="Hydrated",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=("RAMPP1",),
        custom_fields={},
        fetched_at=now,
    )


@pytest.mark.parametrize("use_gather_v2", [False, True])
def test_load_ado_items_via_uil_roundtrips_through_legacy_and_v2_runtime(monkeypatch, tmp_path, use_gather_v2: bool) -> None:
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    program = _demo_program()
    item = _demo_item(current_time)
    hydration_result = SimpleNamespace(
        resources=SimpleNamespace(work_items=(item,), freshness_items=(item,)),
        api_call_count=1,
    )
    called: list[str] = []

    def _fake_legacy_run_channel(*args, **kwargs):
        called.append("legacy")
        return hydration_result, None

    def _fake_v2_run_channel(*args, **kwargs):
        called.append("v2")
        return hydration_result, None

    monkeypatch.setenv("VERTEX_GATHER_V2", "1" if use_gather_v2 else "0")
    monkeypatch.setattr(gather, "_run_channel", _fake_legacy_run_channel)
    monkeypatch.setattr("src.commands.gather_pipeline.run_channel", _fake_v2_run_channel)

    items, freshness_items, ado_calls = gather._load_ado_items_via_uil(
        program,
        (),
        current_time,
        since=current_time - timedelta(days=14),
        programs_root=tmp_path,
        binding=SimpleNamespace(),
    )

    assert items == (item,)
    assert freshness_items == (item,)
    assert ado_calls == 1
    assert called == (["v2"] if use_gather_v2 else ["legacy"])
