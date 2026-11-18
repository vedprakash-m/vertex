from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import pytest

from src.core import jinja_filters as filters
from src.core.exceptions import ConfigError
from src.core.jinja_filters import attribution_tier4, build_anchor, delta_label, executive_html, format_datetime, kusto_tile_data, qg_summary, rich_text_html, risk_bg, risk_label
from src.core.models import DeltaKind, RiskLevel, ScorecardEvidencePacket
from src.core.view_models import KpiTile


def test_risk_filters_return_canonical_tokens() -> None:
    assert risk_label(RiskLevel.HIGH) == "High"
    assert risk_label("unknown") == "Needs Input"
    assert risk_bg(RiskLevel.MEDIUM) == "#FFE699"
    assert risk_bg(RiskLevel.LOW) == "#B4E5A2"
    assert risk_bg(RiskLevel.DONE) == "#4EA72E"


def test_legacy_uppercase_risk_renders_as_needs_input() -> None:
    risk = RiskLevel("UNKNOWN")

    assert risk_label(risk) == "Needs Input"
    assert risk_bg(risk) == risk_bg(RiskLevel.UNKNOWN)


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


@pytest.mark.parametrize("widths", [(376, 132, 132), (160, 240, 130, 110), (140, 310, 120, 70)])
@pytest.mark.parametrize("tag", ["th", "td"])
def test_executive_html_includes_padding_in_numeric_column_budgets(widths: tuple[int, ...], tag: str) -> None:
    cells = "".join(f'<{tag} width="{width}">Column {index}</{tag}>' for index, width in enumerate(widths))
    raw = (
        f'<table width="640"><tr>{cells}</tr>'
        f'<tr><th scope="rowgroup" colspan="{len(widths)}">Group</th></tr>'
        '<tr><td rowspan="2" align="right" style="color:#166534; white-space:nowrap;">'
        '<a href="https://example.com/evidence">Evidence</a></td></tr></table>'
    )
    html = str(executive_html(raw))

    for index, width in enumerate(widths):
        assert f'<{tag} width="{width - 16}" style="width:{width - 16}px; padding:7px 8px;' in html
        assert f'Column {index}</{tag}>' in html
    assert sum(width - 16 + 16 for width in widths) == 640
    assert f'scope="rowgroup" colspan="{len(widths)}"' in html
    assert '<td rowspan="2" align="right" style="padding:7px 8px;' in html
    assert 'white-space:nowrap;' in html and 'color:#166534;' in html
    assert '<a href="https://example.com/evidence">Evidence</a>' in html


@pytest.mark.parametrize("width", ["0", "1", "16"])
@pytest.mark.parametrize("tag", ["th", "td"])
def test_executive_html_refuses_numeric_budgets_without_room_for_padding(width: str, tag: str) -> None:
    with pytest.raises(ConfigError, match="column width must exceed 16px"):
        executive_html(f'<table><tr><{tag} width="{width}">Evidence</{tag}></tr></table>')


def test_executive_html_retains_smallest_valid_budget_and_nonnumeric_width_behavior() -> None:
    html = str(executive_html(
        '<table><tr><th width="17">Narrow</th><th width="50%">Percentage</th>'
        '<td>Widthless</td></tr></table>'
    ))
    assert '<th width="1" style="width:1px; padding:7px 8px;' in html
    assert '<th width="50%"' in html and ">Percentage</th>" in html
    assert '<td style="padding:7px 8px;' in html and ">Widthless</td>" in html


@pytest.mark.parametrize(
    ("value", "label", "short_label"),
    [(None, "Needs Input", "--"), ("needs input", "Needs Input", "--"),
     (" HIGH ", "High", "H"), ("medium", "Medium", "M"),
     ("low", "Low", "L"), ("done", "Done", "Done")],
)
def test_risk_filter_input_normalization(value: str | None, label: str, short_label: str) -> None:
    assert filters.risk_label(value) == label
    assert filters.risk_short_label(value) == short_label


def test_risk_filters_refuse_unsupported_input() -> None:
    with pytest.raises(ValueError, match="Unsupported RiskLevel"):
        filters.risk_label("invalid-assessment")


@pytest.mark.parametrize("formatter", [filters.format_date, filters.format_datetime, filters.format_datetime_pacific])
@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_date_filters_preserve_unavailable_values(
    formatter: Callable[[date | str | None], str], value: str | None,
) -> None:
    assert formatter(value) == (value or "")


