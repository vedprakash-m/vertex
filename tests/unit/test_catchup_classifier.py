from __future__ import annotations

from datetime import datetime, timezone

import pytest

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


@pytest.mark.parametrize(("metadata", "expected"), [
    ({
        "quarantined": True, "quarantined_record_count": 2,
        "error": "WorkIQ structured discovery: 2 inspected record occurrence(s) quarantined for missing or invalid source time.",
    }, "WorkIQ: 2 inspected record occurrence(s) quarantined for missing or invalid source time."),
    ({
        "quarantined": True, "quarantined_record_count": 1, "workiq_structured_record_invalid": True,
        "error": "WorkIQ structured discovery: 1 inspected record occurrence(s) quarantined for malformed or unsafe content.",
    }, "WorkIQ: 1 inspected record occurrence(s) quarantined for malformed or unsafe content."),
    ({
        "workiq_structured_response_unusable": True,
        "error": "WorkIQ structured discovery: 1 attempted plan(s) returned unavailable or invalid responses.",
    }, "WorkIQ: structured discovery response unavailable or invalid."),
    ({
        "quarantined": True, "quarantined_record_count": 1,
        "error": "WorkIQ query private-lane: 1 record(s) quarantined for missing or invalid native observation ID.",
    }, "WorkIQ: 1 inspected record occurrence(s) quarantined."),
    ({
        "quarantined": True, "quarantined_record_count": "Private count",
        "error": "Private malformed metadata",
    }, "WorkIQ: records quarantined."),
    ({
        "quarantined": True, "quarantined_record_count": True, "error": "Private boolean count",
    }, "WorkIQ: records quarantined."),
    ({
        "quarantined": True, "quarantined_record_count": -1, "error": "Private negative count",
    }, "WorkIQ: records quarantined."),
    ({
        "quarantined": True, "quarantined_record_count": 0, "error": None,
    }, "WorkIQ: records quarantined."),
])
def test_catchup_workiq_diagnostic_summary_is_specific_and_private(
    metadata: dict[str, object], expected: str,
) -> None:
    signal = Signal(
        id="diagnostic", timestamp=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc),
        source="vertex/catchup", program_id="fixture", workstream_id=None, entity_refs=(),
        text="Private raw record", raw_ref="system:workiq:synthetic", confidence=Confidence.NONE,
        metadata={"catchup_origin": "system", "integration_source": "workiq", **metadata},
    )

    event = classify_catchup_signals((signal,))[0]

    assert event.summary == expected
    assert event.kind == "generic_change"
    assert event.severity == "info"
    assert event.confidence is Confidence.NONE
    assert "Private" not in event.summary and "private-lane" not in event.summary


def test_catchup_workiq_diagnostic_does_not_infer_fabricated_ado_fields() -> None:
    signal = Signal(
        id="diagnostic", timestamp=datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc),
        source="vertex/catchup", program_id="fixture", workstream_id=None, entity_refs=(),
        text="Private raw record", raw_ref="system:workiq:synthetic", confidence=Confidence.NONE,
        metadata={
            "catchup_origin": "system", "integration_source": "workiq", "quarantined": True,
            "error": "Private \ud800 diagnostic", "field": "TargetDate", "work_item_id": 1234,
            "prior": "2026-10-24", "current": "2026-10-25",
        },
    )

    event = classify_catchup_signals((signal,))[0]

    assert event.kind == "generic_change"
    assert event.work_item_id is None
    assert event.summary == "WorkIQ: records quarantined."