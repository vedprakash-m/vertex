"""specs/people.md Phase 2b, PPL-W2B.1: bootstrap and migrate-shared
preview/apply flows (§6.9).

§6.9's two required flows share one underlying planner:

- `vertex kb registry bootstrap --from-program <id> --apply` "creates the
  first shared root from selected program-local knowledge... previews all
  records, and requires explicit apply." This is exactly `migrate-shared`
  run against an EMPTY existing-shared baseline -- day-0 bootstrap has no
  existing shared records to merge against, so every incoming record is
  either ADDED or QUARANTINED (an incoming-internal alias collision),
  never MERGED or PRESERVED. `bootstrap_shared_factual_files` below is
  therefore a thin day-0 wrapper around `preview_shared_migration`/
  `apply_shared_migration`, not a separate implementation.
- `vertex kb registry migrate-shared <id> --apply` "inventories
  program-local files, detects shadowing/divergence, merges through the
  canonical writer, and produces a conflict report" against a shared root
  that may already hold real data.

§6.9's exact conflict rules, and how each is implemented here:

- "unmatched existing shared records are always preserved" -- any
  existing record whose key has no incoming counterpart is carried into
  the final write set unchanged (`MigrationRecordSummary.preserved`).
- "matching canonical IDs merge field by field" / "pinned/manual fields
  are preserved" -- `_merge_entity`/`_merge_person`/`_merge_team` keep
  every EXISTING scalar/collection field that is already set, only
  filling gaps from the incoming candidate. A pinned `FieldVerification`
  is a strict SUBSET of "already set" (a pinned field is, by definition,
  already set), so this one rule covers both without needing a second,
  finer-grained pin-aware pass. Aliases/identifiers/legacy_programs are
  additive collections -- both sides' values are unioned rather than one
  side replacing the other, since losing a real alias/identifier during a
  merge would silently break future resolution.
- "provider-source priority is explicit" -- not exercised by this item:
  this planner merges LOCAL program-scope YAML into the shared root, not
  competing live-provider observations. Provider-source priority applies
  once a real identity-provider refresh (PPL-W1.9's `provider_refresh_enabled`
  path, still unbuilt) writes through this same shared root; "existing
  wins if already set" degenerates to that exact same rule once one side
  IS a provider observation, so no separate mechanism is needed here.
- "membership observations add/supersede... rather than replacing an
  entire person's team list" -- deliberately OUT of this item's scope.
  §9.1's PPL-W2B.1 row names only `entities.yaml` binding candidates;
  `people_membership_schema.py::observe_membership` (PPL-W2A.3) already
  implements this exact rule and is the natural caller once a dedicated
  memberships-migration pass (turning legacy `team_ids`/`org_chain` WARN
  diagnostics into real `TeamMembership` records) is scoped -- attempting
  it inline here would silently guess `raw_role`/`valid_from` values this
  planner has no real evidence for.
- "destructive removal requires a tombstone or explicit operator
  operation" -- satisfied by construction: this planner never removes an
  existing record just because incoming data omits it (that is exactly
  the "unmatched... always preserved" rule); the only way a record
  disappears from `entities_to_write`/etc. is if it was itself the
  disqualified side of an alias collision, which is reported as a
  conflict, not a silent removal.
- "alias-only collisions are quarantined as conflict candidates until
  bound to stable entities; independent valid entities may commit as an
  explicit partial-success transaction" -- `ConflictCandidate` records
  every such collision (existing-vs-incoming AND incoming-vs-incoming,
  the latter relevant for day-0 bootstrap where there is no existing
  baseline to collide against); the colliding incoming record is excluded
  from the write set while every other, non-colliding incoming record
  still commits in the same apply call -- there is no all-or-nothing gate.

Binding/merge of an already-quarantined conflict candidate to a stable
entity (§6.9: "identity merge/split is always steward-reviewed") is
PPL-W2B.2's `adopt`/`bind` scope, not this item's -- `ConflictCandidate`
is a reportable, journaled fact, not yet an actionable steward workflow.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

from src.core.exceptions import ConfigError
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.people_change_journal import append_people_change_record, append_people_conflict_record
from src.core.people_directory_schema import (
    PersonDirectory,
    Team,
    load_people_directory,
    load_teams,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import (
    ORG_SCOPE_ONLY_ENTITY_TYPES,
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityRedirect,
    EntityStatus,
    ENTITIES_SCHEMA_VERSION,
    is_legacy_schema_0_entities_document,
    load_entities_document,
    preview_entities_migration,
    write_entities_document,
)
from src.core.ledger.ulid import new_ulid
from src.core.people_registry_identity import load_registry_config, load_registry_manifest
from src.core.people_registry_governance import require_adopted_registry
from src.core.people_registry_transaction import (
    commit_registry_files_transaction,
    prepare_registry_files_transaction,
)

@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    kind: str  # "alias_collision"
    record_kind: str  # "entity" | "person" | "team"
    key: str  # the colliding normalized alias/id value
    existing_entity_id: str | None
    incoming_entity_id: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class MigrationRecordSummary:
    preserved: tuple[str, ...]
    merged: tuple[str, ...]
    added: tuple[str, ...]

    @property
    def total_written(self) -> int:
        return len(self.preserved) + len(self.merged) + len(self.added)


@dataclass(frozen=True, slots=True)
class SharedMigrationPlan:
    program_id: str
    entities_to_write: tuple[CanonicalEntity, ...]
    entity_redirects: tuple[EntityRedirect, ...]
    people_to_write: tuple[PersonDirectory, ...]
    teams_to_write: tuple[Team, ...]
    entities_summary: MigrationRecordSummary
    people_summary: MigrationRecordSummary
    teams_summary: MigrationRecordSummary
    conflicts: tuple[ConflictCandidate, ...]
    diagnostics: tuple[str, ...]
    transaction_id: str | None = None
    generation_id: str | None = None

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def partial_success(self) -> bool:
        return self.has_conflicts and any(
            summary.merged or summary.added
            for summary in (self.entities_summary, self.people_summary, self.teams_summary)
        )


_T = TypeVar("_T")


def _normalize(value: str) -> str:
    return value.strip().casefold()


_PERSON_EQUIVALENCE_FIELDS = (
    "display_name", "title", "department", "manager_entity_id", "status", "tenant_relationship", "exempt_from_vitality",
)
_TEAM_EQUIVALENCE_FIELDS = ("name", "kind", "parent_team_id", "status", "area_paths")


def check_dir05_shadow_compliance(
    *,
    shared_people: tuple[PersonDirectory, ...],
    shared_teams: tuple[Team, ...],
    program_people: tuple[PersonDirectory, ...],
    program_teams: tuple[Team, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """specs/people.md §8.3 DIR-05A/05B. A program-local
    `people_directory.yaml`/`teams.yaml` record is entirely SHADOWED --
    never read by any runtime path (§5: "once a shared file exists, the
    corresponding local file is shadowed") -- once the corresponding
    shared file exists, regardless of the local record's own content.

    Returns `(equivalent_keys, divergent_keys)`. `equivalent_keys` are
    local records whose shared counterpart already carries the same
    business-relevant field values -- pure legacy debris, safe to remove
    (DIR-05A, informational). `divergent_keys` are local records with
    either no shared counterpart at all, or a shared counterpart with
    different field values -- the local record's content is invisible to
    every runtime read path, which is a real defect (DIR-05B), not mere
    hygiene: an operator editing the local file would see their edit
    silently discarded.

    `contacts`/`verifications` are deliberately excluded from the field
    comparison -- those are expected to be enriched independently over
    time (e.g. by `vertex kb people refresh`) without that alone
    constituting a meaningful content divergence for this check's
    purpose.
    """
    shared_people_by_alias = {_normalize(person.alias): person for person in shared_people if person.alias.strip()}
    shared_teams_by_id = {_normalize(team.id): team for team in shared_teams if team.id.strip()}

    equivalent: list[str] = []
    divergent: list[str] = []
    for person in program_people:
        key = person.alias or person.entity_id or "<unknown>"
        shared_person = shared_people_by_alias.get(_normalize(person.alias))
        if shared_person is None:
            divergent.append(f"person '{key}': no shared counterpart (never migrated)")
            continue
        diffs = [field for field in _PERSON_EQUIVALENCE_FIELDS if getattr(person, field) != getattr(shared_person, field)]
        if diffs:
            divergent.append(f"person '{key}': diverges on {', '.join(diffs)}")
        else:
            equivalent.append(key)

    for team in program_teams:
        key = team.id or team.entity_id or "<unknown>"
        shared_team = shared_teams_by_id.get(_normalize(team.id))
        if shared_team is None:
            divergent.append(f"team '{key}': no shared counterpart (never migrated)")
            continue
        diffs = [field for field in _TEAM_EQUIVALENCE_FIELDS if getattr(team, field) != getattr(shared_team, field)]
        if diffs:
            divergent.append(f"team '{key}': diverges on {', '.join(diffs)}")
        else:
            equivalent.append(key)

    return tuple(equivalent), tuple(divergent)


def _entity_key(entity: CanonicalEntity) -> str:
    return entity.entity_id


def _entity_aliases(entity: CanonicalEntity) -> tuple[str, ...]:
    return tuple(_normalize(a.value) for a in entity.aliases if a.value.strip())


def _merge_entity(existing: CanonicalEntity, incoming: CanonicalEntity) -> CanonicalEntity:
    existing_alias_values = {_normalize(a.value) for a in existing.aliases}
    merged_aliases = existing.aliases + tuple(a for a in incoming.aliases if _normalize(a.value) not in existing_alias_values)
    existing_identifier_keys = {(i.provider, i.subject_id) for i in existing.identifiers}
    merged_identifiers = existing.identifiers + tuple(
        i for i in incoming.identifiers if (i.provider, i.subject_id) not in existing_identifier_keys
    )
    return dataclasses.replace(
        existing,
        canonical_name=existing.canonical_name or incoming.canonical_name,
        entity_type=existing.entity_type or incoming.entity_type,
        scope=existing.scope or incoming.scope,
        aliases=merged_aliases,
        identifiers=merged_identifiers,
    )


def _plan_entities(
    existing: tuple[CanonicalEntity, ...], incoming: tuple[CanonicalEntity, ...]
) -> tuple[tuple[CanonicalEntity, ...], MigrationRecordSummary, tuple[ConflictCandidate, ...]]:
    existing_by_id = {_entity_key(e): e for e in existing}
    # alias -> owning entity_id, seeded from existing (pre-collision-checked by construction).
    alias_owner: dict[str, str] = {}
    for entity in existing:
        for alias in _entity_aliases(entity):
            alias_owner.setdefault(alias, entity.entity_id)

    merged_ids: list[str] = []
    added_ids: list[str] = []
    conflicts: list[ConflictCandidate] = []
    final: dict[str, CanonicalEntity] = dict(existing_by_id)

    for incoming_entity in sorted(incoming, key=lambda e: e.entity_id):
        if incoming_entity.entity_type in {"person", "team"} and incoming_entity.scope != "org":
            conflicts.append(
                ConflictCandidate(
                    kind="invalid_scope",
                    record_kind="entity",
                    key=incoming_entity.entity_id,
                    existing_entity_id=None,
                    incoming_entity_id=incoming_entity.entity_id,
                    detail=f"entity {incoming_entity.entity_id!r} is {incoming_entity.entity_type!r} scoped to "
                    f"{incoming_entity.scope!r}; people and teams must be org-scoped and were quarantined.",
                )
            )
            continue
        if incoming_entity.entity_id in existing_by_id:
            final[incoming_entity.entity_id] = _merge_entity(existing_by_id[incoming_entity.entity_id], incoming_entity)
            merged_ids.append(incoming_entity.entity_id)
            for alias in _entity_aliases(incoming_entity):
                alias_owner.setdefault(alias, incoming_entity.entity_id)
            continue

        colliding_owner: str | None = None
        for alias in _entity_aliases(incoming_entity):
            owner = alias_owner.get(alias)
            if owner is not None and owner != incoming_entity.entity_id:
                colliding_owner = owner
                break
        if colliding_owner is not None:
            conflicts.append(
                ConflictCandidate(
                    kind="alias_collision",
                    record_kind="entity",
                    key=incoming_entity.entity_id,
                    existing_entity_id=colliding_owner,
                    incoming_entity_id=incoming_entity.entity_id,
                    detail=f"entity {incoming_entity.entity_id!r} shares an alias with entity {colliding_owner!r}; quarantined, not written.",
                )
            )
            continue

        final[incoming_entity.entity_id] = incoming_entity
        added_ids.append(incoming_entity.entity_id)
        for alias in _entity_aliases(incoming_entity):
            alias_owner.setdefault(alias, incoming_entity.entity_id)

    preserved_ids = tuple(sorted(set(existing_by_id) - set(merged_ids)))
    summary = MigrationRecordSummary(preserved=preserved_ids, merged=tuple(sorted(merged_ids)), added=tuple(sorted(added_ids)))
    return tuple(final.values()), summary, tuple(conflicts)


def _merge_person(existing: PersonDirectory, incoming: PersonDirectory) -> PersonDirectory:
    protected_fields = {
        verification.field_name
        for verification in existing.verifications
        if verification.pinned
        or verification.source.casefold() in {"manual", "operator", "operator_assertion"}
        or verification.source.casefold().startswith("manual_")
    }

    def merged_value(field_name: str, current: _T, candidate: _T) -> _T:
        if field_name in protected_fields or candidate in (None, "", (), "unknown"):
            return current
        return candidate

    existing_verifications = {verification.field_name: verification for verification in existing.verifications}
    merged_verifications = dict(existing_verifications)
    for verification in incoming.verifications:
        if verification.field_name not in protected_fields:
            merged_verifications[verification.field_name] = verification
    contact_keys = {(contact.kind, contact.value) for contact in existing.contacts}
    merged_contacts = existing.contacts + tuple(
        contact for contact in incoming.contacts if (contact.kind, contact.value) not in contact_keys
    )
    return dataclasses.replace(
        existing,
        alias=merged_value("alias", existing.alias, incoming.alias),
        contacts=merged_contacts,
        display_name=merged_value("display_name", existing.display_name, incoming.display_name),
        title=merged_value("title", existing.title, incoming.title),
        manager_entity_id=merged_value("manager_entity_id", existing.manager_entity_id, incoming.manager_entity_id),
        department=merged_value("department", existing.department, incoming.department),
        status=merged_value("status", existing.status, incoming.status),
        tenant_relationship=merged_value("tenant_relationship", existing.tenant_relationship, incoming.tenant_relationship),
        departed_at=merged_value("departed_at", existing.departed_at, incoming.departed_at),
        exempt_from_vitality=existing.exempt_from_vitality or incoming.exempt_from_vitality,
        verifications=tuple(merged_verifications[name] for name in sorted(merged_verifications)),
    )


def _person_key(person: PersonDirectory) -> str:
    return person.entity_id or f"alias:{_normalize(person.alias)}"


def _plan_people(
    existing: tuple[PersonDirectory, ...], incoming: tuple[PersonDirectory, ...]
) -> tuple[tuple[PersonDirectory, ...], MigrationRecordSummary, tuple[ConflictCandidate, ...]]:
    existing_by_id = {p.entity_id: p for p in existing if p.entity_id}
    existing_by_alias: dict[str, PersonDirectory] = {}
    for person in existing:
        if person.alias.strip():
            existing_by_alias.setdefault(_normalize(person.alias), person)

    merged_keys: list[str] = []
    added_keys: list[str] = []
    conflicts: list[ConflictCandidate] = []
    final: dict[str, PersonDirectory] = {_person_key(p): p for p in existing}

    for incoming_person in sorted(incoming, key=lambda p: _person_key(p)):
        match: PersonDirectory | None = None
        if incoming_person.entity_id and incoming_person.entity_id in existing_by_id:
            match = existing_by_id[incoming_person.entity_id]
        elif incoming_person.alias.strip():
            candidate = existing_by_alias.get(_normalize(incoming_person.alias))
            if candidate is not None:
                if not candidate.entity_id or not incoming_person.entity_id or candidate.entity_id != incoming_person.entity_id:
                    conflicts.append(
                        ConflictCandidate(
                            kind="alias_collision",
                            record_kind="person",
                            key=_normalize(incoming_person.alias),
                            existing_entity_id=candidate.entity_id,
                            incoming_entity_id=incoming_person.entity_id,
                            detail=f"alias {incoming_person.alias!r} is already bound to entity {candidate.entity_id!r}, "
                            f"but the incoming record claims entity {incoming_person.entity_id!r}; quarantined, not written.",
                        )
                    )
                    continue
                match = candidate

        if match is not None:
            merged_person = _merge_person(match, incoming_person)
            final[_person_key(match)] = merged_person
            merged_keys.append(_person_key(match))
            continue

        key = _person_key(incoming_person)
        if key in final:
            # Two incoming records collapse to the same key without an existing match --
            # an incoming-internal duplicate. Keep the first (deterministic by sort order above).
            continue
        final[key] = incoming_person
        added_keys.append(key)

    preserved_keys = tuple(sorted({_person_key(p) for p in existing} - set(merged_keys)))
    summary = MigrationRecordSummary(preserved=preserved_keys, merged=tuple(sorted(merged_keys)), added=tuple(sorted(added_keys)))
    return tuple(final.values()), summary, tuple(conflicts)


def _merge_team(existing: Team, incoming: Team) -> Team:
    protected_fields = {
        verification.field_name
        for verification in existing.verifications
        if verification.pinned
        or verification.source.casefold() in {"manual", "operator", "operator_assertion"}
        or verification.source.casefold().startswith("manual_")
    }

    def merged_value(field_name: str, current: _T, candidate: _T) -> _T:
        if field_name in protected_fields or candidate in (None, "", (), "unknown"):
            return current
        return candidate

    existing_verifications = {verification.field_name: verification for verification in existing.verifications}
    merged_verifications = dict(existing_verifications)
    for verification in incoming.verifications:
        if verification.field_name not in protected_fields:
            merged_verifications[verification.field_name] = verification
    return dataclasses.replace(
        existing,
        name=merged_value("name", existing.name, incoming.name),
        parent_team_id=merged_value("parent_team_id", existing.parent_team_id, incoming.parent_team_id),
        status=merged_value("status", existing.status, incoming.status),
        area_paths=tuple(dict.fromkeys(existing.area_paths + incoming.area_paths)),
        legacy_programs=tuple(dict.fromkeys(existing.legacy_programs + incoming.legacy_programs)),
        verifications=tuple(merged_verifications[name] for name in sorted(merged_verifications)),
    )


def _team_key(team: Team) -> str:
    return team.entity_id or f"id:{_normalize(team.id)}"


def _plan_teams(
    existing: tuple[Team, ...], incoming: tuple[Team, ...]
) -> tuple[tuple[Team, ...], MigrationRecordSummary, tuple[ConflictCandidate, ...]]:
    existing_by_id = {t.entity_id: t for t in existing if t.entity_id}
    existing_by_key: dict[str, Team] = {t.id: t for t in existing if t.id.strip()}

    merged_keys: list[str] = []
    added_keys: list[str] = []
    conflicts: list[ConflictCandidate] = []
    final: dict[str, Team] = {_team_key(t): t for t in existing}

    for incoming_team in sorted(incoming, key=lambda t: _team_key(t)):
        match: Team | None = None
        if incoming_team.entity_id and incoming_team.entity_id in existing_by_id:
            match = existing_by_id[incoming_team.entity_id]
        elif incoming_team.id.strip():
            candidate = existing_by_key.get(incoming_team.id)
            if candidate is not None:
                if not candidate.entity_id or not incoming_team.entity_id or candidate.entity_id != incoming_team.entity_id:
                    conflicts.append(
                        ConflictCandidate(
                            kind="alias_collision",
                            record_kind="team",
                            key=incoming_team.id,
                            existing_entity_id=candidate.entity_id,
                            incoming_entity_id=incoming_team.entity_id,
                            detail=f"team id {incoming_team.id!r} is already bound to entity {candidate.entity_id!r}, "
                            f"but the incoming record claims entity {incoming_team.entity_id!r}; quarantined, not written.",
                        )
                    )
                    continue
                match = candidate

        if match is not None:
            merged_team = _merge_team(match, incoming_team)
            final[_team_key(match)] = merged_team
            merged_keys.append(_team_key(match))
            continue

        key = _team_key(incoming_team)
        if key in final:
            continue
        final[key] = incoming_team
        added_keys.append(key)

    preserved_keys = tuple(sorted({_team_key(t) for t in existing} - set(merged_keys)))
    summary = MigrationRecordSummary(preserved=preserved_keys, merged=tuple(sorted(merged_keys)), added=tuple(sorted(added_keys)))
    return tuple(final.values()), summary, tuple(conflicts)


def build_shared_migration_plan(
    *,
    program_id: str,
    existing_entities: tuple[CanonicalEntity, ...],
    incoming_entities: tuple[CanonicalEntity, ...],
    existing_people: tuple[PersonDirectory, ...],
    incoming_people: tuple[PersonDirectory, ...],
    existing_teams: tuple[Team, ...],
    incoming_teams: tuple[Team, ...],
    entity_redirects: tuple[EntityRedirect, ...] = (),
    diagnostics: tuple[str, ...] = (),
    additional_conflicts: tuple[ConflictCandidate, ...] = (),
) -> SharedMigrationPlan:
    entities_to_write, entities_summary, entity_conflicts = _plan_entities(existing_entities, incoming_entities)
    final_entity_ids = {entity.entity_id for entity in entities_to_write}
    bindable_people: list[PersonDirectory] = []
    bindable_teams: list[Team] = []
    reference_conflicts: list[ConflictCandidate] = []
    for person in incoming_people:
        if person.entity_id and person.entity_id in final_entity_ids:
            bindable_people.append(person)
        else:
            reference_conflicts.append(
                ConflictCandidate(
                    kind="missing_entity_binding",
                    record_kind="person",
                    key=_person_key(person),
                    existing_entity_id=None,
                    incoming_entity_id=person.entity_id or None,
                    detail=f"person {person.alias!r} refers to unavailable canonical entity {person.entity_id!r}; quarantined, not written.",
                )
            )
    for team in incoming_teams:
        if team.entity_id and team.entity_id in final_entity_ids:
            bindable_teams.append(team)
        else:
            reference_conflicts.append(
                ConflictCandidate(
                    kind="missing_entity_binding",
                    record_kind="team",
                    key=_team_key(team),
                    existing_entity_id=None,
                    incoming_entity_id=team.entity_id or None,
                    detail=f"team {team.id!r} refers to unavailable canonical entity {team.entity_id!r}; quarantined, not written.",
                )
            )
    people_to_write, people_summary, person_conflicts = _plan_people(existing_people, tuple(bindable_people))
    teams_to_write, teams_summary, team_conflicts = _plan_teams(existing_teams, tuple(bindable_teams))
    return SharedMigrationPlan(
        program_id=program_id,
        entities_to_write=entities_to_write,
        entity_redirects=entity_redirects,
        people_to_write=people_to_write,
        teams_to_write=teams_to_write,
        entities_summary=entities_summary,
        people_summary=people_summary,
        teams_summary=teams_summary,
        conflicts=entity_conflicts + person_conflicts + team_conflicts + tuple(reference_conflicts) + additional_conflicts,
        diagnostics=diagnostics,
    )


def _shared_entities_path(knowledge_root: Path) -> Path:
    return knowledge_root / "entities.yaml"


def _shared_people_directory_path(knowledge_root: Path) -> Path:
    return knowledge_root / "people_directory.yaml"


def _shared_teams_path(knowledge_root: Path) -> Path:
    return knowledge_root / "teams.yaml"


def _read_existing_shared(
    knowledge_root: Path,
) -> tuple[tuple[CanonicalEntity, ...], tuple[EntityRedirect, ...], tuple[PersonDirectory, ...], tuple[Team, ...]]:
    entities_doc = load_entities_document(_shared_entities_path(knowledge_root)) if _shared_entities_path(knowledge_root).exists() else None
    people_result = load_people_directory(_shared_people_directory_path(knowledge_root))
    teams_result = load_teams(_shared_teams_path(knowledge_root))
    return (
        entities_doc.entities if entities_doc else (),
        entities_doc.redirects if entities_doc else (),
        people_result.people if people_result else (),
        teams_result.teams if teams_result else (),
    )


def _read_program_local_candidates(
    program_id: str, *, programs_root: Path, workspace_id: str, as_of: datetime
) -> tuple[
    tuple[CanonicalEntity, ...],
    tuple[PersonDirectory, ...],
    tuple[Team, ...],
    tuple[str, ...],
    tuple[ConflictCandidate, ...],
]:
    program_knowledge_dir = programs_root / program_id / "knowledge"
    diagnostics: list[str] = []

    entities: tuple[CanonicalEntity, ...] = ()
    entities_path = program_knowledge_dir / "entities.yaml"
    if entities_path.exists():
        if is_legacy_schema_0_entities_document(entities_path):
            preview = preview_entities_migration(entities_path, workspace_id=workspace_id, as_of=as_of)
            entities = preview.would_create_entities
            diagnostics.extend(preview.diagnostics)
        else:
            doc = load_entities_document(entities_path)
            entities = doc.entities if doc else ()
    else:
        diagnostics.append(f"no program-local entities.yaml at {entities_path}; nothing to bind as entity candidates.")

    people_result = load_people_directory(program_knowledge_dir / "people_directory.yaml")
    people = people_result.people if people_result else ()
    if people_result is not None:
        diagnostics.extend(diag.detail for diag in people_result.diagnostics)

    teams_result = load_teams(program_knowledge_dir / "teams.yaml")
    teams = teams_result.teams if teams_result else ()
    if teams_result is not None:
        diagnostics.extend(diag.detail for diag in teams_result.diagnostics)

    binding_conflicts: list[ConflictCandidate] = []
    entity_aliases: dict[tuple[str, str], list[CanonicalEntity]] = {}
    for entity in entities:
        if entity.entity_type not in {"person", "team"}:
            continue
        for alias in _entity_aliases(entity):
            entity_aliases.setdefault((entity.entity_type, alias), []).append(entity)

    bound_people: list[PersonDirectory] = []
    for person in people:
        if person.entity_id:
            bound_people.append(person)
            continue
        candidates = entity_aliases.get(("person", _normalize(person.alias)), [])
        if len(candidates) == 1:
            bound_people.append(dataclasses.replace(person, entity_id=candidates[0].entity_id))
            continue
        binding_conflicts.append(
            ConflictCandidate(
                kind="unbound_alias",
                record_kind="person",
                key=_normalize(person.alias),
                existing_entity_id=None,
                incoming_entity_id=None,
                detail=f"person alias {person.alias!r} has no unique program-local person entity binding; quarantined, not written.",
            )
        )

    bound_teams: list[Team] = []
    for team in teams:
        if team.entity_id:
            bound_teams.append(team)
            continue
        candidates = entity_aliases.get(("team", _normalize(team.id)), [])
        if len(candidates) == 1:
            bound_teams.append(dataclasses.replace(team, entity_id=candidates[0].entity_id))
            continue
        binding_conflicts.append(
            ConflictCandidate(
                kind="unbound_alias",
                record_kind="team",
                key=_normalize(team.id),
                existing_entity_id=None,
                incoming_entity_id=None,
                detail=f"team key {team.id!r} has no unique program-local team entity binding; quarantined, not written.",
            )
        )

    return entities, tuple(bound_people), tuple(bound_teams), tuple(diagnostics), tuple(binding_conflicts)


def preview_shared_migration(program_id: str, *, programs_root: Path, as_of: datetime | None = None) -> SharedMigrationPlan:
    """Read-only: builds the plan without acquiring a lease or writing
    anything. Safe to call even before the registry is bootstrapped
    (`existing_*` are simply empty in that case, matching bootstrap's own
    day-0 case)."""
    now = as_of or datetime.now(timezone.utc)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config = load_registry_config(knowledge_root)
    workspace_id = config.workspace_id if config is not None else "unbootstrapped-workspace"

    existing_entities, entity_redirects, existing_people, existing_teams = _read_existing_shared(knowledge_root)
    incoming_entities, incoming_people, incoming_teams, diagnostics, binding_conflicts = _read_program_local_candidates(
        program_id, programs_root=programs_root, workspace_id=workspace_id, as_of=now
    )
    return build_shared_migration_plan(
        program_id=program_id,
        existing_entities=existing_entities,
        incoming_entities=incoming_entities,
        entity_redirects=entity_redirects,
        existing_people=existing_people,
        incoming_people=incoming_people,
        existing_teams=existing_teams,
        incoming_teams=incoming_teams,
        diagnostics=diagnostics,
        additional_conflicts=binding_conflicts,
    )


def shared_factual_files_exist(programs_root: Path) -> bool:
    """True once ANY of the shared entities/people_directory/teams files
    exist -- the boundary between "day-0 bootstrap may populate the first
    shared root" and "use migrate-shared, which merges against real
    data" (§6.9: "creates the FIRST shared root")."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    return (
        _shared_entities_path(knowledge_root).exists()
        or _shared_people_directory_path(knowledge_root).exists()
        or _shared_teams_path(knowledge_root).exists()
    )


def _changed_shared_paths(plan: SharedMigrationPlan) -> tuple[str, ...]:
    paths: list[str] = []
    if plan.entities_summary.merged or plan.entities_summary.added:
        paths.append("entities.yaml")
    if plan.people_summary.merged or plan.people_summary.added:
        paths.append("people_directory.yaml")
    if plan.teams_summary.merged or plan.teams_summary.added:
        paths.append("teams.yaml")
    return tuple(paths)


def _write_shared_plan_to_staging(plan: SharedMigrationPlan, staged_dir: Path, paths: tuple[str, ...]) -> None:
    if "entities.yaml" in paths:
        write_entities_document(
            staged_dir / "entities.yaml",
            EntitiesDocument(
                schema_version=ENTITIES_SCHEMA_VERSION,
                entities=tuple(sorted(plan.entities_to_write, key=lambda entity: entity.entity_id)),
                redirects=plan.entity_redirects,
            ),
        )
    if "people_directory.yaml" in paths:
        write_people_directory(staged_dir / "people_directory.yaml", plan.people_to_write)
    if "teams.yaml" in paths:
        write_teams(staged_dir / "teams.yaml", plan.teams_to_write)


def _validate_shared_plan_staging(plan: SharedMigrationPlan, staged_dir: Path, paths: tuple[str, ...]) -> None:
    if "entities.yaml" in paths:
        entities_doc = load_entities_document(staged_dir / "entities.yaml")
        if entities_doc is None or entities_doc.entities != tuple(sorted(plan.entities_to_write, key=lambda entity: entity.entity_id)):
            raise ConfigError("Staged entities.yaml did not round-trip through the production loader.")
        if entities_doc.redirects != plan.entity_redirects:
            raise ConfigError("Staged entities.yaml did not preserve existing redirects.")
    if "people_directory.yaml" in paths:
        people_result = load_people_directory(staged_dir / "people_directory.yaml")
        if people_result is None or people_result.people != tuple(sorted(plan.people_to_write, key=lambda person: person.entity_id or person.alias)):
            raise ConfigError("Staged people_directory.yaml did not round-trip through the production loader.")
    if "teams.yaml" in paths:
        teams_result = load_teams(staged_dir / "teams.yaml")
        if teams_result is None or teams_result.teams != tuple(sorted(plan.teams_to_write, key=lambda team: team.entity_id or team.id)):
            raise ConfigError("Staged teams.yaml did not round-trip through the production loader.")


def _append_record_field_changes(
    *,
    record: object,
    before: object | None,
    entity_id: str,
    transaction_id: str,
    generation_id: str,
    workspace_id: str,
    knowledge_root: Path,
    actor: str,
    reason: str,
    as_of: datetime,
) -> None:
    before_payload = dataclasses.asdict(cast("DataclassInstance", before)) if before is not None else {}
    after_payload = dataclasses.asdict(cast("DataclassInstance", record))
    for field_name in sorted(set(before_payload) | set(after_payload)):
        before_value = before_payload.get(field_name)
        after_value = after_payload.get(field_name)
        if before_value == after_value:
            continue
        append_people_change_record(
            knowledge_root,
            workspace_id=workspace_id,
            transaction_id=transaction_id,
            generation_id=generation_id,
            authenticated_principal=actor,
            operation="create" if before is None else "update",
            entity_id=entity_id,
            field=field_name,
            before=before_value,
            after=after_value,
            source="shared_migration",
            reason=reason,
            as_of=as_of,
        )


def _append_migration_changes(
    plan: SharedMigrationPlan,
    *,
    baseline: dict[str, object],
    knowledge_root: Path,
    workspace_id: str,
    actor: str,
    as_of: datetime,
) -> None:
    assert plan.transaction_id is not None
    assert plan.generation_id is not None
    reason = f"vertex kb registry migrate-shared {plan.program_id} --apply"
    entities = baseline["entities"]
    people = baseline["people"]
    teams = baseline["teams"]
    assert isinstance(entities, dict) and isinstance(people, dict) and isinstance(teams, dict)
    for entity in plan.entities_to_write:
        if entity.entity_id in plan.entities_summary.merged or entity.entity_id in plan.entities_summary.added:
            _append_record_field_changes(
                record=entity, before=entities.get(entity.entity_id), entity_id=entity.entity_id,
                transaction_id=plan.transaction_id, generation_id=plan.generation_id, workspace_id=workspace_id,
                knowledge_root=knowledge_root, actor=actor, reason=reason, as_of=as_of,
            )
    for person in plan.people_to_write:
        key = _person_key(person)
        if key in plan.people_summary.merged or key in plan.people_summary.added:
            _append_record_field_changes(
                record=person, before=people.get(key), entity_id=person.entity_id,
                transaction_id=plan.transaction_id, generation_id=plan.generation_id, workspace_id=workspace_id,
                knowledge_root=knowledge_root, actor=actor, reason=reason, as_of=as_of,
            )
    for team in plan.teams_to_write:
        key = _team_key(team)
        if key in plan.teams_summary.merged or key in plan.teams_summary.added:
            _append_record_field_changes(
                record=team, before=teams.get(key), entity_id=team.entity_id,
                transaction_id=plan.transaction_id, generation_id=plan.generation_id, workspace_id=workspace_id,
                knowledge_root=knowledge_root, actor=actor, reason=reason, as_of=as_of,
            )


def _append_migration_conflicts(
    plan: SharedMigrationPlan,
    *,
    knowledge_root: Path,
    workspace_id: str,
    transaction_id: str,
    actor: str,
    as_of: datetime,
) -> None:
    for conflict_index, conflict in enumerate(plan.conflicts):
        append_people_conflict_record(
            knowledge_root,
            workspace_id=workspace_id,
            conflict_id=f"{transaction_id}-conflict-{conflict_index}",
            decision="quarantined",
            authenticated_principal=actor,
            reason=conflict.detail,
            entity_id=conflict.incoming_entity_id,
            as_of=as_of,
        )


def apply_shared_migration(
    program_id: str, *, programs_root: Path, actor: str, as_of: datetime | None = None
) -> SharedMigrationPlan:
    """Apply through the canonical staged multi-file registry writer.

    The planner is re-run inside the transaction's writer lease before any
    candidate bytes are staged, so a preview is advisory while apply uses a
    fresh, fenced baseline.  Journal records are appended only after the
    factual generation has committed.
    """
    now = as_of or datetime.now(timezone.utc)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config = load_registry_config(knowledge_root)
    if config is None or load_registry_manifest(knowledge_root) is None:
        raise ConfigError(
            "The registry has not been bootstrapped yet. Run 'vertex kb registry bootstrap --apply "
            "--customer-boundary-id <id>' first -- migrate-shared cannot write without an existing manifest."
        )
    require_adopted_registry(knowledge_root, consumer="Shared migration")
    plan = preview_shared_migration(program_id, programs_root=programs_root, as_of=now)
    changed_paths = _changed_shared_paths(plan)
    if not changed_paths:
        _append_migration_conflicts(
            plan, knowledge_root=knowledge_root, workspace_id=config.workspace_id,
            transaction_id=f"shared-migration-noop-{now.strftime('%Y%m%dT%H%M%SZ')}", actor=actor, as_of=now,
        )
        return plan

    baseline: dict[str, object] = {}
    committed_plan: SharedMigrationPlan | None = None

    def write_staged_files(staged_dir: Path) -> None:
        nonlocal committed_plan
        # This re-derivation occurs after the transaction primitive has
        # acquired the workspace lease, closing preview/apply TOCTOU.
        current = preview_shared_migration(program_id, programs_root=programs_root, as_of=now)
        current_paths = _changed_shared_paths(current)
        if current_paths != changed_paths:
            raise ConfigError("Program-local/shared migration inputs changed while waiting for the registry lease; re-run the preview.")
        existing_entities, _, existing_people, existing_teams = _read_existing_shared(knowledge_root)
        baseline["entities"] = {entity.entity_id: entity for entity in existing_entities}
        baseline["people"] = {_person_key(person): person for person in existing_people}
        baseline["teams"] = {_team_key(team): team for team in existing_teams}
        _write_shared_plan_to_staging(current, staged_dir, current_paths)
        committed_plan = current

    def validate_staged_files(staged_dir: Path) -> None:
        assert committed_plan is not None
        _validate_shared_plan_staging(committed_plan, staged_dir, changed_paths)

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        changed_paths,
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        as_of=now,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=now)
    assert committed_plan is not None
    result = dataclasses.replace(
        committed_plan,
        transaction_id=committed.transaction_id,
        generation_id=committed.manifest.generation_id,
    )
    _append_migration_changes(
        result,
        baseline=baseline,
        knowledge_root=knowledge_root,
        workspace_id=config.workspace_id,
        actor=actor,
        as_of=now,
    )
    _append_migration_conflicts(
        result,
        knowledge_root=knowledge_root,
        workspace_id=config.workspace_id,
        transaction_id=committed.transaction_id,
        actor=actor,
        as_of=now,
    )
    migrated_person_team_entity_ids = frozenset(
        entity.entity_id
        for entity in result.entities_to_write
        if entity.entity_type in ORG_SCOPE_ONLY_ENTITY_TYPES
        and entity.entity_id in frozenset(result.entities_summary.added) | frozenset(result.entities_summary.merged)
    )
    if migrated_person_team_entity_ids:
        _clear_migrated_person_team_entities_from_program_local(
            program_id, programs_root=programs_root, migrated_person_team_entity_ids=migrated_person_team_entity_ids,
        )
    # DIR-05 ("shadowed program-local factual file... diverges from shared
    # root"): PPL-W2B.4's own row text already claims onboarding "never
    # creates or merges shadowed program-local people/team files... once a
    # shared root exists" -- extending that same prevention-over-detection
    # posture here rather than building a separate DIR-05A/05B detection
    # check, found genuinely missing (never built, never named as
    # deferred) during people.md's final closure audit. Confirmed present
    # in the shared registry's post-commit set (added/merged), mirroring
    # the entities cleanup above exactly, including the same
    # conservatism: anything quarantined by a conflict is left in the
    # program-local file untouched.
    migrated_person_ids = frozenset(result.people_summary.added) | frozenset(result.people_summary.merged)
    migrated_team_ids = frozenset(result.teams_summary.added) | frozenset(result.teams_summary.merged)
    if migrated_person_ids:
        migrated_aliases = frozenset(
            _normalize(person.alias)
            for person in result.people_to_write
            if person.entity_id in migrated_person_ids and person.alias.strip()
        )
        _clear_migrated_people_from_program_local(
            program_id, programs_root=programs_root,
            migrated_entity_ids=migrated_person_ids, migrated_aliases=migrated_aliases,
        )
    if migrated_team_ids:
        migrated_team_keys = frozenset(
            _normalize(team.id)
            for team in result.teams_to_write
            if team.entity_id in migrated_team_ids and team.id.strip()
        )
        _clear_migrated_teams_from_program_local(
            program_id, programs_root=programs_root,
            migrated_entity_ids=migrated_team_ids, migrated_team_keys=migrated_team_keys,
        )
    return result


