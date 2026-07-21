"""Canonical staged writer for shared people and team factual data."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import copy
import hashlib
from pathlib import Path
import shutil
from typing import Any

import yaml

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import compute_file_checksum
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.ulid import new_ulid
from src.core.people_change_journal import (
    append_people_change_record,
    append_people_conflict_record,
    redact_person_journal_records,
    validate_person_journal_redaction,
)
from src.core.people_material_ledger_events import enqueue_team_membership_changed_events
from src.core.people_directory_schema import (
    ContactKind,
    ContactPoint,
    ContactStatus,
    FieldVerification,
    PersonDirectory,
    PersonStatus,
    Team,
    TeamKind,
    TeamStatus,
    load_people_directory,
    load_teams,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityStatus,
    load_entities_document,
    write_entities_document,
)
from src.core.people_membership_schema import (
    MembershipStatus,
    TeamMembership,
    load_memberships,
    observe_membership,
    write_memberships,
)
from src.core.people_registry_governance import require_adopted_registry
from src.core.people_registry_identity import RegistryConfig, RegistryManifest, load_registry_config, load_registry_manifest
from src.core.people_registry_transaction import (
    commit_registry_files_transaction,
    prepare_registry_files_transaction,
    transactions_root,
)
from src.core.people_shared_migration import build_shared_migration_plan, shared_factual_files_exist
from src.core.profile_encryption import (
    dump_people_profiles_document,
    inspect_people_profiles_file,
    load_people_profiles_document,
    shred_people_profiles_key,
)
from src.core.yaml_utils import load_optional_yaml_mapping


_ENTITIES_PATH = "entities.yaml"
_PEOPLE_PATH = "people_directory.yaml"
_TEAMS_PATH = "teams.yaml"
_MEMBERSHIPS_PATH = "memberships.yaml"
_PROFILES_PATH = "people_profiles.yaml"
_DELEGATIONS_PATH = "delegations.yaml"
_CACHE_DIR = ".cache"


@dataclass(frozen=True, slots=True)
class RegistryPatchOperation:
    """A factual subset of a KB/NCFL mutation."""

    relative_path: str
    action: str
    match_value: str
    fields: tuple[tuple[str, object], ...] = ()
    field_name: str | None = None
    value: object | None = None


@dataclass(frozen=True, slots=True)
class OnboardingPerson:
    alias: str
    email: str
    display_name: str | None
    team_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OnboardingProgramGroup:
    id: str
    name: str
    area_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SharedRegistryWriteResult:
    affected_paths: tuple[str, ...]
    changes: tuple[tuple[str, str, object, object], ...]
    conflicts: tuple[str, ...] = ()
    transaction_id: str | None = None
    generation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SharedRegistryPrivacyForgetResult:
    entity_id: str
    affected_paths: tuple[str, ...]
    memberships_tombstoned: int
    profiles_redacted: int
    delegations_tombstoned: int
    cache_files_removed: int = 0
    transaction_artifacts_redacted: int = 0
    journal_records_redacted: int = 0
    profile_disposition: str = "not_present"
    transaction_id: str | None = None
    generation_id: str | None = None
    journal_event_ids: tuple[str, ...] = ()
    external_backup_action_required: bool = True


@dataclass(frozen=True, slots=True)
class _RegistryState:
    entities: EntitiesDocument
    people: tuple[PersonDirectory, ...]
    teams: tuple[Team, ...]
    memberships: tuple[TeamMembership, ...]


def shared_registry_is_active(programs_root: Path) -> bool:
    """Whether a committed shared factual root owns people/team writes."""

    knowledge_root = get_shared_knowledge_root(programs_root)
    return (
        load_registry_config(knowledge_root) is not None
        and load_registry_manifest(knowledge_root) is not None
        and shared_factual_files_exist(programs_root)
    )


def shared_registry_can_register(programs_root: Path) -> bool:
    """Whether an explicit onboarding registration can create the first root."""

    knowledge_root = get_shared_knowledge_root(programs_root)
    return load_registry_config(knowledge_root) is not None and load_registry_manifest(knowledge_root) is not None


def _now(as_of: datetime | None) -> datetime:
    return (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _require_registry(knowledge_root: Path, *, consumer: str) -> tuple[RegistryConfig, RegistryManifest]:
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError(
            f"{consumer} requires a bootstrapped shared registry. Run "
            "'vertex kb registry bootstrap --apply --customer-boundary-id <id>' first."
        )
    require_adopted_registry(knowledge_root, consumer=consumer)
    return config, manifest


def _load_state(knowledge_root: Path, *, workspace_id: str) -> _RegistryState:
    entities_document = load_entities_document(knowledge_root / _ENTITIES_PATH)
    people_result = load_people_directory(knowledge_root / _PEOPLE_PATH)
    teams_result = load_teams(knowledge_root / _TEAMS_PATH)
    return _RegistryState(
        entities=entities_document
        or EntitiesDocument(schema_version="2.0", entities=()),
        people=people_result.people if people_result is not None else (),
        teams=teams_result.teams if teams_result is not None else (),
        memberships=load_memberships(knowledge_root / _MEMBERSHIPS_PATH),
    )


def _validate_state(state: _RegistryState) -> None:
    active_people = {
        entity.entity_id
        for entity in state.entities.entities
        if entity.entity_type == "person" and entity.status == EntityStatus.ACTIVE
    }
    active_teams = {
        entity.entity_id
        for entity in state.entities.entities
        if entity.entity_type == "team" and entity.status == EntityStatus.ACTIVE
    }
    if len({entity.entity_id for entity in state.entities.entities}) != len(state.entities.entities):
        raise ConfigError("entities.yaml must contain at most one record per canonical entity.")
    if len({person.entity_id for person in state.people}) != len(state.people):
        raise ConfigError("people_directory.yaml must contain at most one record per canonical entity.")
    if len({team.entity_id for team in state.teams}) != len(state.teams):
        raise ConfigError("teams.yaml must contain at most one record per canonical entity.")
    if any(person.entity_id not in active_people for person in state.people):
        raise ConfigError("People-directory records must reference active canonical people.")
    if any(team.entity_id not in active_teams for team in state.teams):
        raise ConfigError("Team records must reference active canonical teams.")
    known_people = active_people | {
        entity.entity_id
        for entity in state.entities.entities
        if entity.entity_type == "person" and entity.status == EntityStatus.TOMBSTONED
    }
    for membership in state.memberships:
        if membership.team_entity_id not in active_teams:
            raise ConfigError("Memberships must reference active canonical teams.")
        if membership.status is MembershipStatus.ACTIVE and membership.person_entity_id not in active_people:
            raise ConfigError("Active memberships must reference active canonical people.")
        if membership.status is not MembershipStatus.ACTIVE and membership.person_entity_id not in known_people:
            raise ConfigError("Historical memberships must reference a known canonical person.")


def _changed_paths(before: _RegistryState, after: _RegistryState) -> tuple[str, ...]:
    paths: list[str] = []
    if before.entities != after.entities:
        paths.append(_ENTITIES_PATH)
    if before.people != after.people:
        paths.append(_PEOPLE_PATH)
    if before.teams != after.teams:
        paths.append(_TEAMS_PATH)
    if before.memberships != after.memberships:
        paths.append(_MEMBERSHIPS_PATH)
    return tuple(paths)


def _write_state_to_staging(state: _RegistryState, staged_dir: Path, paths: tuple[str, ...]) -> None:
    if _ENTITIES_PATH in paths:
        write_entities_document(staged_dir / _ENTITIES_PATH, state.entities)
    if _PEOPLE_PATH in paths:
        write_people_directory(staged_dir / _PEOPLE_PATH, state.people)
    if _TEAMS_PATH in paths:
        write_teams(staged_dir / _TEAMS_PATH, state.teams)
    if _MEMBERSHIPS_PATH in paths:
        write_memberships(staged_dir / _MEMBERSHIPS_PATH, state.memberships)


def _validate_staged_state(state: _RegistryState, staged_dir: Path, paths: tuple[str, ...]) -> None:
    if _ENTITIES_PATH in paths and load_entities_document(staged_dir / _ENTITIES_PATH) != state.entities:
        raise ConfigError("Staged entities.yaml did not round-trip through the production loader.")
    if _PEOPLE_PATH in paths:
        people = load_people_directory(staged_dir / _PEOPLE_PATH)
        if people is None or people.people != tuple(sorted(state.people, key=lambda item: item.entity_id or item.alias)):
            raise ConfigError("Staged people_directory.yaml did not round-trip through the production loader.")
    if _TEAMS_PATH in paths:
        teams = load_teams(staged_dir / _TEAMS_PATH)
        if teams is None or teams.teams != tuple(sorted(state.teams, key=lambda item: item.entity_id or item.id)):
            raise ConfigError("Staged teams.yaml did not round-trip through the production loader.")
    if _MEMBERSHIPS_PATH in paths:
        memberships = load_memberships(staged_dir / _MEMBERSHIPS_PATH)
        if memberships != tuple(sorted(state.memberships, key=lambda item: item.membership_id)):
            raise ConfigError("Staged memberships.yaml did not round-trip through the production loader.")


def _commit(
    *,
    knowledge_root: Path,
    config: RegistryConfig,
    manifest: RegistryManifest,
    consumer: str,
    actor: str,
    source: str,
    source_ref: str | None,
    reason: str,
    preview: SharedRegistryWriteResult,
    build_current,
    apply: bool,
    as_of: datetime,
) -> SharedRegistryWriteResult:
    if not actor.strip():
        raise ConfigError(f"{consumer} requires an authenticated operator principal.")
    if not reason.strip():
        raise ConfigError(f"{consumer} requires a non-empty reason.")
    if not apply or not preview.affected_paths:
        return preview

    committed_result: SharedRegistryWriteResult | None = None

    def write_staged_files(staged_dir: Path) -> None:
        nonlocal committed_result
        require_adopted_registry(knowledge_root, consumer=consumer)
        current_state = _load_state(knowledge_root, workspace_id=config.workspace_id)
        current, after = _state_for_result(build_current, current_state)
        if current.affected_paths != preview.affected_paths:
            raise ConfigError(
                f"{consumer} inputs changed while waiting for the registry lease; re-run the preview."
            )
        _write_state_to_staging(after, staged_dir, current.affected_paths)
        committed_result = current

    def validate_staged_files(staged_dir: Path) -> None:
        if committed_result is None:
            raise ConfigError("Registry writer did not build staged factual state.")
        current_state = _load_state(knowledge_root, workspace_id=config.workspace_id)
        _, after = _state_for_result(build_current, current_state)
        _validate_staged_state(after, staged_dir, committed_result.affected_paths)

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        preview.affected_paths,
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=as_of,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=as_of)
    if committed_result is None:
        raise ConfigError("Registry writer committed without a staged factual result.")
    result = replace(
        committed_result,
        transaction_id=committed.transaction_id,
        generation_id=committed.manifest.generation_id,
    )
    for entity_id, field, before, after in result.changes:
        append_people_change_record(
            knowledge_root,
            workspace_id=config.workspace_id,
            transaction_id=committed.transaction_id,
            generation_id=committed.manifest.generation_id,
            authenticated_principal=actor,
            operation="create" if before is None else "update",
            entity_id=entity_id,
            field=field,
            before=before,
            after=after,
            source=source,
            source_ref=source_ref,
            reason=reason,
            as_of=as_of,
        )
    for index, conflict in enumerate(result.conflicts):
        append_people_conflict_record(
            knowledge_root,
            workspace_id=config.workspace_id,
            conflict_id=f"{committed.transaction_id}-conflict-{index}",
            decision="quarantined",
            authenticated_principal=actor,
            reason=conflict,
            as_of=as_of,
        )
    # PPL-W6.1: `field` values of the form "memberships[<id>].<subfield>"
    # are the only `result.changes` entries this writer ever produces for
    # a membership add/remove/status change (see `_set_memberships`) --
    # matching that prefix, not a fixed field-name allowlist, is what
    # keeps this check correct if that prefix's subfields change shape.
    membership_changed_entity_ids = tuple(
        dict.fromkeys(entity_id for entity_id, field, _, _ in result.changes if field.startswith("memberships["))
    )
    if membership_changed_entity_ids:
        enqueue_team_membership_changed_events(
            knowledge_root, transaction_id=committed.transaction_id, person_entity_ids=membership_changed_entity_ids,
        )
    return result


def _state_for_result(build_current, state: _RegistryState) -> tuple[SharedRegistryWriteResult, _RegistryState]:
    result, after = build_current(state)
    _validate_state(after)
    return replace(result, affected_paths=_changed_paths(state, after)), after


def _field_changes(
    *,
    entity_id: str,
    before: object | None,
    after: object,
    prefix: str,
) -> tuple[tuple[str, str, object, object], ...]:
    before_values = {} if before is None else asdict(before)
    after_values = asdict(after)
    return tuple(
        (entity_id, f"{prefix}.{field}", before_values.get(field), after_values.get(field))
        for field in sorted(set(before_values) | set(after_values))
        if before_values.get(field) != after_values.get(field)
    )


def _onboarding_state(
    state: _RegistryState,
    *,
    config: RegistryConfig,
    program_id: str,
    people: tuple[OnboardingPerson, ...],
    groups: tuple[OnboardingProgramGroup, ...],
    person_entity_ids: dict[str, str],
    team_entity_ids: dict[str, str],
    actor: str,
    source_ref: str,
    now: datetime,
) -> tuple[SharedRegistryWriteResult, _RegistryState]:
    entities: list[CanonicalEntity] = []
    person_records: list[PersonDirectory] = []
    team_records: list[Team] = []
    person_ids: dict[str, str] = {}
    team_ids: dict[str, str] = {}

    for person in people:
        alias = person.alias.strip()
        if not alias or not person.email.strip():
            raise ConfigError("Onboarding shared registration requires each person to have an alias and email.")
        entity_id = person_entity_ids[alias.casefold()]
        person_ids[alias.casefold()] = entity_id
        entities.append(
            CanonicalEntity(
                workspace_id=config.workspace_id,
                entity_id=entity_id,
                entity_type="person",
                canonical_name=person.display_name or alias,
                aliases=(
                    EntityAlias(
                        value=alias,
                        kind="vertex::alias",
                        status=AliasStatus.ACTIVE,
                        valid_from=None,
                        valid_until=None,
                        source="onboarding_operator",
                        source_ref=source_ref,
                        recorded_at=now,
                        verified_at=now,
                        verified_by_principal=actor,
                    ),
                ),
                scope="org",
                created_at=now,
            )
        )
        person_records.append(
            PersonDirectory(
                entity_id=entity_id,
                alias=alias,
                contacts=(
                    ContactPoint(
                        kind=ContactKind.PRIMARY_EMAIL,
                        value=person.email.strip(),
                        status=ContactStatus.ACTIVE,
                        valid_from=None,
                        valid_until=None,
                        source="onboarding_operator",
                        source_ref=source_ref,
                        recorded_at=now,
                        verified_at=now,
                        verified_by_principal=actor,
                        delivery_eligible=True,
                    ),
                ),
                display_name=person.display_name,
                status=PersonStatus.UNKNOWN,
            )
        )
    for group in groups:
        group_id = group.id.strip()
        if not group_id:
            raise ConfigError("Onboarding shared registration requires each workstream to have an id.")
        entity_id = team_entity_ids[group_id]
        team_ids[group_id] = entity_id
        entities.append(
            CanonicalEntity(
                workspace_id=config.workspace_id,
                entity_id=entity_id,
                entity_type="team",
                canonical_name=group.name,
                aliases=(
                    EntityAlias(
                        value=group_id,
                        kind="vertex::alias",
                        status=AliasStatus.ACTIVE,
                        valid_from=None,
                        valid_until=None,
                        source="onboarding_operator",
                        source_ref=source_ref,
                        recorded_at=now,
                        verified_at=now,
                        verified_by_principal=actor,
                    ),
                ),
                scope="org",
                created_at=now,
            )
        )
        team_records.append(
            Team(
                entity_id=entity_id,
                id=group_id,
                name=group.name,
                kind=TeamKind.PROGRAM_GROUP,
                status=TeamStatus.ACTIVE,
                area_paths=group.area_paths,
                legacy_programs=(program_id,),
            )
        )

    plan = build_shared_migration_plan(
        program_id=program_id,
        existing_entities=state.entities.entities,
        incoming_entities=tuple(entities),
        entity_redirects=state.entities.redirects,
        existing_people=state.people,
        incoming_people=tuple(person_records),
        existing_teams=state.teams,
        incoming_teams=tuple(team_records),
    )
    final_entity_ids = {entity.entity_id for entity in plan.entities_to_write}
    memberships = state.memberships
    membership_changes: list[tuple[str, str, object, object]] = []
    for person in people:
        person_id = person_ids[person.alias.casefold()]
        if person_id not in final_entity_ids:
            continue
        for group_id in person.team_ids:
            team_id = team_ids.get(group_id)
            if team_id is None or team_id not in final_entity_ids:
                continue
            memberships, membership = observe_membership(
                memberships,
                provider="onboarding_operator",
                tenant_id=None,
                person_entity_id=person_id,
                team_entity_id=team_id,
                raw_role="member",
                valid_from=None,
                valid_until=None,
                source_ref=source_ref,
                observed_at=now,
                verified_at=now,
            )
            membership_changes.extend(_field_changes(
                entity_id=person_id,
                before=None,
                after=membership,
                prefix=f"memberships[{membership.membership_id}]",
            ))

    after = _RegistryState(
        entities=EntitiesDocument(
            schema_version="2.0",
            entities=tuple(sorted(plan.entities_to_write, key=lambda item: item.entity_id)),
            redirects=plan.entity_redirects,
        ),
        people=plan.people_to_write,
        teams=plan.teams_to_write,
        memberships=memberships,
    )
    changes: list[tuple[str, str, object, object]] = []
    previous_entities = {entity.entity_id: entity for entity in state.entities.entities}
    previous_people = {person.entity_id: person for person in state.people}
    previous_teams = {team.entity_id: team for team in state.teams}
    for entity in after.entities.entities:
        changes.extend(_field_changes(entity_id=entity.entity_id, before=previous_entities.get(entity.entity_id), after=entity, prefix="entity"))
    for person in after.people:
        if previous_people.get(person.entity_id) != person:
            changes.extend(_field_changes(entity_id=person.entity_id, before=previous_people.get(person.entity_id), after=person, prefix="person"))
    for team in after.teams:
        if previous_teams.get(team.entity_id) != team:
            changes.extend(_field_changes(entity_id=team.entity_id, before=previous_teams.get(team.entity_id), after=team, prefix="team"))
    conflicts = tuple(conflict.detail for conflict in plan.conflicts)
    return SharedRegistryWriteResult(affected_paths=(), changes=tuple(changes) + tuple(membership_changes), conflicts=conflicts), after


def register_onboarding_facts(
    *,
    program_id: str,
    people: tuple[OnboardingPerson, ...],
    groups: tuple[OnboardingProgramGroup, ...],
    programs_root: Path,
    actor: str,
    reason: str,
    source_ref: str,
    apply: bool,
    as_of: datetime | None = None,
) -> SharedRegistryWriteResult:
    """Register onboarding people/workstreams without creating local shadows."""

    now = _now(as_of)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config, manifest = _require_registry(knowledge_root, consumer="Onboarding shared registration")
    person_entity_ids = {
        person.alias.strip().casefold(): f"person:{new_ulid(now)}"
        for person in people
    }
    team_entity_ids = {
        group.id.strip(): f"team:{new_ulid(now)}"
        for group in groups
    }
    if len(person_entity_ids) != len(people):
        raise ConfigError("Onboarding shared registration cannot create duplicate person aliases.")
    if len(team_entity_ids) != len(groups):
        raise ConfigError("Onboarding shared registration cannot create duplicate workstream ids.")

    def build_current(state: _RegistryState | None = None):
        current = state or _load_state(knowledge_root, workspace_id=config.workspace_id)
        return _onboarding_state(
            current,
            config=config,
            program_id=program_id,
            people=people,
            groups=groups,
            person_entity_ids=person_entity_ids,
            team_entity_ids=team_entity_ids,
            actor=actor,
            source_ref=source_ref,
            now=now,
        )

    preview, _ = _state_for_result(build_current, _load_state(knowledge_root, workspace_id=config.workspace_id))
    return _commit(
        knowledge_root=knowledge_root,
        config=config,
        manifest=manifest,
        consumer="Onboarding shared registration",
        actor=actor,
        source="onboarding_operator",
        source_ref=source_ref,
        reason=reason,
        preview=preview,
        build_current=build_current,
        apply=apply,
        as_of=now,
    )


def _replace_verification(
    person: PersonDirectory,
    *,
    field_name: str,
    source: str,
    source_ref: str | None,
    actor: str,
    now: datetime,
) -> PersonDirectory:
    existing = next((item for item in person.verifications if item.field_name == field_name), None)
    if existing is not None and existing.pinned:
        raise ConfigError(
            f"Cannot change pinned field {field_name!r} for {person.entity_id}; unpin it through "
            "'vertex kb people unpin' before applying the update."
        )
    replacement = FieldVerification(
        field_name=field_name,
        source=source,
        source_ref=source_ref,
        observed_at=now,
        verified_at=now,
        recorded_at=now,
        verified_by_principal=actor,
    )
    return replace(
        person,
        verifications=tuple(
            sorted(
                tuple(item for item in person.verifications if item.field_name != field_name) + (replacement,),
                key=lambda item: item.field_name,
            )
        ),
    )


def _resolve_person(state: _RegistryState, reference: str) -> PersonDirectory:
    matches = [person for person in state.people if person.alias.casefold() == reference.strip().casefold()]
    if len(matches) != 1:
        raise ConfigError(f"Person reference {reference!r} must resolve to exactly one shared canonical person.")
    return matches[0]


def _resolve_team(state: _RegistryState, reference: str) -> Team:
    matches = [team for team in state.teams if team.id.casefold() == reference.strip().casefold()]
    if len(matches) != 1:
        raise ConfigError(f"Team reference {reference!r} must resolve to exactly one shared canonical team.")
    return matches[0]


def _set_person_email(
    person: PersonDirectory,
    *,
    value: object,
    source: str,
    source_ref: str | None,
    actor: str,
    now: datetime,
) -> PersonDirectory:
    email = str(value).strip()
    if not email:
        raise ConfigError("A shared person email cannot be empty.")
    replacement = ContactPoint(
        kind=ContactKind.PRIMARY_EMAIL,
        value=email,
        status=ContactStatus.ACTIVE,
        valid_from=None,
        valid_until=None,
        source=source,
        source_ref=source_ref,
        recorded_at=now,
        verified_at=now,
        verified_by_principal=actor,
        delivery_eligible=True,
    )
    contacts = tuple(contact for contact in person.contacts if contact.kind != ContactKind.PRIMARY_EMAIL) + (replacement,)
    return _replace_verification(
        replace(person, contacts=contacts),
        field_name="contacts",
        source=source,
        source_ref=source_ref,
        actor=actor,
        now=now,
    )


def _set_memberships(
    state: _RegistryState,
    *,
    person: PersonDirectory,
    requested_team_ids: tuple[str, ...],
    source: str,
    source_ref: str | None,
    now: datetime,
) -> tuple[tuple[TeamMembership, ...], tuple[tuple[str, str, object, object], ...]]:
    resolved = {team.id: team for team in (_resolve_team(state, team_id) for team_id in requested_team_ids)}
    requested_entity_ids = {team.entity_id for team in resolved.values()}
    memberships = state.memberships
    changes: list[tuple[str, str, object, object]] = []
    for membership in memberships:
        if (
            membership.person_entity_id == person.entity_id
            and membership.team_entity_id not in requested_entity_ids
            and membership.status == MembershipStatus.ACTIVE
        ):
            replacement = replace(membership, status=MembershipStatus.TOMBSTONED, valid_until=now)
            memberships = tuple(replacement if item.membership_id == membership.membership_id else item for item in memberships)
            changes.extend(_field_changes(
                entity_id=person.entity_id,
                before=membership,
                after=replacement,
                prefix=f"memberships[{membership.membership_id}]",
            ))
    current_active = {
        membership.team_entity_id
        for membership in memberships
        if membership.person_entity_id == person.entity_id and membership.status == MembershipStatus.ACTIVE
    }
    for team in resolved.values():
        if team.entity_id in current_active:
            continue
        memberships, membership = observe_membership(
            memberships,
            provider=source,
            tenant_id=None,
            person_entity_id=person.entity_id,
            team_entity_id=team.entity_id,
            raw_role="member",
            valid_from=None,
            valid_until=None,
            source_ref=source_ref,
            observed_at=now,
            verified_at=now,
        )
        changes.extend(_field_changes(
            entity_id=person.entity_id,
            before=None,
            after=membership,
            prefix=f"memberships[{membership.membership_id}]",
        ))
    return memberships, tuple(changes)


def _patch_state(
    state: _RegistryState,
    *,
    operations: tuple[RegistryPatchOperation, ...],
    source: str,
    source_ref: str | None,
    actor: str,
    now: datetime,
) -> tuple[SharedRegistryWriteResult, _RegistryState]:
    people = state.people
    teams = state.teams
    memberships = state.memberships
    changes: list[tuple[str, str, object, object]] = []
    for operation in operations:
        if operation.relative_path == "knowledge/people_directory.yaml":
            person = _resolve_person(
                _RegistryState(state.entities, people, teams, memberships),
                operation.match_value,
            )
            updated = person
            if operation.action == "set_fields":
                for field_name, value in operation.fields:
                    if field_name == "title":
                        updated = _replace_verification(
                            replace(updated, title=None if value is None else str(value)),
                            field_name="title",
                            source=source,
                            source_ref=source_ref,
                            actor=actor,
                            now=now,
                        )
                    elif field_name == "display_name":
                        updated = _replace_verification(
                            replace(updated, display_name=None if value is None else str(value)),
                            field_name="display_name",
                            source=source,
                            source_ref=source_ref,
                            actor=actor,
                            now=now,
                        )
                    elif field_name == "department":
                        updated = _replace_verification(
                            replace(updated, department=None if value is None else str(value)),
                            field_name="department",
                            source=source,
                            source_ref=source_ref,
                            actor=actor,
                            now=now,
                        )
                    elif field_name == "email":
                        updated = _set_person_email(
                            updated,
                            value=value,
                            source=source,
                            source_ref=source_ref,
                            actor=actor,
                            now=now,
                        )
                    elif field_name == "team_ids":
                        values = tuple(str(item) for item in (value or ()))
                        memberships, membership_changes = _set_memberships(
                            _RegistryState(state.entities, people, teams, memberships),
                            person=updated,
                            requested_team_ids=values,
                            source=source,
                            source_ref=source_ref,
                            now=now,
                        )
                        changes.extend(membership_changes)
                    else:
                        raise ConfigError(f"Unsupported shared person field {field_name!r}.")
            elif operation.action in {"add_list_value", "remove_list_value"} and operation.field_name == "team_ids":
                active_team_ids = tuple(
                    team.id
                    for team in teams
                    if any(
                        membership.person_entity_id == person.entity_id
                        and membership.team_entity_id == team.entity_id
                        and membership.status == MembershipStatus.ACTIVE
                        for membership in memberships
                    )
                )
                requested = list(active_team_ids)
                value = str(operation.value)
                if operation.action == "add_list_value" and value not in requested:
                    requested.append(value)
                if operation.action == "remove_list_value":
                    requested = [item for item in requested if item != value]
                memberships, membership_changes = _set_memberships(
                    _RegistryState(state.entities, people, teams, memberships),
                    person=updated,
                    requested_team_ids=tuple(requested),
                    source=source,
                    source_ref=source_ref,
                    now=now,
                )
                changes.extend(membership_changes)
            else:
                raise ConfigError(
                    "Shared registry KB updates support person title, display_name, department, email, and team_ids changes; "
                    "use a steward correction for destructive identity changes."
                )
            if updated != person:
                people = tuple(updated if item.entity_id == person.entity_id else item for item in people)
                changes.extend(_field_changes(entity_id=person.entity_id, before=person, after=updated, prefix="person"))
            continue

        if operation.relative_path == "knowledge/teams.yaml":
            if operation.action != "set_fields":
                raise ConfigError("Shared team updates support only set_fields.")
            team = _resolve_team(_RegistryState(state.entities, people, teams, memberships), operation.match_value)
            updated_team = team
            for field_name, value in operation.fields:
                if field_name == "name":
                    updated_team = replace(updated_team, name=str(value))
                elif field_name == "area_paths":
                    if not isinstance(value, (list, tuple)):
                        raise ConfigError("Shared team area_paths must be a list.")
                    updated_team = replace(updated_team, area_paths=tuple(str(item) for item in value))
                elif field_name == "status":
                    updated_team = replace(updated_team, status=TeamStatus(str(value)))
                elif field_name == "kind":
                    kind = TeamKind(str(value))
                    if kind == TeamKind.ORG_TEAM:
                        raise ConfigError(
                            "KB updates cannot classify a team as org_team without explicit provider or steward confirmation."
                        )
                    updated_team = replace(updated_team, kind=kind)
                else:
                    raise ConfigError(f"Unsupported shared team field {field_name!r}.")
            if updated_team != team:
                teams = tuple(updated_team if item.entity_id == team.entity_id else item for item in teams)
                changes.extend(_field_changes(entity_id=team.entity_id, before=team, after=updated_team, prefix="team"))
            continue
        raise ConfigError(f"Unsupported shared factual KB path {operation.relative_path!r}.")
    return SharedRegistryWriteResult(affected_paths=(), changes=tuple(changes)), _RegistryState(
        entities=state.entities,
        people=people,
        teams=teams,
        memberships=memberships,
    )


def apply_shared_registry_patch(
    *,
    operations: tuple[RegistryPatchOperation, ...],
    programs_root: Path,
    actor: str,
    reason: str,
    source: str,
    source_ref: str | None = None,
    apply: bool,
    as_of: datetime | None = None,
) -> SharedRegistryWriteResult:
    """Preview or commit a factual KB/NCFL patch through the staged writer."""

    now = _now(as_of)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config, manifest = _require_registry(knowledge_root, consumer="Shared factual update")

    def build_current(state: _RegistryState | None = None):
        return _patch_state(
            state or _load_state(knowledge_root, workspace_id=config.workspace_id),
            operations=operations,
            source=source,
            source_ref=source_ref,
            actor=actor,
            now=now,
        )

    preview, _ = _state_for_result(build_current, _load_state(knowledge_root, workspace_id=config.workspace_id))
    return _commit(
        knowledge_root=knowledge_root,
        config=config,
        manifest=manifest,
        consumer="Shared factual update",
        actor=actor,
        source=source,
        source_ref=source_ref,
        reason=reason,
        preview=preview,
        build_current=build_current,
        apply=apply,
        as_of=now,
    )


def read_shared_registry_factual_value(
    *,
    programs_root: Path,
    target_store: str,
    target_key: str,
    target_field: str,
) -> str | None:
    """Read a factual field for NCFL's optimistic-concurrency check."""

    if not shared_registry_is_active(programs_root):
        return None
    knowledge_root = get_shared_knowledge_root(programs_root)
    config = load_registry_config(knowledge_root)
    if config is None:
        return None
    state = _load_state(knowledge_root, workspace_id=config.workspace_id)
    if target_store == "people_directory":
        person = _resolve_person(state, target_key)
        if target_field == "email":
            contact = next((item for item in person.contacts if item.kind == ContactKind.PRIMARY_EMAIL), None)
            return None if contact is None else contact.value
        if target_field in {"title", "display_name"}:
            value = getattr(person, target_field)
            return "" if value is None else str(value)
    if target_store == "teams":
        team = _resolve_team(state, target_key)
        if target_field in {"name", "status", "kind"}:
            value = getattr(team, target_field)
            return str(getattr(value, "value", value))
    raise ConfigError(f"Unsupported shared factual NCFL field {target_store}.{target_field}.")


