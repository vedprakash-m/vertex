"""RELO-W5.8: operation capability contract and the semantic/provider key tiers."""
from __future__ import annotations

import dataclasses

import pytest

from src.core.actuation_outbox import DISPATCHABLE_OPERATIONS, actuation_idempotency_key, create_task_idempotency_key
from src.core.operation_capabilities import (
    OPERATION_CAPABILITIES,
    CapabilityRefusal,
    EffectClass,
    OperationCapability,
    create_target_identity,
    lookup_capability,
    provider_request_key,
    semantic_effect_key,
    desired_effect_hash,
)


def test_registry_covers_dispatchable_ado_operations() -> None:
    assert set(DISPATCHABLE_OPERATIONS) == {
        "create_task.v1",
    }
    assert set(DISPATCHABLE_OPERATIONS) <= set(OPERATION_CAPABILITIES)
    for operation, capability in OPERATION_CAPABILITIES.items():
        assert capability.operation == operation
        assert capability.max_blast_radius == 1


def test_capability_is_frozen() -> None:
    capability = OPERATION_CAPABILITIES["add_tag.v1"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        capability.compensable = False  # type: ignore[misc]


def test_create_task_capability_contract() -> None:
    capability = lookup_capability("create_task.v1")
    assert isinstance(capability, OperationCapability)
    assert capability.effect_class is EffectClass.CREATE
    assert capability.supports_remote_marker is True
    assert capability.marker_strategy == "ado_tag_or_custom_field"
    assert capability.reconcilable is True
    assert capability.compensable is False


def test_mutation_capabilities_are_compensable_with_hidden_marker() -> None:
    for operation in ("add_comment.v1", "add_tag.v1", "set_field.v1"):
        capability = lookup_capability(operation)
        assert isinstance(capability, OperationCapability)
        assert capability.compensable is True
        assert capability.supports_remote_marker is True
        assert capability.marker_strategy == "hidden_comment"
        assert capability.reconcilable is True
    assert lookup_capability("add_comment.v1").effect_class is EffectClass.APPEND  # type: ignore[union-attr]
    assert lookup_capability("set_field.v1").effect_class is EffectClass.MUTATE  # type: ignore[union-attr]


@pytest.mark.parametrize("operation", ["delete_work_item.v1", "create_task", "", "CREATE_TASK.V1"])
def test_unregistered_operation_returns_typed_refusal(operation: str) -> None:
    result = lookup_capability(operation)
    assert result == CapabilityRefusal(operation=operation, reason="unknown_capability")


def test_semantic_effect_key_is_deterministic_and_payload_order_insensitive() -> None:
    identity = {
        "program_id": "p1", "provider": "ado", "tenant": "contoso", "project": "One",
        "operation": "set_field.v1", "target_identity": "42",
        "causal_occurrence_id": "meeting:action-1",
    }
    first = semantic_effect_key(
        **identity, payload={"a": 1, "b": "x"},
    )
    second = semantic_effect_key(
        **identity, payload={"b": "x", "a": 1},
    )
    assert first == second
    assert first.startswith("sek1:") and len(first) == len("sek1:") + 64


def test_semantic_effect_key_distinguishes_each_component() -> None:
    base = {
        "program_id": "p1", "provider": "ado", "tenant": "contoso", "project": "One",
        "operation": "set_field.v1", "target_identity": "42",
        "causal_occurrence_id": "meeting:action-1", "payload": {"a": 1},
    }
    key = semantic_effect_key(**base)
    for field, value in (
        ("program_id", "p2"), ("provider", "graph"), ("tenant", "other"), ("project", "Other"),
        ("operation", "add_tag.v1"), ("target_identity", "43"),
        ("causal_occurrence_id", "meeting:action-2"), ("payload", {"a": 2}),
    ):
        assert semantic_effect_key(**{**base, field: value}) != key


def test_provider_request_key_is_byte_identical_to_existing_outbox_key() -> None:
    args = {
        "program_id": "p1", "org": "o", "project": "proj", "operation": "add_comment.v1",
        "target_identity": "42", "operation_intent_id": "abc",
    }
    assert provider_request_key(**args) == actuation_idempotency_key(**args)
    assert provider_request_key(**args) == "p1:o/proj:add_comment.v1:42:abc"


def test_provider_request_key_matches_create_task_key() -> None:
    assert provider_request_key(
        program_id="p1", org="o", project="proj", operation="create_task.v1",
        target_identity="new", operation_intent_id="intent-1",
    ) == create_task_idempotency_key(program_id="p1", org="o", project="proj", operation_intent_id="intent-1")


def test_create_target_is_stable_origin_not_fresh_intent() -> None:
    identity = {
        "provider": "ado",
        "tenant": "contoso",
        "project": "One",
        "causal_occurrence_id": "meeting:action-1",
        "payload": {"title": "Ship"},
    }
    assert create_target_identity(**identity) == create_target_identity(**identity)
    assert create_target_identity(**identity) != create_target_identity(
        **{**identity, "causal_occurrence_id": "meeting:action-2"},
    )
    assert create_target_identity(**identity).startswith("origin1:")


def test_desired_effect_hash_rejects_non_json_values() -> None:
    with pytest.raises(ValueError, match="JSON serializable"):
        desired_effect_hash({"value": object()})
