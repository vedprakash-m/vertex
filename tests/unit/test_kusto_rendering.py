from __future__ import annotations

import pytest

from src.core.config_loader import KustoQuerySettings, KustoSettings
from src.core.exceptions import AuthError
from src.core.kusto_rendering import build_kusto_sections
from src.core.kusto_templates import KustoTemplateContext


@pytest.mark.parametrize("fallback", [False, True])
def test_actual_report_cache_write_binds_executed_definition(fallback, tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    import src.core.kusto_rendering as rendering
    from src.core.chart_cache_store import chart_cache_contract_identity, load_chart_cache
    from src.core.models_v2 import KustoQuery

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line"}, chart_renderer_id="synthetic::line",
        fallback_kql="fallback" if fallback else None,
    )
    registry = MagicMock()
    registry.get_renderer.return_value.build.return_value = (b"synthetic-image", {})
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)

    def execute(current):
        return [] if fallback and current.kql == "primary" else [{"Value": 0}]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), execute,
        chart_registry=registry, chart_cache_loader=load_chart_cache, programs_root=tmp_path,
        program_id="synthetic", observed_at=datetime.now(timezone.utc),
    )
    actual = replace(query, id="synthetic__fallback", kql="fallback") if fallback else query
    entry = load_chart_cache("synthetic", actual.id, programs_root=tmp_path)
    assert entry.query_contract_hash == chart_cache_contract_identity(actual)
    equivalent = KustoQuery(
        id=actual.id, cluster=actual.cluster, database=actual.database, kql=actual.kql,
        section=actual.section, render_as=actual.render_as, confidence=actual.confidence,
        chart_config=actual.chart_config, chart_renderer_id=actual.chart_renderer_id,
    )
    assert entry.query_contract_hash == chart_cache_contract_identity(equivalent)
    from src.commands.chart_gather import _gather_single_chart

    def refuse_live(_query):
        pytest.fail("A matching report-produced cache should be reusable without another execution")

    result = _gather_single_chart(
        equivalent, "synthetic", refuse_live, datetime.now(timezone.utc), load_chart_cache, tmp_path,
    )
    assert result.cache_hit
    assert result.error is None
    if fallback:
        assert entry.query_contract_hash != chart_cache_contract_identity(query)
    assert sections
    assert observations[0].execution_state == "success"
    assert not warnings


@pytest.mark.parametrize("failure", ["rows", "bytes", "schema", "scalar", "query", "auth"])
def test_fallback_refusal_is_degraded_before_any_chart_or_output(failure: str) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary", section="Synthetic",
        render_as="chart_image", confidence="high", kusto_section_validates_slice=False,
        caveats=(), reference_url="https://example.test/telemetry", fallback_kql="fallback",
        result_limits=KustoResultLimits(("Value",), 2 if failure == "scalar" else 1, 2 if failure == "bytes" else 100),
        expected_cardinality="scalar_required" if failure == "scalar" else "zero_ok",
    )
    calls: list[str] = []

    def execute(current):
        calls.append(current.kql)
        if current.kql == "primary":
            # A scalar-required primary is invalid even before fallback.
            return [{"Value": 1}, {"Value": 2}] if failure == "scalar" else []
        if failure == "query":
            raise QueryError("synthetic refused fallback")
        if failure == "auth":
            raise AuthError("synthetic unavailable identity")
        return [{"Other" if failure == "schema" else "Value": value} for value in ([1, 2] if failure == "rows" else [1])]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), execute,
        chart_builder=lambda *_args: pytest.fail("Refused rows must never reach chart rendering"),
    )
    assert calls == (["primary"] if failure == "scalar" else ["primary", "fallback"])
    assert sections[0].is_degraded
    assert observations[0].execution_state == "degraded"
    assert observations[0].last_successful_fetch_at is None
    assert warnings


@pytest.mark.parametrize("fallback", ["valid", "empty", "legacy_error"])
def test_declared_fallback_preserves_valid_empty_and_legacy_failure_behavior(fallback: str) -> None:
    from src.core.kusto_result_limits import KustoResultLimits

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary", section="Synthetic",
        render_as="table", confidence="high", kusto_section_validates_slice=False,
        caveats=(), reference_url=None, fallback_kql="fallback", result_limits=KustoResultLimits(("Value",), 1, 100),
    )
    calls: list[str] = []

    def execute(current):
        calls.append(current.kql)
        if current.kql == "primary":
            return []
        if fallback == "legacy_error":
            raise RuntimeError("Pre-existing noncontract failure")
        return [{"Value": 0}] if fallback == "valid" else []

    sections, observations, warnings = build_kusto_sections(KustoSettings(enabled=True, queries=(query,)), execute)
    assert calls == ["primary", "fallback"]
    assert not warnings
    if fallback == "valid":
        assert observations[0].query_id == "synthetic__fallback"
        assert observations[0].execution_state == "success"
        assert sections[0].rows
    else:
        assert observations[0].execution_state == "empty"