@dataclass(frozen=True, slots=True)
class _PrivacyForgetPlan:
    state: _RegistryState
    entity_id: str
    profiles: dict[str, Any] | None
    delegations: dict[str, Any] | None
    affected_paths: tuple[str, ...]
    memberships_tombstoned: int
    profiles_redacted: int
    delegations_tombstoned: int
    profile_disposition: str
    profile_key_id_to_shred: str | None


def _write_yaml_mapping(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False, default_flow_style=False), encoding="utf-8")


def _resolve_active_person_entity(state: _RegistryState, reference: str) -> tuple[CanonicalEntity, PersonDirectory]:
    candidate = reference.strip()
    if not candidate:
        raise ConfigError("--person must be a non-empty canonical person ID or uniquely resolving alias.")
    direct_matches = [entity for entity in state.entities.entities if entity.entity_id == candidate]
    alias_matches = [
        entity
        for entity in state.entities.entities
        if any(alias.value.casefold() == candidate.casefold() for alias in entity.aliases)
    ]
    matches = direct_matches or alias_matches
    people = [entity for entity in matches if entity.entity_type == "person"]
    if len(people) != 1:
        raise ConfigError(f"Person reference {reference!r} must resolve to exactly one canonical person entity.")
    entity = people[0]
    if entity.status is not EntityStatus.ACTIVE:
        raise ConfigError(f"Person {entity.entity_id!r} is already tombstoned and cannot be forgotten again.")
    records = [person for person in state.people if person.entity_id == entity.entity_id]
    if len(records) != 1:
        raise ConfigError(f"Canonical person {entity.entity_id!r} must have exactly one active people-directory record.")
    return entity, records[0]


