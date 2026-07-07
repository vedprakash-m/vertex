from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.contradiction_engine import build_contradiction_packets
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import (
    ActionItem,
    ActionSourceType,
    ActionStatus,
    ClaimEntry,
    Dependency,
    DependencyStatus,
    DependencyType,
    ForecastCalibrationModifier,
    Milestone,
    MilestoneStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    Signal,
    Workstream,
    WorkstreamSignalSources,
)


def _dependency(
    dependency_id: str,
    *,
    from_item_id: int | None = None,
    to_item_id: int | None = None,
    status: DependencyStatus = DependencyStatus.ACTIVE,
) -> Dependency:
    return Dependency(
        id=dependency_id,
        from_program_id="demo",
        from_workstream_id=None,
        from_item_id=from_item_id,
        from_milestone_id=None,
        to_program_id="demo",
        to_workstream_id=None,
        to_item_id=to_item_id,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Downstream execution slips.",
        mitigation=None,
        status=status,
        owner_alias=None,
    )


def _dependency_claim(
    *,
    claimed_status_value: str | None,
    entity_refs: tuple[str, ...] = ("DEP:dep-1",),
    claim_id: str = "claim-dep-1",
) -> ClaimEntry:
    return ClaimEntry(
        id=claim_id,
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=77,
        workstream_id="deployment",
        text="The dependency on team X is now broken.",
        entity_refs=entity_refs,
        claim_date=date(2026, 5, 20),
        owner_alias=None,
        due_date=None,
        claimed_status_family="dependency",
        claimed_status_value=claimed_status_value,
    )


