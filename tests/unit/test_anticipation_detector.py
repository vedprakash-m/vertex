from __future__ import annotations

from datetime import datetime, timezone

from src.core.anticipation_detector import detect_anticipated_questions
from src.core.models import Confidence, ReviewState, RiskLevel, WorkItem
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType, LeadershipReader, Signal
from src.core.trajectory_analyzer import DriftPattern
from src.core.view_models import WorkstreamData


def test_detect_anticipated_questions_flags_eta_drift_for_timeline_reader() -> None:
    findings = detect_anticipated_questions(
        readers=(
            LeadershipReader(
                name="Jordan Lee",
                cares_about=("ramp timeline",),
            ),
        ),
        workstreams=(
            WorkstreamData(
                section_id="deployment_readiness",
                title="Deployment Readiness",
                blurb="Validation is slipping behind the prior checkpoint.",
                dependency_cascades=(),
                items=(
                    WorkItem(
                        id=900001,
                        type="Feature",
                        title="UD chunking rollout",
                        state="Active",
                        assigned_to="Vertex Maintainer",
                        assigned_to_email="maintainer@example.com",
                        area_path="One\\Adventure\\Acme\\Deployment",
                        iteration_path="FY26\\Sprint 20",
                        target_date=None,
                        risk_level=RiskLevel.MEDIUM,
                        tags=[],
                        custom_fields={},
                        revisions=[],
                        comments=[],
                        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                    ),
                ),
                citations=(),
                review_state=ReviewState.PENDING,
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.MEDIUM,
                total_items=1,
            ),
        ),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped 3 times in the last 90 days.",
                occurrences=3,
                window_days=90,
            ),
        ),
        approved_signals=(),
        summaries={"deployment_readiness": "Ramp timeline remains conditional until the next deployment checkpoint."},
    )

    eta_drift_finding = next(finding for finding in findings if finding.pattern == "eta_drift")

    assert eta_drift_finding.reader == "Jordan Lee"
    assert eta_drift_finding.confidence == Confidence.HIGH
    assert "slipped 3 times" in eta_drift_finding.question_seed


def test_detect_anticipated_questions_flags_dependency_chain_impact() -> None:
    findings = detect_anticipated_questions(
        readers=(
            LeadershipReader(
                name="Michael Myrah",
                cares_about=("cross-org dependency drift",),
            ),
        ),
        workstreams=(
            WorkstreamData(
                section_id="fleet_readiness",
                title="Fleet Readiness",
                blurb="Fleet readiness stays tied to BIOS compliance closure.",
                dependency_cascades=(),
                items=(),
                citations=(),
                review_state=ReviewState.PENDING,
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.MEDIUM,
                total_items=1,
            ),
        ),
        drift_patterns=(),
        approved_signals=(
            Signal(
                id="manual-1",
                timestamp=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                source="manual",
                program_id="acme",
                workstream_id="fleet_readiness",
                entity_refs=(),
                text="BIOS compliance remains the gating dependency for fleet readiness this week.",
                raw_ref=None,
                confidence=Confidence.HIGH,
            ),
        ),
        summaries={"fleet_readiness": "Fleet readiness is blocked until BIOS compliance closes."},
        dependencies=(
            Dependency(
                id="dep-1",
                from_program_id="acme",
                from_workstream_id="BIOS compliance",
                from_item_id=None,
                from_milestone_id=None,
                to_program_id="acme",
                to_workstream_id="Fleet readiness",
                to_item_id=None,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="Fleet readiness remains blocked until BIOS compliance closes.",
                mitigation=None,
                status=DependencyStatus.ACTIVE,
                owner_alias=None,
            ),
        ),
    )

    dependency_finding = next(finding for finding in findings if finding.pattern == "dependency_chain_impact")

    assert dependency_finding.pattern == "dependency_chain_impact"
    assert "BIOS compliance" in dependency_finding.question_seed