def _tombstone_label(entity_id: str) -> str:
    digest = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:16]
    return f"erased-person-{digest}"


def _profile_matches_person(profile: object, *, entity_id: str, aliases: frozenset[str]) -> bool:
    if not isinstance(profile, dict):
        return False
    if profile.get("entity_id") == entity_id:
        return True
    alias = profile.get("alias")
    return isinstance(alias, str) and alias.casefold() in aliases


def _redact_profiles(
    document: dict[str, Any],
    *,
    entity_id: str,
    aliases: frozenset[str],
) -> tuple[dict[str, Any], int]:
    profiles = document.get("profiles") or []
    if not isinstance(profiles, list):
        raise ConfigError("people_profiles.yaml 'profiles' must be a list.")
    removed = sum(
        _profile_matches_person(profile, entity_id=entity_id, aliases=aliases)
        for profile in profiles
    )
    if not removed:
        return document, 0
    redacted = copy.deepcopy(document)
    redacted["profiles"] = [
        profile
        for profile in profiles
        if not _profile_matches_person(profile, entity_id=entity_id, aliases=aliases)
    ]
    return redacted, removed


_DELEGATION_PERSON_REFERENCE_FIELDS = frozenset(
    {
        "from_person_entity_id",
        "to_person_entity_id",
        "person_entity_id",
        "delegate_entity_id",
        "owner_entity_id",
    }
)