def _clear_migrated_people_from_program_local(
    program_id: str, *, programs_root: Path, migrated_entity_ids: frozenset[str], migrated_aliases: frozenset[str],
) -> tuple[str, ...]:
    """DIR-05 companion to `_clear_migrated_person_team_entities_from_program_local`:
    removes program-local `people_directory.yaml` records confirmed
    migrated into the shared registry's post-commit set.

    A record with a non-blank `entity_id` is matched against that exact
    id (`migrated_entity_ids`) -- this is required, not just sufficient:
    two program-local records can share the same alias (that's precisely
    what triggers a quarantine -- see
    `test_apply_shared_migration_preserves_quarantined_entities_in_program_local_source`'s
    "intruder" fixture, which shares alias "alice" with the real
    person:alice but must NOT be removed), so alias alone cannot
    disambiguate once an `entity_id` is already recorded.

    A record with a BLANK `entity_id` falls back to normalized-alias
    matching (`migrated_aliases`): this is the common onboarding case
    (`_seed_first_program`-style fixtures document it deliberately --
    "blank IDs... prove that bootstrap binds local factual records to the
    uniquely compatible entity candidates"), where the `entity_id`
    resolved during migration planning is only ever recorded on the
    in-memory bound copy, never written back to the raw local file.

    Either way, a record that doesn't match is left untouched --
    including anything quarantined by a conflict."""
    program_people_path = programs_root / program_id / "knowledge" / "people_directory.yaml"
    if not program_people_path.exists():
        return ()
    result = load_people_directory(program_people_path)
    if result is None:
        return ()

    def _is_migrated(person: PersonDirectory) -> bool:
        if person.entity_id:
            return person.entity_id in migrated_entity_ids
        return _normalize(person.alias) in migrated_aliases

    remaining = tuple(person for person in result.people if not _is_migrated(person))
    removed_aliases = tuple(person.alias for person in result.people if person not in remaining)
    if not removed_aliases:
        return ()
    write_people_directory(program_people_path, remaining)
    return removed_aliases


