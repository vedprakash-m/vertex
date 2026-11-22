from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.core.integration_types import (
    KustoHydrationOutput,
    KustoResultColumn,
    KustoResultKind,
    KustoResultSchema,
    KustoResultSet,
)
from src.core.kusto_signal_extractor import (
    KustoSignalExtractor,
    format_kusto_result_text,
    kusto_source_event_time,
)


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


def test_kusto_signal_extractor_emits_breach_verdict_when_slo_configured() -> None:
    """ADF-W2.3 (Section 8.5.2): the canonical spec example."""
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="samplealpha-safety-pass-rate",
                    rows=({"PassRate": 92.3},),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                    metric_id="Safety pass rate",
                    result_column="PassRate",
                    unit="%",
                    slo_target=95.0,
                    comparison=">=",
                    observed_value=92.3,
                    is_breach=True,
                    row_count=1,
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == "Safety pass rate = 92.3% against SLO >=95%: BREACH."
    assert result.signals[0].metadata["is_breach"] is True
    assert result.signals[0].metadata["observed_value"] == 92.3


def test_kusto_signal_extractor_emits_ok_verdict_when_slo_satisfied() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="samplealpha-safety-pass-rate",
                    rows=({"PassRate": 97.5},),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                    metric_id="Safety pass rate",
                    result_column="PassRate",
                    unit="%",
                    slo_target=95.0,
                    comparison=">=",
                    observed_value=97.5,
                    is_breach=False,
                    row_count=1,
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == "Safety pass rate = 97.5% against SLO >=95%: OK."


def test_kusto_signal_extractor_emits_measured_value_without_slo() -> None:
    """A metric_id can be configured without an SLO -- measured, no verdict."""
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="samplealpha-active-sev1",
                    rows=({"Count": 3},),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                    metric_id="Active Sev1 incidents",
                    result_column="Count",
                    observed_value=3.0,
                    row_count=1,
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == "Active Sev1 incidents = 3."
    assert result.signals[0].metadata["is_breach"] is None


def test_kusto_signal_extractor_flags_invalid_schema_when_result_column_missing() -> None:
    """ADF-W2.3 (Section 8.5.2) "invalid schema": metric_id/result_column
    configured but the actual result rows don't have that column."""
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="samplealpha-safety-pass-rate",
                    rows=({"SomeOtherColumn": 1},),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                    metric_id="Safety pass rate",
                    result_column="PassRate",
                    row_count=1,
                ),
            )
        ),
        "demo",
    )

    assert "invalid schema" in result.signals[0].text
    assert "PassRate" in result.signals[0].text
    assert result.signals[0].confidence.value == "high"  # rows exist, just misconfigured -- still a real observation


def test_kusto_signal_extractor_legacy_query_without_metric_id_uses_exact_facts() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="legacy-query",
                    rows=({"Value": 1}, {"Value": 2}),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == (
        "Kusto query legacy-query [kind=table, interpretation=unqualified]: "
        'admitted_rows=2; columns=[Value]; facts=[{"Value":1},{"Value":2}]; '
        "no qualified trend or SLO verdict."
    )


def test_kusto_signal_extractor_interprets_unbound_table_as_bounded_exact_facts() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="region-counts",
                    rows=(
                        {"Region": "west", "Count": 3},
                        {"Region": "east", "Count": 1},
                    ),
                    observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == (
        "Kusto query region-counts [kind=table, interpretation=unqualified]: "
        'admitted_rows=2; columns=[Region,Count]; facts=[{"Region":"west","Count":3},'
        '{"Region":"east","Count":1}]; no qualified trend or SLO verdict.'
    )


