from __future__ import annotations

from datetime import datetime, timezone

from src.core.integration_types import KustoHydrationOutput, KustoResultSet
from src.core.kusto_signal_extractor import KustoSignalExtractor


def test_kusto_signal_extractor_fans_out_per_workstream() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="query-a",
                    rows=({"Value": 1},),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                    workstream_ids=("ws-a", "ws-b"),
                ),
            )
        ),
        "demo",
    )

    assert [signal.id for signal in result.signals] == [
        result.signals[0].id,
        result.signals[1].id,
    ]
    assert {signal.workstream_id for signal in result.signals} == {"ws-a", "ws-b"}
    assert all(signal.source == "kusto" for signal in result.signals)
    assert {signal.entity_refs for signal in result.signals} == {
        ("kusto:query-a", "WS:ws-a"),
        ("kusto:query-a", "WS:ws-b"),
    }


def test_kusto_signal_extractor_preserves_structured_work_item_and_incident_refs() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="query-b",
                    rows=(
                        {
                            "WorkItemId": 12345,
                            "IncidentId": "98765",
                            "Summary": "Mitigation tracking for WI:23456 remains blocked.",
                        },
                    ),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                    workstream_ids=("ws-a",),
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].entity_refs == (
        "kusto:query-b",
        "WS:ws-a",
        "ICM:98765",
        "WI:12345",
        "WI:23456",
    )