from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.cascade_detector import detect_dependency_cascades
from src.core.config_loader import ProgramDependency
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType, Scorecard, ScorecardDimension, Signal, Workstream
from src.core.trajectory_analyzer import DriftPattern


def test_detect_dependency_cascades_matches_signal_and_drift_triggers() -> None:
    dependencies = (
        ProgramDependency(source="SCHIE gap closure", target="SCHIE Gaps", impact="Ramp stays blocked"),
        ProgramDependency(source="AutoTuning and LSO rollout", target="Networking", impact="Network readiness slips"),
    )
    signals = (
        Signal(
            id="sig-1",
            timestamp=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("SCHIE gap closure",),
            text="SCHIE gap closure remains blocked pending owner sign-off.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    )
    items = (
        WorkItem(
            id=900001,
            type="Feature",
            title="AutoTuning and LSO rollout",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.HIGH,
            tags=(),
            custom_fields={},
            revisions=(),
            comments=(),
            fetched_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        ),
    )
    drift_patterns = (
        DriftPattern(
            work_item_id=900001,
            pattern="eta_drift",
            severity="high",
            detail="Target date slipped twice in the last 30 days.",
            occurrences=2,
            window_days=30,
        ),
    )
    scorecards = (
        Scorecard(
            name="Acme Readiness",
            dimensions=(
                ScorecardDimension(name="SCHIE Gaps", workstream_id="acme"),
                ScorecardDimension(name="Networking", workstream_id="acme"),
            ),
        ),
    )
    workstreams = (Workstream(id="acme", name="Acme Ramp"),)

    cascades = detect_dependency_cascades(
        dependencies=dependencies,
        signals=signals,
        drift_patterns=drift_patterns,
        items=items,
        scorecards=scorecards,
        workstreams=workstreams,
    )

    assert len(cascades) == 2
    assert cascades[0].target_sections == (("Acme Readiness", "Networking"),)
    assert cascades[0].confidence == Confidence.HIGH
    assert cascades[0].trigger_kind == "drift"
    assert cascades[1].target_sections == (("Acme Readiness", "SCHIE Gaps"),)
    assert cascades[1].confidence == Confidence.HIGH
    assert cascades[1].trigger_kind == "signal"


def test_detect_dependency_cascades_keeps_cross_program_targets_without_local_section_match() -> None:
    dependencies = (
        Dependency(
            id="acme-fabrikam-buildouts",
            from_program_id="acme",
            from_workstream_id=None,
            from_item_id=900001,
            from_milestone_id=None,
            to_program_id="fabrikam",
            to_workstream_id="buildouts",
            to_item_id=None,
            to_milestone_id=None,
            dependency_type=DependencyType.INFORMS,
            risk_if_broken="Fabrikam buildout planning remains provisional until Acme lands the freeze date.",
            mitigation=None,
            status=DependencyStatus.ACTIVE,
            owner_alias="maintainer",
        ),
    )
    signals = (
        Signal(
            id="sig-cross-program",
            timestamp=datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Partner freeze remains provisional and keeps Fabrikam buildout timing tentative.",
            raw_ref="WI:900001",
            confidence=Confidence.HIGH,
        ),
    )

    cascades = detect_dependency_cascades(
        dependencies=dependencies,
        signals=signals,
        drift_patterns=(),
        items=(),
        scorecards=(),
        workstreams=(),
    )

    assert len(cascades) == 1
    assert cascades[0].target_item == "fabrikam:buildouts"
    assert cascades[0].target_sections == ()
    assert cascades[0].confidence == Confidence.HIGH
    assert cascades[0].trigger_kind == "signal"


def test_detect_dependency_cascades_traverses_multi_hop_dependencies_up_to_max_hops() -> None:
    dependencies = (
        ProgramDependency(source="Telemetry readiness", target="Deployment Velocity", impact="Deployment slips"),
        ProgramDependency(source="Deployment Velocity", target="Launch Readiness", impact="Launch review slips"),
        ProgramDependency(source="Launch Readiness", target="Executive Update", impact="Leadership update loses confidence"),
    )
    signals = (
        Signal(
            id="sig-hop",
            timestamp=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("Telemetry readiness",),
            text="Telemetry readiness is blocked on validation gaps.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    )
    scorecards = (
        Scorecard(
            name="Acme Readiness",
            dimensions=(
                ScorecardDimension(name="Deployment Velocity", workstream_id="acme"),
                ScorecardDimension(name="Launch Readiness", workstream_id="acme"),
                ScorecardDimension(name="Executive Update", workstream_id="acme"),
            ),
        ),
    )
    workstreams = (Workstream(id="acme", name="Acme Ramp"),)

    cascades = detect_dependency_cascades(
        dependencies=dependencies,
        signals=signals,
        drift_patterns=(),
        items=(),
        scorecards=scorecards,
        workstreams=workstreams,
    )

    assert [cascade.target_item for cascade in cascades] == ["Deployment Velocity", "Launch Readiness"]


def test_detect_dependency_cascades_breaks_cycles_when_traversing_multi_hop_dependencies() -> None:
    dependencies = (
        ProgramDependency(source="Telemetry readiness", target="Deployment Velocity", impact="Deployment slips"),
        ProgramDependency(source="Deployment Velocity", target="Telemetry readiness", impact="Telemetry revalidation stays blocked"),
    )
    signals = (
        Signal(
            id="sig-cycle",
            timestamp=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("Telemetry readiness",),
            text="Telemetry readiness is blocked on validation gaps.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    )
    scorecards = (
        Scorecard(
            name="Acme Readiness",
            dimensions=(
                ScorecardDimension(name="Deployment Velocity", workstream_id="acme"),
                ScorecardDimension(name="Telemetry readiness", workstream_id="acme"),
            ),
        ),
    )
    workstreams = (Workstream(id="acme", name="Acme Ramp"),)

    cascades = detect_dependency_cascades(
        dependencies=dependencies,
        signals=signals,
        drift_patterns=(),
        items=(),
        scorecards=scorecards,
        workstreams=workstreams,
        max_hops=3,
    )

    assert [cascade.target_item for cascade in cascades] == ["Deployment Velocity", "Telemetry readiness"]