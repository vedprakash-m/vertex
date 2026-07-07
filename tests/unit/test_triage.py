from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.coverage_gap import CoverageGap
from src.core.forecast_engine import ETAForecast
from src.core.issue_projection import IssueProjection
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import Signal
from src.core.quality_gates import GateEvaluation, QualityGateReport
from src.core.triage import ReadinessAssessment, TriageReport, build_readiness_assessment, finalize_triage_report, render_triage_report
from src.core.vitality_scorer import VitalitySummary


def test_build_readiness_assessment_uses_quality_and_gap_counts() -> None:
    readiness = build_readiness_assessment(
        quality_gate_report=QualityGateReport(
            results=(
                GateEvaluation("QG-1", True, "Freshness gate passed.", 3),
                GateEvaluation("QG-4", True, "Ban-list validation passed.", 3),
                GateEvaluation("QG-5", True, "Verbosity validation passed.", 3),
                GateEvaluation("QG-6", True, "Manifest hash matches snapshot.", 3),
                GateEvaluation("QG-8", False, "Missing risk levels for: Demo", 3),
            )
        ),
        unreviewed_signal_count=3,
        missing_narrative_count=2,
        total_narrative_count=7,
        missing_override_count=2,
        total_override_count=14,
        coverage_gap_count=4,
    )

    assert readiness.score == 73
    assert readiness.summary == "Draft readiness: 73% — 2 narratives missing, 3 signals unreviewed, 2 overrides missing, 4 coverage gaps."


def test_finalize_triage_report_surfaces_forecasts_and_gate_failures() -> None:
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    item = WorkItem(
        id=36830830,
        type="Feature",
        title="UD Chunking Fix",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Sprint 1",
        target_date=date(2026, 7, 10),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={"changed_date": as_of.isoformat()},
        revisions=[],
        comments=[],
        fetched_at=as_of,
    )
    triage = finalize_triage_report(
        edition_name="acme_weekly",
        issue_number=78,
        program_id="acme",
        quality_gate_report=QualityGateReport(results=(GateEvaluation("QG-8", False, "Missing risk levels for: Demo", 3),)),
        unreviewed_signals=(
            Signal(
                id="sig-1",
                timestamp=as_of,
                source="workiq/email",
                program_id="acme",
                workstream_id="deployment_readiness",
                entity_refs=("WI:36830830",),
                text="Needs review",
                raw_ref=None,
                confidence=Confidence.MEDIUM,
                metadata=None,
                thread_id=None,
            ),
        ),
        missing_narrative_ids=("deployment_readiness",),
        total_narrative_count=3,
        missing_override_names=("Deployment Velocity",),
        total_override_count=4,
        coverage_gaps=(CoverageGap(work_item_id=37777351, title="SCHIE Auth", state="Active", assigned_to=None, confidence=Confidence.HIGH),),
        eta_forecasts={
            36830830: ETAForecast(
                work_item_id=36830830,
                ado_target_date=date(2026, 7, 10),
                predicted_target_date=date(2026, 7, 17),
                confidence=Confidence.LOW,
                slip_probability=0.78,
                reasoning="2 prior slips in 90 days",
                prior_slips=2,
                p50_date=date(2026, 7, 12),
                p80_date=date(2026, 7, 17),
                p95_date=date(2026, 7, 24),
            )
        },
        items=(item,),
        coverage_gap_window_days=14,
        vitality_summary=VitalitySummary(
            total_items=2,
            updated_this_week=1,
            updated_this_week_percentage=50,
            freshness_average_days=9.0,
            stale_owner_aliases=("owner",),
        ),
    )

    assert triage.exit_code == 3
    assert triage.blockers == ("QG-8: Missing risk levels for: Demo",)
    assert any("vertex signals review --program acme" in line for line in triage.needs_attention)
    assert any("UD Chunking Fix" in line for line in triage.needs_attention)
    assert any("forecast p50 Jul 12, p80 Jul 17, p95 Jul 24" in line for line in triage.needs_attention)
    assert triage.ready[0] == "2/3 workstream narratives written"
    assert triage.vitality_enabled is True
    assert triage.vitality_summary is not None
    assert triage.vitality_summary.updated_this_week_percentage == 50


def test_render_triage_report_includes_coverage_gap_confidence() -> None:
    report = TriageReport(
        edition_name="acme_weekly",
        issue_number=78,
        program_id="acme",
        readiness=ReadinessAssessment(
            score=84,
            quality_gate_pass_rate=90,
            quality_gate_passed=9,
            quality_gate_total=10,
            unreviewed_signal_count=0,
            missing_narrative_count=0,
            missing_override_count=0,
            coverage_gap_count=1,
            written_narrative_count=5,
            total_narrative_count=5,
            set_override_count=5,
            total_override_count=5,
        ),
        blockers=(),
        needs_attention=(),
        milestones=(),
        risks=(),
        actions=(),
        decisions=(),
        assumptions=(),
        cross_program_cascades=(),
        active_issues=(),
        coverage_gaps=(
            CoverageGap(
                work_item_id=37777351,
                title="SCHIE Auth",
                state="Active",
                assigned_to=None,
                confidence=Confidence.HIGH,
            ),
        ),
        ready=(),
        coverage_gap_window_days=14,
    )

    rendered = render_triage_report(report)

    assert 'WI:37777351 "SCHIE Auth" (Active; high confidence)' in rendered


def test_render_triage_report_includes_issue_projection_confidence() -> None:
    report = TriageReport(
        edition_name="acme_weekly",
        issue_number=78,
        program_id="acme",
        readiness=ReadinessAssessment(
            score=84,
            quality_gate_pass_rate=90,
            quality_gate_passed=9,
            quality_gate_total=10,
            unreviewed_signal_count=1,
            missing_narrative_count=1,
            missing_override_count=0,
            coverage_gap_count=0,
            written_narrative_count=4,
            total_narrative_count=5,
            set_override_count=5,
            total_override_count=5,
        ),
        blockers=(),
        needs_attention=(),
        milestones=(),
        risks=(),
        actions=(),
        decisions=(),
        assumptions=(),
        cross_program_cascades=(),
        active_issues=(
            IssueProjection(
                work_item_id=1101,
                source_type="ado_blocked",
                severity="block",
                summary='WI:1101 "Blocked rollout item" blocked in ADO (Blocked)',
                owner_alias="owner@example.com",
                workstream_id=None,
                ado_url="https://example/1101",
                linked_entity_ids=("ask-1",),
                confidence=Confidence.HIGH,
            ),
        ),
        coverage_gaps=(),
        ready=(),
        coverage_gap_window_days=14,
    )

    rendered = render_triage_report(report)

    assert "BLOCK | ado blocked | high confidence | WI:1101 \"Blocked rollout item\" blocked in ADO (Blocked)" in rendered