def _delegation_references_person(record: object, *, entity_id: str) -> bool:
    return isinstance(record, dict) and any(
        record.get(field_name) == entity_id
        for field_name in _DELEGATION_PERSON_REFERENCE_FIELDS
    )


def _tombstone_delegations(
    document: dict[str, Any],
    *,
    entity_id: str,
    now: datetime,
) -> tuple[dict[str, Any], int]:
    delegations = document.get("delegations") or []
    if not isinstance(delegations, list):
        raise ConfigError("delegations.yaml 'delegations' must be a list.")
    changed = 0
    redacted_delegations: list[object] = []
    for delegation in delegations:
        if not _delegation_references_person(delegation, entity_id=entity_id):
            redacted_delegations.append(delegation)
            continue
        assert isinstance(delegation, dict)
        replacement = copy.deepcopy(delegation)
        replacement["status"] = "tombstoned"
        replacement["valid_until"] = now.isoformat()
        replacement["reason"] = "[REDACTED]"
        replacement["actor_principal"] = "[REDACTED]"
        redacted_delegations.append(replacement)
        changed += 1
    if not changed:
        return document, 0
    redacted = copy.deepcopy(document)
    redacted["delegations"] = redacted_delegations
    return redacted, changed


def _build_privacy_forget_plan(
    knowledge_root: Path,
    *,
    workspace_id: str,
    person_ref: str,
    now: datetime,
) -> _PrivacyForgetPlan:
    state = _load_state(knowledge_root, workspace_id=workspace_id)
    entity, person = _resolve_active_person_entity(state, person_ref)
    aliases = frozenset(
        alias.value.casefold()
        for alias in entity.aliases
    ) | frozenset({person.alias.casefold()})
    tombstoned_entity = replace(
        entity,
        canonical_name=_tombstone_label(entity.entity_id),
        aliases=(),
        identifiers=(),
        status=EntityStatus.TOMBSTONED,
        tombstoned_at=now,
    )
    redacted_memberships: list[TeamMembership] = []
    memberships_tombstoned = 0
    for membership in state.memberships:
        if membership.person_entity_id != entity.entity_id:
            redacted_memberships.append(membership)
            continue
        redacted_memberships.append(
            replace(
                membership,
                status=MembershipStatus.TOMBSTONED,
                valid_until=now,
                source_ref=None,
            )
        )
        memberships_tombstoned += int(membership.status is not MembershipStatus.TOMBSTONED)
    redacted_state = _RegistryState(
        entities=EntitiesDocument(
            schema_version=state.entities.schema_version,
            entities=tuple(
                tombstoned_entity if item.entity_id == entity.entity_id else item
                for item in state.entities.entities
            ),
            redirects=state.entities.redirects,
        ),
        people=tuple(person_record for person_record in state.people if person_record.entity_id != entity.entity_id),
        teams=state.teams,
        memberships=tuple(redacted_memberships),
    )

    profiles_path = knowledge_root / _PROFILES_PATH
    profiles: dict[str, Any] | None = None
    profiles_redacted = 0
    profile_disposition = "not_present"
    profile_key_id_to_shred: str | None = None
    if profiles_path.exists():
        original_profiles = load_people_profiles_document(profiles_path)
        profiles, profiles_redacted = _redact_profiles(
            original_profiles,
            entity_id=entity.entity_id,
            aliases=aliases,
        )
        if profiles_redacted:
            profile_status = inspect_people_profiles_file(profiles_path)
            profile_disposition = "redacted"
            remaining_profiles = profiles.get("profiles") or []
            if profile_status.storage == "encrypted" and not remaining_profiles:
                if profile_status.key_id is None:
                    raise ConfigError("Encrypted people_profiles.yaml has no key ID; refusing cryptographic shredding.")
                profile_disposition = "cryptographic_shred"
                profile_key_id_to_shred = profile_status.key_id

    delegations_path = knowledge_root / _DELEGATIONS_PATH
    delegations: dict[str, Any] | None = None
    delegations_tombstoned = 0
    if delegations_path.exists():
        original_delegations = load_optional_yaml_mapping(delegations_path)
        assert original_delegations is not None
        delegations, delegations_tombstoned = _tombstone_delegations(
            original_delegations,
            entity_id=entity.entity_id,
            now=now,
        )

    paths = [_ENTITIES_PATH, _PEOPLE_PATH]
    if memberships_tombstoned:
        paths.append(_MEMBERSHIPS_PATH)
    if profiles_redacted:
        paths.append(_PROFILES_PATH)
    if delegations_tombstoned:
        paths.append(_DELEGATIONS_PATH)
    return _PrivacyForgetPlan(
        state=redacted_state,
        entity_id=entity.entity_id,
        profiles=profiles,
        delegations=delegations,
        affected_paths=tuple(sorted(paths)),
        memberships_tombstoned=memberships_tombstoned,
        profiles_redacted=profiles_redacted,
        delegations_tombstoned=delegations_tombstoned,
        profile_disposition=profile_disposition,
        profile_key_id_to_shred=profile_key_id_to_shred,
    )


