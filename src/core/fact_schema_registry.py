"""WI-1.5: Fact schema registry for new fact types introduced by the re-debt substrate.

Validates fact payloads against per-type schemas.
Acceptance: valid/invalid/warn-vs-strict test coverage.

Zone A module (INV-1 applies — must not import from src.ai or src.m365).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FactValidationResult:
    fact_type: str
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def ok(self) -> bool:
        return self.valid and not self.errors


# ---------------------------------------------------------------------------
# Per-type payload schemas (JSON Schema v2020-12 subset)
# ---------------------------------------------------------------------------

_FACT_SCHEMAS: dict[str, dict[str, Any]] = {
    "signal.observation": {
        "required": ["source", "signal_id", "signal_class"],
        "properties": {
            "source": {"type": "str"},
            "signal_id": {"type": "str"},
            "signal_class": {"type": "str"},
            "title": {"type": "str"},
            "body": {"type": "str"},
            "url": {"type": "str"},
            "observed_at": {"type": "str", "format": "datetime"},
        },
        "warn_if_missing": ["title", "observed_at"],
    },
    "fact.corroboration": {
        "required": ["entity_id", "family", "day_bucket", "corroborating_signal_ids"],
        "properties": {
            "entity_id": {"type": "str"},
            "family": {"type": "str"},
            "day_bucket": {"type": "str"},
            "corroborating_signal_ids": {"type": "list"},
            "corroboration_count": {"type": "int"},
        },
        "warn_if_missing": ["corroboration_count"],
    },
    "fact.conflict": {
        "required": ["entity_id", "family", "day_bucket", "conflicting_signal_ids", "conflict_description"],
        "properties": {
            "entity_id": {"type": "str"},
            "family": {"type": "str"},
            "day_bucket": {"type": "str"},
            "conflicting_signal_ids": {"type": "list"},
            "conflict_description": {"type": "str"},
            "severity": {"type": "str"},
        },
        "warn_if_missing": ["severity"],
    },
    "fact.conflict_resolved": {
        "required": ["conflict_id", "resolution_reason", "resolved_by"],
        "properties": {
            "conflict_id": {"type": "str"},
            "resolution_reason": {"type": "str"},
            "resolved_by": {"type": "str"},
            "resolution_authority": {"type": "str"},
            "resolved_at": {"type": "str", "format": "datetime"},
        },
        "warn_if_missing": ["resolution_authority", "resolved_at"],
    },
    "fact.reconfirmation": {
        "required": ["target_natural_key", "day_bucket", "reconfirmed_by"],
        "properties": {
            "target_natural_key": {"type": "str"},
            "day_bucket": {"type": "str"},
            "reconfirmed_by": {"type": "str"},
            "reconfirmed_at": {"type": "str", "format": "datetime"},
        },
        "warn_if_missing": ["reconfirmed_at"],
    },
    "fact.source_sync": {
        "required": ["target_natural_key", "day_bucket", "source"],
        "properties": {
            "target_natural_key": {"type": "str"},
            "day_bucket": {"type": "str"},
            "source": {"type": "str"},
            "sync_mode": {"type": "str"},
        },
        "warn_if_missing": ["sync_mode"],
    },
    "trust.source_score": {
        "required": ["source", "signal_class", "score", "breaker_verdict"],
        "properties": {
            "source": {"type": "str"},
            "signal_class": {"type": "str"},
            "qualifier": {"type": "str"},
            "score": {"type": "float"},
            "breaker_verdict": {"type": "str"},
            "computed_at": {"type": "str", "format": "datetime"},
            "sample_count": {"type": "int"},
            "suspended": {"type": "bool"},
        },
        "warn_if_missing": ["computed_at", "sample_count"],
    },
    "trust.bootstrap_grant": {
        "required": ["source", "signal_class", "granted_by", "grant_score"],
        "properties": {
            "source": {"type": "str"},
            "signal_class": {"type": "str"},
            "granted_by": {"type": "str"},
            "grant_score": {"type": "float"},
            "granted_at": {"type": "str", "format": "datetime"},
            "rationale": {"type": "str"},
        },
        "warn_if_missing": ["rationale", "granted_at"],
    },
    "entity.alias": {
        "required": ["entity_type", "canonical_id", "alias", "scope"],
        "properties": {
            "entity_type": {"type": "str"},
            "canonical_id": {"type": "str"},
            "alias": {"type": "str"},
            "scope": {"type": "str"},
            "confidence": {"type": "float"},
            "source": {"type": "str"},
        },
        "warn_if_missing": ["confidence", "source"],
    },
    "commitment.entry": {
        "required": ["commitment_id", "title", "dri", "due_date", "direction"],
        "properties": {
            "commitment_id": {"type": "str"},
            "title": {"type": "str"},
            "dri": {"type": "str"},
            "due_date": {"type": "str"},
            "direction": {"type": "str", "enum": ["inbound", "outbound"]},
            "status": {"type": "str"},
            "slip_history": {"type": "list"},
        },
        "warn_if_missing": ["status"],
        "enum_fields": {
            "direction": ["inbound", "outbound"],
        },
    },
    "action.proposal": {
        "required": ["proposal_id", "adapter", "operation", "entity_ref"],
        "properties": {
            "proposal_id": {"type": "str"},
            "adapter": {"type": "str"},
            "operation": {"type": "str"},
            "entity_ref": {"type": "str"},
            "payload": {"type": "dict"},
            "proposed_at": {"type": "str", "format": "datetime"},
            "approved": {"type": "bool"},
        },
        "warn_if_missing": ["proposed_at"],
    },
    "action.executed": {
        "required": ["proposal_id", "adapter", "operation", "executed_by"],
        "properties": {
            "proposal_id": {"type": "str"},
            "adapter": {"type": "str"},
            "operation": {"type": "str"},
            "executed_by": {"type": "str"},
            "executed_at": {"type": "str", "format": "datetime"},
            "result_status": {"type": "str"},
        },
        "warn_if_missing": ["executed_at", "result_status"],
    },
    "action.failed": {
        "required": ["proposal_id", "adapter", "operation", "failure_reason"],
        "properties": {
            "proposal_id": {"type": "str"},
            "adapter": {"type": "str"},
            "operation": {"type": "str"},
            "failure_reason": {"type": "str"},
            "failed_at": {"type": "str", "format": "datetime"},
            "retry_count": {"type": "int"},
            "terminal": {"type": "bool"},
        },
        "warn_if_missing": ["failed_at"],
    },
}

_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": (int, float),  # type: ignore[dict-item]
    "bool": bool,
    "list": list,
    "dict": dict,
}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_fact_payload(
    fact_type: str,
    payload: dict[str, Any],
    *,
    strict: bool = False,
) -> FactValidationResult:
    """Validate a fact payload against the registered schema for its type.

    Args:
        fact_type: The fact type string (e.g. 'signal.observation').
        payload: The payload dict to validate.
        strict: If True, warnings also count as errors.

    Returns:
        FactValidationResult with valid/errors/warnings.
    """
    if fact_type not in _FACT_SCHEMAS:
        return FactValidationResult(
            fact_type=fact_type,
            valid=True,
            errors=(),
            warnings=(f"No schema registered for fact_type={fact_type!r}; skipping validation.",),
        )

    schema = _FACT_SCHEMAS[fact_type]
    errors: list[str] = []
    warnings: list[str] = []

    # Required fields check
    for req in schema.get("required", []):
        if req not in payload:
            errors.append(f"Missing required field: {req!r}")

    # Type checks for present fields
    props = schema.get("properties", {})
    for key, val in payload.items():
        if key not in props:
            continue
        expected_type_name = props[key].get("type")
        if expected_type_name and expected_type_name in _TYPE_MAP:
            expected = _TYPE_MAP[expected_type_name]
            if not isinstance(val, expected):
                errors.append(
                    f"Field {key!r}: expected {expected_type_name}, got {type(val).__name__}"
                )

    # Enum field checks
    for field_name, allowed in schema.get("enum_fields", {}).items():
        if field_name in payload and payload[field_name] not in allowed:
            errors.append(
                f"Field {field_name!r}: value {payload[field_name]!r} not in allowed values {allowed}"
            )

    # Warn-if-missing fields
    for warn_field in schema.get("warn_if_missing", []):
        if warn_field not in payload:
            warnings.append(f"Recommended field {warn_field!r} is absent.")

    valid = len(errors) == 0
    if strict and warnings:
        # In strict mode, warnings become errors
        errors.extend(warnings)
        warnings = []
        valid = len(errors) == 0

    return FactValidationResult(
        fact_type=fact_type,
        valid=valid,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def get_registered_fact_types() -> frozenset[str]:
    """Return the set of fact types with registered schemas."""
    return frozenset(_FACT_SCHEMAS.keys())


def is_known_fact_type(fact_type: str) -> bool:
    """Return True if the fact type has a registered schema."""
    return fact_type in _FACT_SCHEMAS
