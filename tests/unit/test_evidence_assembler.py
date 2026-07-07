from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.evidence_assembler import assemble_section_evidence_brief
from src.core.models import Comment, Confidence, EditionType, Revision, RiskLevel, Snapshot, SnapshotItem, WorkItem
from src.core.models_v2 import ClaimEntry, PersonDirectory, Signal, VitalityScore
from src.core.view_models import KpiTile


def test_assemble_section_evidence_brief_for_workstream_includes_deltas_signals_kpis_and_stale_claims() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    current_item = _work_item(
        101,
        title="Deployment gate",
        risk_level=RiskLevel.HIGH,
        target_date=date(2026, 5, 20),
    )
    previous_snapshot = Snapshot(
        issue_number=77,
        generated_at=as_of,
        ado_data_as_of=as_of,
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=101,
                type="Feature",
                title="Deployment gate",
                state="Active",
                assigned_to="Operator",
                area_path="One\\Adventure\\Acme",
                target_date=date(2026, 5, 18),
                risk_level=RiskLevel.MEDIUM,
                tags=[],
            ),
        ),
        scorecards=(),
    )
    signals = (
        Signal(
            id="sig-ado",
            timestamp=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:101",),
            text="Risk moved to high.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
        Signal(
            id="sig-kpi",
            timestamp=datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc),
            source="kusto_kpi",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:101",),
            text="Deploy P50 4.2 hrs",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
        ),
        Signal(
            id="sig-other",
            timestamp=datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="dd_on_pf",
            entity_refs=("WI:201",),
            text="DD signal",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    )
    vitality_scores = (
        VitalityScore(
            work_item_id=101,
            owner_alias="operator",
            workstream_id="acme",
            freshness_days=16,
            freshness_grade="red",
            richness_score=40,
            richness_missing=("target_date",),
            leakage_events=0,
            workiq_signal_count=1,
            composite_score=55,
            suggested_update=None,
        ),
    )
    kpi_tiles = (
        KpiTile(
            query_id="acme-deployment-velocity",
            label="Deploy P50 (hrs)",
            value="4.2",
            unit=None,
            trend=None,
            confidence="high",
            as_of=as_of,
            source_signal_id="sig-kpi",
        ),
    )
    claims = (
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="acme",
            text="Deployment gate will close by 2026-05-18.",
            entity_refs=("WI:101",),
            claim_date=date(2026, 5, 10),
            owner_alias="operator",
            due_date=date(2026, 5, 18),
        ),
    )

    brief = assemble_section_evidence_brief(
        "ws_nova",
        "acme",
        current_items=(current_item,),
        previous_snapshot=previous_snapshot,
        journal_signals=signals,
        vitality_scores=vitality_scores,
        kpi_tiles=kpi_tiles,
        claims=claims,
        issue_number=78,
        as_of=as_of,
    )

    assert brief.ado_delta_summary == "1 risk changed; 1 ETA changed."
    assert brief.risk_changed_items == (101,)
    assert brief.eta_changed_items == (101,)
    assert brief.top_signals == ("sig-ado", "sig-kpi")
    assert brief.kpi_summary == "Deploy P50 (hrs) 4.2"
    assert brief.stale_claims == ("claim-1",)
    assert brief.vitality_summary == "1 items scanned; 1 stale, 1 missing fields."
    assert brief.confidence is Confidence.HIGH


