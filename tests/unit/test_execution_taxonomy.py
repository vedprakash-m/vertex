from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from src.core.execution_taxonomy import (
    ExecutionClass,
    ModelRegistryEntry,
    RegistryFinding,
    RegistryFindingCode,
    check_model_use,
    model_pin_to_registry_entry,
    lookup_model,
    parse_execution_class,
    validate_registry,
)
from src.core.model_registry import ModelPin


def _entry(model_id: str = "gpt-4o", **overrides: object) -> ModelRegistryEntry:
    values: dict[str, object] = {
        "model_id": model_id,
        "provider": "azure_openai",
        "purpose": "narrative_draft",
        "execution_class": ExecutionClass.AI_GENERATED,
        "enabled": True,
    }
    values.update(overrides)
    return ModelRegistryEntry(**values)  # type: ignore[arg-type]


def test_execution_class_values_are_stable_json_strings() -> None:
    assert [member.value for member in ExecutionClass] == [
        "deterministic",
        "ai_assisted",
        "ai_generated",
        "external_effect",
    ]
    assert ExecutionClass("ai_assisted") is ExecutionClass.AI_ASSISTED


def test_parse_execution_class_accepts_members_and_strings_and_rejects_unknown() -> None:
    assert parse_execution_class(ExecutionClass.DETERMINISTIC) is ExecutionClass.DETERMINISTIC
    assert parse_execution_class("external_effect") is ExecutionClass.EXTERNAL_EFFECT
    assert parse_execution_class("magic") is None
    assert parse_execution_class(None) is None


def test_entry_is_frozen_with_slots() -> None:
    entry = _entry()
    assert not hasattr(entry, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.enabled = False  # type: ignore[misc]


def test_model_pin_adapter_requires_and_preserves_explicit_registry_policy() -> None:
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    pin = ModelPin(
        feature_name="claim_extractor",
        model_id="gpt-4o-2024-08-06",
        deployment_id="vertex-claims",
        pinned_at=now,
        deprecation_review_at=now + timedelta(days=90),
        recert_at=None,
    )

    entry = model_pin_to_registry_entry(
        pin,
        provider="azure_openai",
        purpose="claim_extraction",
        execution_class="ai_generated",
        enabled=False,
    )

    assert entry == ModelRegistryEntry(
        model_id="gpt-4o-2024-08-06",
        provider="azure_openai",
        purpose="claim_extraction",
        execution_class="ai_generated",
        enabled=False,
    )


def test_model_pin_adapter_requires_each_registry_policy_field() -> None:
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    pin = ModelPin(
        feature_name="claim_extractor",
        model_id="gpt-4o-2024-08-06",
        deployment_id="vertex-claims",
        pinned_at=now,
        deprecation_review_at=now + timedelta(days=90),
        recert_at=None,
    )

    with pytest.raises(TypeError):
        model_pin_to_registry_entry(  # type: ignore[call-arg]
            pin,
            provider="azure_openai",
            purpose="claim_extraction",
            execution_class="ai_generated",
        )


def test_valid_registry_has_no_findings() -> None:
    registry = (_entry("a"), _entry("b", execution_class="ai_assisted"))
    assert validate_registry(registry) == ()


def test_duplicate_model_ids_produce_one_finding_per_duplicate_id() -> None:
    registry = (_entry("a"), _entry("b"), _entry("a"), _entry("a"))
    assert validate_registry(registry) == (
        RegistryFinding(RegistryFindingCode.DUPLICATE_MODEL_ID, "a", "model_id registered 3 times"),
    )


def test_unknown_execution_class_is_reported() -> None:
    findings = validate_registry((_entry("a", execution_class="magic"),))
    assert findings == (
        RegistryFinding(
            RegistryFindingCode.UNKNOWN_EXECUTION_CLASS, "a", "unknown execution_class 'magic'"
        ),
    )


def test_deterministic_class_is_not_valid_for_a_model_entry() -> None:
    findings = validate_registry((_entry("a", execution_class=ExecutionClass.DETERMINISTIC),))
    assert [finding.code for finding in findings] == [RegistryFindingCode.DETERMINISTIC_MODEL]


def test_findings_are_sorted_deterministically() -> None:
    registry = (_entry("z", execution_class="magic"), _entry("b"), _entry("b"), _entry("a", execution_class="nope"))
    findings = validate_registry(registry)
    assert [(f.model_id, f.code) for f in findings] == [
        ("a", RegistryFindingCode.UNKNOWN_EXECUTION_CLASS),
        ("b", RegistryFindingCode.DUPLICATE_MODEL_ID),
        ("z", RegistryFindingCode.UNKNOWN_EXECUTION_CLASS),
    ]
    assert validate_registry(tuple(reversed(registry))) == findings


def test_lookup_returns_entry_or_none() -> None:
    entry = _entry("a")
    registry = (entry, _entry("b"))
    assert lookup_model(registry, "a") is entry
    assert lookup_model(registry, "missing") is None


def test_lookup_rejects_ambiguous_duplicate_ids() -> None:
    assert lookup_model((_entry("a"), _entry("a", purpose="other")), "a") is None


def test_check_model_use_reports_unknown_and_disabled_models() -> None:
    registry = (_entry("on"), _entry("off", enabled=False))
    assert check_model_use(registry, "on") == ()
    assert check_model_use(registry, "off") == (
        RegistryFinding(RegistryFindingCode.DISABLED_MODEL, "off", "model is disabled"),
    )
    assert check_model_use(registry, "ghost") == (
        RegistryFinding(RegistryFindingCode.UNKNOWN_MODEL, "ghost", "model is not registered"),
    )


def test_check_model_use_reports_ambiguous_registration_as_duplicate() -> None:
    registry = (_entry("a"), _entry("a"))
    assert [f.code for f in check_model_use(registry, "a")] == [RegistryFindingCode.DUPLICATE_MODEL_ID]