def _write_privacy_forget_plan(
    plan: _PrivacyForgetPlan,
    *,
    knowledge_root: Path,
    staged_dir: Path,
) -> None:
    _write_state_to_staging(plan.state, staged_dir, plan.affected_paths)
    if _PROFILES_PATH in plan.affected_paths:
        assert plan.profiles is not None
        profiles_path = staged_dir / _PROFILES_PATH
        if plan.profile_key_id_to_shred is not None:
            _write_yaml_mapping(
                profiles_path,
                {
                    "schema_version": str(plan.profiles.get("schema_version") or "2.0"),
                    "profiles": [],
                },
            )
        else:
            profiles_path.parent.mkdir(parents=True, exist_ok=True)
            profiles_path.write_text(
                dump_people_profiles_document(
                    plan.profiles,
                    existing_path=knowledge_root / _PROFILES_PATH,
                ),
                encoding="utf-8",
            )
    if _DELEGATIONS_PATH in plan.affected_paths:
        assert plan.delegations is not None
        _write_yaml_mapping(staged_dir / _DELEGATIONS_PATH, plan.delegations)


def _validate_privacy_forget_plan(plan: _PrivacyForgetPlan, *, staged_dir: Path) -> None:
    _validate_state(plan.state)
    _validate_staged_state(plan.state, staged_dir, tuple(
        path for path in plan.affected_paths
        if path in {_ENTITIES_PATH, _PEOPLE_PATH, _TEAMS_PATH, _MEMBERSHIPS_PATH}
    ))
    if _PROFILES_PATH in plan.affected_paths:
        profiles = load_people_profiles_document(staged_dir / _PROFILES_PATH)
        if profiles.get("profiles") is not None and not isinstance(profiles["profiles"], list):
            raise ConfigError("Staged people_profiles.yaml 'profiles' must be a list.")
    if _DELEGATIONS_PATH in plan.affected_paths:
        load_optional_yaml_mapping(staged_dir / _DELEGATIONS_PATH)


