"""ADF-W5.5: src/core/cockpit_html.py -- HTML escaping and URL allowlist."""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    finalize_cockpit_snapshot,
)
from src.core.cockpit_html import render_cockpit_html

_NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def _snapshot(*, findings: tuple[CockpitFinding, ...] = ()) -> CockpitSnapshot:
    snap = CockpitSnapshot(
        schema_version="1", program_id="xpf", edition_id="xpf_weekly",
        generated_at=_NOW, as_of=_NOW,
        program_summary=ProgramCockpitSummary(
            overall_risk="green", readiness_percent=80, blocker_count=0, top_three_candidates=(), next_action=None
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=5, required_total=5, stale_sources=(), degraded_sources=(),
            manual_sources=(), newest_watermarks={},
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=0.5, verification_coverage=0.3, extraction_quality=(), contradiction_count=0
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=0.6, frontier_cost_usd=1.5, cache_hit_rate=0.2, context_tokens_in=100
        ),
        value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None),
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0,
            duplicate_preventions=0, audit_coverage=None,
        ),
        findings=findings,
        input_hash="",
    )
    return finalize_cockpit_snapshot(snap)


def _finding(**overrides: object) -> CockpitFinding:
    defaults: dict[str, object] = dict(
        finding_id="f1", area="program", status="warn", summary="A finding", detail="Detail text",
        owner=None, next_command=None, evidence_refs=(), observed_at=_NOW,
    )
    defaults.update(overrides)
    return CockpitFinding(**defaults)  # type: ignore[arg-type]


def test_renders_valid_html_document() -> None:
    html = render_cockpit_html(_snapshot())
    assert html.startswith("<!doctype html>")
    assert "<html lang=\"en\">" in html
    assert "xpf" in html


def test_no_external_script_or_stylesheet_references() -> None:
    html = render_cockpit_html(_snapshot())
    assert "<script" not in html
    assert 'rel="stylesheet"' not in html
    assert "http://" not in html.split("<style>")[0]  # no external CDN links in <head>


def test_hostile_finding_summary_is_escaped() -> None:
    hostile = _finding(summary='<script>alert(1)</script>', detail="<img src=x onerror=alert(2)>")
    html = render_cockpit_html(_snapshot(findings=(hostile,)))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_hostile_owner_and_next_command_are_escaped() -> None:
    hostile = _finding(owner="<b>alex</b>", next_command="vertex --flag=\"<script>x</script>\"")
    html = render_cockpit_html(_snapshot(findings=(hostile,)))
    assert "<b>alex</b>" not in html
    assert "&lt;b&gt;alex&lt;/b&gt;" in html
    assert "<script>x</script>" not in html


def test_http_https_evidence_refs_become_real_links() -> None:
    finding = _finding(evidence_refs=("https://dev.azure.com/org/project/_workitems/123",))
    html = render_cockpit_html(_snapshot(findings=(finding,)))
    assert '<a href="https://dev.azure.com/org/project/_workitems/123">' in html


def test_non_http_scheme_evidence_ref_is_not_a_link() -> None:
    finding = _finding(evidence_refs=("javascript:alert(1)",))
    html = render_cockpit_html(_snapshot(findings=(finding,)))
    assert "<a href=\"javascript:" not in html
    assert "javascript:alert(1)" in html  # rendered as plain escaped text


def test_file_scheme_evidence_ref_is_not_a_link() -> None:
    finding = _finding(evidence_refs=("file:///etc/passwd",))
    html = render_cockpit_html(_snapshot(findings=(finding,)))
    assert "<a href=\"file://" not in html


def test_plain_reference_id_is_not_a_link() -> None:
    finding = _finding(evidence_refs=("sig-abc123",))
    html = render_cockpit_html(_snapshot(findings=(finding,)))
    assert "<a href=" not in html
    assert "sig-abc123" in html


def test_no_findings_renders_a_placeholder() -> None:
    html = render_cockpit_html(_snapshot())
    assert "No findings." in html


def test_risk_color_reflects_overall_risk() -> None:
    green_html = render_cockpit_html(_snapshot())
    assert "#1a7f37" in green_html  # green


def test_skip_link_present_for_keyboard_navigation() -> None:
    html = render_cockpit_html(_snapshot())
    assert 'class="skip-link"' in html
    assert 'href="#main"' in html
    assert 'id="main"' in html


def test_viewport_meta_present_for_responsiveness() -> None:
    html = render_cockpit_html(_snapshot())
    assert 'name="viewport"' in html