def test_kusto_signal_extractor_interprets_unbound_timeseries_without_inventing_trend() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="latency",
                    rows=(
                        {"Timestamp": "2026-11-19T12:00:00Z", "Value": 9.5},
                        {"Timestamp": "2026-11-19T10:00:00Z", "Value": 8.0},
                    ),
                    observed_at=datetime(2026, 11, 19, 12, 30, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == (
        "Kusto query latency [kind=timeseries, interpretation=unqualified]: "
        'admitted_rows=2; columns=[Timestamp,Value]; points=[{"Timestamp":"2026-11-19T10:00:00Z","Value":8.0},'
        '{"Timestamp":"2026-11-19T12:00:00Z","Value":9.5}]; '
        "data_as_of=2026-11-19T12:00:00+00:00; no qualified trend or SLO verdict."
    )
    assert "increased" not in result.signals[0].text
    assert "BREACH" not in result.signals[0].text


def test_kusto_signal_extractor_honors_typed_table_kind_over_row_heuristics() -> None:
    rows = (
        {"Timestamp": "2026-11-19T10:00:00Z", "Value": 8.0},
        {"Timestamp": "2026-11-19T12:00:00Z", "Value": 9.5},
    )
    text = format_kusto_result_text(
        KustoResultSet(
            query_id="snapshot-table",
            rows=rows,
            observed_at=datetime(2026, 11, 19, 12, 30, tzinfo=timezone.utc),
            row_count=2,
            schema=KustoResultSchema(
                kind=KustoResultKind.TABLE,
                columns=(
                    KustoResultColumn("Timestamp", "datetime"),
                    KustoResultColumn("Value", "real"),
                ),
                row_count=2,
                time_column=None,
                value_columns=(),
            ),
        )
    )

    assert "[kind=table, interpretation=unqualified]" in text
    assert "points=" not in text
    assert "trend=" not in text


def test_kusto_signal_extractor_preserves_typed_time_series_trend() -> None:
    rows = (
        {"ObservedAt": "2026-11-19T12:00:00Z", "Latency": 9.5},
        {"ObservedAt": "2026-11-19T10:00:00Z", "Latency": 8.0},
    )
    text = format_kusto_result_text(
        KustoResultSet(
            query_id="typed-latency",
            rows=rows,
            observed_at=datetime(2026, 11, 19, 12, 30, tzinfo=timezone.utc),
            row_count=2,
            schema=KustoResultSchema(
                kind=KustoResultKind.TIME_SERIES,
                columns=(
                    KustoResultColumn("ObservedAt", "datetime"),
                    KustoResultColumn("Latency", "real"),
                ),
                row_count=2,
                time_column="ObservedAt",
                value_columns=("Latency",),
            ),
        )
    )

    assert "[kind=timeseries, interpretation=unqualified]" in text
    assert '"ObservedAt":"2026-11-19T10:00:00Z","Latency":8.0' in text
    assert "trend=increasing" in text
    assert "trend=" not in text.replace("trend=increasing", "")


def test_kusto_signal_extractor_refuses_typed_schema_drift_without_values() -> None:
    text = format_kusto_result_text(
        KustoResultSet(
            query_id="drifted",
            rows=({"Name": "must-not-surface"},),
            observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
            row_count=1,
            schema=KustoResultSchema(
                kind=KustoResultKind.TABLE,
                columns=(KustoResultColumn("Count", "long"),),
                row_count=1,
            ),
        )
    )

    assert "[kind=table, interpretation=ambiguous]" in text
    assert "refusal=schema drift" in text
    assert "must-not-surface" not in text


def test_kusto_signal_extractor_uses_declared_measure_for_unbound_timeseries() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="latency-percentiles",
                    rows=(
                        {
                            "Timestamp": "2026-11-19T10:00:00Z",
                            "Primary": 8,
                            "Secondary": 12,
                        },
                    ),
                    result_column="Primary",
                    observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == (
        "Kusto query latency-percentiles [kind=timeseries, interpretation=unqualified]: "
        "admitted_rows=1; columns=[Timestamp,Primary,Secondary]; "
        'points=[{"Timestamp":"2026-11-19T10:00:00Z","Primary":8}]; '
        "data_as_of=2026-11-19T10:00:00+00:00; no qualified trend or SLO verdict."
    )
    assert '"Secondary":12' not in result.signals[0].text


@pytest.mark.parametrize(
    "rows,refusal",
    [
        (
            (
                {"Timestamp": "2026-11-19T10:00:00Z", "Primary": 1, "Secondary": 2},
                {"Timestamp": "2026-11-19T11:00:00Z", "Primary": 3, "Secondary": 4},
            ),
            "multiple numeric measures",
        ),
        (
            (
                {"Timestamp": "2026-11-19T10:00:00Z", "Value": 1},
                {"Timestamp": "2026-11-19T10:00:00Z", "Value": 2},
            ),
            "duplicate time points",
        ),
        (
            (
                {
                    "Timestamp": "2026-11-19T10:00:00Z",
                    "Snapshot": "2026-11-19T10:05:00Z",
                    "Value": 1,
                },
            ),
            "multiple time columns",
        ),
        (
            ({"Timestamp": "2026-11-19T10:00:00Z", "Status": "ok"},),
            "missing numeric measure",
        ),
        (
            (
                {
                    "Timestamp": "2026-11-19T10:00:00Z",
                    "Region": "west",
                    "Value": 1,
                },
            ),
            "multiple series dimensions",
        ),
        (
            ({"Timestamp": "not-a-time", "Value": 1},),
            "invalid time point",
        ),
    ],
)
def test_kusto_signal_extractor_marks_ambiguous_timeseries_without_claiming_a_series(
    rows: tuple[dict[str, object], ...], refusal: str,
) -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="ambiguous-series",
                    rows=rows,  # type: ignore[arg-type]
                    observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    text = result.signals[0].text
    assert "[kind=table, interpretation=ambiguous]" in text
    assert f"refusal={refusal}." in text
    assert "points=" not in text
    assert "data_as_of=" not in text
    assert "trend" not in text.lower()


@pytest.mark.parametrize(
    "rows,refusal",
    [
        (({"": 1},), "schema drift"),
        (({"Value": float("nan")},), "unsupported row value"),
        (({"Value": {"nested": 1}},), "unsupported row value"),
    ],
)
def test_kusto_signal_extractor_refuses_unsafe_unbound_values(
    rows: tuple[dict[str, object], ...], refusal: str,
) -> None:
    text = format_kusto_result_text(
        KustoResultSet(
            query_id="unsafe",
            rows=rows,  # type: ignore[arg-type]
            observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
        )
    )

    assert "[kind=table, interpretation=ambiguous]" in text
    assert f"refusal={refusal}." in text
    assert "nested" not in text


def test_kusto_signal_extractor_serializes_supported_null_date_and_datetime_values() -> None:
    text = format_kusto_result_text(
        KustoResultSet(
            query_id="typed-values",
            rows=(
                {
                    "Missing": None,
                    "Day": date(2026, 11, 19),
                    "When": datetime(2026, 11, 19, 10),
                },
            ),
            observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
        )
    )

    assert text == (
        "Kusto query typed-values [kind=table, interpretation=unqualified]: "
        'admitted_rows=1; columns=[Missing,Day,When]; facts=[{"Missing":null,'
        '"Day":"2026-11-19","When":"2026-11-19T10:00:00+00:00"}]; '
        "no qualified trend or SLO verdict."
    )


def test_kusto_signal_extractor_refuses_row_count_mismatch() -> None:
    text = format_kusto_result_text(
        KustoResultSet(
            query_id="count-mismatch",
            rows=({"Value": 1},),
            row_count=2,
            observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
        )
    )

    assert text == (
        "Kusto query count-mismatch [kind=table, interpretation=ambiguous]: "
        "admitted_rows=1; refusal=row count mismatch."
    )


def test_kusto_signal_formatter_handles_explicit_empty_table_without_schema() -> None:
    text = format_kusto_result_text(
        KustoResultSet(
            query_id="empty-table",
            rows=(),
            row_count=0,
            observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
        )
    )

    assert text == (
        "Kusto query empty-table [kind=table, interpretation=unqualified]: "
        "admitted_rows=0; no qualified trend or SLO verdict."
    )


def test_kusto_signal_extractor_refuses_schema_drift_without_raw_values() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="drifted",
                    rows=(
                        {"Name": "known"},
                        {"Name": "raw-secret", "Count": 2},
                    ),
                    observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == (
        "Kusto query drifted [kind=table, interpretation=ambiguous]: "
        "admitted_rows=2; refusal=schema drift."
    )
    assert "raw-secret" not in result.signals[0].text


def test_kusto_signal_extractor_refuses_partial_unbound_result_without_exact_facts() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="partial-table",
                    rows=({"Name": "must-not-be-interpreted"},),
                    observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
                    is_partial=True,
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == (
        "Kusto query partial-table [kind=table, interpretation=ambiguous]: "
        "admitted_rows=1; refusal=partial result."
    )
    assert "must-not-be-interpreted" not in result.signals[0].text


def test_kusto_signal_extractor_bounds_unbound_fact_preview_count() -> None:
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="many-rows",
                    rows=tuple({"Id": index} for index in range(4)),
                    observed_at=datetime(2026, 11, 19, 12, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    assert result.signals[0].text == (
        "Kusto query many-rows [kind=table, interpretation=unqualified]: "
        'admitted_rows=4; columns=[Id]; facts=[{"Id":0},{"Id":1},{"Id":2}]; '
        "omitted_rows=1; no qualified trend or SLO verdict."
    )


def test_kusto_signal_extractor_empty_unconfigured_result_emits_nothing() -> None:
    """Preserves the pre-existing silent-skip for a legacy query with zero
    rows -- no semantic config means no opinion about "expected" cardinality."""
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="legacy-empty",
                    rows=(),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                ),
            )
        ),
        "demo",
    )

    assert result.signals == ()


