"""specs/people.md Phase 6, PPL-W6.3a: steward-authorized person
lifecycle-status transitions.

§7.6: "Departure/inactivation is represented explicitly; removing a
record is not the normal offboarding path." "Rehire/reinstatement
updates lifecycle with history." No write path anywhere in this codebase
transitioned `PersonStatus` after initial record creation before this
item -- confirmed by a repo-wide grep of every `PersonStatus.*` write
site during PPL-W6.1/W6.2's own investigation (specs/people.md §9.6's
PPL-W6.3 row): every existing site was either record creation or a pure
reader. This module is that missing write path -- a genuine transition
(active <-> inactive <-> departed, or into/out of unknown), never a
silent default, and always attributed/reasoned/journaled like every
other steward correction in this codebase.

Steward-gated, mirroring `people_registry_corrections.py`'s merge/split/
bind bar (`directory_steward_principals` membership required) rather
than `people_registry_governance.py::govern_person_fields`'s lighter
any-authenticated-actor bar for pin/unpin/attest -- a lifecycle
transition is more consequential than a field-verification pin: §7.6
itself names real downstream effects ("a departed/inactive person
referenced as an active accountable owner is a governance failure";
"a departed/inactive person cannot be added to an external-send
audience"), so the higher authorization bar matches the higher stakes.

Follows `govern_person_fields`'s own staged-transaction shape (a single-
file `people_directory.yaml` transaction via `prepare_registry_files_transaction`/
`commit_registry_files_transaction`) rather than `people_registry_corrections.py`'s
heavier multi-file `_RegistryState` machinery -- a status transition, like
a field-verification pin, touches exactly one person's one record, not a
cross-entity structural change like merge/split.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from src.core.exceptions import ConfigError
from src.core.people_change_journal import append_people_change_record
from src.core.people_directory_schema import PersonDirectory, PersonStatus, load_people_directory, write_people_directory
from src.core.people_entity_schema import load_entities_document
from src.core.people_material_ledger_events import enqueue_identity_lifecycle_changed_event
from src.core.people_registry_governance import require_adopted_registry
from src.core.people_registry_identity import RegistryConfig, load_registry_config, load_registry_manifest
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction


@dataclass(frozen=True, slots=True)
class PersonLifecycleTransitionResult:
    entity_id: str
    from_status: PersonStatus
    to_status: PersonStatus
    transaction_id: str | None = None
    generation_id: str | None = None


def _require_steward(knowledge_root: Path, actor: str) -> RegistryConfig:
    if not actor.strip() or actor == "<preview>":
        raise ConfigError("An authenticated directory-steward principal is required for a lifecycle-status transition.")
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError("The registry has not been bootstrapped yet; no managed registry generation exists.")
    if actor not in config.directory_steward_principals:
        raise ConfigError(
            f"Authenticated principal {actor!r} is not an authorized directory steward. "
            "Add it to registry.yaml directory_steward_principals through the governed configuration flow."
        )
    require_adopted_registry(knowledge_root, consumer="Person lifecycle transition")
    return config


def _resolve_active_person(knowledge_root: Path, person_ref: str) -> PersonDirectory:
    document = load_entities_document(knowledge_root / "entities.yaml")
    directory = load_people_directory(knowledge_root / "people_directory.yaml")
    if document is None or directory is None:
        raise ConfigError("A lifecycle-status transition requires committed shared entities.yaml and people_directory.yaml.")
    reference = person_ref.strip()
    if not reference:
        raise ConfigError("A non-empty canonical person ID or uniquely resolving alias is required.")
    direct = [entity for entity in document.entities if entity.entity_id == reference]
    alias_matches = [
        entity
        for entity in document.entities
        if any(alias.value.casefold() == reference.casefold() for alias in entity.aliases)
    ]
    matches = direct or alias_matches
    people_matches = [entity for entity in matches if entity.entity_type == "person"]
    if len(people_matches) != 1:
        raise ConfigError(f"Person reference {person_ref!r} must resolve to exactly one canonical person entity.")
    person = [candidate for candidate in directory.people if candidate.entity_id == people_matches[0].entity_id]
    if len(person) != 1:
        raise ConfigError(f"Canonical person {people_matches[0].entity_id!r} must have exactly one people-directory record.")
    return person[0]


def transition_person_lifecycle_status(
    knowledge_root: Path,
    *,
    person_ref: str,
    new_status: PersonStatus,
    reason: str,
    actor: str,
    apply: bool,
    as_of: datetime | None = None,
) -> PersonLifecycleTransitionResult:
    """`apply=False` mirrors `people_registry_corrections.py::merge_people`'s
    own preview convention: resolves the person and computes the
    would-be transition, but skips the steward-authorization check and
    never writes -- a caller can preview a transition without steward
    credentials."""
    if not reason.strip():
        raise ConfigError("A non-empty steward review reason is required for a lifecycle-status transition.")
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)

    person = _resolve_active_person(knowledge_root, person_ref)
    if person.status is new_status:
        raise ConfigError(f"Person {person.entity_id!r} already has status {new_status.value!r}; nothing to transition.")

    result = PersonLifecycleTransitionResult(entity_id=person.entity_id, from_status=person.status, to_status=new_status)
    if not apply:
        return result

    config = _require_steward(knowledge_root, actor)
    # §7.6: "Rehire/reinstatement updates lifecycle with history" -- a
    # transition back to ACTIVE clears departed_at (no longer departed);
    # a transition INTO DEPARTED stamps the transition time; any other
    # transition (e.g. active <-> inactive) leaves a prior departed_at
    # untouched, preserving that history rather than erasing it.
    updated_person = replace(
        person,
        status=new_status,
        departed_at=now if new_status is PersonStatus.DEPARTED else (None if new_status is PersonStatus.ACTIVE else person.departed_at),
    )

    def write_staged_files(staged_dir: Path) -> None:
        require_adopted_registry(knowledge_root, consumer="Person lifecycle transition")
        directory = load_people_directory(knowledge_root / "people_directory.yaml")
        assert directory is not None
        current = next((candidate for candidate in directory.people if candidate.entity_id == person.entity_id), None)
        if current is None or current.status is not person.status:
            raise ConfigError("Person lifecycle status changed while waiting for the writer lease; re-run the preview.")
        replacement = tuple(updated_person if candidate.entity_id == person.entity_id else candidate for candidate in directory.people)
        write_people_directory(staged_dir / "people_directory.yaml", replacement)

    def validate_staged_files(staged_dir: Path) -> None:
        if load_people_directory(staged_dir / "people_directory.yaml") is None:
            raise ConfigError("Staged people_directory.yaml is missing.")

    manifest = load_registry_manifest(knowledge_root)
    if manifest is None:
        raise ConfigError("The registry manifest is missing; recover bootstrap before transitioning a person's lifecycle status.")
    prepared = prepare_registry_files_transaction(
        knowledge_root,
        ("people_directory.yaml",),
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=now,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=now)

    append_people_change_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id=committed.transaction_id,
        generation_id=committed.manifest.generation_id,
        authenticated_principal=actor,
        operation="lifecycle_transition",
        entity_id=person.entity_id,
        field="status",
        before=person.status.value,
        after=new_status.value,
        source="directory_steward",
        reason=reason,
        as_of=now,
    )
    # PPL-W6.3b: reuses PPL-W6.1/W6.2's exact material-ledger event
    # pattern, now that this function is the first (and only) real
    # trigger site for identity.lifecycle_changed.
    enqueue_identity_lifecycle_changed_event(
        knowledge_root, transaction_id=committed.transaction_id,
        person_entity_id=person.entity_id, from_status=person.status, to_status=new_status,
    )
    return replace(result, transaction_id=committed.transaction_id, generation_id=committed.manifest.generation_id)
