"""Guards the D-09 / Phase 3 peel of continuity and rendering helpers."""
from __future__ import annotations

from src.core.quality_gates import evaluate_continuity_gates
from src.core.quality_gates.continuity import evaluate_continuity_gates as continuity_module_entry
from src.core.quality_gates.rendering import evaluate_outlook_compatibility_gate, find_visible_tool_attribution, visible_html_text


def _valid_continuity_html(issue_number: int = 77) -> str:
    return f"""
    <html>
        <body>
            <!-- vertex: manifest=12345678-1234-5678-1234-567812345678 -->
            <table data-vertex-block="brand-header"></table>
            <h1>Platform on PF | Issue {issue_number} | May 08, 2026</h1>
            <table data-vertex-block="cadence-note"><tr><td>Detailed edition cadence note.</td></tr></table>
            <table data-vertex-block="scorecard-band-primary"></table>
            <table data-vertex-block="scorecard-band-secondary"></table>
            <table data-vertex-block="exec-summary"><tr><td>Leadership ask: None this week.</td></tr></table>
            <table data-vertex-block="jump-to-section"></table>
            <table data-vertex-block="chapter-schie_map_day_gaps"></table>
        </body>
    </html>
    """


def test_continuity_entry_point_is_reexported() -> None:
    assert evaluate_continuity_gates is continuity_module_entry


def test_evaluate_continuity_gates_passes_for_valid_html() -> None:
    report = continuity_module_entry(html_content=_valid_continuity_html(), issue_number=77)

    assert report.passed is True
    assert report.qg_results == {
        "CG-01": True,
        "CG-02": True,
        "CG-03": True,
        "CG-04": True,
        "CG-05": True,
        "CG-06": True,
        "CG-07": True,
        "CG-08": True,
        "CG-09": True,
    }


def test_evaluate_continuity_gates_blocks_visible_tool_attribution() -> None:
    html = _valid_continuity_html().replace(
        "<table data-vertex-block=\"exec-summary\"><tr><td>Leadership ask: None this week.</td></tr></table>",
        "<table data-vertex-block=\"exec-summary\"><tr><td>Vertex manifest 12345678-1234-5678-1234-567812345678. Issue 077 remains blocked.</td></tr></table>",
    )

    report = continuity_module_entry(html_content=html, issue_number=77)

    assert report.passed is False
    assert report.qg_results["CG-07"] is False
    assert report.qg_results["CG-09"] is False


def test_visible_html_text_strips_comments_hidden_blocks_and_tags() -> None:
    html = """
    <html>
        <body>
            <!-- hidden comment -->
            <style>.x { color: red; }</style>
            <div>Visible <strong>signal</strong></div>
            <div style="display:none">Hidden text</div>
            <div max-height:0>Also hidden</div>
        </body>
    </html>
    """

    assert visible_html_text(html) == "Visible signal"


def test_find_visible_tool_attribution_detects_tool_or_manifest() -> None:
    assert find_visible_tool_attribution("Vertex generated this.") == "Vertex"
    assert (
        find_visible_tool_attribution("12345678-1234-5678-9234-567812345678")
        == "12345678-1234-5678-9234-567812345678"
    )


def test_evaluate_outlook_compatibility_gate_flags_style_blocks() -> None:
    result = evaluate_outlook_compatibility_gate(
        '<html><body><style>.x { color: red; }</style><table style="width:100%"></table></body></html>'
    )

    assert result.passed is False
    assert result.gate_id == "QG-18"
    assert "<style> block" in result.message


def test_evaluate_outlook_compatibility_gate_passes_for_inline_styled_tables() -> None:
    result = evaluate_outlook_compatibility_gate(
        '<html><body><table style="width:100%; background:#ffffff"><tr><td style="color:#111827">OK</td></tr></table></body></html>'
    )

    assert result.passed is True
    assert result.gate_id == "QG-18"