@pytest.mark.parametrize(
    "value",
    [date(2026, 1, 5), datetime(2026, 1, 5, 0), "2026-01-05T00:00:00Z",
     datetime(2026, 1, 5, 2, tzinfo=timezone(timedelta(hours=2)))],
)
def test_date_filters_normalize_supported_values_to_utc(value: date | str) -> None:
    assert filters.format_date(value) == "Jan 05, 2026"
    assert filters.format_datetime(value) == "Jan 05 2026, 00:00 UTC"
    assert filters.format_datetime_pacific(value) == "Jan 04 2026, 04:00 PM PST"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [(DeltaKind.RISK_DOWN, "\u25bc was High"), (DeltaKind.NEW, "\u25cf NEW"),
     (DeltaKind.CLOSED, "\u2713 Closed"), (DeltaKind.OWNER_CHANGED, "\u21ba Owner changed"),
     (None, "\u2014 no change"), ("unchanged", "\u2014 no change")],
)
def test_delta_filters_preserve_kind_labels(kind: DeltaKind | str | None, expected: str) -> None:
    assert filters.delta_label(kind, old=RiskLevel.HIGH) == expected
    assert filters.delta_bg(kind).startswith("#")
    assert filters.delta_fg(kind).startswith("#")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
     (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (111, "111th")],
)
def test_ordinal_suffix_boundaries(value: int, expected: str) -> None:
    assert filters.ordinal(value) == expected


@pytest.mark.parametrize(("risk_load", "expected"), [(-1.0, 0), (0.0, 0), (1.5, 40), (3.0, 80), (4.0, 80)])
def test_risk_load_bar_bounds(risk_load: float, expected: int) -> None:
    assert filters.risk_load_bar_width(risk_load) == expected
    assert filters.risk_load_bar_width(risk_load, width=160) == 2 * expected


def test_labels_and_top_item_tokens_keep_configured_and_fallback_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filters, "_SCORECARD_SHORT_LABELS", {})
    filters.configure_scorecard_labels({"Readiness": " Ready ", "Count": 7, "Empty": "", "Invalid": []})
    assert filters.scorecard_short_label("Readiness") == "Ready"
    assert filters.scorecard_short_label("Count") == "7"
    assert filters.scorecard_short_label("Invalid") == "Invalid"
    assert filters.scorecard_short_label(None) == ""
    filters.configure_scorecard_labels(None)
    assert filters.scorecard_short_label("Readiness") == "Readiness"
    assert filters.top_item_icon(" ASK ") == "\U0001f534"
    assert filters.top_item_border("win") == "#047857"
    assert filters.top_item_border(None) == "#92400E"
    assert filters.top_item_icon("unsupported") == "\U0001f7e1"
    assert filters.build_anchor(None) == "section"
    assert filters.build_anchor("!!!") == "section"
    assert filters.pluralize(1, "item") == "item"
    assert filters.pluralize(2, "item") == "items"
    assert filters.pluralize(2, "person", "people") == "people"
    assert filters.qg_summary(None) == "QG: pending"
    assert filters.qg_summary({"QG-8": False, "QG-4": False}) == "QG: failed QG-4, QG-8"
    assert filters.risk_vector_label("New High") == "New High"


def test_ado_links_are_optional_and_escape_surrounding_html(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filters, "_ADO_WEB_ORG", "")
    monkeypatch.setattr(filters, "_ADO_WEB_PROJECT", "")
    assert filters.linkify_ado_tokens_markdown(None) == ""
    assert filters.linkify_ado_tokens_markdown("") == ""
    assert filters.linkify_ado_tokens_markdown("See ADO#101") == "See ADO#101"
    assert "href=" not in str(filters.rich_text_html("See ADO#101 & <evidence>"))
    assert "&amp; &lt;evidence&gt;" in str(filters.rich_text_html("See ADO#101 & <evidence>"))
    filters.configure_ado_web_url(org="example-org", project="ExampleProject")
    assert filters.linkify_ado_tokens_markdown("See ADO#101") == (
        "See [ADO#101](https://dev.azure.com/example-org/ExampleProject/_workitems/edit/101)"
    )