def _privacy_transaction_artifact_paths(knowledge_root: Path) -> tuple[Path, ...]:
    root = transactions_root(knowledge_root)
    if not root.exists():
        return ()
    return tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.name in {
                _ENTITIES_PATH,
                _PEOPLE_PATH,
                _MEMBERSHIPS_PATH,
                _PROFILES_PATH,
                _DELEGATIONS_PATH,
            }
        )
    )


def _assert_privacy_artifacts_readable(knowledge_root: Path) -> None:
    for path in _privacy_transaction_artifact_paths(knowledge_root):
        if path.name == _ENTITIES_PATH:
            load_entities_document(path)
        elif path.name == _PEOPLE_PATH:
            load_people_directory(path)
        elif path.name == _MEMBERSHIPS_PATH:
            load_memberships(path)
        elif path.name == _PROFILES_PATH:
            load_people_profiles_document(path)
        else:
            load_optional_yaml_mapping(path)


def _scrub_transaction_artifacts(
    knowledge_root: Path,
    *,
    entity_id: str,
    aliases: frozenset[str],
    now: datetime,
) -> int:
    changed = 0
    for path in _privacy_transaction_artifact_paths(knowledge_root):
        if path.name == _ENTITIES_PATH:
            document = load_entities_document(path)
            if document is None:
                continue
            matching = next((entity for entity in document.entities if entity.entity_id == entity_id), None)
            if matching is None:
                continue
            redacted = EntitiesDocument(
                schema_version=document.schema_version,
                entities=tuple(
                    replace(
                        entity,
                        canonical_name=_tombstone_label(entity_id),
                        aliases=(),
                        identifiers=(),
                        status=EntityStatus.TOMBSTONED,
                        tombstoned_at=now,
                    )
                    if entity.entity_id == entity_id
                    else entity
                    for entity in document.entities
                ),
                redirects=document.redirects,
            )
            write_entities_document(path, redacted)
            changed += 1
        elif path.name == _PEOPLE_PATH:
            directory = load_people_directory(path)
            if directory is None or not any(person.entity_id == entity_id for person in directory.people):
                continue
            write_people_directory(
                path,
                tuple(person for person in directory.people if person.entity_id != entity_id),
            )
            changed += 1
        elif path.name == _MEMBERSHIPS_PATH:
            memberships = load_memberships(path)
            if not any(membership.person_entity_id == entity_id for membership in memberships):
                continue
            write_memberships(
                path,
                tuple(
                    replace(
                        membership,
                        status=MembershipStatus.TOMBSTONED,
                        valid_until=now,
                        source_ref=None,
                    )
                    if membership.person_entity_id == entity_id
                    else membership
                    for membership in memberships
                ),
            )
            changed += 1
        elif path.name == _PROFILES_PATH:
            document = load_people_profiles_document(path)
            redacted, profile_count = _redact_profiles(document, entity_id=entity_id, aliases=aliases)
            if not profile_count:
                continue
            path.write_text(dump_people_profiles_document(redacted, existing_path=path), encoding="utf-8")
            changed += 1
        else:
            document = load_optional_yaml_mapping(path)
            if document is None:
                continue
            redacted, delegation_count = _tombstone_delegations(document, entity_id=entity_id, now=now)
            if not delegation_count:
                continue
            _write_yaml_mapping(path, redacted)
            changed += 1
    return changed


