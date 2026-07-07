from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.jinja_filters import attribution_tier4, build_anchor, delta_label, executive_html, format_datetime, kusto_tile_data, qg_summary, rich_text_html, risk_bg, risk_label
from src.core.models import DeltaKind, RiskLevel
from src.core.view_models import KpiTile


def test_risk_filters_return_canonical_tokens() -> None:
    assert risk_label(RiskLevel.HIGH) == "High"
    assert risk_label("unknown") == "Needs Input"
    assert risk_bg(RiskLevel.MEDIUM) == "#FFE699"
    assert risk_bg(RiskLevel.LOW) == "#B4E5A2"
    assert risk_bg(RiskLevel.DONE) == "#4EA72E"


def test_delta_label_formats_risk_and_eta_changes() -> None:
    assert delta_label(DeltaKind.RISK_UP, RiskLevel.LOW, RiskLevel.HIGH) == "▲ was Low"
    assert delta_label(DeltaKind.ETA_CHANGED, date(2026, 5, 1), date(2026, 5, 8)) == "05/01 → 05/08"


def test_build_anchor_and_datetime_summary_are_stable() -> None:
    assert build_anchor("Acme Adventure/SYN-A 100% Ramp Readiness") == "acme-adventure-syn-a-100-ramp-readiness"
    assert format_datetime(datetime(2026, 5, 5, 9, 30, tzinfo=timezone.utc)) == "May 05 2026, 09:30 UTC"


def test_qg_summary_formats_success_and_failures() -> None:
    assert qg_summary({"QG-4": True, "QG-5": True}) == "QG: All gates passed"
    assert qg_summary({"QG-4": False, "QG-5": True}) == "QG: failed QG-4"


def test_rich_text_html_preserves_explicit_markdown_links() -> None:
    from src.core.jinja_filters import configure_ado_web_url
    configure_ado_web_url(org="your-org", project="your-project")
    rendered = str(
        rich_text_html(
            "Tracked in [Example work item](https://dev.azure.com/example-org/ExampleProject/_workitems/edit/900103) and ADO#900110."
        )
    )

    assert 'href="https://dev.azure.com/example-org/ExampleProject/_workitems/edit/900103"' in rendered
    assert 'href="https://dev.azure.com/your-org/your-project/_workitems/edit/900110"' in rendered


def test_kusto_tile_data_formats_validation_and_table_notice_states() -> None:
    awaiting_data = kusto_tile_data(
        KpiTile(
            query_id="acme-placeholder",
            label="Awaiting KPI",
            value="",
            unit=None,
            trend=None,
            confidence="medium",
            as_of=None,
            source_signal_id=None,
            validated=False,
            refresh_on_gather=False,
            owner_alias="testowner",
        )
    )
    awaiting_validation = kusto_tile_data(
        KpiTile(
            query_id="acme-candidate",
            label="Candidate KPI",
            value="",
            unit=None,
            trend=None,
            confidence="medium",
            as_of=None,
            source_signal_id=None,
            validated=False,
            refresh_on_gather=True,
        )
    )
    table_notice = kusto_tile_data(
        KpiTile(
            query_id="acme-table",
            label="Table KPI",
            value="",
            unit=None,
            trend=None,
            confidence="medium",
            as_of=None,
            source_signal_id=None,
            render_mode="table",
            refresh_on_gather=True,
        )
    )

    assert awaiting_data["variant"] == "awaiting_data"
    assert awaiting_data["owner"] == "testowner"
    assert awaiting_validation["variant"] == "awaiting_validation"
    assert awaiting_validation["status"] == "Awaiting validation - gather pending"
    assert table_notice["variant"] == "table_notice"
    assert "inspect kusto --query acme-table" in table_notice["status"]


def test_attribution_tier4_renders_catalog_provenance_link() -> None:
    rendered = str(
        attribution_tier4(
            {
                "dashboard_name": "Fleet Health",
                "dashboard_id": "abc123",
                "page_name": "Overview",
                "query_ref": "Q-7",
            }
        )
    )

    assert 'href="https://dataexplorer.azure.com/dashboards/abc123"' in rendered
    assert 'Dashboard "<a href=' in rendered
    assert 'page "Overview" query Q-7.' in rendered

def test_rich_text_html_renders_prose_entities_once() -> None:
    html = str(rich_text_html("Ramp 9/15&#8211;9/28 &amp; EXD ≤ 50%"))

    assert "9/15–9/28" in html
    assert "&amp;#8211;" not in html
    assert "&amp;amp;" not in html


def test_executive_html_normalises_tables_sources_and_links() -> None:
    raw = (
        '<p style="margin:0 0 10px 0;">Body</p>'
        '<p style="margin:0 0 10px 0;">Source: mail</p>'
        '<a style="color:#2563EB; text-decoration:underline;" href="x">x</a>'
        '<table border="1"><tr><th>Item</th><th>Status</th></tr>'
        '<tr><th scope="rowgroup" colspan="2">Done</th></tr>'
        '<tr><td>A</td><td>Done</td></tr></table>'
    )
    html = str(executive_html(raw))
    assert "font-size:14px; line-height:21px;\">Body" in html
    assert "font-size:11px" in html and "color:#6b7280;\">Source: mail" in html
    assert "#1d4ed8" in html and "#2563EB" not in html
    assert 'border="1"' not in html
    assert str(executive_html(None)) == ""
