"""specs/people.md Phase 4, PPL-W4.3: field allowlist and confidence-threshold
validation for provider observations (§6.7, §6.8).

"Every field name/value is validated through a registered field schema
before entering Zone A state. Confidence is constrained to [0.0, 1.0];
only exact structured provider observations at the policy's auto-accept
threshold may apply without attestation, while lower-confidence
observations become conflicts/candidates." This module is the one place
that decision gets made -- PPL-W4.4's writer integration consumes its
output rather than re-deriving accept/quarantine/reject logic itself.

Three outcomes per field observation, never a fourth:

- ACCEPTED: the field is in BOTH the provider's configured
  `allowed_fields` AND the registered Zone A field vocabulary
  (`people_registry_governance.PERSON_FIELDS`, reused directly rather
  than duplicated), the observation is `PRESENT`, and its confidence
  meets the policy's `auto_accept_confidence_threshold`.
- QUARANTINED: allowlisted and registered, but below the confidence
  threshold -- a human-reviewable conflict/candidate, not a write.
  Reused the REAL runtime quarantine surface
  (`people_change_journal.append_people_conflict_record` /
  `people_query.list_conflicts`, PPL-W1.7/PPL-W3.1) rather than the
  migration-time-only `ConflictCandidate` class in
  `people_shared_migration.py`, which does not apply here.
- REJECTED: the field is outside the provider's configured allowlist,
  or is not a registered field at all (e.g. `manager_alias` -- an
  intentionally unresolved intermediate PPL-W4.2's adapter emits, never
  a canonical write target) -- rejected before reaching any writer,
  never silently coerced into some other outcome.

Registered-but-unresolved fields (currently just `manager_alias`) are
neither ACCEPTED nor QUARANTINED nor REJECTED as a validation failure --
they route to a fourth, distinct disposition, UNRESOLVED, since they are
not malformed input; they are correctly-shaped input a later resolution
stage (not yet built) must turn into a canonical field before it can ever
be ACCEPTED. Treating them as REJECTED would be misleading (nothing is
wrong with them) and treating them as QUARANTINED would incorrectly
surface them to a human as an ordinary confidence-driven conflict.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from src.core.identity_provider_port import (
    FieldObservation,
    IdentityLookupRequest,
    IdentityProviderConfig,
    ObservationState,
    ProviderBatchResult,
)
from src.core.people_change_journal import append_people_conflict_record
from src.core.people_registry_governance import PERSON_FIELDS

_POLICY_PATH = Path("vertex/policies/identity_source_authority.yaml")
_REPO_ROOT = Path(".")
_OVERRIDE_RELATIVE_PATH = Path("policies/identity_source_authority.yaml")
#: Registered but not yet resolvable to a canonical Zone A field -- see
#: `src/core/identity_provider_local_import.py`'s own docstring for why
#: `manager_alias` is emitted this way rather than as `manager_entity_id`.
UNRESOLVED_FIELDS = frozenset({"manager_alias"})

ACCEPTED = "accepted"
QUARANTINED = "quarantined"
REJECTED = "rejected"
UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class IdentitySourceAuthorityPolicy:
    schema_version: str
    provider_priority: tuple[str, ...]
    auto_accept_confidence_threshold: float


@functools.lru_cache(maxsize=1)
def _load_policy_doc(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_identity_source_authority_policy(
    *,
    knowledge_root: Path | None = None,
    repo_root: Path = _REPO_ROOT,
    override_path: Path | None = None,
) -> IdentitySourceAuthorityPolicy:
    """Platform default (`vertex/policies/identity_source_authority.yaml`),
    shallow-overridden by `knowledge_root/policies/identity_source_authority.yaml`'s
    `identity_source_authority_override` key if present -- the exact
    override shape `knowledge/people_registry_policy.example.yaml` already
    illustrates. Cached per path; tests pass `override_path` directly."""
    base_path = override_path or (repo_root / _POLICY_PATH)
    raw = dict(_load_policy_doc(base_path))

    if knowledge_root is not None:
        override_file = knowledge_root / _OVERRIDE_RELATIVE_PATH
        if override_file.exists():
            override_doc = yaml.safe_load(override_file.read_text(encoding="utf-8")) or {}
            override_section = override_doc.get("identity_source_authority_override") or {}
            raw = {**raw, **override_section}

    return IdentitySourceAuthorityPolicy(
        schema_version=str(raw.get("policy_schema_version", "1")),
        provider_priority=tuple(str(p) for p in (raw.get("provider_priority") or ())),
        auto_accept_confidence_threshold=float(raw.get("auto_accept_confidence_threshold", 0.95)),
    )


@dataclass(frozen=True, slots=True)
class ValidatedFieldObservation:
    request_id: str
    entity_id: str | None
    field_name: str
    value: object
    confidence: float
    observed_at: datetime
    outcome: str  # ACCEPTED | QUARANTINED | REJECTED | UNRESOLVED
    reason: str


def validate_and_route_observations(
    batch: ProviderBatchResult,
    *,
    requests: tuple[IdentityLookupRequest, ...],
    provider_config: IdentityProviderConfig,
    policy: IdentitySourceAuthorityPolicy,
) -> tuple[ValidatedFieldObservation, ...]:
    """Classify every `FieldObservation` in `batch` into exactly one of
    ACCEPTED/QUARANTINED/REJECTED/UNRESOLVED. Pure function -- no journal
    writes here; callers that want a durable quarantine record call
    `quarantine_field_observation` per QUARANTINED entry (PPL-W4.4's job,
    since only it holds the transaction/actor context a journal write
    needs)."""
    entity_id_by_request: dict[str, str | None] = {request.request_id: request.entity_id for request in requests}
    allowed = set(provider_config.allowed_fields)

    results: list[ValidatedFieldObservation] = []
    for observation in batch.observations:
        if observation.state is not ObservationState.PRESENT:
            continue
        entity_id = entity_id_by_request.get(observation.request_id)
        for field in observation.fields:
            results.append(_classify_field(observation.request_id, entity_id, field, allowed=allowed, policy=policy))
    return tuple(results)


def _classify_field(
    request_id: str,
    entity_id: str | None,
    field: FieldObservation,
    *,
    allowed: set[str],
    policy: IdentitySourceAuthorityPolicy,
) -> ValidatedFieldObservation:
    common = dict(
        request_id=request_id, entity_id=entity_id, field_name=field.field_name,
        value=field.value, confidence=field.confidence, observed_at=field.observed_at,
    )
    if field.field_name not in allowed:
        return ValidatedFieldObservation(
            **common, outcome=REJECTED,
            reason=f"{field.field_name!r} is not in this provider's configured allowed_fields.",
        )
    if field.field_name in UNRESOLVED_FIELDS:
        return ValidatedFieldObservation(
            **common, outcome=UNRESOLVED,
            reason=f"{field.field_name!r} is a registered-but-unresolved observation; a resolution stage must map it to a canonical field before it can be accepted.",
        )
    if field.field_name not in PERSON_FIELDS:
        return ValidatedFieldObservation(
            **common, outcome=REJECTED,
            reason=f"{field.field_name!r} is not a registered Zone A person field.",
        )
    if not (0.0 <= field.confidence <= 1.0):
        return ValidatedFieldObservation(
            **common, outcome=REJECTED,
            reason=f"confidence {field.confidence!r} is outside the valid [0.0, 1.0] range.",
        )
    if field.confidence < policy.auto_accept_confidence_threshold:
        return ValidatedFieldObservation(
            **common, outcome=QUARANTINED,
            reason=f"confidence {field.confidence:.3f} is below the auto-accept threshold {policy.auto_accept_confidence_threshold:.3f}.",
        )
    return ValidatedFieldObservation(**common, outcome=ACCEPTED, reason="within allowlist and at/above the auto-accept confidence threshold.")


def quarantine_field_observation(
    knowledge_root: Path,
    *,
    workspace_id: str,
    provider: str,
    refresh_run_id: str,
    observation: ValidatedFieldObservation,
    actor: str,
    as_of: datetime | None = None,
) -> dict:
    """Append one `people_conflicts.jsonl` record for a QUARANTINED
    observation, reusing the real PPL-W1.7 journal primitive rather than
    inventing a parallel one. `conflict_id` is deterministic
    (refresh_run_id + field) so a repeat call for the same observation
    within the same refresh run is idempotent in intent, though the
    journal itself is append-only (repeat calls append repeat records,
    same as every other journal write in this codebase)."""
    conflict_id = f"{refresh_run_id}-{observation.request_id}-{observation.field_name}"
    return append_people_conflict_record(
        knowledge_root,
        workspace_id=workspace_id,
        conflict_id=conflict_id,
        decision="quarantined",
        authenticated_principal=actor,
        reason=f"provider {provider!r} field {observation.field_name!r}: {observation.reason}",
        entity_id=observation.entity_id,
        as_of=as_of,
    )
