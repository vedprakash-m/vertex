from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from src.core.html_renderer import HTMLRenderer
from src.core.view_models import KpiTile


def test_kusto_tile_partial_renders_standard_metric_tile() -> None:
    renderer = HTMLRenderer("acme_weekly")

    rendered = renderer.render_fragment(
        "_kusto_tile.j2",
        tile_width=180,
        tile=KpiTile(
            query_id="acme-deployment-velocity",
            label="Deploy P50 (hrs)",
            value="4.2",
            unit=None,
            trend=None,
            confidence="high",
            as_of=datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc),
            source_signal_id="signal-1",
        ),
    )

    assert "Deploy P50 (hrs)" in rendered
    assert ">4.2<" in rendered
    assert "As of May 05" in rendered


def test_kusto_tile_partial_renders_aggregate_incident_badges() -> None:
    renderer = HTMLRenderer("acme_weekly")

    rendered = renderer.render_fragment(
        "_kusto_tile.j2",
        tile_width=180,
        tile=KpiTile(
            query_id="acme-active-incidents",
            label="Active Incidents (Acme)",
            value="",
            unit=None,
            trend=None,
            confidence="high",
            as_of=None,
            source_signal_id="signal-2",
            result_payload={
                "Sev0Count": 1,
                "Sev1Count": 2,
                "Sev2Count": 3,
                "OldestAgeHours": 9,
                "OldestIncidentId": "ICM-12345",
                "OldestUrl": "https://portal.microsofticm.com/incidents/12345",
            },
        ),
    )

    assert "Sev0 1" in rendered
    assert "Sev1 2" in rendered
    assert "Sev2 3" in rendered
    assert "Oldest: 9h - ICM-12345" in rendered


def test_kusto_tile_partial_renders_clear_aggregate_tile() -> None:
    renderer = HTMLRenderer("acme_weekly")

    rendered = renderer.render_fragment(
        "_kusto_tile.j2",
        tile_width=180,
        tile=KpiTile(
            query_id="acme-active-incidents",
            label="Active Incidents (Acme)",
            value="",
            unit=None,
            trend=None,
            confidence="high",
            as_of=None,
            source_signal_id="signal-3",
            result_payload={"Sev0Count": 0, "Sev1Count": 0, "Sev2Count": 0},
        ),
    )

    assert "✓ No active Sev 0-2" in rendered


def test_kusto_tile_partial_renders_awaiting_data_and_validation_states() -> None:
    renderer = HTMLRenderer("acme_weekly")

    awaiting_data = renderer.render_fragment(
        "_kusto_tile.j2",
        tile_width=180,
        tile=KpiTile(
            query_id="acme-placeholder",
            label="Fleet Healthy %",
            value="",
            unit=None,
            trend=None,
            confidence="medium",
            as_of=None,
            source_signal_id=None,
            validated=False,
            refresh_on_gather=False,
            owner_alias="testowner",
        ),
    )
    awaiting_validation = renderer.render_fragment(
        "_kusto_tile.j2",
        tile_width=180,
        tile=KpiTile(
            query_id="acme-fleet-health",
            label="Fleet Healthy %",
            value="",
            unit=None,
            trend=None,
            confidence="medium",
            as_of=None,
            source_signal_id=None,
            validated=False,
            refresh_on_gather=True,
        ),
    )

    assert "Awaiting data" in awaiting_data
    assert "Owner: testowner" in awaiting_data
    assert "Awaiting validation - gather pending" in awaiting_validation


def test_kusto_tile_partial_renders_table_notice_and_tier4_footer() -> None:
    renderer = HTMLRenderer("acme_weekly")

    rendered = renderer.render_fragment(
        "_kusto_tile.j2",
        tile_width=180,
        tile=KpiTile(
            query_id="acme-table-kpi",
            label="Incidents Table",
            value="",
            unit=None,
            trend=None,
            confidence="medium",
            as_of=None,
            source_signal_id=None,
            render_mode="table",
            refresh_on_gather=True,
            reference_url="https://dash.example/kusto",
            catalog_source={
                "dashboard_name": "Ops Dashboard",
                "dashboard_id": "dash-001",
                "page_name": "Incident Rollup",
                "query_ref": "Q-12",
            },
        ),
    )

    assert "Data available - see CLI inspector" in rendered
    assert "inspect kusto --query acme-table-kpi" in rendered
    assert "Source: Dashboard" in rendered
    assert "Ops Dashboard" in rendered


def test_kpi_bar_partial_renders_tiles_from_workstream() -> None:
    renderer = HTMLRenderer("acme_weekly")
    ws = SimpleNamespace(
        kpi_tiles=(
            KpiTile(
                query_id="acme-deployment-velocity",
                label="Deploy P50 (hrs)",
                value="4.2",
                unit=None,
                trend=None,
                confidence="high",
                as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                source_signal_id="signal-kpi-1",
            ),
        )
    )

    rendered = renderer.render_fragment("partials/kpi_bar.j2", ws=ws)

    assert "Deploy P50 (hrs)" in rendered
    assert ">4.2<" in rendered
    assert "As of May 10" in rendered