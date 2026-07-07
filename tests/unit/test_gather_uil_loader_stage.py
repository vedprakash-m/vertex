from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands.gather_pipeline import uil_loader_stage
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, KustoQuery, Program, Signal


def test_load_ado_items_via_uil_returns_hydrated_items_and_freshness_items(tmp_path: Path) -> None:
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    program = Program(
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
    item = WorkItem(
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
        tags=["RAMPP1"],
        custom_fields={},
        fetched_at=current_time,
    )
    hydration_result = SimpleNamespace(
        resources=SimpleNamespace(work_items=(item,), freshness_items=(item,)),
        api_call_count=1,
    )

    items, freshness_items, ado_calls = uil_loader_stage.load_ado_items_via_uil(
        program,
        current_time,
        since=current_time,
        programs_root=tmp_path,
        binding=SimpleNamespace(),
        integration_error_sink=[],
        env_flag_fn=lambda _name: False,
        run_channel_fn=lambda *args, **kwargs: (hydration_result, None),
    )

    assert items == (item,)
    assert freshness_items == (item,)
    assert ado_calls == 1


def test_load_signal_channel_via_uil_returns_extracted_signals(tmp_path: Path) -> None:
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    program = Program(schema_version="2.0", id="demo", name="Demo")
    signal = Signal(
        id="teams/demo",
        timestamp=current_time,
        source="teams",
        program_id="demo",
        workstream_id="demo.slice",
        entity_refs=("teams:demo",),
        text="Teams result.",
        raw_ref="teams/demo",
        confidence=Confidence.HIGH,
    )
    hydration_result = SimpleNamespace(api_call_count=2)
    extraction_result = SimpleNamespace(signals=(signal,))

    signals, api_calls = uil_loader_stage.load_signal_channel_via_uil(
        program,
        current_time,
        programs_root=tmp_path,
        binding=SimpleNamespace(),
        integration_error_sink=[],
        env_flag_fn=lambda _name: False,
        run_channel_with_extraction_fn=lambda *args, **kwargs: (hydration_result, extraction_result, None),
    )

    assert signals == (signal,)
    assert api_calls == 2


def test_record_uil_kusto_query_states_records_rows_and_errors(tmp_path: Path) -> None:
    query = KustoQuery(
        id="query-a",
        cluster="https://cluster",
        database="db",
        kql="StormEvents | take 1",
        section="A",
        render_as="table",
        confidence="high",
        workstream_ids=("demo.slice",),
        validated=True,
    )
    captured_calls: list[dict[str, object]] = []

    uil_loader_stage.record_uil_kusto_query_states(
        "demo",
        binding=SimpleNamespace(
            hydration_provider=SimpleNamespace(_query_loader=lambda program_id, programs_root: (query,)),
            hydration_config=SimpleNamespace(programs_root=tmp_path),
        ),
        hydration_result=SimpleNamespace(
            resources=SimpleNamespace(result_sets=(SimpleNamespace(query_id="query-a", rows=({"Value": 1},)),)),
            errors=(SimpleNamespace(ref_id="query-a", message="ignored because rows exist"),),
        ),
        as_of=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
        query_state_sink={},
        previous_query_states={},
        include_unvalidated=False,
        record_kusto_query_state_fn=lambda sink, query, **kwargs: captured_calls.append(
            {"sink": sink, "query_id": query.id, **kwargs}
        ),
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["query_id"] == "query-a"
    assert captured_calls[0]["rows"] == [{"Value": 1}]


def test_kusto_query_loader_without_refresh_on_gather_filters_refresh_queries() -> None:
    query_a = KustoQuery(
        id="query-a",
        cluster="https://cluster",
        database="db",
        kql="A",
        section="A",
        render_as="table",
        confidence="high",
        validated=True,
    )
    query_b = KustoQuery(
        id="query-b",
        cluster="https://cluster",
        database="db",
        kql="B",
        section="B",
        render_as="table",
        confidence="high",
        validated=True,
        refresh_on_gather=True,
    )

    filtered = uil_loader_stage.kusto_query_loader_without_refresh_on_gather(
        lambda program_id, programs_root: (query_a, query_b)
    )

    assert filtered("demo", Path(".")) == (query_a,)
