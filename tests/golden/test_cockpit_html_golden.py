"""Golden test for src/core/cockpit_html.py's static HTML cockpit renderer
(specs/backlog.md BL-H2 action (5): "cockpit golden snapshots to prevent
silent visual regression").

tests/golden/test_cockpit_terminal.py already pins the terminal renderer;
this file closes the equivalent gap for the HTML renderer using the exact
same fixture-snapshot/golden-diff pattern. `render_cockpit_html` takes no
program-specific fixture; the same `CockpitSnapshot` construction is reused
here for parity with the terminal golden (values are shared where both
renderers surface the same fields; the HTML fixture is otherwise
self-contained so this file has no import-time dependency on the terminal
test module).

Builds a fixed, synthetic ``CockpitSnapshot`` (no live program data) and
freezes its render so the HTML output is pinned. Use ``pytest
--update-golden`` to regenerate the golden file.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
from pathlib import Path

from src.core.cockpit_html import render_cockpit_html
from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    ValueConfidence,
    ValueMetric,
    finalize_cockpit_snapshot,
)

GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"
_NOW = datetime(2026, 7, 12, 8, 0, 0, tzinfo=timezone.utc)


def _fixture_snapshot() -> CockpitSnapshot:
    findings = (
        CockpitFinding(
            finding_id="program.risk.high_active_count",
            area="program",
            status="warn",
            summary="2 active risk(s) at high probability*impact.",
            detail="Derived from the existing human-assessed risk register.",
            owner=None,
            next_command="vertex doctor --edition fixture_weekly",
            evidence_refs=("risk-1", "risk-2"),
            observed_at=_NOW,
        ),
        CockpitFinding(
            finding_id="economics.ai_telemetry.empty",
            area="economics",
            status="info",
            summary="No AI telemetry recorded yet.",
            detail="frontier_cost_usd and context_tokens_in are 0.",
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=_NOW,
        ),
        CockpitFinding(
            finding_id="source.health.not_probed",
            area="source",
            status="info",
            summary="Source health and watermarks are not probed yet (QG-38).",
            detail="Channel execution policy lands with ADF-W1.4/ADF-W2.2.",
            owner=None,
            next_command="vertex prefetch --program fixture_prog --channel workiq",
            evidence_refs=(),
            observed_at=_NOW,
        ),
    )
    snapshot = CockpitSnapshot(
        schema_version="1",
        program_id="fixture_prog",
        edition_id="fixture_prog_weekly",
        generated_at=_NOW,
        as_of=_NOW,
        program_summary=ProgramCockpitSummary(
            overall_risk="red",
            readiness_percent=None,
            blocker_count=2,
            top_three_candidates=(),
            next_action="vertex doctor --edition fixture_weekly",
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=1, required_total=3, stale_sources=("kusto",), degraded_sources=(), manual_sources=(), newest_watermarks={}
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=None, verification_coverage=None, extraction_quality=(), contradiction_count=0
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=None, frontier_cost_usd=0.0, cache_hit_rate=None, context_tokens_in=0
        ),
        value_summary=ValueCockpitSummary(
            metrics=(
                ValueMetric(
                    metric_id="report_wall_time_seconds",
                    program_id="fixture_prog",
                    edition_id="fixture_prog_weekly",
                    scope="program_aggregate",
                    label="Report wall-time (oldest retained run -> latest run)",
                    value=420.0,
                    unit="seconds",
                    confidence=ValueConfidence.MEASURED,
                    baseline_value=1200.0,
                    delta_value=780.0,
                    formula_version="run_telemetry.v1",
                    evidence_refs=("run:run-1", "run:run-9"),
                    period_start=_NOW,
                    period_end=_NOW,
                ),
            ),
            time_savings_certification=None,
        ),
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0, duplicate_preventions=1, audit_coverage=None
        ),
        findings=findings,
        input_hash="",
    )
    return finalize_cockpit_snapshot(snapshot)


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if update or not golden_path.exists():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        if not update:
            import pytest

            pytest.skip(f"Created new golden file: {name}.golden")
        return

    golden = golden_path.read_text(encoding="utf-8")
    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise AssertionError(f"Cockpit HTML output does not match golden file: {name}.golden\n\nDiff:\n{diff}")


def test_cockpit_html_contract_no_external_resources() -> None:
    """Section 10.3: local static HTML only -- no external CSS/JS, no network."""
    rendered = render_cockpit_html(_fixture_snapshot())
    assert "<script" not in rendered.lower(), "cockpit HTML must not embed external/inline JS"
    assert "http://" not in rendered.replace("http://https", ""), "no bare non-HTTPS external references expected in fixture"
    assert "<link" not in rendered.lower(), "cockpit HTML must not reference external stylesheets"


def test_cockpit_html_golden(update_golden: bool) -> None:
    rendered = render_cockpit_html(_fixture_snapshot())
    _compare_with_golden("cockpit_html", rendered, update_golden)
