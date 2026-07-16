"""ADF-W2.8 (specs/arch-data-fix.md Section 8.9.1/8.9.2): tests for AISchemaGateway."""

from __future__ import annotations

import pytest

from src.core.ai_schema_gateway import (
    BoundsPolicy,
    SchemaGatewayError,
    validate_and_upcast,
    validate_bounded_payload,
)


def test_validate_bounded_payload_accepts_small_payload() -> None:
    validate_bounded_payload({"a": 1, "b": [1, 2, 3], "c": "short string"})  # must not raise


def test_validate_bounded_payload_rejects_excess_depth() -> None:
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(20):
        cursor["child"] = {}
        cursor = cursor["child"]  # type: ignore[assignment]
    with pytest.raises(SchemaGatewayError, match="max depth"):
        validate_bounded_payload(nested, bounds=BoundsPolicy(max_depth=5))


def test_validate_bounded_payload_rejects_excess_array_length() -> None:
    with pytest.raises(SchemaGatewayError, match="max_array_length"):
        validate_bounded_payload({"items": list(range(10))}, bounds=BoundsPolicy(max_array_length=5))


def test_validate_bounded_payload_rejects_excess_string_length() -> None:
    with pytest.raises(SchemaGatewayError, match="max_string_length"):
        validate_bounded_payload({"text": "x" * 100}, bounds=BoundsPolicy(max_string_length=10))


def test_validate_bounded_payload_rejects_excess_object_keys() -> None:
    payload = {f"key{i}": i for i in range(10)}
    with pytest.raises(SchemaGatewayError, match="max_object_keys"):
        validate_bounded_payload(payload, bounds=BoundsPolicy(max_object_keys=5))


def test_validate_bounded_payload_rejects_non_string_keys_would_be_caught_by_dict_semantics() -> None:
    # JSON payloads always have string keys by construction; this exercises
    # the defensive check for a non-JSON-originated dict passed by mistake.
    with pytest.raises(SchemaGatewayError, match="non-string key"):
        validate_bounded_payload({1: "value"})


def test_error_messages_never_include_raw_field_values() -> None:
    secret = "super-secret-credential-value-should-never-appear-in-error"
    try:
        validate_bounded_payload({"text": secret}, bounds=BoundsPolicy(max_string_length=5))
    except SchemaGatewayError as error:
        assert secret not in str(error)
    else:
        pytest.fail("expected SchemaGatewayError")


def _upcast_v1_to_v2(payload: dict) -> dict:
    return {**payload, "schema_version": "2", "new_field": payload.get("old_field", "")}


def test_validate_and_upcast_same_version_skips_upcast_chain() -> None:
    result = validate_and_upcast(
        {"schema_version": "2", "value": 1},
        payload_version="2",
        current_version="2",
        upcasters={},
        validate_old=None,
        validate_current=lambda payload: None,
    )
    assert result == {"schema_version": "2", "value": 1}


def test_validate_and_upcast_walks_chain_to_current_version() -> None:
    result = validate_and_upcast(
        {"schema_version": "1", "old_field": "hello"},
        payload_version="1",
        current_version="2",
        upcasters={"1": _upcast_v1_to_v2},
        validate_old=None,
        validate_current=lambda payload: None,
    )
    assert result["schema_version"] == "2"
    assert result["new_field"] == "hello"


def test_validate_and_upcast_raises_when_no_upcast_path_exists() -> None:
    with pytest.raises(SchemaGatewayError, match="no upcast path"):
        validate_and_upcast(
            {"schema_version": "1"},
            payload_version="1",
            current_version="99",
            upcasters={},
            validate_old=None,
            validate_current=lambda payload: None,
        )


def test_validate_and_upcast_raises_when_validate_old_fails() -> None:
    def _fail(payload: dict) -> None:
        raise ValueError("old payload is invalid")

    with pytest.raises(SchemaGatewayError, match="failed validate_old"):
        validate_and_upcast(
            {"schema_version": "1"},
            payload_version="1",
            current_version="2",
            upcasters={"1": _upcast_v1_to_v2},
            validate_old=_fail,
            validate_current=lambda payload: None,
        )


def test_validate_and_upcast_raises_when_validate_current_fails() -> None:
    def _fail(payload: dict) -> None:
        raise ValueError("upcasted payload is still invalid")

    with pytest.raises(SchemaGatewayError, match="failed validate_current"):
        validate_and_upcast(
            {"schema_version": "1", "old_field": "x"},
            payload_version="1",
            current_version="2",
            upcasters={"1": _upcast_v1_to_v2},
            validate_old=None,
            validate_current=_fail,
        )


def test_validate_and_upcast_detects_cycle() -> None:
    def _no_op_upcast(payload: dict) -> dict:
        return {**payload, "schema_version": "1"}  # never advances -> cycle

    with pytest.raises(SchemaGatewayError, match="cycle"):
        validate_and_upcast(
            {"schema_version": "1"},
            payload_version="1",
            current_version="2",
            upcasters={"1": _no_op_upcast},
            validate_old=None,
            validate_current=lambda payload: None,
        )


def test_validate_and_upcast_enforces_bounds_on_final_result() -> None:
    def _bloat(payload: dict) -> dict:
        return {**payload, "schema_version": "2", "text": "x" * 1000}

    with pytest.raises(SchemaGatewayError, match="max_string_length"):
        validate_and_upcast(
            {"schema_version": "1"},
            payload_version="1",
            current_version="2",
            upcasters={"1": _bloat},
            validate_old=None,
            validate_current=lambda payload: None,
            bounds=BoundsPolicy(max_string_length=10),
        )