def _clear_migrated_teams_from_program_local(
    program_id: str, *, programs_root: Path, migrated_entity_ids: frozenset[str], migrated_team_keys: frozenset[str],
) -> tuple[str, ...]:
    """DIR-05 companion, teams half -- see `_clear_migrated_people_from_program_local`.
    Same entity_id-first, normalized-`Team.id`-fallback matching, for the
    same reasons (alias/id collisions on quarantine; blank `entity_id`
    on the common onboarding path)."""
    program_teams_path = programs_root / program_id / "knowledge" / "teams.yaml"
    if not program_teams_path.exists():
        return ()
    result = load_teams(program_teams_path)
    if result is None:
        return ()

    def _is_migrated(team: Team) -> bool:
        if team.entity_id:
            return team.entity_id in migrated_entity_ids
        return _normalize(team.id) in migrated_team_keys

    remaining = tuple(team for team in result.teams if not _is_migrated(team))
    removed_ids = tuple(team.id for team in result.teams if team not in remaining)
    if not removed_ids:
        return ()
    write_teams(program_teams_path, remaining)
    return removed_ids


def _clear_migrated_person_team_entities_from_program_local(
    program_id: str, *, programs_root: Path, migrated_person_team_entity_ids: frozenset[str],
) -> tuple[str, ...]:
    """PPL-W6.4 fix, found during a real pilot smoke test: `migrate-shared`
    copies program-local person/team entities into the shared registry but
    previously never removed them from the program-local source -- leaving
    a residual `doctor --kb` DIR-11 failure every single time, since §5.6's
    already-ratified binding decision ("people/teams are always org-scoped")
    means a program-scope `entities.yaml` must never contain one at all,
    unconditionally (`check_dir11_compliance`, not gated on a collision).
    This completes what the command's own name promises -- MIGRATE, not
    copy -- by clearing exactly the person/team entities that are now
    confirmed present in the shared registry's post-commit `entities_to_write`
    set. Deliberately narrower than "remove every program-local person/team
    entity": anything quarantined by a conflict (`plan.conflicts`) is
    NOT in `migrated_person_team_entity_ids` and is left in place --
    removing a quarantined entity would lose data with no shared copy to
    fall back on. Only runs for a schema-2.0 program-local file (loaded via
    `load_entities_document`); a legacy schema-0 file is left untouched
    entirely, matching `preview_entities_migration`'s own established
    "never silently promoted" contract -- this is a narrow cleanup, not a
    schema migration."""
    program_entities_path = programs_root / program_id / "knowledge" / "entities.yaml"
    if not program_entities_path.exists() or is_legacy_schema_0_entities_document(program_entities_path):
        return ()
    doc = load_entities_document(program_entities_path)
    if doc is None:
        return ()
    remaining: list[CanonicalEntity] = []
    removed_ids: list[str] = []
    for entity in doc.entities:
        if entity.entity_type in ORG_SCOPE_ONLY_ENTITY_TYPES and entity.entity_id in migrated_person_team_entity_ids:
            removed_ids.append(entity.entity_id)
        else:
            remaining.append(entity)
    if not removed_ids:
        return ()
    write_entities_document(
        program_entities_path,
        EntitiesDocument(schema_version=doc.schema_version, entities=tuple(remaining), redirects=doc.redirects),
    )
    return tuple(removed_ids)