def test_build_kusto_sections_supports_all_render_modes() -> None:
    settings = KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="table-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="table query",
                section="Incident Summary",
                render_as="table",
                confidence="medium",
                kusto_section_validates_slice=False,
                caveats=("Synthetic data",),
                reference_url="https://dash.example/incidents",
            ),
            KustoQuerySettings(
                id="metric-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="metric query",
                section="Latency Snapshot",
                render_as="metric_highlight",
                confidence="high",
                kusto_section_validates_slice=True,
                caveats=(),
                reference_url=None,
            ),
            KustoQuerySettings(
                id="chart-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="chart query",
                section="Fleet Health",
                render_as="chart_image",
                confidence="high",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url="https://dash.example/fleet",
            ),
        ),
    )

    sections, observations, warnings = build_kusto_sections(
        settings,
        _query_results,
        chart_builder=lambda query, rows: "data:image/png;base64,ZmFrZQ==" if query.id == "chart-query" else None,
        observed_at=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 7, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
    )

    assert warnings == ()
    assert [section.render_mode for section in sections] == ["table", "metric_highlight", "chart_image"]
    assert [observation.execution_state for observation in observations] == ["success", "success", "success"]
    assert observations[1].kusto_section_validates_slice is True
    assert sections[0].rows[0][-1].href == "https://portal.microsofticm.com/imp/v3/incidents/details/12345"
    assert sections[1].metrics[0].label == "P50 Hours"
    assert sections[1].metrics[0].value == "4.2"
    assert sections[2].image_data_url == "data:image/png;base64,ZmFrZQ=="


def test_build_kusto_sections_degrades_auth_failures_to_reference_links() -> None:
    settings = KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="mttr-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="mttr query",
                section="Incident MTTR",
                render_as="metric_highlight",
                confidence="medium",
                kusto_section_validates_slice=False,
                caveats=("Auth required",),
                reference_url="https://dash.example/mttr",
            ),
            KustoQuerySettings(
                id="auth-no-link",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="auth no link",
                section="Hidden Section",
                render_as="table",
                confidence="low",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url=None,
            ),
        ),
    )

    sections, observations, warnings = build_kusto_sections(settings, _auth_fail_results)

    assert len(sections) == 1
    assert [observation.execution_state for observation in observations] == ["degraded", "degraded"]
    assert sections[0].is_degraded is True
    assert sections[0].reference_url == "https://dash.example/mttr"
    assert "Run vertex admin auth setup" in sections[0].message
    assert any("mttr-query" in warning for warning in warnings)
    assert any("auth-no-link" in warning for warning in warnings)


def test_build_kusto_sections_renders_query_templates_before_execution() -> None:
    settings = KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="templated-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql='Telemetry | where Program == "{program_id}" | where AreaPath == "{area_path}" | where Timestamp > ago({date_range}) | take 1',
                section="Telemetry",
                render_as="table",
                confidence="medium",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url=None,
            ),
        ),
    )
    rendered_kql: list[str] = []

    def _capture_query(query: KustoQuerySettings) -> list[dict[str, object]]:
        rendered_kql.append(query.kql)
        return [{"Count": 1}]

    build_kusto_sections(
        settings,
        _capture_query,
        template_context=KustoTemplateContext(
            program_id="acme",
            area_paths=("One\\Adventure\\Acme",),
            date_window_days=14,
        ),
    )

    assert rendered_kql == [
        'Telemetry | where Program == "acme" | where AreaPath == "One\\Adventure\\Acme" | where Timestamp > ago(14d) | take 1'
    ]


def _query_results(query: KustoQuerySettings) -> list[dict[str, object]]:
    if query.id == "table-query":
        return [
            {
                "IncidentId": "ICM-12345",
                "Severity": "3",
                "Title": "Fleet capacity alert",
                "IncidentUrl": "https://portal.microsofticm.com/imp/v3/incidents/details/12345",
            }
        ]
    if query.id == "metric-query":
        return [{"P50Hours": 4.2, "P90Hours": 7.8}]
    if query.id == "chart-query":
        return [
            {"Date": "2026-05-01", "HealthyPct": 98.2},
            {"Date": "2026-05-02", "HealthyPct": 98.9},
        ]
    raise AssertionError(f"Unexpected query id: {query.id}")


def _auth_fail_results(query: KustoQuerySettings) -> list[dict[str, object]]:
    raise AuthError(f"credential unavailable for {query.id}")
