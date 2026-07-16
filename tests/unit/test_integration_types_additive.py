"""Unit tests for additive ProviderCapability/KustoResultSet fields (ADF-W0.17).

Verifies that existing producers/consumers compile unchanged: both types can be
constructed with their pre-existing argument lists (new fields default safely),
and the new ADF fields round-trip when supplied.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.integration_types import (
    DiscoveryCompleteness,
    HydrationMode,
    KustoResultSet,
    ProviderCapability,
)


def _base_capability_kwargs() -> dict:
    return dict(
        channel="ado",
        discovery_modes=(DiscoveryCompleteness.FULL,),
        hydration_modes=(HydrationMode.FULL,),
        supports_since=True,
        max_batch_size=200,
        rate_limit_rpm=None,
        retry_max_attempts=3,
        retry_backoff_seconds=0.5,
        privacy_class="internal",
        timeout_seconds=30,
    )


def test_provider_capability_constructs_with_pre_existing_arguments() -> None:
    cap = ProviderCapability(**_base_capability_kwargs())
    assert cap.channel == "ado"
    assert cap.supports_pagination is False
    assert cap.supports_relations is False
    assert cap.supports_full_content is False
    assert cap.supports_durable_identity is False
    assert cap.supports_cancellation is False
    assert cap.max_page_size is None
    assert cap.completeness_modes == ()


def test_provider_capability_accepts_new_additive_fields() -> None:
    cap = ProviderCapability(
        **_base_capability_kwargs(),
        supports_pagination=True,
        supports_relations=True,
        supports_full_content=True,
        supports_durable_identity=True,
        supports_cancellation=True,
        max_page_size=500,
        completeness_modes=("complete", "partial", "degraded"),
    )
    assert cap.supports_pagination is True
    assert cap.supports_relations is True
    assert cap.supports_full_content is True
    assert cap.supports_durable_identity is True
    assert cap.supports_cancellation is True
    assert cap.max_page_size == 500
    assert cap.completeness_modes == ("complete", "partial", "degraded")


def test_kusto_result_set_constructs_with_pre_existing_arguments() -> None:
    rs = KustoResultSet(
        query_id="xpf-safety",
        rows=({"pass_rate": 0.92},),
        observed_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    assert rs.query_id == "xpf-safety"
    assert rs.metric_id is None
    assert rs.unit is None
    assert rs.slo_target is None
    assert rs.comparison is None
    assert rs.observed_value is None
    assert rs.is_breach is None
    assert rs.is_partial is False
    assert rs.row_count is None


def test_kusto_result_set_accepts_metric_semantics_fields() -> None:
    rs = KustoResultSet(
        query_id="xpf-safety",
        rows=({"pass_rate": 0.923},),
        observed_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        metric_id="safety_pass_rate",
        result_column="pass_rate",
        unit="percent",
        slo_target=0.95,
        comparison=">=",
        observed_value=0.923,
        is_breach=True,
        is_partial=False,
        row_count=1,
    )
    assert rs.metric_id == "safety_pass_rate"
    assert rs.unit == "percent"
    assert rs.slo_target == 0.95
    assert rs.comparison == ">="
    assert rs.observed_value == 0.923
    assert rs.is_breach is True
    assert rs.row_count == 1


def test_provider_capability_is_frozen() -> None:
    cap = ProviderCapability(**_base_capability_kwargs())
    with pytest.raises(Exception):
        cap.supports_pagination = True  # type: ignore[misc]


def test_kusto_result_set_is_frozen() -> None:
    rs = KustoResultSet(query_id="q", rows=(), observed_at=datetime(2026, 7, 11, tzinfo=timezone.utc))
    with pytest.raises(Exception):
        rs.metric_id = "m"  # type: ignore[misc]