def bootstrap_shared_factual_files(
    program_id: str, *, programs_root: Path, actor: str, apply: bool, as_of: datetime | None = None
) -> SharedMigrationPlan:
    """§6.9: "creates the first shared root from selected program-local
    knowledge... previews all records, and requires explicit apply."
    Raises `ConfigError` if a shared factual file already exists --
    `migrate-shared` is the correct command once real shared data exists,
    matching this function's own "FIRST shared root" text."""
    if shared_factual_files_exist(programs_root):
        raise ConfigError(
            "A shared entities.yaml/people_directory.yaml/teams.yaml already exists. "
            "Use 'vertex kb registry migrate-shared' to merge program-local data into an existing shared root instead."
        )
    if not apply:
        return preview_shared_migration(program_id, programs_root=programs_root, as_of=as_of)
    return apply_shared_migration(program_id, programs_root=programs_root, actor=actor, as_of=as_of)


# ---------------------------------------------------------------------------
# specs/bklg.md BL-E3: one-time entity_id backfill.
#
# `people_directory_schema.py`'s `_person_from_payload`/`_team_from_payload`
# already anticipate this exact state and emit a WARN diagnostic for it:
# "record ... has no entity_id -- a migration gap, not a new identity."
# It arises when `knowledge/people_directory.yaml`/`teams.yaml` were
# populated directly (predating `entities.yaml`'s introduction) rather than
# through `migrate-shared`'s program-local promotion path -- so there is no
# canonical `CanonicalEntity` for any of these real records yet, and
# `vertex kb people show`/`find_person` cannot resolve them even though the
# raw directory data is real and complete. `build_shared_migration_plan`'s
# merge machinery does not fit this case (its `_plan_people`/`_plan_entities`
# assume "new incoming data from elsewhere" and would flag every record as
# an `alias_collision` against itself), so this is a separate, simpler
# planner: mint one canonical entity per orphaned record and patch that
# record's own `entity_id` field in place -- never touching any other field.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityIdBackfillPlan:
    entities_to_write: tuple[CanonicalEntity, ...]
    people_to_write: tuple[PersonDirectory, ...]
    teams_to_write: tuple[Team, ...]
    new_entity_ids: tuple[str, ...]
    people_backfilled: tuple[str, ...]  # aliases
    teams_backfilled: tuple[str, ...]  # team ids
    diagnostics: tuple[str, ...]
    transaction_id: str | None = None
    generation_id: str | None = None

    @property
    def is_noop(self) -> bool:
        return not self.people_backfilled and not self.teams_backfilled