def test_kusto_signal_extractor_empty_configured_result_surfaces_data_gap() -> None:
    """ADF-W2.3 (Section 8.5.2) "query/data gap": a semantically-configured
    query returning zero rows is now an explicit, surfaced finding rather
    than a silent skip."""
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="samplealpha-safety-pass-rate",
                    rows=(),
                    observed_at=datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
                    metric_id="Safety pass rate",
                    result_column="PassRate",
                    row_count=0,
                ),
            )
        ),
        "demo",
    )

    assert len(result.signals) == 1
    assert "data gap" in result.signals[0].text
    assert "Safety pass rate" in result.signals[0].text


# ---------------------------------------------------------------------------
# ADF-W2.3: source-waiver policy integration
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402

from src.core.source_health import SourceWaiver  # noqa: E402
from src.core.source_waiver_store import (  # noqa: E402
    find_waiver_for_query,
    is_waiver_active,
)

_WAIVER = SourceWaiver(
    contract_id="acme.kusto.safety",
    role="telemetry",
    owner="owner@example.com",
    reason="Pipeline upgrade in progress; breach is expected through Q3.",
    granted=date(2026, 6, 1),
    expires=date(2026, 9, 30),
)


def _breach_result_set() -> KustoResultSet:
    return KustoResultSet(
        query_id="safety-pass-rate",
        rows=({"SafetyPassRate": 92.3},),
        observed_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        metric_id="Safety pass rate",
        result_column="SafetyPassRate",
        unit="%",
        slo_target=95.0,
        comparison=">=",
        observed_value=92.3,
        is_breach=True,
        row_count=1,
    )


