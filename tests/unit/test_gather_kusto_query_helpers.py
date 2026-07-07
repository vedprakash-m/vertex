"""Direct coverage for the extracted shared Kusto gather helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from src.commands.gather_pipeline import kusto_query_helpers
from src.core.models_v2 import KustoQuery


def _kusto_query(query_id: str = "query-a") -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster="https://adventure.kusto.windows.net",
        database="xdataanalytics",
        kql="Metrics | take 1",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="high",
        engine="kusto",
        result_column="P50",
        workstream_ids=("acme",),
    )


def test_build_kusto_signals_emits_signal_and_query_state() -> None:
    query_state_sink: dict[str, dict[str, object]] = {}

    signals = kusto_query_helpers.build_kusto_signals(
        queries=(_kusto_query(),),
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        executor=lambda query: [{"P50": 4.2, "Timestamp": "2026-05-10T07:00:00Z"}],
        query_state_sink=query_state_sink,
        previous_query_states={"query-a": {"value_last_4": [4.2, 4.2, 4.2]}},
    )

    assert len(signals) == 1
    assert signals[0].source == "kusto"
    assert "Kusto query-a returned 1 row(s): P50=4.2" in signals[0].text
    state = query_state_sink["query-a"]
    assert state["last_cycle_succeeded"] is True
    assert state["data_freshness_ok"] is True
    assert state["value_last_4"] == [4.2, 4.2, 4.2, 4.2]
    assert state["value_frozen_warning"] is True


def test_record_kusto_query_state_preserves_last_success_on_error() -> None:
    query_state_sink: dict[str, dict[str, object]] = {}

    kusto_query_helpers.record_kusto_query_state(
        query_state_sink,
        _kusto_query("query-b"),
        rows=[],
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        duration_ms=250,
        error="boom",
        previous_state={"last_succeeded_at": "2026-05-09T08:00:00+00:00", "value_last_4": [7.5, 7.5, 7.5]},
    )

    state = query_state_sink["query-b"]
    assert state["last_cycle_succeeded"] is False
    assert state["last_error"] == "boom"
    assert state["last_succeeded_at"] == datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
    assert state["value_last_4"] == [7.5, 7.5, 7.5]
