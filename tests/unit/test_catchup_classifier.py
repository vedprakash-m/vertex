from __future__ import annotations

from datetime import datetime, timezone

from src.core.feedback.catchup_classifier import build_catchup_events, build_catchup_summaries, classify_catchup_signals
from src.core.models import Confidence
from src.core.models_v2 import Signal


def test_classify_catchup_signals_builds_typed_events() -> None:
    signals = (
        Signal(
            id="sig-1",
            timestamp=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            source="vertex/catchup",
            program_id="acme",
            workstream_id="deployment",
            entity_refs=("WI:1234",),
            text="raw eta text",
            raw_ref="wi:1234:rev:7:targetdate",
            confidence=Confidence.HIGH,
            metadata={
                "work_item_id": 1234,
                "field": "TargetDate",
                "prior": "2026-06-15",
                "current": "2026-06-22",
            },
        ),
        Signal(
            id="sig-2",
            timestamp=datetime(2026, 5, 19, 18, 1, tzinfo=timezone.utc),
            source="vertex/catchup",
            program_id="acme",
            workstream_id="deployment",
            entity_refs=("WI:1235",),
            text="raw owner text",
            raw_ref="wi:1235:rev:9:assignedto",
            confidence=Confidence.HIGH,
            metadata={
                "work_item_id": 1235,
                "field": "AssignedTo",
                "prior": "priya",
                "current": "alex",
            },
        ),
    )

    events = classify_catchup_signals(signals, salience_weights={"deployment": 0.9})

    assert events[0].kind == "eta_slip"
    assert events[0].severity == "warn"
    assert events[0].summary == "ETA slip: ADO#1234 moved from 2026-06-15 to 2026-06-22."
    assert events[0].salience_score == 0.9
    assert events[0].confidence is Confidence.HIGH
    assert events[1].kind == "silent_owner_change"
    assert events[1].severity == "warn"
    assert events[1].summary == "Owner change: ADO#1235 moved from priya to alex."


def test_build_catchup_summaries_orders_by_severity_then_salience() -> None:
    signals = (
        Signal(
            id="sig-1",
            timestamp=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            source="vertex/catchup",
            program_id="acme",
            workstream_id="manageability",
            entity_refs=("WI:1234",),
            text="raw state text",
            raw_ref="wi:1234:rev:7:system.state",
            confidence=Confidence.HIGH,
            metadata={
                "work_item_id": 1234,
                "field": "System.State",
                "prior": "Proposed",
                "current": "Active",
            },
        ),
        Signal(
            id="sig-2",
            timestamp=datetime(2026, 5, 19, 18, 1, tzinfo=timezone.utc),
            source="vertex/catchup",
            program_id="acme",
            workstream_id="deployment",
            entity_refs=("WI:1235",),
            text="raw eta text",
            raw_ref="wi:1235:rev:9:targetdate",
            confidence=Confidence.HIGH,
            metadata={
                "work_item_id": 1235,
                "field": "TargetDate",
                "prior": "2026-06-15",
                "current": "2026-06-22",
            },
        ),
    )

    summaries = build_catchup_summaries(
        signals,
        salience_weights={"manageability": 0.2, "deployment": 0.8},
        limit=2,
    )

    assert summaries == (
        "ETA slip: ADO#1235 moved from 2026-06-15 to 2026-06-22.",
        "State change: ADO#1234 moved from Proposed to Active.",
    )


def test_build_catchup_events_accepts_normalized_state_field() -> None:
    signals = (
        Signal(
            id="sig-1",
            timestamp=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            source="vertex/catchup",
            program_id="acme",
            workstream_id="manageability",
            entity_refs=("WI:1234",),
            text="raw state text",
            raw_ref="wi:1234:rev:7:state",
            confidence=Confidence.HIGH,
            metadata={
                "work_item_id": 1234,
                "field": "State",
                "prior": "Proposed",
                "current": "Active",
            },
        ),
    )

    events = build_catchup_events(signals)

    assert events[0].kind == "state_change"
    assert events[0].summary == "State change: ADO#1234 moved from Proposed to Active."