def test_breach_signal_annotated_with_active_waiver() -> None:
    """ADF-W2.3 (Section 8.5.3): a breached Kusto metric whose telemetry source
    has an active waiver is annotated with '(waived: ...)' so it does not
    present as an un-contextualized alarm."""
    extractor = KustoSignalExtractor(waiver_lookup=lambda q: _WAIVER if q == "safety-pass-rate" else None)
    result = extractor.extract(
        KustoHydrationOutput(result_sets=(_breach_result_set(),)),
        "demo",
    )
    assert len(result.signals) == 1
    assert "BREACH" in result.signals[0].text
    assert "(waived" in result.signals[0].text
    assert "Pipeline upgrade" in result.signals[0].text
    assert "expires 2026-09-30" in result.signals[0].text
    assert result.signals[0].metadata["waiver_active"] is True
    assert result.signals[0].metadata["waiver_contract_id"] == "acme.kusto.safety"


def test_breach_signal_without_waiver_has_no_annotation() -> None:
    """A breach with no waiver (or no lookup injected) has no waiver suffix."""
    # No waiver_lookup injected (backward-compatible default)
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(result_sets=(_breach_result_set(),)),
        "demo",
    )
    assert len(result.signals) == 1
    assert "waived" not in result.signals[0].text
    assert result.signals[0].metadata["waiver_active"] is False
    assert result.signals[0].metadata["waiver_contract_id"] is None

    # Lookup injected but returns None for this query
    extractor = KustoSignalExtractor(waiver_lookup=lambda q: None)
    result2 = extractor.extract(
        KustoHydrationOutput(result_sets=(_breach_result_set(),)),
        "demo",
    )
    assert "waived" not in result2.signals[0].text
    assert result2.signals[0].metadata["waiver_active"] is False


