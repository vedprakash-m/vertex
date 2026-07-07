from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.contradiction_engine import build_contradiction_packets
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import ClaimEntry, ForecastCalibrationModifier, Signal, Workstream, WorkstreamSignalSources


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