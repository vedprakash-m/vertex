"""Steward-reviewed people identity corrections for PPL-W2B.3.

These operations deliberately use the shared staged registry writer.  They
only rewrite mutable current projections; append-only ledger and journal
history is resolved through ``EntityRedirect`` and is never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import shutil
from typing import Any, cast

import yaml

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import compute_file_checksum
from src.core.ledger.ulid import new_ulid
from src.core.people_change_journal import (
    STREAM_PEOPLE_CHANGES,
    append_people_change_record,
    append_people_conflict_record,
    read_journal_records,
)
from src.core.people_directory_schema import (
    PersonDirectory,
    PersonStatus,
    TenantRelationship,
    load_people_directory,
    load_people_profiles,
    write_people_directory,
)
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityIdentifier,
    EntityRedirect,
    EntityStatus,
    IdentifierStatus,
    entity_to_payload,
    load_entities_document,
    write_entities_document,
)
from src.core.people_membership_schema import TeamMembership, load_memberships, write_memberships
from src.core.people_material_ledger_events import enqueue_ownership_changed_event
from src.core.people_namespace_bridge import normalize_alias_for_lookup, resolve_entity_redirect
from src.core.people_registry_governance import require_adopted_registry
from src.core.people_registry_identity import RegistryConfig, RegistryManifest, load_registry_config, load_registry_manifest
from src.core.people_registry_transaction import (
    commit_registry_files_transaction,
    prepare_registry_files_transaction,
    transactions_root,
)
from src.core.profile_encryption import dump_people_profiles_document, load_people_profiles_document

_ENTITIES_PATH = "entities.yaml"
_PEOPLE_PATH = "people_directory.yaml"
_PROFILES_PATH = "people_profiles.yaml"
_MEMBERSHIPS_PATH = "memberships.yaml"
_DELEGATIONS_PATH = "delegations.yaml"
_CACHE_DIR = ".cache"
_MACHINE_ENTITY_REF_KEYS = frozenset(
    {
        "entity_id",
        "entity_ids",
        "person_entity_id",
        "person_entity_ids",
        "canonical_entity_id",
        "canonical_entity_ids",
        "manager_entity_id",
        "owner_entity_id",
        "stakeholder_entity_id",
        "delegate_entity_id",
        "from_person_entity_id",
        "to_person_entity_id",
    }
)


@dataclass(frozen=True, slots=True)
class CorrectionConflict:
    kind: str
    detail: str
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class AuthoredReference:
    source_path: str
    field_path: str


@dataclass(frozen=True, slots=True)
class PeopleCorrectionResult:
    operation: str
    source_entity_id: str | None
    target_entity_id: str
    affected_paths: tuple[str, ...]
    conflicts: tuple[CorrectionConflict, ...] = ()
    authored_references: tuple[AuthoredReference, ...] = ()
    transaction_id: str | None = None
    generation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RegistryState:
    document: EntitiesDocument
    people: tuple[PersonDirectory, ...]
    memberships: tuple[TeamMembership, ...]
    profiles: dict[str, Any] | None
    delegations: dict[str, Any] | None
    caches: tuple[tuple[str, dict[str, Any] | list[Any]], ...]


def _now(as_of: datetime | None) -> datetime:
    return (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _require_steward(knowledge_root: Path, actor: str) -> tuple[RegistryConfig, RegistryManifest]:
    if not actor.strip() or actor == "<preview>":
        raise ConfigError("An authenticated directory-steward principal is required for this correction.")
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError("The registry has not been bootstrapped yet; no managed registry generation exists.")
    if actor not in config.directory_steward_principals:
        raise ConfigError(
            f"Authenticated principal {actor!r} is not an authorized directory steward. "
            "Add it to registry.yaml directory_steward_principals through the governed configuration flow."
        )
    require_adopted_registry(knowledge_root, consumer="People identity correction")
    return config, manifest


def _load_yaml_document(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, (dict, list)):
        raise ConfigError(f"Expected mapping or list in {path}.")
    return document


def _load_json_document(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(document, (dict, list)):
        raise ConfigError(f"Expected JSON mapping or list in {path}.")
    return document


def _load_cache_documents(knowledge_root: Path) -> tuple[tuple[str, dict[str, Any] | list[Any]], ...]:
    cache_root = knowledge_root / _CACHE_DIR
    if not cache_root.exists():
        return ()
    documents: list[tuple[str, dict[str, Any] | list[Any]]] = []
    for path in sorted(candidate for candidate in cache_root.rglob("*") if candidate.is_file() and candidate.suffix in {".yaml", ".yml", ".json"}):
        document = _load_json_document(path) if path.suffix == ".json" else _load_yaml_document(path)
        if document is not None:
            documents.append((path.relative_to(knowledge_root).as_posix(), document))
    return tuple(documents)


def _load_state(knowledge_root: Path) -> _RegistryState:
    document = load_entities_document(knowledge_root / _ENTITIES_PATH)
    people_result = load_people_directory(knowledge_root / _PEOPLE_PATH)
    if document is None or people_result is None:
        raise ConfigError("People identity corrections require committed shared entities.yaml and people_directory.yaml.")
    profiles_path = knowledge_root / _PROFILES_PATH
    profiles = load_people_profiles_document(profiles_path) if profiles_path.exists() else None
    if profiles is not None and not isinstance(profiles, dict):
        raise ConfigError(f"Expected mapping at top-level in {profiles_path}.")
    delegations = _load_yaml_document(knowledge_root / _DELEGATIONS_PATH)
    if delegations is not None and not isinstance(delegations, dict):
        raise ConfigError(f"Expected mapping at top-level in {knowledge_root / _DELEGATIONS_PATH}.")
    return _RegistryState(
        document=document,
        people=people_result.people,
        memberships=load_memberships(knowledge_root / _MEMBERSHIPS_PATH),
        profiles=profiles,
        delegations=delegations,
        caches=_load_cache_documents(knowledge_root),
    )


def _resolve_active_person(state: _RegistryState, reference: str) -> CanonicalEntity:
    candidate = reference.strip()
    if not candidate:
        raise ConfigError("A non-empty canonical person ID or uniquely resolving alias is required.")
    direct = [entity for entity in state.document.entities if entity.entity_id == candidate]
    matches = direct or [
        entity
        for entity in state.document.entities
        if any(normalize_alias_for_lookup(alias.value) == normalize_alias_for_lookup(candidate) for alias in entity.aliases)
    ]
    persons = [entity for entity in matches if entity.entity_type == "person"]
    if len(persons) != 1:
        raise ConfigError(f"Person reference {reference!r} must resolve to exactly one canonical person entity.")
    entity = persons[0]
    if entity.status is not EntityStatus.ACTIVE:
        raise ConfigError(f"Person {entity.entity_id!r} is not active and cannot be corrected as a current person.")
    return entity


def _person_by_entity_id(people: tuple[PersonDirectory, ...], entity_id: str) -> PersonDirectory:
    matches = [person for person in people if person.entity_id == entity_id]
    if len(matches) != 1:
        raise ConfigError(f"Canonical person {entity_id!r} must have exactly one people-directory record.")
    return matches[0]


def _identifier_key(identifier: EntityIdentifier) -> tuple[str, str]:
    return identifier.provider, identifier.subject_id


def _alias_key(alias: EntityAlias) -> str:
    return normalize_alias_for_lookup(alias.value)


def _merge_people(target: PersonDirectory, source: PersonDirectory) -> tuple[PersonDirectory | None, tuple[CorrectionConflict, ...]]:
    conflicts: list[CorrectionConflict] = []
    scalar_fields = (
        "display_name",
        "title",
        "manager_entity_id",
        "department",
        "status",
        "tenant_relationship",
        "departed_at",
    )
    values: dict[str, object] = {}
    for field_name in scalar_fields:
        target_value = getattr(target, field_name)
        source_value = getattr(source, field_name)
        target_unknown = target_value is None or target_value == "" or getattr(target_value, "value", None) == "unknown"
        source_unknown = source_value is None or source_value == "" or getattr(source_value, "value", None) == "unknown"
        if target_unknown:
            values[field_name] = source_value
        elif source_unknown or target_value == source_value:
            values[field_name] = target_value
        else:
            conflicts.append(
                CorrectionConflict(
                    "person_projection_conflict",
                    f"Cannot merge distinct {field_name!r} values for {target.entity_id}; steward must reconcile projections first.",
                )
            )
    target_verifications = {verification.field_name: verification for verification in target.verifications}
    for verification in source.verifications:
        existing = target_verifications.get(verification.field_name)
        if existing is None:
            target_verifications[verification.field_name] = verification
        elif existing != verification:
            conflicts.append(
                CorrectionConflict(
                    "person_verification_conflict",
                    f"Cannot merge distinct verification for {verification.field_name!r} on {target.entity_id}.",
                )
            )
    if conflicts:
        return None, tuple(conflicts)
    contacts = { (contact.kind, contact.value): contact for contact in target.contacts }
    contacts.update({(contact.kind, contact.value): contact for contact in source.contacts})
    return (
        replace(
            target,
            contacts=tuple(contacts[key] for key in sorted(contacts, key=lambda value: (value[0].value, value[1]))),
            verifications=tuple(target_verifications[name] for name in sorted(target_verifications)),
            exempt_from_vitality=target.exempt_from_vitality or source.exempt_from_vitality,
            display_name=cast(str | None, values["display_name"]),
            title=cast(str | None, values["title"]),
            manager_entity_id=cast(str | None, values["manager_entity_id"]),
            department=cast(str | None, values["department"]),
            status=cast(PersonStatus, values["status"]),
            tenant_relationship=cast(TenantRelationship, values["tenant_relationship"]),
            departed_at=cast(datetime | None, values["departed_at"]),
        ),
        (),
    )


def _merge_profiles(document: dict[str, Any] | None, *, source_id: str, target_id: str, target_alias: str) -> tuple[dict[str, Any] | None, tuple[CorrectionConflict, ...], bool]:
    if document is None:
        return None, (), False
    profiles = document.get("profiles") or []
    if not isinstance(profiles, list):
        raise ConfigError("people_profiles.yaml 'profiles' must be a list.")
    source_profiles = [profile for profile in profiles if isinstance(profile, dict) and profile.get("entity_id") == source_id]
    target_profiles = [profile for profile in profiles if isinstance(profile, dict) and profile.get("entity_id") == target_id]
    if not source_profiles:
        return document, (), False
    if len(source_profiles) != 1 or len(target_profiles) > 1:
        return document, (CorrectionConflict("profile_cardinality", "Profiles must contain at most one record per canonical entity before merge."),), False
    source = copy.deepcopy(source_profiles[0])
    source["entity_id"] = target_id
    source["alias"] = target_alias
    replacement: dict[str, Any]
    if target_profiles:
        target = copy.deepcopy(target_profiles[0])
        target["entity_id"] = target_id
        comm_style = target.get("comm_style")
        source_style = source.get("comm_style")
        if comm_style and source_style and comm_style != source_style:
            return document, (CorrectionConflict("profile_conflict", "Cannot merge distinct profile comm_style values without an explicit profile edit."),), False
        replacement = target
        replacement["comm_style"] = comm_style or source_style
        for field_name in ("cares_about", "pet_peeves"):
            target_values = list(target.get(field_name) or [])
            for value in source.get(field_name) or []:
                if value not in target_values:
                    target_values.append(value)
            replacement[field_name] = target_values
    else:
        replacement = source
    result_profiles = [
        profile
        for profile in profiles
        if not (isinstance(profile, dict) and profile.get("entity_id") in {source_id, target_id})
    ]
    result_profiles.append(replacement)
    result = copy.deepcopy(document)
    result["profiles"] = result_profiles
    return result, (), True


def _rewrite_entity_refs(value: Any, *, source_id: str, target_id: str) -> tuple[Any, int]:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        changed = 0
        for key, child in value.items():
            if key in _MACHINE_ENTITY_REF_KEYS:
                if isinstance(child, str) and child == source_id:
                    rewritten[key] = target_id
                    changed += 1
                    continue
                if isinstance(child, list):
                    rewritten_values = [target_id if item == source_id else item for item in child]
                    rewritten[key] = rewritten_values
                    changed += sum(item == source_id for item in child)
                    continue
            rewritten_child, child_changed = _rewrite_entity_refs(child, source_id=source_id, target_id=target_id)
            rewritten[key] = rewritten_child
            changed += child_changed
        return rewritten, changed
    if isinstance(value, list):
        rewritten_list: list[Any] = []
        changed = 0
        for child in value:
            rewritten_child, child_changed = _rewrite_entity_refs(child, source_id=source_id, target_id=target_id)
            rewritten_list.append(rewritten_child)
            changed += child_changed
        return rewritten_list, changed
    return value, 0


def _rewrite_memberships(memberships: tuple[TeamMembership, ...], *, source_id: str, target_id: str) -> tuple[tuple[TeamMembership, ...], bool]:
    rewritten = tuple(
        replace(membership, person_entity_id=target_id) if membership.person_entity_id == source_id else membership
        for membership in memberships
    )
    return rewritten, rewritten != memberships


def _collect_merge_paths(
    state: _RegistryState,
    *,
    source_id: str,
    target_id: str,
    target_alias: str,
) -> tuple[_RegistryState, tuple[str, ...], tuple[CorrectionConflict, ...]]:
    source_person = _person_by_entity_id(state.people, source_id)
    target_person = _person_by_entity_id(state.people, target_id)
    merged_person, conflicts = _merge_people(target_person, source_person)
    if conflicts:
        return state, (), conflicts
    assert merged_person is not None
    source_entity = next(entity for entity in state.document.entities if entity.entity_id == source_id)
    target_entity = next(entity for entity in state.document.entities if entity.entity_id == target_id)
    aliases = {_alias_key(alias): alias for alias in target_entity.aliases}
    for alias in source_entity.aliases:
        aliases.setdefault(_alias_key(alias), alias)
    identifiers = {_identifier_key(identifier): identifier for identifier in target_entity.identifiers}
    for identifier in source_entity.identifiers:
        identifiers.setdefault(_identifier_key(identifier), identifier)
    tombstoned_source = replace(source_entity, status=EntityStatus.TOMBSTONED, tombstoned_at=_now(None))
    merged_target = replace(
        target_entity,
        aliases=tuple(aliases[key] for key in sorted(aliases)),
        identifiers=tuple(identifiers[key] for key in sorted(identifiers)),
    )
    updated_people = tuple(
        replace(person, manager_entity_id=target_id) if person.manager_entity_id == source_id else person
        for person in state.people
        if person.entity_id != source_id
    )
    updated_people = tuple(merged_person if person.entity_id == target_id else person for person in updated_people)
    updated_memberships, memberships_changed = _rewrite_memberships(state.memberships, source_id=source_id, target_id=target_id)
    profiles, profile_conflicts, profiles_changed = _merge_profiles(
        state.profiles, source_id=source_id, target_id=target_id, target_alias=target_alias
    )
    if profile_conflicts:
        return state, (), profile_conflicts
    delegations, delegation_count = _rewrite_entity_refs(state.delegations, source_id=source_id, target_id=target_id)
    rewritten_caches: list[tuple[str, dict[str, Any] | list[Any]]] = []
    cache_paths: list[str] = []
    for path, document in state.caches:
        rewritten, count = _rewrite_entity_refs(document, source_id=source_id, target_id=target_id)
        rewritten_caches.append((path, rewritten))
        if count:
            cache_paths.append(path)
    redirects = tuple(redirect for redirect in state.document.redirects if redirect.from_entity_id != source_id) + (
        EntityRedirect(
            from_entity_id=source_id,
            to_entity_id=target_id,
            recorded_at=_now(None),
            principal_id="<pending steward>",
            reason="<pending merge reason>",
        ),
    )
    new_document = EntitiesDocument(
        schema_version=state.document.schema_version,
        entities=tuple(
            tombstoned_source if entity.entity_id == source_id else merged_target if entity.entity_id == target_id else entity
            for entity in state.document.entities
        ),
        redirects=redirects,
    )
    paths = [_ENTITIES_PATH, _PEOPLE_PATH]
    if memberships_changed:
        paths.append(_MEMBERSHIPS_PATH)
    if profiles_changed:
        paths.append(_PROFILES_PATH)
    if delegation_count:
        paths.append(_DELEGATIONS_PATH)
    paths.extend(cache_paths)
    return (
        _RegistryState(
            document=new_document,
            people=updated_people,
            memberships=updated_memberships,
            profiles=profiles,
            delegations=delegations if isinstance(delegations, dict) else state.delegations,
            caches=tuple(rewritten_caches),
        ),
        tuple(sorted(paths)),
        (),
    )


def _validate_state(state: _RegistryState) -> None:
    entity_by_id = {entity.entity_id: entity for entity in state.document.entities}
    if len(entity_by_id) != len(state.document.entities):
        raise ConfigError("Canonical entity IDs must be unique.")
    active_aliases: dict[str, str] = {}
    active_identifiers: dict[tuple[str, str], str] = {}
    for entity in state.document.entities:
        if entity.status is not EntityStatus.ACTIVE:
            continue
        for alias in entity.aliases:
            if alias.status is AliasStatus.RETIRED:
                continue
            key = _alias_key(alias)
            prior = active_aliases.get(key)
            if prior is not None and prior != entity.entity_id:
                raise ConfigError(f"Active alias {alias.value!r} is bound to both {prior!r} and {entity.entity_id!r}.")
            active_aliases[key] = entity.entity_id
        for identifier in entity.identifiers:
            if identifier.status is not IdentifierStatus.ACTIVE:
                continue
            identifier_key = _identifier_key(identifier)
            prior = active_identifiers.get(identifier_key)
            if prior is not None and prior != entity.entity_id:
                raise ConfigError(
                    f"Active provider identifier {identifier.provider!r}/{identifier.subject_id!r} is bound to both {prior!r} and {entity.entity_id!r}."
                )
            active_identifiers[identifier_key] = entity.entity_id
    redirect_sources: set[str] = set()
    for redirect in state.document.redirects:
        if redirect.from_entity_id in redirect_sources:
            raise ConfigError(f"More than one redirect exists for {redirect.from_entity_id!r}.")
        redirect_sources.add(redirect.from_entity_id)
        source = entity_by_id.get(redirect.from_entity_id)
        target = entity_by_id.get(redirect.to_entity_id)
        if source is None or target is None or source.status is not EntityStatus.TOMBSTONED:
            raise ConfigError("Every redirect must join a tombstoned source to an existing target.")
        resolve_entity_redirect(redirect.from_entity_id, state.document.redirects)
    active_people = {entity.entity_id for entity in state.document.entities if entity.entity_type == "person" and entity.status is EntityStatus.ACTIVE}
    if len({person.entity_id for person in state.people}) != len(state.people):
        raise ConfigError("people_directory.yaml must contain at most one record per canonical entity.")
    if any(person.entity_id not in active_people for person in state.people):
        raise ConfigError("People-directory records must reference active canonical people.")
    if any(membership.person_entity_id not in active_people for membership in state.memberships):
        raise ConfigError("Memberships must reference active canonical people.")


def _write_generic_document(path: Path, document: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(document, sort_keys=False, default_flow_style=False), encoding="utf-8")


def _write_state(
    state: _RegistryState,
    *,
    knowledge_root: Path,
    staged_dir: Path,
    paths: tuple[str, ...],
) -> None:
    if _ENTITIES_PATH in paths:
        write_entities_document(staged_dir / _ENTITIES_PATH, state.document)
    if _PEOPLE_PATH in paths:
        write_people_directory(staged_dir / _PEOPLE_PATH, state.people)
    if _MEMBERSHIPS_PATH in paths:
        write_memberships(staged_dir / _MEMBERSHIPS_PATH, state.memberships)
    if _PROFILES_PATH in paths:
        assert state.profiles is not None
        (staged_dir / _PROFILES_PATH).parent.mkdir(parents=True, exist_ok=True)
        (staged_dir / _PROFILES_PATH).write_text(
            dump_people_profiles_document(state.profiles, existing_path=knowledge_root / _PROFILES_PATH),
            encoding="utf-8",
        )
    documents = dict(state.caches)
    if state.delegations is not None:
        documents[_DELEGATIONS_PATH] = state.delegations
    for relative_path in paths:
        if relative_path in {_ENTITIES_PATH, _PEOPLE_PATH, _MEMBERSHIPS_PATH, _PROFILES_PATH}:
            continue
        document = documents.get(relative_path)
        if document is None:
            raise ConfigError(f"No staged document available for {relative_path}.")
        _write_generic_document(staged_dir / relative_path, document)


def _validate_staged_state(staged_dir: Path, paths: tuple[str, ...]) -> None:
    document = load_entities_document(staged_dir / _ENTITIES_PATH) if _ENTITIES_PATH in paths else None
    people_result = load_people_directory(staged_dir / _PEOPLE_PATH) if _PEOPLE_PATH in paths else None
    if document is not None and people_result is not None:
        _validate_state(
            _RegistryState(
                document=document,
                people=people_result.people,
                memberships=load_memberships(staged_dir / _MEMBERSHIPS_PATH) if _MEMBERSHIPS_PATH in paths else (),
                profiles=None,
                delegations=None,
                caches=(),
            )
        )
    if _PROFILES_PATH in paths:
        load_people_profiles(staged_dir / _PROFILES_PATH)
    for relative_path in paths:
        if relative_path in {_ENTITIES_PATH, _PEOPLE_PATH, _MEMBERSHIPS_PATH, _PROFILES_PATH}:
            continue
        if Path(relative_path).suffix == ".json":
            _load_json_document(staged_dir / relative_path)
        else:
            _load_yaml_document(staged_dir / relative_path)


def _record_correction(
    knowledge_root: Path,
    *,
    config: RegistryConfig,
    result: PeopleCorrectionResult,
    actor: str,
    reason: str,
    before: object,
    after: object,
    as_of: datetime,
) -> None:
    assert result.transaction_id is not None and result.generation_id is not None
    append_people_change_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id=result.transaction_id,
        generation_id=result.generation_id,
        authenticated_principal=actor,
        operation=result.operation,
        entity_id=result.source_entity_id or result.target_entity_id,
        field="identity_correction",
        before=before,
        after=after,
        source="directory_steward",
        reason=reason,
        as_of=as_of,
    )
    append_people_conflict_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        conflict_id=f"{result.transaction_id}-{result.operation}",
        decision=result.operation,
        authenticated_principal=actor,
        reason=reason,
        entity_id=result.source_entity_id or result.target_entity_id,
        as_of=as_of,
    )
    for index, conflict in enumerate(result.conflicts):
        append_people_conflict_record(
            knowledge_root,
            workspace_id=config.workspace_id,
            conflict_id=f"{result.transaction_id}-{result.operation}-conflict-{index}",
            decision="conflict",
            authenticated_principal=actor,
            reason=conflict.detail,
            entity_id=result.source_entity_id or result.target_entity_id,
            as_of=as_of,
        )


def _commit_state(
    *,
    knowledge_root: Path,
    config: RegistryConfig,
    manifest: RegistryManifest,
    state: _RegistryState,
    paths: tuple[str, ...],
    result: PeopleCorrectionResult,
    actor: str,
    reason: str,
    before: object,
    after: object,
    as_of: datetime,
) -> PeopleCorrectionResult:
    _validate_state(state)

    def write_staged_files(staged_dir: Path) -> None:
        require_adopted_registry(knowledge_root, consumer=f"People {result.operation}")
        _write_state(state, knowledge_root=knowledge_root, staged_dir=staged_dir, paths=paths)

    def validate_staged_files(staged_dir: Path) -> None:
        _validate_staged_state(staged_dir, paths)

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        paths,
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=as_of,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=as_of)
    applied = replace(result, transaction_id=committed.transaction_id, generation_id=committed.manifest.generation_id)
    if isinstance(after, dict) and "source_hashes" in after:
        after["source_hashes"] = {
            relative_path: compute_file_checksum(knowledge_root / relative_path)
            for relative_path in paths
        }
    _record_correction(
        knowledge_root,
        config=config,
        result=applied,
        actor=actor,
        reason=reason,
        before=before,
        after=after,
        as_of=as_of,
    )
    return applied


def merge_people(
    knowledge_root: Path,
    *,
    source_ref: str,
    target_ref: str,
    reason: str,
    actor: str,
    apply: bool,
    as_of: datetime | None = None,
) -> PeopleCorrectionResult:
    if not reason.strip():
        raise ConfigError("A non-empty steward review reason is required for a merge.")
    state = _load_state(knowledge_root)
    source = _resolve_active_person(state, source_ref)
    target = _resolve_active_person(state, target_ref)
    if source.entity_id == target.entity_id:
        raise ConfigError("Merge source and target must be different canonical people.")
    target_person = _person_by_entity_id(state.people, target.entity_id)
    updated_state, paths, conflicts = _collect_merge_paths(
        state, source_id=source.entity_id, target_id=target.entity_id, target_alias=target_person.alias
    )
    result = PeopleCorrectionResult("merge", source.entity_id, target.entity_id, paths, conflicts)
    if conflicts or not apply:
        return result
    config, manifest = _require_steward(knowledge_root, actor)
    updated_state = replace(
        updated_state,
        document=EntitiesDocument(
            schema_version=updated_state.document.schema_version,
            entities=updated_state.document.entities,
            redirects=tuple(
                replace(redirect, principal_id=actor, reason=reason)
                if redirect.from_entity_id == source.entity_id
                else redirect
                for redirect in updated_state.document.redirects
            ),
        ),
    )
    reports_of_source = tuple(person.entity_id for person in state.people if person.manager_entity_id == source.entity_id)
    committed = _commit_state(
        knowledge_root=knowledge_root,
        config=config,
        manifest=manifest,
        state=updated_state,
        paths=paths,
        result=result,
        actor=actor,
        reason=reason,
        before={"restorable_paths": list(paths)},
        after={"target_entity_id": target.entity_id, "source_hashes": {}},
        as_of=_now(as_of),
    )
    # PPL-W6.2: reports whose manager_entity_id pointed at the tombstoned
    # source now point at the merge target -- an ownership change for
    # each, per §7.6's "existing/extended ownership.changed."
    if committed.transaction_id is not None:
        for report_entity_id in reports_of_source:
            enqueue_ownership_changed_event(
                knowledge_root, transaction_id=committed.transaction_id,
                person_entity_id=report_entity_id, new_manager_entity_id=target.entity_id,
            )
    return committed


def bind_person_identifier(
    knowledge_root: Path,
    *,
    person_ref: str,
    provider: str,
    subject_id: str,
    reason: str,
    actor: str,
    apply: bool,
    as_of: datetime | None = None,
) -> PeopleCorrectionResult:
    if not reason.strip() or not provider.strip() or not subject_id.strip():
        raise ConfigError("A provider, subject ID, and non-empty steward review reason are required for a bind.")
    state = _load_state(knowledge_root)
    target = _resolve_active_person(state, person_ref)
    key = provider, subject_id
    owners = [
        entity.entity_id
        for entity in state.document.entities
        if entity.status is EntityStatus.ACTIVE and any(_identifier_key(identifier) == key for identifier in entity.identifiers)
    ]
    conflicts: tuple[CorrectionConflict, ...] = ()
    if owners and owners != [target.entity_id]:
        conflicts = (
            CorrectionConflict(
                "stable_identifier_already_bound",
                f"Provider identifier {provider!r}/{subject_id!r} is already bound to active person(s): {', '.join(owners)}.",
            ),
        )
    target_identifiers = { _identifier_key(identifier): identifier for identifier in target.identifiers }
    target_identifiers.setdefault(
        key,
        EntityIdentifier(
            provider=provider,
            kind="provider_subject",
            subject_id=subject_id,
            recorded_at=_now(as_of),
            verified_by_principal=actor if apply else "<preview>",
        ),
    )
    updated_target = replace(target, identifiers=tuple(target_identifiers[value] for value in sorted(target_identifiers)))
    updated_state = replace(
        state,
        document=EntitiesDocument(
            schema_version=state.document.schema_version,
            entities=tuple(updated_target if entity.entity_id == target.entity_id else entity for entity in state.document.entities),
            redirects=state.document.redirects,
        ),
    )
    result = PeopleCorrectionResult("bind", None, target.entity_id, (_ENTITIES_PATH,), conflicts)
    if conflicts or not apply:
        return result
    config, manifest = _require_steward(knowledge_root, actor)
    return _commit_state(
        knowledge_root=knowledge_root,
        config=config,
        manifest=manifest,
        state=updated_state,
        paths=result.affected_paths,
        result=result,
        actor=actor,
        reason=reason,
        before=None,
        after={"provider": provider, "subject_id": subject_id},
        as_of=_now(as_of),
    )


def _parse_identifier_partition(value: str) -> tuple[str, str]:
    provider, separator, subject_id = value.partition(":")
    if not separator or not provider.strip() or not subject_id.strip():
        raise ConfigError("--identifier must use provider:subject-id.")
    return provider, subject_id


def _find_authored_references(programs_root: Path | None, source_id: str) -> tuple[AuthoredReference, ...]:
    if programs_root is None or not programs_root.exists():
        return ()
    references: list[AuthoredReference] = []

    def walk(value: Any, *, path: str, field_path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{field_path}.{key}" if field_path else str(key)
                if key in _MACHINE_ENTITY_REF_KEYS and child == source_id:
                    references.append(AuthoredReference(path, child_path))
                walk(child, path=path, field_path=child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path=path, field_path=f"{field_path}[{index}]")

    for path in sorted(programs_root.rglob("*.yaml")) + sorted(programs_root.rglob("*.yml")):
        document = _load_yaml_document(path)
        if document is not None:
            walk(document, path=path.relative_to(programs_root).as_posix(), field_path="")
    return tuple(references)


def split_person(
    knowledge_root: Path,
    *,
    person_ref: str,
    aliases_for_new_person: tuple[str, ...],
    aliases_retained_by_source: tuple[str, ...],
    identifiers_for_new_person: tuple[str, ...],
    identifiers_retained_by_source: tuple[str, ...],
    reason: str,
    actor: str,
    apply: bool,
    programs_root: Path | None = None,
    new_entity_id: str | None = None,
    as_of: datetime | None = None,
) -> PeopleCorrectionResult:
    if not reason.strip() or not aliases_for_new_person:
        raise ConfigError("A non-empty steward review reason and at least one --alias are required for a split.")
    state = _load_state(knowledge_root)
    source = _resolve_active_person(state, person_ref)
    selected_aliases = {normalize_alias_for_lookup(alias) for alias in aliases_for_new_person}
    retained_aliases = {normalize_alias_for_lookup(alias) for alias in aliases_retained_by_source}
    available_aliases = {_alias_key(alias): alias for alias in source.aliases}
    if selected_aliases & retained_aliases or selected_aliases | retained_aliases != set(available_aliases):
        raise ConfigError(
            "Split requires an explicit, non-overlapping partition of every source alias: "
            "use --alias for the new person and --retain-alias for the source."
        )
    selected_identifier_keys = {_parse_identifier_partition(value) for value in identifiers_for_new_person}
    retained_identifier_keys = {_parse_identifier_partition(value) for value in identifiers_retained_by_source}
    available_identifiers = {_identifier_key(identifier): identifier for identifier in source.identifiers}
    if selected_identifier_keys & retained_identifier_keys or selected_identifier_keys | retained_identifier_keys != set(available_identifiers):
        raise ConfigError(
            "Split requires an explicit, non-overlapping partition of every source provider identifier: "
            "use --identifier for the new person and --retain-identifier for the source."
        )
    now = _now(as_of)
    target_id = new_entity_id or f"person:{new_ulid(now)}"
    if not target_id.startswith("person:") or target_id in {entity.entity_id for entity in state.document.entities}:
        raise ConfigError("--new-id must be a new opaque canonical person ID beginning with 'person:'.")
    selected_alias_records = tuple(available_aliases[key] for key in sorted(selected_aliases))
    selected_identifier_records = tuple(available_identifiers[key] for key in sorted(selected_identifier_keys))
    source_alias_records = tuple(alias for alias in source.aliases if _alias_key(alias) in retained_aliases)
    source_identifier_records = tuple(identifier for identifier in source.identifiers if _identifier_key(identifier) in retained_identifier_keys)
    new_entity = CanonicalEntity(
        workspace_id=source.workspace_id,
        entity_id=target_id,
        entity_type="person",
        canonical_name=selected_alias_records[0].value,
        aliases=selected_alias_records,
        scope=source.scope,
        created_at=now,
        identifiers=selected_identifier_records,
    )
    updated_source = replace(source, aliases=source_alias_records, identifiers=source_identifier_records)
    source_person = _person_by_entity_id(state.people, source.entity_id)
    new_person = PersonDirectory(
        entity_id=target_id,
        alias=selected_alias_records[0].value,
        status=PersonStatus.UNKNOWN,
        tenant_relationship=TenantRelationship.UNKNOWN,
    )
    updated_state = replace(
        state,
        document=EntitiesDocument(
            schema_version=state.document.schema_version,
            entities=tuple(updated_source if entity.entity_id == source.entity_id else entity for entity in state.document.entities) + (new_entity,),
            redirects=state.document.redirects,
        ),
        people=state.people + (new_person,),
    )
    authored_references = _find_authored_references(programs_root, source.entity_id)
    conflicts = tuple(
        CorrectionConflict(
            "ambiguous_authored_reference",
            f"Authored reference {reference.field_path} remains bound to {source.entity_id}; steward must decide whether it belongs to {target_id}.",
            reference.source_path,
        )
        for reference in authored_references
    )
    result = PeopleCorrectionResult("split", source.entity_id, target_id, (_ENTITIES_PATH, _PEOPLE_PATH), conflicts, authored_references)
    _validate_state(updated_state)
    if not apply:
        return result
    config, manifest = _require_steward(knowledge_root, actor)
    return _commit_state(
        knowledge_root=knowledge_root,
        config=config,
        manifest=manifest,
        state=updated_state,
        paths=result.affected_paths,
        result=result,
        actor=actor,
        reason=reason,
        before={"source_person_entity_id": source_person.entity_id, "aliases": list(selected_aliases), "identifiers": list(selected_identifier_keys)},
        after={"new_person_entity_id": target_id},
        as_of=now,
    )


def _find_merge_snapshot(knowledge_root: Path, source_id: str) -> dict[str, Any]:
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    snapshots = [
        record
        for record in records
        if record.get("operation") == "merge"
        and record.get("entity_id") == source_id
        and record.get("field") == "identity_correction"
        and isinstance(record.get("before"), dict)
    ]
    if not snapshots:
        raise ConfigError(f"No auditable merge snapshot exists for {source_id!r}; refusing an unsafe unmerge.")
    return snapshots[-1]


def _write_checkpoint_documents(
    checkpoint_dir: Path,
    paths: tuple[str, ...],
    *,
    staged_dir: Path,
) -> None:
    for relative_path in paths:
        source = checkpoint_dir / relative_path
        if not source.is_file():
            raise ConfigError(f"Merge checkpoint is missing {relative_path!r}; refusing an unsafe unmerge.")
        destination = staged_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def unmerge_people(
    knowledge_root: Path,
    *,
    source_ref: str,
    reason: str,
    actor: str,
    apply: bool,
    as_of: datetime | None = None,
) -> PeopleCorrectionResult:
    if not reason.strip():
        raise ConfigError("A non-empty steward review reason is required for an unmerge.")
    state = _load_state(knowledge_root)
    source_id = source_ref.strip()
    source = next((entity for entity in state.document.entities if entity.entity_id == source_id), None)
    redirect = next((item for item in state.document.redirects if item.from_entity_id == source_id), None)
    if source is None or source.status is not EntityStatus.TOMBSTONED or redirect is None:
        raise ConfigError("--from must name a tombstoned source entity with a known merge redirect.")
    snapshot_record = _find_merge_snapshot(knowledge_root, source_id)
    before = snapshot_record["before"]
    restorable_paths = before.get("restorable_paths") if isinstance(before, dict) else None
    if (
        not isinstance(restorable_paths, list)
        or not restorable_paths
        or any(not isinstance(path, str) or not path for path in restorable_paths)
    ):
        raise ConfigError("The known merge lacks complete restorable-path metadata; refusing an unsafe unmerge.")
    target_id = redirect.to_entity_id
    paths = tuple(sorted(restorable_paths))
    merge_transaction_id = snapshot_record.get("transaction_id")
    if not isinstance(merge_transaction_id, str) or not merge_transaction_id:
        raise ConfigError("The known merge lacks a transaction ID; refusing an unsafe unmerge.")
    checkpoint_dir = transactions_root(knowledge_root) / merge_transaction_id / "checkpoint"
    if not checkpoint_dir.is_dir():
        raise ConfigError("The known merge checkpoint is unavailable; refusing an unsafe unmerge.")
    result = PeopleCorrectionResult("unmerge", source_id, target_id, paths)
    manifest = load_registry_manifest(knowledge_root)
    if manifest is None:
        raise ConfigError("The registry manifest is missing; refusing an unsafe unmerge.")
    if manifest.generation_id != snapshot_record.get("generation_id"):
        raise ConfigError("The merge has downstream committed generations; explicit forward repartition is required before unmerge.")
    after = snapshot_record.get("after")
    hashes = after.get("source_hashes") if isinstance(after, dict) else None
    if not isinstance(hashes, dict):
        raise ConfigError("The known merge has no committed source hashes; refusing an unsafe unmerge.")
    for relative_path, expected_hash in hashes.items():
        path = knowledge_root / str(relative_path)
        if not path.is_file() or compute_file_checksum(path) != expected_hash:
            raise ConfigError(f"Mutable file {relative_path!r} changed after merge; refusing an unsafe unmerge.")
    for relative_path in paths:
        if not (checkpoint_dir / relative_path).is_file():
            raise ConfigError(f"Merge checkpoint is missing {relative_path!r}; refusing an unsafe unmerge.")
    if not apply:
        return result
    config, manifest = _require_steward(knowledge_root, actor)
    if manifest.generation_id != snapshot_record.get("generation_id"):
        raise ConfigError("The merge has downstream committed generations; explicit forward repartition is required before unmerge.")

    def write_staged_files(staged_dir: Path) -> None:
        require_adopted_registry(knowledge_root, consumer="People unmerge")
        _write_checkpoint_documents(checkpoint_dir, paths, staged_dir=staged_dir)

    def validate_staged_files(staged_dir: Path) -> None:
        document = load_entities_document(staged_dir / _ENTITIES_PATH)
        people = load_people_directory(staged_dir / _PEOPLE_PATH)
        if document is None or people is None:
            raise ConfigError("Unmerge snapshot is missing required typed people documents.")
        _validate_state(
            _RegistryState(
                document=document,
                people=people.people,
                memberships=load_memberships(staged_dir / _MEMBERSHIPS_PATH) if _MEMBERSHIPS_PATH in paths else (),
                profiles=None,
                delegations=None,
                caches=(),
            )
        )
        if _PROFILES_PATH in paths:
            load_people_profiles(staged_dir / _PROFILES_PATH)
        for relative_path in paths:
            if relative_path not in {_ENTITIES_PATH, _PEOPLE_PATH, _MEMBERSHIPS_PATH, _PROFILES_PATH}:
                if Path(relative_path).suffix == ".json":
                    _load_json_document(staged_dir / relative_path)
                else:
                    _load_yaml_document(staged_dir / relative_path)

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        paths,
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=_now(as_of),
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_now(as_of))
    applied = replace(result, transaction_id=committed.transaction_id, generation_id=committed.manifest.generation_id)
    _record_correction(
        knowledge_root,
        config=config,
        result=applied,
        actor=actor,
        reason=reason,
        before={"merge_transaction_id": snapshot_record.get("transaction_id"), "merge_generation_id": snapshot_record.get("generation_id")},
        after={"restored_source_entity_id": source_id, "removed_redirect_to": target_id},
        as_of=_now(as_of),
    )
    return applied