def test_ok_signal_with_waiver_still_annotated() -> None:
    """An OK (non-breach) result with a waiver is still annotated -- the
    waiver applies to the source, not just to breaches."""
    ok_result = KustoResultSet(
        query_id="safety-pass-rate",
        rows=({"SafetyPassRate": 96.0},),
        observed_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        metric_id="Safety pass rate",
        result_column="SafetyPassRate",
        unit="%",
        slo_target=95.0,
        comparison=">=",
        observed_value=96.0,
        is_breach=False,
        row_count=1,
    )
    extractor = KustoSignalExtractor(waiver_lookup=lambda q: _WAIVER)
    result = extractor.extract(KustoHydrationOutput(result_sets=(ok_result,)), "demo")
    assert "OK" in result.signals[0].text
    assert "(waived" in result.signals[0].text


def test_waiver_lookup_failure_does_not_break_extraction() -> None:
    """A waiver-store failure (e.g. missing file) must never break signal
    extraction -- the breach signal is still emitted without the annotation."""

    def _broken_lookup(query_id: str) -> object:
        raise OSError("source_waivers.yaml not found")

    extractor = KustoSignalExtractor(waiver_lookup=_broken_lookup)
    result = extractor.extract(
        KustoHydrationOutput(result_sets=(_breach_result_set(),)),
        "demo",
    )
    assert len(result.signals) == 1
    assert "BREACH" in result.signals[0].text
    assert "waived" not in result.signals[0].text
    assert result.signals[0].metadata["waiver_active"] is False


# ---------------------------------------------------------------------------
# find_waiver_for_query + is_waiver_active unit tests
# ---------------------------------------------------------------------------


class _FakeTelemetry:
    def __init__(self, query_id: str) -> None:
        self.query_id = query_id


class _FakeSourceContract:
    def __init__(self, query_id: str) -> None:
        self.telemetry = _FakeTelemetry(query_id)


class _FakeSliceContract:
    def __init__(self, contract_id: str, query_id: str) -> None:
        self.id = contract_id
        self.source_contract = _FakeSourceContract(query_id)


def test_is_waiver_active_within_window() -> None:
    assert is_waiver_active(_WAIVER, today=date(2026, 7, 14)) is True
    assert is_waiver_active(_WAIVER, today=date(2026, 6, 1)) is True  # granted (inclusive)
    assert is_waiver_active(_WAIVER, today=date(2026, 9, 30)) is True  # expires (inclusive)


def test_is_waiver_active_outside_window() -> None:
    assert is_waiver_active(_WAIVER, today=date(2026, 5, 31)) is False  # before granted
    assert is_waiver_active(_WAIVER, today=date(2026, 10, 1)) is False  # after expires


def test_find_waiver_for_query_matches_via_slice_contract() -> None:
    contracts = (_FakeSliceContract("acme.kusto.safety", "safety-pass-rate"),)
    waiver = find_waiver_for_query("safety-pass-rate", (_WAIVER,), contracts, today=date(2026, 7, 14))
    assert waiver is not None
    assert waiver.contract_id == "acme.kusto.safety"


def test_find_waiver_for_query_returns_none_when_no_contract() -> None:
    """A query with no bound slice contract has no waiver path."""
    waiver = find_waiver_for_query("unknown-query", (_WAIVER,), (), today=date(2026, 7, 14))
    assert waiver is None


def test_find_waiver_for_query_returns_none_when_expired() -> None:
    """An expired waiver is not returned."""
    contracts = (_FakeSliceContract("acme.kusto.safety", "safety-pass-rate"),)
    waiver = find_waiver_for_query("safety-pass-rate", (_WAIVER,), contracts, today=date(2026, 10, 15))
    assert waiver is None


def test_find_waiver_for_query_returns_none_when_wrong_role() -> None:
    """A waiver for a non-telemetry role does not apply to Kusto telemetry."""
    advisory_waiver = SourceWaiver(
        contract_id="acme.kusto.safety",
        role="advisory",
        owner="owner@example.com",
        reason="Advisory role waived for review.",
        granted=date(2026, 6, 1),
        expires=date(2026, 9, 30),
    )
    contracts = (_FakeSliceContract("acme.kusto.safety", "safety-pass-rate"),)
    waiver = find_waiver_for_query("safety-pass-rate", (advisory_waiver,), contracts, today=date(2026, 7, 14))
    assert waiver is None