def test_rich_text_handles_lists_bold_and_authored_html_blocks() -> None:
    raw_table = '<table width="640">\n<tr><td>Evidence</td></tr>\n</table>'
    raw_paragraph = '<p style="color:#111827;">Authored</p>'
    result = str(filters.rich_text_html(
        "**First** line\ncontinued\n\n- __One__\n* Two\n\n" + raw_table + "\n" + raw_paragraph + "\nAfter"
    ))
    assert "<strong>First</strong> line continued" in result
    assert '<li style="margin:0 0 6px 0;"><strong>One</strong></li>' in result
    assert '<li style="margin:0 0 6px 0;">Two</li>' in result
    assert raw_table in result and raw_paragraph in result
    assert result.index("</ul>") < result.index(raw_table) < result.index(raw_paragraph) < result.index(">After</p>")
    assert str(filters.rich_text_html(None)) == ""
    assert str(filters.rich_text_html("  ")) == ""


@pytest.mark.parametrize("prior", [None, RiskLevel.MEDIUM])
def test_evidence_tooltip_retains_counts_and_prior_risk(prior: RiskLevel | None) -> None:
    packet = ScorecardEvidencePacket(
        dimension_name="Readiness", dimension_description="", total_items=5,
        items_by_risk={"high": 2}, stale_items=(101,), stale_count=1,
        overdue_items=(102, 103), overdue_count=2, blocked_items=(), blocked_count=0,
        unowned_items=(), unowned_count=0, high_activity_items=(),
        prior_confirmed_risk=prior, author_risk=None, ado_query_url="", item_links=(),
    )
    expected_prior = "Medium" if prior is not None else "None"
    assert filters.evidence_tooltip(packet) == f"5 items \u00b7 2 High \u00b7 1 stale \u00b7 2 overdue \u00b7 Prior: {expected_prior}"
    assert filters.evidence_tooltip(None) == "No evidence available"


def test_kusto_mapping_input_preserves_metric_and_reference_metadata() -> None:
    catalog = {"dashboard": "Health", "page": "Overview", "query": "Q1"}
    result = filters.kusto_tile_data({
        "query_id": "availability", "value": "99.9", "as_of": "2026-01-05T00:00:00Z",
        "unit": "%", "trend": "up", "source_signal_id": "signal-1",
        "owner_alias": "testowner", "reference_url": "https://example.com/evidence",
        "catalog_source": catalog, "shared": True,
    })
    assert result == {
        "variant": "metric", "label": "availability", "value": "99.9",
        "as_of": datetime(2026, 1, 5, tzinfo=timezone.utc),
        "reference_url": "https://example.com/evidence", "catalog_source": catalog, "shared": True,
    }
    assert filters.kusto_tile_data(None) == {}
    assert filters.kusto_tile_data({})["label"] == "KPI"


@pytest.mark.parametrize("total", [0, 2])
def test_incident_tile_aggregate_uses_aliases_and_valid_fallback_counts(total: int) -> None:
    result = filters.kusto_tile_data({
        "label": "Incidents",
        "result_payload": {
            "Sev0Count": "invalid", "sev0_count": str(total), "sev1": 0, "sev2_count": 0,
            "OldestAgeHours": {}, "oldest_age_hours": "12",
            "OldestIncidentId": " ", "oldest_incident_id": "INC-101",
            "oldest_url": "https://example.com/incidents/101",
        },
    })
    if total == 0:
        assert result["variant"] == "aggregate_clear"
        assert result["status"] == "\u2713 No active Sev 0-2"
    else:
        assert result["variant"] == "aggregate_badges"
        assert [badge["value"] for badge in result["badges"]] == [2, 0, 0]
        assert result["footer"] == "Oldest: 12h - INC-101"
        assert result["footer_href"] == "https://example.com/incidents/101"


def test_incident_tile_missing_age_or_identity_does_not_invent_footer() -> None:
    result = filters.kusto_tile_data({"result_payload": {"sev0": 1, "sev1": 0, "sev2": 0}})
    assert result["variant"] == "aggregate_badges"
    assert result["footer"] is None


def test_catalog_attribution_requires_complete_provenance_and_escapes_text() -> None:
    assert str(filters.attribution_tier4(None)) == ""
    assert str(filters.attribution_tier4({"dashboard": "Health"})) == ""
    rendered = str(filters.attribution_tier4({"dashboard": "<Health>", "page": "A&B", "query": "<Q1>"}))
    assert rendered == 'Source: Dashboard "&lt;Health&gt;" page "A&amp;B" query &lt;Q1&gt;.'
    assert "href=" not in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "0 B"), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1024 * 1024, "1.0 MB")],
)
def test_filesizeformat_unit_boundaries(value: int | None, expected: str) -> None:
    assert filters.filesizeformat(value) == expected