def _clear_registry_caches(knowledge_root: Path) -> int:
    cache_root = knowledge_root / _CACHE_DIR
    if not cache_root.exists():
        return 0
    count = sum(1 for path in cache_root.rglob("*") if path.is_file())
    shutil.rmtree(cache_root)
    return count


def forget_shared_registry_person(
    *,
    programs_root: Path,
    person_ref: str,
    reason: str,
    actor: str,
    on_behalf_of: str | None = None,
    apply: bool,
    as_of: datetime | None = None,
) -> SharedRegistryPrivacyForgetResult:
    """Preview or apply one privacy-authorized person's erasure through the
    same manifest-last, fenced shared-registry transaction used by factual
    writers.  The canonical entity survives only as a non-resolvable,
    pseudonymous tombstone so historical relationships remain valid."""
    if not reason.strip():
        raise ConfigError("A non-empty privacy erasure reason is required.")
    now = _now(as_of)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config, manifest = _require_registry(knowledge_root, consumer="People privacy forget")
    preview_plan = _build_privacy_forget_plan(
        knowledge_root,
        workspace_id=config.workspace_id,
        person_ref=person_ref,
        now=now,
    )
    preview = SharedRegistryPrivacyForgetResult(
        entity_id=preview_plan.entity_id,
        affected_paths=preview_plan.affected_paths,
        memberships_tombstoned=preview_plan.memberships_tombstoned,
        profiles_redacted=preview_plan.profiles_redacted,
        delegations_tombstoned=preview_plan.delegations_tombstoned,
        profile_disposition=preview_plan.profile_disposition,
    )
    if not apply:
        return preview
    if not actor.strip() or actor == "<preview>":
        raise ConfigError("People privacy forget requires an authenticated privacy-authorized principal.")
    if actor not in config.pii_reveal_principals:
        raise ConfigError(
            f"Authenticated principal {actor!r} is not authorized for privacy export/forget. "
            "Add it to registry.yaml pii_reveal_principals through the governed configuration flow."
        )
    _assert_privacy_artifacts_readable(knowledge_root)
    validate_person_journal_redaction(
        knowledge_root,
        workspace_id=config.workspace_id,
        entity_id=preview_plan.entity_id,
    )
    expected_hashes = {
        relative_path: compute_file_checksum(knowledge_root / relative_path)
        if (knowledge_root / relative_path).is_file()
        else None
        for relative_path in preview_plan.affected_paths
    }
    committed_plan: _PrivacyForgetPlan | None = None
    original_state = _load_state(knowledge_root, workspace_id=config.workspace_id)
    original_entity, original_person = _resolve_active_person_entity(original_state, person_ref)
    original_aliases = frozenset(alias.value.casefold() for alias in original_entity.aliases) | frozenset(
        {original_person.alias.casefold()}
    )

    def write_staged_files(staged_dir: Path) -> None:
        nonlocal committed_plan
        require_adopted_registry(knowledge_root, consumer="People privacy forget")
        current_plan = _build_privacy_forget_plan(
            knowledge_root,
            workspace_id=config.workspace_id,
            person_ref=person_ref,
            now=now,
        )
        if current_plan.affected_paths != preview_plan.affected_paths:
            raise ConfigError("Registry privacy-erasure scope changed while waiting for the writer lease; re-run the preview.")
        current_hashes = {
            relative_path: compute_file_checksum(knowledge_root / relative_path)
            if (knowledge_root / relative_path).is_file()
            else None
            for relative_path in current_plan.affected_paths
        }
        if current_hashes != expected_hashes:
            raise ConfigError("Registry privacy-erasure inputs changed while waiting for the writer lease; re-run the preview.")
        _write_privacy_forget_plan(current_plan, knowledge_root=knowledge_root, staged_dir=staged_dir)
        committed_plan = current_plan

    def validate_staged_files(staged_dir: Path) -> None:
        if committed_plan is None:
            raise ConfigError("Privacy erasure did not build a staged registry state.")
        _validate_privacy_forget_plan(committed_plan, staged_dir=staged_dir)

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        preview_plan.affected_paths,
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        expected_generation_id=manifest.generation_id,
        as_of=now,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=now)
    if committed_plan is None:
        raise ConfigError("Privacy erasure committed without a staged registry state.")

    try:
        transaction_artifacts_redacted = _scrub_transaction_artifacts(
            knowledge_root,
            entity_id=committed_plan.entity_id,
            aliases=original_aliases,
            now=now,
        )
        cache_files_removed = _clear_registry_caches(knowledge_root)
        journal_redaction = redact_person_journal_records(
            knowledge_root,
            workspace_id=config.workspace_id,
            entity_id=committed_plan.entity_id,
        )
        if committed_plan.profile_key_id_to_shred is not None:
            shred_people_profiles_key(committed_plan.profile_key_id_to_shred)
    except ConfigError as error:
        append_people_change_record(
            knowledge_root,
            workspace_id=config.workspace_id,
            transaction_id=committed.transaction_id,
            generation_id=committed.manifest.generation_id,
            authenticated_principal=actor,
            on_behalf_of=on_behalf_of,
            operation="privacy_erasure_incomplete",
            entity_id=committed_plan.entity_id,
            field="privacy_erasure",
            before=None,
            after={"completed": False},
            source="privacy",
            reason=str(error),
            as_of=now,
        )
        raise

    audit = append_people_change_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id=committed.transaction_id,
        generation_id=committed.manifest.generation_id,
        authenticated_principal=actor,
        on_behalf_of=on_behalf_of,
        operation="privacy_erasure",
        entity_id=committed_plan.entity_id,
        field="privacy_erasure",
        before=None,
        after={
            "tombstoned": True,
            "memberships_tombstoned": committed_plan.memberships_tombstoned,
            "profiles_redacted": committed_plan.profiles_redacted,
            "delegations_tombstoned": committed_plan.delegations_tombstoned,
            "transaction_artifacts_redacted": transaction_artifacts_redacted,
            "cache_files_removed": cache_files_removed,
            "journal_records_redacted": journal_redaction.redacted_record_count,
            "profile_disposition": committed_plan.profile_disposition,
            "external_backup_action_required": True,
        },
        source="privacy",
        reason=reason,
        as_of=now,
    )
    conflict = append_people_conflict_record(
        knowledge_root,
        workspace_id=config.workspace_id,
        conflict_id=f"{committed.transaction_id}-privacy-erasure",
        decision="privacy_erasure",
        authenticated_principal=actor,
        reason=reason,
        entity_id=committed_plan.entity_id,
        as_of=now,
    )
    return SharedRegistryPrivacyForgetResult(
        entity_id=committed_plan.entity_id,
        affected_paths=committed_plan.affected_paths,
        memberships_tombstoned=committed_plan.memberships_tombstoned,
        profiles_redacted=committed_plan.profiles_redacted,
        delegations_tombstoned=committed_plan.delegations_tombstoned,
        cache_files_removed=cache_files_removed,
        transaction_artifacts_redacted=transaction_artifacts_redacted,
        journal_records_redacted=journal_redaction.redacted_record_count,
        profile_disposition=committed_plan.profile_disposition,
        transaction_id=committed.transaction_id,
        generation_id=committed.manifest.generation_id,
        journal_event_ids=(audit["event_id"], conflict["event_id"]),
    )