def test_build_contradiction_packets_detects_claim_and_signal_target_date_disagreements() -> None:
    item = WorkItem(
        id=1001,
        type="Feature",
        title="Deployment chunking",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 10),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(
            ClaimEntry(
                id="claim-1",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Expected by 2026-06-01",
                entity_refs=("WI:1001",),
                claim_date=date(2026, 5, 20),
                owner_alias="priya",
                due_date=date(2026, 6, 1),
            ),
        ),
        signals=(
            Signal(
                id="signal-1",
                timestamp=datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc),
                source="workiq/risk",
                program_id="demo",
                workstream_id="deployment",
                entity_refs=("WI:1001",),
                text="The team is now talking about June 24 as the likely landing date.",
                raw_ref="workiq:1",
                confidence=Confidence.MEDIUM,
            ),
        ),
        workstreams=(
            Workstream(
                id="deployment",
                name="Deployment",
                area_paths=("One\\Demo\\Deployment",),
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=ForecastCalibrationModifier(
            workstream_modifiers={"deployment": 0.18},
            dri_modifiers={"priya": 0.16},
            confidence=Confidence.HIGH,
        ),
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet.work_item_id == 1001
    assert packet.workstream_id == "deployment"
    assert len(packet.contradictions) == 2
    assert packet.recommended_resolution is not None
    assert packet.recommended_resolution.winning_source.value == "workiq"
    assert packet.recommended_resolution.confidence == Confidence.HIGH


def test_build_contradiction_packets_ignores_non_date_signals_and_matching_claims() -> None:
    item = WorkItem(
        id=1002,
        type="Feature",
        title="Repair follow-up",
        state="Active",
        assigned_to="Alex",
        assigned_to_email="alex@example.com",
        area_path="One\\Demo\\Repair",
        iteration_path="Sprint 2",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(
            ClaimEntry(
                id="claim-2",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=77,
                workstream_id="repair",
                text="Expected by 2026-06-15",
                entity_refs=("WI:1002",),
                claim_date=date(2026, 5, 20),
                owner_alias="alex",
                due_date=date(2026, 6, 15),
            ),
        ),
        signals=(
            Signal(
                id="signal-2",
                timestamp=datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc),
                source="workiq/risk",
                program_id="demo",
                workstream_id="repair",
                entity_refs=("WI:1002",),
                text="The team sounded worried but did not name a new date.",
                raw_ref="workiq:2",
                confidence=Confidence.MEDIUM,
            ),
        ),
        workstreams=(
            Workstream(
                id="repair",
                name="Repair",
                area_paths=("One\\Demo\\Repair",),
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
    )

    assert packets == ()


def test_build_contradiction_packets_detects_claim_owner_disagreement() -> None:
    item = WorkItem(
        id=1003,
        type="Feature",
        title="Owner mismatch case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 10),
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(
            ClaimEntry(
                id="claim-3",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Alex committed to close this out.",
                entity_refs=("WI:1003",),
                claim_date=date(2026, 5, 20),
                owner_alias="alex",
                due_date=date(2026, 6, 10),
            ),
        ),
        signals=(),
        workstreams=(
            Workstream(
                id="deployment",
                name="Deployment",
                area_paths=("One\\Demo\\Deployment",),
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet.work_item_id == 1003
    owner_contradictions = [c for c in packet.contradictions if c.field == "owner"]
    assert len(owner_contradictions) == 1
    assert "priya" in owner_contradictions[0].summary
    assert "alex" in owner_contradictions[0].summary
    assert owner_contradictions[0].source_a == "ado/assigned_to"
    assert owner_contradictions[0].source_b == "journal/claim"


def test_build_contradiction_packets_ignores_matching_owner_regardless_of_email_domain() -> None:
    item = WorkItem(
        id=1004,
        type="Feature",
        title="Owner match case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="Priya@Example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 10),
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(
            ClaimEntry(
                id="claim-4",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Priya committed to close this out.",
                entity_refs=("WI:1004",),
                claim_date=date(2026, 5, 20),
                owner_alias="priya",
                due_date=date(2026, 6, 10),
            ),
        ),
        signals=(),
        workstreams=(
            Workstream(
                id="deployment",
                name="Deployment",
                area_paths=("One\\Demo\\Deployment",),
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
    )

    assert packets == ()


def test_build_contradiction_packets_skips_owner_check_when_claim_has_no_owner() -> None:
    item = WorkItem(
        id=1005,
        type="Feature",
        title="No claim owner case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 10),
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(
            ClaimEntry(
                id="claim-5",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Someone will close this out.",
                entity_refs=("WI:1005",),
                claim_date=date(2026, 5, 20),
                owner_alias=None,
                due_date=date(2026, 6, 10),
            ),
        ),
        signals=(),
        workstreams=(
            Workstream(
                id="deployment",
                name="Deployment",
                area_paths=("One\\Demo\\Deployment",),
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
    )

    assert packets == ()


def test_build_contradiction_packets_reports_both_date_and_owner_contradictions_together() -> None:
    item = WorkItem(
        id=1006,
        type="Feature",
        title="Double mismatch case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 10),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(
            ClaimEntry(
                id="claim-6",
                program_id="demo",
                edition_id="demo_weekly",
                issue_number=77,
                workstream_id="deployment",
                text="Alex says this lands 2026-07-01.",
                entity_refs=("WI:1006",),
                claim_date=date(2026, 5, 20),
                owner_alias="alex",
                due_date=date(2026, 7, 1),
            ),
        ),
        signals=(),
        workstreams=(
            Workstream(
                id="deployment",
                name="Deployment",
                area_paths=("One\\Demo\\Deployment",),
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
    )

    assert len(packets) == 1
    fields = {c.field for c in packets[0].contradictions}
    assert fields == {"target_date", "owner"}


def _bare_workstream() -> tuple[Workstream, ...]:
    return (
        Workstream(
            id="deployment",
            name="Deployment",
            area_paths=("One\\Demo\\Deployment",),
            signal_sources=WorkstreamSignalSources(),
        ),
    )


def test_build_contradiction_packets_detects_dependency_status_disagreement() -> None:
    item = WorkItem(
        id=2001,
        type="Feature",
        title="Dependency status mismatch case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(_dependency_claim(claimed_status_value="broken"),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
        dependencies=(_dependency("dep-1", from_item_id=2001, status=DependencyStatus.ACTIVE),),
    )

    assert len(packets) == 1
    packet = packets[0]
    assert packet.work_item_id == 2001
    dependency_contradictions = [c for c in packet.contradictions if c.field == "dependency_status"]
    assert len(dependency_contradictions) == 1
    contradiction = dependency_contradictions[0]
    assert "active" in contradiction.summary
    assert "broken" in contradiction.summary
    assert contradiction.source_a == "dependency_graph/status"
    assert contradiction.source_b == "journal/claim"
    assert contradiction.confidence == Confidence.MEDIUM


def test_dependency_status_contradiction_produces_no_misleading_ado_recommendation() -> None:
    # ADF-W2.10: `_recommend_resolution` used to unconditionally label any
    # `source_b == "journal/claim"` contradiction "prefer ado", even though
    # dependency_status's actual competing source (`source_a`) is
    # "dependency_graph/status" -- Vertex's own register, not ADO. With a
    # calibration modifier strong enough to trigger a recommendation, the
    # fixed code must produce NO recommendation here (honest: DataSourceType
    # has no representation for "the dependency graph") rather than a
    # mislabeled one.
    item = WorkItem(
        id=2001,
        type="Feature",
        title="Dependency status mismatch case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(_dependency_claim(claimed_status_value="broken"),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=ForecastCalibrationModifier(
            workstream_modifiers={"deployment": 0.2},
            dri_modifiers={},
            confidence=Confidence.HIGH,
        ),
        dependencies=(_dependency("dep-1", from_item_id=2001, status=DependencyStatus.ACTIVE),),
    )

    assert len(packets) == 1
    assert packets[0].recommended_resolution is None


def test_build_contradiction_packets_ignores_matching_dependency_status() -> None:
    item = WorkItem(
        id=2002,
        type="Feature",
        title="Dependency status match case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(_dependency_claim(claimed_status_value="active"),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
        dependencies=(_dependency("dep-1", from_item_id=2002, status=DependencyStatus.ACTIVE),),
    )

    assert packets == ()


def test_build_contradiction_packets_skips_dependency_status_when_ref_unknown() -> None:
    item = WorkItem(
        id=2003,
        type="Feature",
        title="Unknown dependency ref case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(_dependency_claim(claimed_status_value="broken", entity_refs=("DEP:does-not-exist",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
        dependencies=(_dependency("dep-1", from_item_id=2003, status=DependencyStatus.ACTIVE),),
    )

    assert packets == ()


def test_build_contradiction_packets_skips_dependency_status_when_no_dependencies_passed() -> None:
    """Backward-compatibility: existing callers that don't pass `dependencies`
    (default ()) must see no behavior change -- the rule silently no-ops."""
    item = WorkItem(
        id=2004,
        type="Feature",
        title="No dependencies passed case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(_dependency_claim(claimed_status_value="broken"),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
    )

    assert packets == ()


def test_build_contradiction_packets_skips_dependency_status_when_no_attachable_work_item() -> None:
    """Neither end of the dependency resolves to a known work item, so there
    is nowhere to attach the contradiction packet -- skip, don't raise."""
    item = WorkItem(
        id=2005,
        type="Feature",
        title="Unrelated item",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(_dependency_claim(claimed_status_value="broken"),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
        dependencies=(_dependency("dep-1", from_item_id=9999, to_item_id=None, status=DependencyStatus.ACTIVE),),
    )

    assert packets == ()


def test_build_contradiction_packets_skips_dependency_status_when_claim_family_not_dependency() -> None:
    item = WorkItem(
        id=2006,
        type="Feature",
        title="Non-dependency family case",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.LOW,
        tags=[],
        custom_fields={},
    )
    claim = ClaimEntry(
        id="claim-risk-1",
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=77,
        workstream_id="deployment",
        text="The risk level is now high.",
        entity_refs=("DEP:dep-1",),
        claim_date=date(2026, 5, 20),
        owner_alias=None,
        due_date=None,
        claimed_status_family="risk",
        claimed_status_value="broken",
    )
    packets = build_contradiction_packets(
        items=(item,),
        claims=(claim,),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        calibration_modifier=None,
        dependencies=(_dependency("dep-1", from_item_id=2006, status=DependencyStatus.ACTIVE),),
    )

    assert packets == ()


# ---------------------------------------------------------------------------
# ADF-W2.10 P7 (Section 8.10.9): risk / milestone / action status families.
# Mirrors the dependency-status test set (detect, match-ignored, unknown-ref,
# no-records-passed, no-attachable-work-item) one family at a time.
# ---------------------------------------------------------------------------


def _work_item(item_id: int) -> WorkItem:
    return WorkItem(
        id=item_id,
        type="Feature",
        title=f"Item {item_id}",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Demo\\Deployment",
        iteration_path="Sprint 1",
        target_date=None,
        risk_level=RiskLevel.MEDIUM,
        tags=[],
        custom_fields={},
    )


def _status_claim(
    *,
    family: str,
    claimed_status_value: str | None,
    entity_refs: tuple[str, ...],
    claim_id: str = "claim-status-1",
) -> ClaimEntry:
    return ClaimEntry(
        id=claim_id,
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=77,
        workstream_id="deployment",
        text=f"The {family} status is {claimed_status_value}.",
        entity_refs=entity_refs,
        claim_date=date(2026, 5, 20),
        owner_alias=None,
        due_date=None,
        claimed_status_family=family,
        claimed_status_value=claimed_status_value,
    )


def _risk(
    risk_id: str,
    *,
    linked_work_item_ids: tuple[int, ...] = (3001,),
    status: RiskStatus = RiskStatus.OPEN,
) -> RiskEntry:
    return RiskEntry(
        id=risk_id,
        program_id="demo",
        title="Supply risk",
        description="A key supplier may slip.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.EXTERNAL,
        owner_alias="priya",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=linked_work_item_ids,
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=status,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(),
    )


def _milestone(
    milestone_id: str,
    *,
    linked_work_item_ids: tuple[int, ...] = (3002,),
    status: MilestoneStatus = MilestoneStatus.ON_TRACK,
) -> Milestone:
    return Milestone(
        id=milestone_id,
        program_id="demo",
        name="GA milestone",
        target_date=date(2026, 8, 1),
        owner_alias="priya",
        status=status,
        exit_criteria=("All features deployed",),
        linked_workstream_ids=(),
        linked_work_item_ids=linked_work_item_ids,
    )


def _action(
    action_id: str,
    *,
    linked_work_item_ids: tuple[int, ...] = (3003,),
    status: ActionStatus = ActionStatus.OPEN,
) -> ActionItem:
    return ActionItem(
        id=action_id,
        program_id="demo",
        text="Follow up with vendor.",
        owner_alias="priya",
        due_date=None,
        status=status,
        source_signal_id=None,
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=linked_work_item_ids,
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id="deployment",
        created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )


def test_risk_status_disagreement_detected() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3001),),
        claims=(_status_claim(family="risk", claimed_status_value="mitigated", entity_refs=("RISK:risk-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        risks=(_risk("risk-1", status=RiskStatus.OPEN),),
    )
    assert len(packets) == 1
    risk_contradictions = [c for c in packets[0].contradictions if c.field == "risk_status"]
    assert len(risk_contradictions) == 1
    assert risk_contradictions[0].source_a == "risk_register/status"
    assert "open" in risk_contradictions[0].summary
    assert "mitigated" in risk_contradictions[0].summary


def test_risk_status_match_ignored() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3001),),
        claims=(_status_claim(family="risk", claimed_status_value="open", entity_refs=("RISK:risk-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        risks=(_risk("risk-1", status=RiskStatus.OPEN),),
    )
    assert packets == ()


def test_risk_status_unknown_ref_skipped() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3001),),
        claims=(_status_claim(family="risk", claimed_status_value="mitigated", entity_refs=("RISK:does-not-exist",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        risks=(_risk("risk-1", status=RiskStatus.OPEN),),
    )
    assert packets == ()


def test_risk_status_unrecognized_value_skipped() -> None:
    """A claim value outside the RiskStatus vocabulary is skipped, not raised
    (mapping freer narrative wording is the extractor's job)."""
    packets = build_contradiction_packets(
        items=(_work_item(3001),),
        claims=(_status_claim(family="risk", claimed_status_value="supercritical", entity_refs=("RISK:risk-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        risks=(_risk("risk-1", status=RiskStatus.OPEN),),
    )
    assert packets == ()


def test_risk_status_no_records_passed_backward_compat() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3001),),
        claims=(_status_claim(family="risk", claimed_status_value="mitigated", entity_refs=("RISK:risk-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    assert packets == ()


def test_risk_status_no_attachable_work_item_skipped() -> None:
    """A risk whose linked work items are not in the item set has nowhere to
    attach a packet -- the claim is skipped, not raised."""
    packets = build_contradiction_packets(
        items=(_work_item(9999),),
        claims=(_status_claim(family="risk", claimed_status_value="mitigated", entity_refs=("RISK:risk-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        risks=(_risk("risk-1", linked_work_item_ids=(4242,), status=RiskStatus.OPEN),),
    )
    assert packets == ()


def test_milestone_status_disagreement_detected() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3002),),
        claims=(_status_claim(family="milestone", claimed_status_value="at_risk", entity_refs=("MS:ms-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        milestones=(_milestone("ms-1", status=MilestoneStatus.ON_TRACK),),
    )
    assert len(packets) == 1
    ms_contradictions = [c for c in packets[0].contradictions if c.field == "milestone_status"]
    assert len(ms_contradictions) == 1
    assert ms_contradictions[0].source_a == "milestones/status"
    assert "on_track" in ms_contradictions[0].summary


def test_milestone_status_match_ignored() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3002),),
        claims=(_status_claim(family="milestone", claimed_status_value="on_track", entity_refs=("MS:ms-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        milestones=(_milestone("ms-1", status=MilestoneStatus.ON_TRACK),),
    )
    assert packets == ()


def test_milestone_status_no_records_passed_backward_compat() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3002),),
        claims=(_status_claim(family="milestone", claimed_status_value="at_risk", entity_refs=("MS:ms-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    assert packets == ()


def test_action_status_disagreement_detected() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3003),),
        claims=(_status_claim(family="action", claimed_status_value="done", entity_refs=("ACTION:act-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        actions=(_action("act-1", status=ActionStatus.OPEN),),
    )
    assert len(packets) == 1
    action_contradictions = [c for c in packets[0].contradictions if c.field == "action_status"]
    assert len(action_contradictions) == 1
    assert action_contradictions[0].source_a == "actions/status"
    assert "open" in action_contradictions[0].summary


def test_action_status_match_ignored() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3003),),
        claims=(_status_claim(family="action", claimed_status_value="open", entity_refs=("ACTION:act-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        actions=(_action("act-1", status=ActionStatus.OPEN),),
    )
    assert packets == ()


def test_action_status_no_records_passed_backward_compat() -> None:
    packets = build_contradiction_packets(
        items=(_work_item(3003),),
        claims=(_status_claim(family="action", claimed_status_value="done", entity_refs=("ACTION:act-1",)),),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    assert packets == ()


def test_all_three_status_families_coexist_on_distinct_work_items() -> None:
    """All four structured-fact rules (dependency + risk + milestone + action)
    run in one pass and attach to the correct work items independently."""
    packets = build_contradiction_packets(
        items=(_work_item(3001), _work_item(3002), _work_item(3003), _work_item(3004)),
        claims=(
            _status_claim(family="risk", claimed_status_value="mitigated", entity_refs=("RISK:risk-1",), claim_id="c-risk"),
            _status_claim(family="milestone", claimed_status_value="missed", entity_refs=("MS:ms-1",), claim_id="c-ms"),
            _status_claim(family="action", claimed_status_value="done", entity_refs=("ACTION:act-1",), claim_id="c-action"),
        ),
        signals=(),
        workstreams=_bare_workstream(),
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        risks=(_risk("risk-1", linked_work_item_ids=(3001,), status=RiskStatus.OPEN),),
        milestones=(_milestone("ms-1", linked_work_item_ids=(3002,), status=MilestoneStatus.ON_TRACK),),
        actions=(_action("act-1", linked_work_item_ids=(3003,), status=ActionStatus.OPEN),),
    )
    fields = {p.work_item_id: [c.field for c in p.contradictions] for p in packets}
    assert "risk_status" in fields[3001]
    assert "milestone_status" in fields[3002]
    assert "action_status" in fields[3003]