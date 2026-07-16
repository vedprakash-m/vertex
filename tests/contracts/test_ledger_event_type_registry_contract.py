from __future__ import annotations

from src.core.ledger.event_types import EVENT_TYPE_REGISTRY, count_control_event_types


def test_registry_declares_73_event_types() -> None:
    # Activation v1.6: +1 discovery.candidate_revoked audit event.
    # ADF-W0.18: +16 specs/arch-data-fix.md Appendix A.2 event payload contracts.
    assert len(EVENT_TYPE_REGISTRY) == 73


def test_registry_declares_required_metadata_for_every_type() -> None:
    for event_type, registration in EVENT_TYPE_REGISTRY.items():
        assert registration.payload_type is not None, event_type
        assert registration.entity_ref_fields is not None, event_type
        assert registration.affects_support_tables is not None, event_type
        assert registration.dedupe_core_fields is not None, event_type
        assert isinstance(registration.is_control, bool), event_type


def test_registry_control_totals_match_spec() -> None:
    # Activation v1.6: 54 non-control + 3 operator-control = 57.
    # ADF-W0.18: +16 non-control event types = 70 non-control + 3 operator-control = 73.
    assert count_control_event_types() == (70, 3)


def test_only_three_control_types_exist() -> None:
    control_types = {event_type for event_type, registration in EVENT_TYPE_REGISTRY.items() if registration.is_control}
    assert control_types == {
        "operator.correction.v1",
        "operator.field_lock.v1",
        "operator.field_unlock.v1",
    }