_BACKFILL_SOURCE = "directory_backfill"
_BACKFILL_UNVERIFIED_PRINCIPAL = "<unverified -- entity_id backfill from existing shared directory>"


def _backfill_alias(*, value: str, kind: str, now: datetime) -> EntityAlias:
    return EntityAlias(
        value=value,
        kind=kind,
        status=AliasStatus.ACTIVE,
        valid_from=now,
        valid_until=None,
        source=_BACKFILL_SOURCE,
        source_ref=None,
        recorded_at=now,
        verified_at=now,
        verified_by_principal=_BACKFILL_UNVERIFIED_PRINCIPAL,
    )


def _build_entity_id_backfill_plan(
    *,
    workspace_id: str,
    existing_entities: tuple[CanonicalEntity, ...],
    existing_people: tuple[PersonDirectory, ...],
    existing_teams: tuple[Team, ...],
    now: datetime,
) -> EntityIdBackfillPlan:
    existing_entity_ids = {entity.entity_id for entity in existing_entities}
    new_entities: list[CanonicalEntity] = []
    new_entity_ids: list[str] = []
    updated_people: list[PersonDirectory] = []
    updated_teams: list[Team] = []
    backfilled_people: list[str] = []
    backfilled_teams: list[str] = []

    for person in existing_people:
        if person.entity_id and person.entity_id in existing_entity_ids:
            updated_people.append(person)
            continue
        new_id = f"person:{new_ulid(now)}"
        aliases = (_backfill_alias(value=person.alias, kind="vertex::alias", now=now),) if person.alias.strip() else ()
        new_entities.append(
            CanonicalEntity(
                workspace_id=workspace_id,
                entity_id=new_id,
                entity_type="person",
                canonical_name=person.display_name or person.alias,
                aliases=aliases,
                scope="org",
                created_at=now,
                status=EntityStatus.ACTIVE,
            )
        )
        new_entity_ids.append(new_id)
        updated_people.append(dataclasses.replace(person, entity_id=new_id))
        backfilled_people.append(person.alias)

    for team in existing_teams:
        if team.entity_id and team.entity_id in existing_entity_ids:
            updated_teams.append(team)
            continue
        new_id = f"team:{new_ulid(now)}"
        aliases = (_backfill_alias(value=team.id, kind="vertex::team_key", now=now),) if team.id.strip() else ()
        new_entities.append(
            CanonicalEntity(
                workspace_id=workspace_id,
                entity_id=new_id,
                entity_type="team",
                canonical_name=team.name or team.id,
                aliases=aliases,
                scope="org",
                created_at=now,
                status=EntityStatus.ACTIVE,
            )
        )
        new_entity_ids.append(new_id)
        updated_teams.append(dataclasses.replace(team, entity_id=new_id))
        backfilled_teams.append(team.id)

    if new_entities:
        diagnostics = (
            f"{len(backfilled_people)} person and {len(backfilled_teams)} team record(s) would get a "
            "freshly-minted canonical entity_id; no other field changes.",
        )
    else:
        diagnostics = ("Every people_directory.yaml/teams.yaml record already carries a valid entity_id; nothing to backfill.",)

    return EntityIdBackfillPlan(
        entities_to_write=tuple(existing_entities) + tuple(new_entities),
        people_to_write=tuple(updated_people),
        teams_to_write=tuple(updated_teams),
        new_entity_ids=tuple(new_entity_ids),
        people_backfilled=tuple(backfilled_people),
        teams_backfilled=tuple(backfilled_teams),
        diagnostics=diagnostics,
    )


