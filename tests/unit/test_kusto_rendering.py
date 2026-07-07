from __future__ import annotations

from src.core.config_loader import KustoQuerySettings, KustoSettings
from src.core.exceptions import AuthError
from src.core.kusto_rendering import build_kusto_sections
from src.core.kusto_templates import KustoTemplateContext


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
