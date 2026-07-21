"""specs/people.md Phase 5b, PPL-W5b.2: delegation lifecycle write path.

Mirrors `people_registry_corrections.py`'s steward-authorized pattern
(reason required, `directory_steward_principals` authorization, journal
entries via PPL-W1.7) but through a dedicated single-file staged
transaction on `delegations.yaml` rather than the full multi-document
merge/split machinery -- creating or revoking a delegation never touches
`entities.yaml`/`people_directory.yaml`/`memberships.yaml` themselves.

Gated by the `delegation_enabled` kill switch (scaffolded in
`people_registry_identity.py`/`people_registry_modes.py`): checked via
`load_effective_registry_config` before any read or write, so a disabled
workspace's create/revoke calls fail closed before touching the registry
at all, matching `identity_provider_refresh.py`'s and
`audience_scope_recipients.py`'s own established kill-switch precedent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.core.exceptions import ConfigError
from src.core.ledger.ulid import new_ulid
from src.core.people_change_journal import append_people_change_record
from src.core.people_delegation_schema import (
    Delegation,
    DelegationStatus,
    delegation_to_payload,
    delegations_path,
    load_delegations,
    write_delegations,
)
from src.core.people_entity_schema import EntityStatus, load_entities_document
from src.core.people_namespace_bridge import resolve_ref_to_canonical_entity_id
from src.core.people_registry_governance import require_adopted_registry
from src.core.people_registry_identity import RegistryConfig, load_registry_config, load_registry_manifest
from src.core.people_registry_modes import load_effective_registry_config
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction

_DELEGATIONS_PATH = "delegations.yaml"


def _require_delegation_enabled(knowledge_root: Path) -> None:
    effective = load_effective_registry_config(knowledge_root)
    if effective is None or not effective.effective_delegation_enabled:
        raise ConfigError(
            "Delegation is disabled for this workspace. Enable it via "
            "'vertex kb registry mode set-flag delegation_enabled true' before creating or revoking a delegation."
        )


def _require_steward(knowledge_root: Path, actor: str) -> RegistryConfig:
    if not actor.strip() or actor == "<preview>":
        raise ConfigError("An authenticated directory-steward principal is required for this operation.")
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError("The registry has not been bootstrapped yet; no managed registry generation exists.")
    if actor not in config.directory_steward_principals:
        raise ConfigError(
            f"Authenticated principal {actor!r} is not an authorized directory steward. "
            "Add it to registry.yaml directory_steward_principals through the governed configuration flow."
        )
    require_adopted_registry(knowledge_root, consumer="Delegation lifecycle")
    return config


def _resolve_active_person_entity_id(knowledge_root: Path, ref: str, *, field_name: str) -> str:
    document = load_entities_document(knowledge_root / "entities.yaml")
    if document is None:
        raise ConfigError("Delegations require a committed shared entities.yaml.")
    resolution = resolve_ref_to_canonical_entity_id(ref, entities=document.entities, redirects=document.redirects)
    if resolution.canonical_entity_id is None:
        raise ConfigError(f"{field_name} {ref!r} does not resolve to a known canonical person.")
    entity = next((entity for entity in document.entities if entity.entity_id == resolution.canonical_entity_id), None)
    if entity is None or entity.entity_type != "person" or entity.status is not EntityStatus.ACTIVE:
        raise ConfigError(f"{field_name} {ref!r} must resolve to an active canonical person.")
    return entity.entity_id


def _commit_delegations(
    knowledge_root: Path,
    updated: tuple[Delegation, ...],
    *,
    actor: str,
    as_of: datetime,
) -> tuple[str, str]:
    manifest = load_registry_manifest(knowledge_root)
    if manifest is None:
        raise ConfigError("The registry manifest is missing; recover bootstrap before writing a delegation.")

    def write_staged_files(staged_dir: Path) -> None:
        write_delegations(staged_dir / _DELEGATIONS_PATH, updated)

    def validate_staged_files(staged_dir: Path) -> None:
        load_delegations(staged_dir / _DELEGATIONS_PATH)

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        (_DELEGATIONS_PATH,),
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=as_of,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=as_of)
    return committed.transaction_id, committed.manifest.generation_id


def create_delegation(
    knowledge_root: Path,
    *,
    from_ref: str,
    to_ref: str,
    surfaces: tuple[str, ...],
    valid_from: datetime,
    valid_until: datetime,
    reason: str,
    actor: str,
    apply: bool,
    program_ids: tuple[str, ...] = (),
    workstream_ids: tuple[str, ...] = (),
    as_of: datetime | None = None,
) -> Delegation:
    """`apply=False` mirrors `people_registry_corrections.py::merge_people`'s
    own preview convention: validates and resolves references, returns the
    would-be `Delegation`, but skips the kill-switch/steward-authorization
    checks and never writes -- a caller can preview a delegation before the
    `delegation_enabled` flag is even turned on."""
    if not reason.strip():
        raise ConfigError("A non-empty steward review reason is required to create a delegation.")
    if not surfaces:
        raise ConfigError("At least one surface is required to create a delegation.")
    if valid_until <= valid_from:
        raise ConfigError("valid_until must be after valid_from.")
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)

    from_entity_id = _resolve_active_person_entity_id(knowledge_root, from_ref, field_name="from_person")
    to_entity_id = _resolve_active_person_entity_id(knowledge_root, to_ref, field_name="to_person")
    if from_entity_id == to_entity_id:
        raise ConfigError("A person cannot delegate to themselves.")

    new_delegation = Delegation(
        delegation_id=f"delegation:{new_ulid(now)}",
        from_person_entity_id=from_entity_id,
        to_person_entity_id=to_entity_id,
        surfaces=surfaces,
        valid_from=valid_from,
        valid_until=valid_until,
        reason=reason,
        actor_principal=actor,
        program_ids=program_ids,
        workstream_ids=workstream_ids,
        status=DelegationStatus.ACTIVE,
    )
    if not apply:
        return new_delegation

    _require_delegation_enabled(knowledge_root)
    config = _require_steward(knowledge_root, actor)
    existing = load_delegations(delegations_path(knowledge_root))
    updated = (*existing, new_delegation)
    transaction_id, generation_id = _commit_delegations(knowledge_root, updated, actor=actor, as_of=now)

    append_people_change_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id=transaction_id,
        generation_id=generation_id,
        authenticated_principal=actor,
        operation="delegation_create",
        entity_id=new_delegation.delegation_id,
        field="delegation",
        before=None,
        after=delegation_to_payload(new_delegation),
        source="directory_steward",
        reason=reason,
        as_of=now,
    )
    return new_delegation


def revoke_delegation(
    knowledge_root: Path,
    *,
    delegation_id: str,
    reason: str,
    actor: str,
    apply: bool,
    as_of: datetime | None = None,
) -> Delegation:
    if not reason.strip():
        raise ConfigError("A non-empty steward review reason is required to revoke a delegation.")
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)

    existing = load_delegations(delegations_path(knowledge_root))
    target = next((delegation for delegation in existing if delegation.delegation_id == delegation_id), None)
    if target is None:
        raise ConfigError(f"Delegation {delegation_id!r} does not exist.")
    if target.status is DelegationStatus.REVOKED:
        raise ConfigError(f"Delegation {delegation_id!r} is already revoked.")
    revoked = replace(target, status=DelegationStatus.REVOKED)
    if not apply:
        return revoked

    _require_delegation_enabled(knowledge_root)
    config = _require_steward(knowledge_root, actor)
    updated = tuple(revoked if delegation.delegation_id == delegation_id else delegation for delegation in existing)
    transaction_id, generation_id = _commit_delegations(knowledge_root, updated, actor=actor, as_of=now)

    append_people_change_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id=transaction_id,
        generation_id=generation_id,
        authenticated_principal=actor,
        operation="delegation_revoke",
        entity_id=delegation_id,
        field="delegation",
        before=delegation_to_payload(target),
        after=delegation_to_payload(revoked),
        source="directory_steward",
        reason=reason,
        as_of=now,
    )
    return revoked


def list_delegations(knowledge_root: Path, *, active_only: bool = False, as_of: datetime | None = None) -> tuple[Delegation, ...]:
    """Read-only; deliberately ungated by the kill switch or steward
    authorization -- listing what already exists is not itself a registry
    write, matching every other read-path in this codebase (e.g.
    `program_shadow_status`) that never gates reads behind write-only
    authorization checks."""
    existing = load_delegations(delegations_path(knowledge_root))
    if not active_only:
        return existing
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return tuple(
        delegation
        for delegation in existing
        if delegation.status is DelegationStatus.ACTIVE and delegation.valid_from <= now <= delegation.valid_until
    )
