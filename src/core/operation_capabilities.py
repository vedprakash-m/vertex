"""RELO-W5.8: operation capability contract and the three-tier effect key.

Tier 1 -- ``semantic_effect_key``: what the effect *means* (program,
provider/tenant/project, versioned operation, stable target, causal occurrence
and canonical desired effect). It excludes approval policy/identity and the
per-attempt ``operation_intent_id``, so retries and policy revalidation do not
create a second effect.

Tier 2 -- ``provider_request_key``: the provider-scoped request identity used
as the outbox idempotency key. It delegates to
``actuation_idempotency_key`` so existing outbox keys stay byte-identical.

Tier 3 -- ``OperationCapability``: the per-operation contract (effect class,
remote marker, reconciliation, compensation, blast radius). Unregistered
operations get a typed ``unknown_capability`` refusal, never a default.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Literal, Mapping

SEMANTIC_EFFECT_KEY_PREFIX = "sek1:"


class EffectClass(str, Enum):
    CREATE = "create"
    APPEND = "append"
    MUTATE = "mutate"


@dataclass(frozen=True, slots=True)
class OperationCapability:
    operation: str
    effect_class: EffectClass
    supports_remote_marker: bool
    reconcilable: bool
    compensable: bool
    max_blast_radius: int
    marker_strategy: Literal["ado_tag_or_custom_field", "hidden_comment"] | None = None


@dataclass(frozen=True, slots=True)
class CapabilityRefusal:
    operation: str
    reason: Literal["unknown_capability"] = "unknown_capability"


# Create uses an ADO tag/custom field; when governance does not permit one,
# ADR-0020 ratifies a hidden comment marker for the same stable intent/effect.
# Non-create mutations are compensated by ADOWriter rollback but do not yet
# claim marker-reconciliation qualification.
OPERATION_CAPABILITIES: Mapping[str, OperationCapability] = MappingProxyType({
    capability.operation: capability
    for capability in (
        OperationCapability(
            "create_task.v1", EffectClass.CREATE, True, True, False, 1,
            "ado_tag_or_custom_field",
        ),
        OperationCapability(
            "add_comment.v1", EffectClass.APPEND, True, False, True, 1,
            "hidden_comment",
        ),
        OperationCapability(
            "add_tag.v1", EffectClass.MUTATE, True, False, True, 1,
            "hidden_comment",
        ),
        OperationCapability(
            "set_field.v1", EffectClass.MUTATE, True, False, True, 1,
            "hidden_comment",
        ),
    )
})


def lookup_capability(operation: str) -> OperationCapability | CapabilityRefusal:
    """Return the registered capability, or an ``unknown_capability`` refusal."""
    return OPERATION_CAPABILITIES.get(operation) or CapabilityRefusal(operation=operation)


def semantic_effect_key(
    *,
    program_id: str,
    provider: str,
    tenant: str,
    project: str,
    operation: str,
    target_identity: str,
    causal_occurrence_id: str,
    payload: Mapping[str, object],
) -> str:
    """Bind one semantic effect independently of approval or attempt identity."""
    for name, value in (
        ("program_id", program_id),
        ("provider", provider),
        ("tenant", tenant),
        ("project", project),
        ("operation", operation),
        ("target_identity", target_identity),
        ("causal_occurrence_id", causal_occurrence_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
    canonical = json.dumps(
        {
            "program_id": program_id,
            "provider": provider,
            "tenant": tenant,
            "project": project,
            "operation": operation,
            "target_identity": target_identity,
            "causal_occurrence_id": causal_occurrence_id,
            "desired_effect_hash": desired_effect_hash(payload),
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return SEMANTIC_EFFECT_KEY_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def desired_effect_hash(payload: Mapping[str, object]) -> str:
    """Return the order/format-insensitive hash of one normalized desired effect."""
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError("desired effect must be JSON serializable") from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_target_identity(
    *,
    provider: str,
    tenant: str,
    project: str,
    causal_occurrence_id: str,
    payload: Mapping[str, object],
) -> str:
    """Stable create target derived from origin, destination and desired effect."""
    canonical = json.dumps(
        {
            "provider": provider,
            "tenant": tenant,
            "project": project,
            "causal_occurrence_id": causal_occurrence_id,
            "desired_effect_hash": desired_effect_hash(payload),
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return "origin1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def provider_request_key(
    *, program_id: str, org: str, project: str, operation: str, target_identity: str, operation_intent_id: str,
) -> str:
    """Byte-identical to ``actuation_idempotency_key``:
    ``program_id:org/project:operation:target_identity:operation_intent_id``."""
    return f"{program_id}:{org}/{project}:{operation}:{target_identity}:{operation_intent_id}"


def _normalized_tags(value: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise TypeError("tags must be a string")
    normalized = tuple(
        dict.fromkeys(part.strip() for part in value.split(";") if part.strip())
    )
    if not normalized and not allow_empty:
        raise ValueError("tags must contain at least one value")
    return normalized


def _patch_write(
    payload: Mapping[str, object], required_path: str
) -> Mapping[str, object]:
    body = payload.get("body", payload)
    if not isinstance(body, list):
        raise ValueError("operation requires a JSON Patch body")
    for item in body:
        if (
            isinstance(item, Mapping)
            and item.get("op") in {"add", "replace"}
            and isinstance(item.get("path"), str)
            and (
                not required_path
                or item["path"].casefold().endswith(required_path.casefold())
            )
        ):
            return item
    raise ValueError(
        f"operation payload is missing a {required_path or '/fields/'} write"
    )


def normalize_semantic_payload(
    operation: str, payload: Mapping[str, object]
) -> dict[str, object]:
    if isinstance(lookup_capability(operation), CapabilityRefusal):
        raise ValueError(f"Operation {operation!r} has no semantic normalization")
    if operation == "create_task.v1":
        semantic: dict[str, object] = {}
        for field in (
            "title",
            "description",
            "assigned_to",
            "area_path",
            "iteration_path",
            "target_date",
        ):
            value = payload.get(field)
            if value:
                semantic[field] = value
        if payload.get("priority") is not None:
            semantic["priority"] = payload["priority"]
        if payload.get("tags"):
            semantic["tags"] = _normalized_tags(payload["tags"])
        return semantic
    if operation == "add_comment.v1":
        text = payload.get("text", payload.get("proposed_value"))
        body = payload.get("body")
        if text is None and isinstance(body, Mapping):
            text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("Operation add_comment.v1 requires comment text")
        return {"text": text}
    if operation == "add_tag.v1":
        if "tag" in payload:
            value = payload["tag"]
        elif "proposed_value" in payload:
            value = payload["proposed_value"]
        else:
            value = _patch_write(payload, "/fields/System.Tags").get("value")
        return {"tags": _normalized_tags(value)}
    if operation == "set_field.v1":
        if "field" in payload or "value" in payload:
            if "field" not in payload or "value" not in payload:
                raise ValueError("Operation set_field.v1 requires field and value")
            field_name = payload["field"]
            value = payload["value"]
        else:
            patch = _patch_write(payload, "")
            path = patch.get("path")
            if not isinstance(path, str) or not path.casefold().startswith("/fields/"):
                raise ValueError("Operation set_field.v1 requires an ADO field path")
            field_name = path[len("/fields/"):]
            value = patch.get("value")
        if not isinstance(field_name, str) or not field_name.strip():
            raise ValueError("Operation set_field.v1 requires a field")
        return {"field": field_name.strip().casefold(), "value": value}
    raise AssertionError(f"Registered operation {operation!r} lacks normalization")


__all__ = [
    "CapabilityRefusal",
    "EffectClass",
    "OPERATION_CAPABILITIES",
    "OperationCapability",
    "create_target_identity",
    "desired_effect_hash",
    "lookup_capability",
    "normalize_semantic_payload",
    "provider_request_key",
    "semantic_effect_key",
]