def preview_entity_id_backfill(*, programs_root: Path, as_of: datetime | None = None) -> EntityIdBackfillPlan:
    now = as_of or datetime.now(timezone.utc)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config = load_registry_config(knowledge_root)
    if config is None:
        raise ConfigError(
            "The registry has not been bootstrapped yet. Run 'vertex kb registry bootstrap --apply "
            "--customer-boundary-id <id>' first -- the entity_id backfill needs a minted workspace_id."
        )
    existing_entities, _redirects, existing_people, existing_teams = _read_existing_shared(knowledge_root)
    return _build_entity_id_backfill_plan(
        workspace_id=config.workspace_id,
        existing_entities=existing_entities,
        existing_people=existing_people,
        existing_teams=existing_teams,
        now=now,
    )


def apply_entity_id_backfill(*, programs_root: Path, actor: str, as_of: datetime | None = None) -> EntityIdBackfillPlan:
    """Apply through the canonical staged multi-file registry writer --
    the same `prepare_registry_files_transaction`/`commit_registry_files_transaction`
    path `apply_shared_migration` uses, so this backfill is lease-governed,
    checkpointed, and journaled identically to every other shared-registry
    mutation, not a special-cased raw file write."""
    now = as_of or datetime.now(timezone.utc)
    knowledge_root = get_shared_knowledge_root(programs_root)
    config = load_registry_config(knowledge_root)
    if config is None or load_registry_manifest(knowledge_root) is None:
        raise ConfigError(
            "The registry has not been bootstrapped yet. Run 'vertex kb registry bootstrap --apply "
            "--customer-boundary-id <id>' first -- the entity_id backfill needs a minted workspace_id."
        )
    require_adopted_registry(knowledge_root, consumer="Entity-id backfill")

    plan = preview_entity_id_backfill(programs_root=programs_root, as_of=now)
    if plan.is_noop:
        return plan

    changed_paths = ("entities.yaml", "people_directory.yaml", "teams.yaml")
    baseline: dict[str, dict[str, object]] = {}
    committed_plan: EntityIdBackfillPlan | None = None

    def write_staged_files(staged_dir: Path) -> None:
        nonlocal committed_plan
        # Re-derived after the lease is held, closing the preview/apply TOCTOU
        # the same way apply_shared_migration's write_staged_files does.
        current_entities, _redirects, current_people, current_teams = _read_existing_shared(knowledge_root)
        current_plan = _build_entity_id_backfill_plan(
            workspace_id=config.workspace_id,
            existing_entities=current_entities,
            existing_people=current_people,
            existing_teams=current_teams,
            now=now,
        )
        if (current_plan.people_backfilled, current_plan.teams_backfilled) != (plan.people_backfilled, plan.teams_backfilled):
            raise ConfigError(
                "The shared directory changed while waiting for the registry lease; re-run the preview."
            )
        baseline["people"] = {person.alias: person for person in current_people}
        baseline["teams"] = {team.id: team for team in current_teams}
        write_entities_document(
            staged_dir / "entities.yaml",
            EntitiesDocument(
                schema_version=ENTITIES_SCHEMA_VERSION,
                entities=tuple(sorted(current_plan.entities_to_write, key=lambda entity: entity.entity_id)),
                redirects=(),
            ),
        )
        write_people_directory(staged_dir / "people_directory.yaml", current_plan.people_to_write)
        write_teams(staged_dir / "teams.yaml", current_plan.teams_to_write)
        committed_plan = current_plan

    def validate_staged_files(staged_dir: Path) -> None:
        assert committed_plan is not None
        entities_doc = load_entities_document(staged_dir / "entities.yaml")
        expected_entities = tuple(sorted(committed_plan.entities_to_write, key=lambda entity: entity.entity_id))
        if entities_doc is None or entities_doc.entities != expected_entities:
            raise ConfigError("Staged entities.yaml did not round-trip through the production loader.")
        people_result = load_people_directory(staged_dir / "people_directory.yaml")
        expected_people = tuple(sorted(committed_plan.people_to_write, key=lambda person: person.entity_id or person.alias))
        if people_result is None or people_result.people != expected_people:
            raise ConfigError("Staged people_directory.yaml did not round-trip through the production loader.")
        teams_result = load_teams(staged_dir / "teams.yaml")
        expected_teams = tuple(sorted(committed_plan.teams_to_write, key=lambda team: team.entity_id or team.id))
        if teams_result is None or teams_result.teams != expected_teams:
            raise ConfigError("Staged teams.yaml did not round-trip through the production loader.")

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        changed_paths,
        owner=actor,
        write_staged_files=write_staged_files,
        validate_staged_files=validate_staged_files,
        as_of=now,
    )
    committed = commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=now)
    assert committed_plan is not None
    result = dataclasses.replace(
        committed_plan,
        transaction_id=committed.transaction_id,
        generation_id=committed.manifest.generation_id,
    )

    assert result.transaction_id is not None
    assert result.generation_id is not None
    reason = "one-time entity_id backfill for pre-existing knowledge/people_directory.yaml + teams.yaml records"
    new_entity_id_set = frozenset(result.new_entity_ids)
    for entity in result.entities_to_write:
        if entity.entity_id in new_entity_id_set:
            _append_record_field_changes(
                record=entity, before=None, entity_id=entity.entity_id,
                transaction_id=result.transaction_id, generation_id=result.generation_id, workspace_id=config.workspace_id,
                knowledge_root=knowledge_root, actor=actor, reason=reason, as_of=now,
            )
    backfilled_person_aliases = frozenset(result.people_backfilled)
    for person in result.people_to_write:
        if person.alias in backfilled_person_aliases:
            _append_record_field_changes(
                record=person, before=baseline["people"].get(person.alias), entity_id=person.entity_id,
                transaction_id=result.transaction_id, generation_id=result.generation_id, workspace_id=config.workspace_id,
                knowledge_root=knowledge_root, actor=actor, reason=reason, as_of=now,
            )
    backfilled_team_ids = frozenset(result.teams_backfilled)
    for team in result.teams_to_write:
        if team.id in backfilled_team_ids:
            _append_record_field_changes(
                record=team, before=baseline["teams"].get(team.id), entity_id=team.entity_id,
                transaction_id=result.transaction_id, generation_id=result.generation_id, workspace_id=config.workspace_id,
                knowledge_root=knowledge_root, actor=actor, reason=reason, as_of=now,
            )
    return result
