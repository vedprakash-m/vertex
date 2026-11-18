"""RELO-W5.8: operation capability contract and the three-tier effect key.

Tier 1 -- ``semantic_effect_key``: what the effect *means* (program, versioned
operation, target, canonical payload). It is independent of provider tenant
and of the per-attempt ``operation_intent_id``, so two approvals that would
produce the same remote change share one semantic key.

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

from src.core.actuation_outbox import actuation_idempotency_key

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


@dataclass(frozen=True, slots=True)
class CapabilityRefusal:
    operation: str
    reason: Literal["unknown_capability"] = "unknown_capability"


# create_task: tagged ``vertex-intent-<id>`` (remote marker) and recovered by
# ``reconcile_uncertain_create_task``; ADO rollback does not delete tasks.
# The others are compensated by ``ADOWriter.rollback_manifest`` and have no
# outbox reconciler. Every operation touches exactly one work item.
OPERATION_CAPABILITIES: Mapping[str, OperationCapability] = MappingProxyType({
    capability.operation: capability
    for capability in (
        OperationCapability("create_task.v1", EffectClass.CREATE, True, True, False, 1),
        OperationCapability("add_comment.v1", EffectClass.APPEND, False, False, True, 1),
        OperationCapability("add_tag.v1", EffectClass.MUTATE, False, False, True, 1),
        OperationCapability("set_field.v1", EffectClass.MUTATE, False, False, True, 1),
    )
})


def lookup_capability(operation: str) -> OperationCapability | CapabilityRefusal:
    """Return the registered capability, or an ``unknown_capability`` refusal."""
    return OPERATION_CAPABILITIES.get(operation) or CapabilityRefusal(operation=operation)


def semantic_effect_key(
    *, program_id: str, operation: str, target_identity: str, payload: Mapping[str, object],
) -> str:
    """``sek1:`` + sha256 hex of canonical JSON (sorted keys, compact
    separators, UTF-8) over program, operation, target and payload."""
    canonical = json.dumps(
        {"program_id": program_id, "operation": operation, "target_identity": target_identity, "payload": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return SEMANTIC_EFFECT_KEY_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def provider_request_key(
    *, program_id: str, org: str, project: str, operation: str, target_identity: str, operation_intent_id: str,
) -> str:
    """Byte-identical to ``actuation_idempotency_key``:
    ``program_id:org/project:operation:target_identity:operation_intent_id``."""
    return actuation_idempotency_key(
        program_id=program_id, org=org, project=project, operation=operation,
        target_identity=target_identity, operation_intent_id=operation_intent_id,
    )