def test_assemble_section_evidence_brief_for_exec_summary_aggregates_and_omits_kpis() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    items = (
        _work_item(101, title="Acme gate", risk_level=RiskLevel.HIGH, target_date=date(2026, 5, 20), area_path="One\\Adventure\\Acme"),
        _work_item(201, title="DD gate", risk_level=RiskLevel.MEDIUM, target_date=date(2026, 5, 22), area_path="One\\Adventure\\Contoso"),
    )
    signals = (
        Signal(
            id="sig-acme",
            timestamp=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:101",),
            text="Acme risk moved up.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
        Signal(
            id="sig-dd",
            timestamp=datetime(2026, 5, 16, 8, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="dd_on_pf",
            entity_refs=("WI:201",),
            text="DD follow-up.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
        ),
    )
    vitality_scores = (
        VitalityScore(
            work_item_id=101,
            owner_alias="operator",
            workstream_id="acme",
            freshness_days=3,
            freshness_grade="green",
            richness_score=70,
            richness_missing=(),
            leakage_events=0,
            workiq_signal_count=1,
            composite_score=75,
            suggested_update=None,
        ),
        VitalityScore(
            work_item_id=201,
            owner_alias="yasser",
            workstream_id="dd_on_pf",
            freshness_days=10,
            freshness_grade="red",
            richness_score=50,
            richness_missing=("description",),
            leakage_events=0,
            workiq_signal_count=1,
            composite_score=55,
            suggested_update=None,
        ),
    )

    brief = assemble_section_evidence_brief(
        "exec_summary",
        None,
        current_items=items,
        previous_snapshot=None,
        journal_signals=signals,
        vitality_scores=vitality_scores,
        kpi_tiles=(
            KpiTile(
                query_id="ignored",
                label="Ignored KPI",
                value="1",
                unit=None,
                trend=None,
                confidence="high",
                as_of=as_of,
                source_signal_id="sig-kpi",
            ),
        ),
        claims=(),
        issue_number=78,
        as_of=as_of,
    )

    assert brief.kpi_summary is None
    assert brief.top_signals == ("sig-acme", "sig-dd")
    assert brief.vitality_summary == "2 items scanned; 1 stale, 1 missing fields."
    assert brief.confidence is Confidence.HIGH


def test_assemble_section_evidence_brief_uses_people_and_source_order_for_signal_ranking() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    signal_status = Signal(
        id="sig-status",
        timestamp=datetime(2026, 5, 17, 11, 50, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:101",),
        text="Status update: rollout remains on track.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "ic"},
    )
    signal_decision = Signal(
        id="sig-decision",
        timestamp=datetime(2026, 5, 17, 11, 30, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:101",),
        text="Decision: leadership approved the rollout.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "vp"},
    )

    brief = assemble_section_evidence_brief(
        "ws_nova",
        "acme",
        current_items=(_work_item(101, title="Deployment gate", risk_level=RiskLevel.HIGH, target_date=date(2026, 5, 20)),),
        previous_snapshot=None,
        journal_signals=(signal_status, signal_decision),
        vitality_scores=(),
        kpi_tiles=(),
        claims=(),
        issue_number=78,
        as_of=as_of,
        people_directory=(
            PersonDirectory(alias="vp", title="Vice President"),
            PersonDirectory(alias="ic", title="Software Engineer"),
        ),
        source_confidence_order=("workiq", "ado", "kusto"),
    )

    assert brief.top_signals[0] == "sig-decision"


def _work_item(
    work_item_id: int,
    *,
    title: str,
    risk_level: RiskLevel,
    target_date: date,
    area_path: str = "One\\Adventure\\Acme",
) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=title,
        state="Active",
        assigned_to="Operator",
        assigned_to_email="operator@example.com",
        area_path=area_path,
        iteration_path="One\\Sprint 24",
        target_date=target_date,
        risk_level=risk_level,
        tags=[],
        custom_fields={},
        revisions=(
            Revision(
                work_item_id=work_item_id,
                rev_number=1,
                changed_by="Operator",
                changed_by_email="operator@example.com",
                changed_date=datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Proposed", "Active")},
            ),
        ),
        comments=(
            Comment(
                work_item_id=work_item_id,
                comment_id=1,
                created_by="Operator",
                created_by_email="operator@example.com",
                created_date=datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
                text="Current status.",
            ),
        ),
        fetched_at=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# FR-SG-19: build_workstream_evidence_packet
# ---------------------------------------------------------------------------

from src.core.evidence_assembler import build_workstream_evidence_packet
from src.core.models_v2 import (
    DecisionEntry,
    DecisionStatus,
    SectionEvidenceBrief,
    WorkstreamEvidencePacket,
)
from src.core.external_dependency import ExternalDependency
from src.core.chronicle import ProgramEvent


def _minimal_section_brief(section_id: str = "ws_nova") -> SectionEvidenceBrief:
    return SectionEvidenceBrief(
        section_id=section_id,
        ado_delta_summary="no changes",
        new_items=(),
        closed_items=(),
        risk_changed_items=(),
        eta_changed_items=(),
        top_signals=(),
        kpi_summary=None,
        stale_claims=(),
        vitality_summary="0 items scanned; 0 stale, 0 missing fields.",
        confidence=Confidence.LOW,
    )


def _decision_entry(dec_id: str = "dec-1", workstream_id: str | None = None) -> DecisionEntry:
    return DecisionEntry(
        id=dec_id,
        program_id="acme",
        title="Use blue-green deployments",
        context="Deployment risk requires zero-downtime.",
        decision="Adopt blue-green deployment strategy.",
        rationale="Minimises blast radius.",
        alternatives_considered=(),
        decided_by="operator",
        decision_date=date(2026, 5, 1),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=workstream_id,
        entity_refs=(),
    )


def _program_event(ws_dim: str = "acme", days_ago: int = 5) -> ProgramEvent:
    return ProgramEvent(
        event_type="commitment",
        event_date=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc) - __import__("datetime").timedelta(days=days_ago),
        description="Leadership committed to M4 target.",
        source="meeting",
        actors=("operator",),
        linked_dimensions=(ws_dim,),
        event_id="ev-1",
    )


def test_build_workstream_evidence_packet_returns_packet() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    packet = build_workstream_evidence_packet(
        "acme",
        section_brief=_minimal_section_brief(),
        all_decisions=(_decision_entry("dec-1", workstream_id="acme"),),
        all_dependencies=(),
        chronicle_events=(),
        as_of=as_of,
    )
    assert isinstance(packet, WorkstreamEvidencePacket)
    assert packet.workstream_id == "acme"


def test_build_workstream_evidence_packet_includes_scoped_decisions() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    dec_this = _decision_entry("dec-1", workstream_id="acme")
    dec_other = _decision_entry("dec-2", workstream_id="dd_on_pf")
    packet = build_workstream_evidence_packet(
        "acme",
        section_brief=_minimal_section_brief(),
        all_decisions=(dec_this, dec_other),
        all_dependencies=(),
        chronicle_events=(),
        as_of=as_of,
    )
    assert all(d.id != "dec-2" for d in packet.top_decisions)
    assert any(d.id == "dec-1" for d in packet.top_decisions)


def test_build_workstream_evidence_packet_program_level_decisions_included() -> None:
    """Decisions with workstream_id=None (program-level) are included for all workstreams."""
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    program_dec = _decision_entry("dec-program", workstream_id=None)
    packet = build_workstream_evidence_packet(
        "acme",
        section_brief=_minimal_section_brief(),
        all_decisions=(program_dec,),
        all_dependencies=(),
        chronicle_events=(),
        as_of=as_of,
    )
    assert any(d.id == "dec-program" for d in packet.top_decisions)


def test_build_workstream_evidence_packet_limits_decisions() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    decisions = tuple(_decision_entry(f"dec-{i}", workstream_id=None) for i in range(10))
    packet = build_workstream_evidence_packet(
        "acme",
        section_brief=_minimal_section_brief(),
        all_decisions=decisions,
        all_dependencies=(),
        chronicle_events=(),
        as_of=as_of,
        top_decisions_limit=3,
    )
    assert len(packet.top_decisions) <= 3


def test_build_workstream_evidence_packet_scopes_chronicle_by_window() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    recent_ev = _program_event("acme", days_ago=5)
    old_ev = _program_event("acme", days_ago=60)
    packet = build_workstream_evidence_packet(
        "acme",
        section_brief=_minimal_section_brief(),
        all_decisions=(),
        all_dependencies=(),
        chronicle_events=(recent_ev, old_ev),
        as_of=as_of,
        chronicle_days_window=30,
    )
    # Only recent event (5 days ago) should be included; 60 days ago is outside window
    assert len(packet.chronicle_events) == 1
    assert packet.chronicle_events[0].event_id == "ev-1"


def test_build_workstream_evidence_packet_eta_and_credibility_fields() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    packet = build_workstream_evidence_packet(
        "acme",
        section_brief=_minimal_section_brief(),
        all_decisions=(),
        all_dependencies=(),
        chronicle_events=(),
        eta_summary="M4 on track for 2026-05-30",
        timeline_credibility=0.8,
        as_of=as_of,
    )
    assert packet.eta_summary == "M4 on track for 2026-05-30"
    assert packet.timeline_credibility == 0.8


def test_build_workstream_evidence_packet_deps_scoped_by_gate() -> None:
    as_of = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    dep_in = ExternalDependency(
        dep_id="dep-1", team="HW", tracked_items=(101,),
        approval_type="ado", gates=("acme",),
        canonical_owner_program=None, last_seen=None,
    )
    dep_out = ExternalDependency(
        dep_id="dep-2", team="OS", tracked_items=(201,),
        approval_type="ado", gates=("dd_on_pf",),
        canonical_owner_program=None, last_seen=None,
    )
    packet = build_workstream_evidence_packet(
        "acme",
        section_brief=_minimal_section_brief(),
        all_decisions=(),
        all_dependencies=(dep_in, dep_out),
        chronicle_events=(),
        as_of=as_of,
    )
    assert any(d.dep_id == "dep-1" for d in packet.open_dependencies)
    assert all(d.dep_id != "dep-2" for d in packet.open_dependencies)
