"""Direct coverage for the extracted shared Kusto gather helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.commands.gather_pipeline import kusto_query_helpers
from src.core.models_v2 import KustoQuery


def _kusto_query(query_id: str = "query-a") -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster="https://example.kusto.windows.net",
        database="ExampleDb",
        kql="Metrics | take 1",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="high",
        engine="kusto",
        result_column="P50",
        workstream_ids=("acme",),
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_build_kusto_signals_emits_signal_and_query_state(legacy: bool) -> None:
    query_state_sink: dict[str, dict[str, object]] = {}
    previous: dict = {"query-a": {"value_last_4": [4.2, 4.2, 4.2]}}
    if not legacy:
        previous = {}
        for _ in range(3):
            sink: dict = {}
            kusto_query_helpers.record_kusto_query_state(
                sink, _kusto_query(), rows=[{"P50": 4.2}],
                as_of=datetime(2026, 5, 9, tzinfo=timezone.utc), duration_ms=1,
                previous_state=previous.get("query-a"),
            )
            previous = sink

    signals = kusto_query_helpers.build_kusto_signals(
        queries=(_kusto_query(),),
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        executor=lambda query: [{"P50": 4.2, "Timestamp": "2026-05-10T07:00:00Z"}],
        query_state_sink=query_state_sink,
        previous_query_states=previous,
    )

    assert len(signals) == 1
    assert signals[0].source == "kusto"
    assert "Kusto query-a returned 1 row(s): P50=4.2" in signals[0].text
    state = query_state_sink["query-a"]
    assert state["last_cycle_succeeded"] is True
    assert state["data_freshness_ok"] is True
    assert state["value_last_4"] == ([4.2] if legacy else [4.2, 4.2, 4.2, 4.2])
    assert state["value_frozen_warning"] is (not legacy)
    assert state["history_contract_identity"]
    assert state["history_break_reason"] == "legacy_unverified"


def test_build_kusto_signal_shares_across_all_configured_workstreams() -> None:
    """BL-F2 (D-19) real fan-out fix, 2026-07-25: a query genuinely shared
    across multiple workstreams previously produced a signal with
    workstream_id=None, invisible to every section's filter -- not the
    documented "one section gets it, others get a placeholder" behavior.
    The signal must now carry all configured workstreams in workstream_ids,
    with the first as the backward-compat scalar workstream_id."""
    shared_query = replace(_kusto_query(), workstream_ids=("acme", "contoso"))

    signals = kusto_query_helpers.build_kusto_signals(
        queries=(shared_query,),
        program_id="acme",
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        executor=lambda query: [{"P50": 4.2, "Timestamp": "2026-05-10T07:00:00Z"}],
    )

    assert len(signals) == 1
    assert signals[0].workstream_id == "acme"
    assert signals[0].workstream_ids == ("acme", "contoso")


@pytest.mark.parametrize("value,unit,target,comparison,expected", [
    (0, "%", 0, ">=", "demo.p50 = 0% against SLO >=0%: OK."),
    (4.2, "s", 4, "<=", "demo.p50 = 4.2s against SLO <=4s: BREACH."),
    ("4.2%", "%", 5, "<", "demo.p50 = 4.2% against SLO <5%: OK."),
    ("1,000", None, None, None, "demo.p50 = 1000."),
    (4.2, "s", None, None, "demo.p50 = 4.2s."),
    (4.2, None, 4, None, "demo.p50 = 4.2."),
    (4.2, None, None, ">", "demo.p50 = 4.2."),
])
def test_bound_metric_signal_uses_declared_semantics_not_row_preview(
    value: int | float | str, unit: str | None, target: float | None,
    comparison: str | None, expected: str,
) -> None:
    query = replace(
        _kusto_query(), metric_id="demo.p50", unit=unit,
        slo_target=target, comparison=comparison,
    )
    as_of = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    rows = [{"Unrelated": "not metric prose", "Other": 99, "Third": 88, "P50": value}]
    signal = kusto_query_helpers.build_kusto_signal(query=query, rows=rows, program_id="acme", as_of=as_of)
    control = kusto_query_helpers.build_kusto_signal(
        query=query, rows=[{"P50": value}], program_id="acme", as_of=as_of,
    )

    assert signal is not None
    assert control is not None
    assert signal.text == expected
    assert signal.id == control.id
    assert signal.timestamp == control.timestamp
    assert signal.raw_ref == control.raw_ref
    assert signal.confidence == control.confidence
    assert signal.entity_refs == control.entity_refs
    assert signal.workstream_ids == ("acme",)
    assert signal.metadata == control.metadata


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


@pytest.mark.parametrize("metric_id", [None, "p50"])
@pytest.mark.parametrize("previous_values", [[], [7.5, 7.5, 7.5]])
def test_failed_numeric_result_cannot_advance_successful_history(
    metric_id: str | None, previous_values: list[float],
) -> None:
    state: dict[str, dict[str, object]] = {}
    previous = {
        "last_succeeded_at": "2026-05-09T08:00:00+00:00",
        "value_last_4": previous_values.copy(),
    }
    kusto_query_helpers.record_kusto_query_state(
        state, replace(_kusto_query(), metric_id=metric_id),
        rows=[{"P50": 7.5}],
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        duration_ms=250, error="query failed", previous_state=previous,
    )
    current = state["query-a"]
    assert current["last_cycle_succeeded"] is False
    assert current["is_degraded"] is True
    assert current["last_error"] == "query failed"
    assert current["last_succeeded_at"] == datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
    assert current.get("value_last_4", []) == previous_values
    assert current.get("value_frozen_warning", False) is False
    assert previous["value_last_4"] == previous_values