def test_find_waiver_for_query_empty_inputs() -> None:
    assert find_waiver_for_query("any", (), (), today=date(2026, 7, 14)) is None
    assert find_waiver_for_query("", (_WAIVER,), (_FakeSliceContract("c", "q"),), today=date(2026, 7, 14)) is None

def test_kusto_source_event_time_uses_supported_aliases_and_maximum() -> None:
    expected = datetime(2026, 5, 24, 18, 30, tzinfo=timezone.utc)

    actual = kusto_source_event_time(
        (
            {"Timestamp": datetime(2026, 5, 24, 10, 0)},
            {"Date": date(2026, 5, 24)},
            {"Event_Timestamp": "2026-05-24T11:00:00-07:00"},
            {"CreateDate": "not-a-date", "ResolvedDate": "2026-05-24T18:30:00Z"},
            {"Snapshot": "2026-05-24"},
            {"Timestamp": None, "Date": ""},
            {"Timestamp": datetime(2026, 5, 24, 18, 0, tzinfo=timezone.utc)},
        )
    )

    assert actual == expected
    assert actual.tzinfo == timezone.utc


def test_kusto_source_event_time_returns_none_for_unknown_or_malformed_rows() -> None:
    assert kusto_source_event_time(({"Revision": "2026-05-24T12:00:00Z"},)) is None
    assert kusto_source_event_time(({"Timestamp": "not-a-date"},)) is None
    assert kusto_source_event_time(()) is None


@pytest.mark.parametrize(
    "value",
    [
        None, "", " ", " 2026-05-24 ", "2026-05-24T12:00:00Z",
        "2026-05-24 12:00:00", "20260524", "2026-W21-7", "2026-05-24",
        "2026-05-24T12:00:00+05:30", "invalid", 0, True, [], {},
        date(2026, 5, 24), datetime(2026, 5, 24, 12),
        datetime(2026, 5, 24, 12, tzinfo=timezone.utc),
    ],
)
def test_kusto_source_event_time_matches_original_legacy_parser(value: object) -> None:
    from datetime import time
    from src.commands.gather_pipeline.ado_pipeline_stage import _parse_datetime
    from src.commands.gather_pipeline.support import parse_date

    expected = _parse_datetime(value)
    if expected is None:
        parsed_date = parse_date(value)
        if parsed_date is not None:
            expected = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    assert kusto_source_event_time(({" Timestamp ": value},)) == expected


def test_kusto_source_event_time_preserves_legacy_date_subclass_parse_precedence() -> None:
    class _SourceDate(date):
        def __str__(self) -> str:
            return "2026-05-25"

    assert kusto_source_event_time(({"Date": _SourceDate(2026, 5, 24)},)) == datetime(
        2026, 5, 25, tzinfo=timezone.utc
    )

    class _UnparseableDate(date):
        def __str__(self) -> str:
            return "invalid"

    assert kusto_source_event_time(({"Date": _UnparseableDate(2026, 5, 24)},)) == datetime(
        2026, 5, 24, tzinfo=timezone.utc
    )


def test_kusto_signal_extractor_emits_source_event_time_only_when_known() -> None:
    observed_at = datetime(2026, 5, 25, 12, tzinfo=timezone.utc)
    extracted = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="query-time",
                    rows=({"Timestamp": "2026-05-24T11:00:00-07:00", "Value": 2},),
                    observed_at=observed_at,
                ),
                KustoResultSet(
                    query_id="query-unknown-time",
                    rows=({"Revision": "2026-05-24T12:00:00Z", "Value": 3},),
                    observed_at=observed_at,
                ),
            )
        ),
        "demo",
    )

    assert extracted.signals[0].metadata["event_timestamp"] == "2026-05-24T18:00:00+00:00"
    assert extracted.signals[0].timestamp == observed_at
    assert "event_timestamp" not in extracted.signals[1].metadata
    assert extracted.signals[1].timestamp == observed_at
