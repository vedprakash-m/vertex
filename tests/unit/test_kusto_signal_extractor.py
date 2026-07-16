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


def test_kusto_signal_extractor_emits_breach_verdict_when_slo_configured() -> None:
    """ADF-W2.3 (Section 8.5.2): the canonical spec example."""
    result = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="xpf-safety-pass-rate",
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
                    query_id="xpf-safety-pass-rate",
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
                    query_id="xpf-active-sev1",
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
                    query_id="xpf-safety-pass-rate",
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


def test_kusto_signal_extractor_legacy_query_without_metric_id_keeps_row_count_text() -> None:
    """Full backward compatibility: an unconfigured query gets the exact
    pre-ADF-W2.3 text."""
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

    assert result.signals[0].text == "Kusto query legacy-query: 2 row(s) observed."


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
                    query_id="xpf-safety-pass-rate",
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