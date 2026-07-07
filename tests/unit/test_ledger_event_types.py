from __future__ import annotations

import pytest

from src.core.ledger.event_types import count_control_event_types, get_event_schema, get_registered_event_types, support_table_update, validate_event_payload


def test_registry_contains_all_57_event_types() -> None:
    # Activation v1.6: +1 discovery.candidate_revoked audit event.
    assert len(get_registered_event_types()) == 57


def test_registry_has_expected_control_split() -> None:
    # Activation v1.6: 54 non-control + 3 operator-control = 57.
    assert count_control_event_types() == (54, 3)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("milestone.date_revised.v1", {"milestone_id": "milestone:m1", "new_target_date": "2026-06-30"}),
        ("decision.made.v1", {"decision_id": "decision:d1", "title": "Ship", "decision_text": "Ship it", "decided_by": ["operator"]}),
        ("metric.observed.v1", {"kpi_id": "kpi:deployments", "value": 42, "unit": "count", "window_end": "2026-06-10", "dimensions": {"ring": "prod"}}),
        ("pipeline.gap_detected.v1", {"pipeline": "workiq", "gap_kind": "null_ids", "detail": "weekly yield [0,0,0]"}),
        (
            "discovery.candidate_revoked.v1",
            {
                "candidate_id": "cand-1",
                "resulting_event_id": "evt-result",
                "revocation_event_id": "evt-revoke",
                "triage_actor": "operator",
            },
        ),
    ],
)
def test_representative_valid_payloads_pass(event_type: str, payload: dict[str, object]) -> None:
    validate_event_payload(event_type, payload)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("milestone.date_revised.v1", {"milestone_id": "milestone:m1"}),
        ("decision.made.v1", {"decision_id": "decision:d1", "title": "Ship", "decision_text": "Ship it", "decided_by": "operator"}),
        ("metric.observed.v1", {"kpi_id": "kpi:deployments", "value": "forty-two"}),
        ("operator.baseline_hardlock.v1", {"issue_number": "77", "snapshot_hash": "sha256:x", "event_id_watermark": "01", "contributing_event_count": 4}),
    ],
)
def test_representative_invalid_payloads_fail(event_type: str, payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_event_payload(event_type, payload)


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        get_event_schema("unknown.event.v1")


def test_support_table_update_for_gap_event() -> None:
    class _Event:
        event_type = "pipeline.gap_detected.v1"
        event_id = "01TEST"
        payload = {"pipeline": "workiq", "gap_kind": "null_ids", "detail": "weekly yield [0,0,0]"}

    updates = support_table_update(_Event())

    assert updates == {
        "gaps": {
            "event_id": "01TEST",
            "pipeline": "workiq",
            "gap_kind": "null_ids",
            "window_start": None,
            "window_end": None,
            "detail": "weekly yield [0,0,0]",
            "acknowledged": 0,
        }
    }


def test_operator_correction_tombstone_payload_is_allowed() -> None:
    validate_event_payload(
        "operator.correction.v1",
        {"corrects_event_id": "01EVENT", "corrected_payload": None, "reason": "tombstone"},
    )
