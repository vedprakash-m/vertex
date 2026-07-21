"""specs/people.md Phase 3, PPL-W3.2: DIR-01/02/03/06/07/15 doctor-check
logic (§8.3).

Investigated the real current state before deciding scope (not assumed):
DIR-11 (`people_entity_schema.py::check_dir11_compliance`, PPL-W2A.1) and
DIR-14A/DIR-14B (`people_registry_governance.py`'s `_is_critical_field`/
`ManagedRegistryEdit.critical`, PPL-W2B.2) were ALREADY implemented and
wired into `run_kb_doctor` before this item -- this module does not
duplicate either.

Four groups of DIR codes are explicitly OUT of this item's scope,
each blocked on a real, not-yet-built dependency rather than merely
unattempted:

- DIR-08A/08B (PII retention/redaction/encryption/reveal policy): no
  graded encryption/retention policy schema exists on `RegistryConfig`
  or anywhere else to compare against -- needs a policy schema defined
  first.
- DIR-09A/09B (provider capability/refresh health): `identity_providers.yaml`
  is still Phase-4-only synthetic example data; no real provider/refresh
  module exists.
- DIR-10 (audience scope references unverified/non-org memberships): no
  audience-scope schema exists yet -- explicitly Phase 5a's own scope
  per §9's phase table ("Phase 5a ... Depends on: Phases 3-4").
- DIR-13A/DIR-13B (cache missing/stale/corrupted): no
  `knowledge/.cache/` exists -- explicitly PPL-W3.4's own scope (§8.5),
  which this item (PPL-W3.2) precedes in §9.2's dependency order.

DIR-04 and DIR-12A/DIR-12B (originally deferred here to PPL-W3.2b) are
now also implemented in this module, added by PPL-W3.2b once cross-
program `ProgramContext` loading was in scope:

- `find_stakeholder_lifecycle_violations` (DIR-04): for every known
  program's `ProgramContext.stakeholder_aliases` (itself already the
  fully-resolved union of every RACI/owner/register alias reference in
  that program -- STK-01/02/03 already guarantee any RACI/owner alias
  NOT in `stakeholder_register` is caught separately, so iterating this
  one set is sufficient, not partial), resolves each alias against the
  shared registry via `resolve_ref_to_canonical_entity_id` and flags an
  unresolved alias, a resolved entity with no `people_directory.yaml`
  record ("ambiguous identity," reusing PPL-W5a.5's own established
  definition of that term), or a resolved person whose status is not
  `ACTIVE`.
- `find_conflict_accountability_findings` (DIR-12A/DIR-12B): for every
  open conflict (`people_query.py::list_conflicts`), checks whether the
  conflicting canonical entity's own aliases intersect the UNION of
  every known program's stakeholder aliases -- "has an active
  accountability reference" (DIR-12A, more urgent) vs does not
  (DIR-12B, still open but nothing currently depends on it for
  accountability).

What IS implemented here, each a pure function operating on already-loaded
typed data (the caller in `src/commands/doctor_checks/kb_checks.py` does
the loading and wraps results as `DoctorCheck`s, matching the existing
`check_dir11_compliance` convention):

- `find_duplicate_identifiers` (DIR-01): normalized (casefold) collisions
  among `entity_id` values across entities/people/teams, and among alias
  values belonging to DIFFERENT entities -- reusing the exact alias-owner-
  map technique `people_shared_migration.py::_plan_entities` already
  proved correct for collision detection, applied here read-only against
  the FINAL committed state rather than a migration plan.
- `find_unresolved_references` (DIR-02): every `PersonDirectory`/`Team`
  `entity_id` must reference an ACTIVE canonical entity of the matching
  type; every `TeamMembership` must reference an active team and an
  active-or-tombstoned person -- mirrors `people_registry_writer.py::_validate_state`'s
  write-time guard, but read-only and returning structured findings
  instead of raising.
- `find_manager_and_team_hierarchy_cycles` (DIR-06): bounded, cycle-
  detecting walk of `PersonDirectory.manager_entity_id` chains and
  `Team.parent_team_id` chains -- same bounded-walk-with-seen-set pattern
  `people_registry_transaction.py::_validate_transaction`'s hierarchy-
  cycle check already uses for a different record type.
- DIR-07's journal/checkpoint integrity check reuses
  `people_change_journal.py::verify_journal_hash_chain` and
  `people_registry_transaction.py::recover_registry_transactions`
  directly in `kb_checks.py` -- no new pure function was needed here,
  those two are already exactly DIR-07's contract.
- DIR-15's "legacy reference resolves differently from its shadow
  canonical ID" reuses `people_shadow_parity.py::compute_shadow_parity`
  wholesale (a shadow-parity divergence IS this exact defect, by
  construction) -- also no new pure function needed here, wired directly
  in `kb_checks.py`.
- DIR-03 wraps `people_query.py::list_stale_people` directly in
  `kb_checks.py` too -- same v1-placeholder-freshness caveat documented
  there (real "configured freshness SLA" is a still-unbuilt
  `freshness_policy.yaml` `people_registry` section).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.people_directory_schema import PersonDirectory, PersonStatus, Team
from src.core.people_entity_schema import CanonicalEntity, EntityRedirect, EntityStatus
from src.core.people_membership_schema import MembershipStatus, TeamMembership
from src.core.people_namespace_bridge import resolve_ref_to_canonical_entity_id
from src.core.people_query import ConflictQueryEntry


def _normalize(value: str) -> str:
    return value.strip().casefold()


@dataclass(frozen=True, slots=True)
class Dir01Violation:
    kind: str  # "duplicate_entity_id" | "alias_collision"
    key: str
    detail: str


def find_duplicate_identifiers(
    *, entities: tuple[CanonicalEntity, ...], people: tuple[PersonDirectory, ...], teams: tuple[Team, ...]
) -> tuple[Dir01Violation, ...]:
    violations: list[Dir01Violation] = []

    all_ids = [entity.entity_id for entity in entities] + [p.entity_id for p in people if p.entity_id] + [
        t.entity_id for t in teams if t.entity_id
    ]
    normalized_seen: dict[str, str] = {}
    for entity_id in all_ids:
        normalized = _normalize(entity_id)
        if normalized in normalized_seen and normalized_seen[normalized] != entity_id:
            violations.append(
                Dir01Violation(
                    kind="duplicate_entity_id", key=normalized,
                    detail=f"entity_id values {normalized_seen[normalized]!r} and {entity_id!r} collide when normalized.",
                )
            )
        else:
            normalized_seen.setdefault(normalized, entity_id)

    alias_owner: dict[str, str] = {}
    for entity in entities:
        for alias in entity.aliases:
            normalized_alias = _normalize(alias.value)
            if not normalized_alias:
                continue
            owner = alias_owner.get(normalized_alias)
            if owner is not None and owner != entity.entity_id:
                violations.append(
                    Dir01Violation(
                        kind="alias_collision", key=normalized_alias,
                        detail=f"alias {alias.value!r} is bound to both {owner!r} and {entity.entity_id!r}.",
                    )
                )
            else:
                alias_owner.setdefault(normalized_alias, entity.entity_id)

    return tuple(violations)


@dataclass(frozen=True, slots=True)
class Dir02Violation:
    kind: str  # "person_entity_missing" | "team_entity_missing" | "membership_person_missing" | "membership_team_missing"
    entity_id: str
    detail: str


def find_unresolved_references(
    *,
    entities: tuple[CanonicalEntity, ...],
    people: tuple[PersonDirectory, ...],
    teams: tuple[Team, ...],
    memberships: tuple[TeamMembership, ...],
) -> tuple[Dir02Violation, ...]:
    violations: list[Dir02Violation] = []
    active_people = {e.entity_id for e in entities if e.entity_type == "person" and e.status == EntityStatus.ACTIVE}
    active_teams = {e.entity_id for e in entities if e.entity_type == "team" and e.status == EntityStatus.ACTIVE}
    known_people = active_people | {e.entity_id for e in entities if e.entity_type == "person" and e.status == EntityStatus.TOMBSTONED}

    for person in people:
        if person.entity_id and person.entity_id not in active_people:
            violations.append(
                Dir02Violation(
                    kind="person_entity_missing", entity_id=person.entity_id,
                    detail=f"people_directory.yaml record {person.entity_id!r} does not reference an active canonical person entity.",
                )
            )
    for team in teams:
        if team.entity_id and team.entity_id not in active_teams:
            violations.append(
                Dir02Violation(
                    kind="team_entity_missing", entity_id=team.entity_id,
                    detail=f"teams.yaml record {team.entity_id!r} does not reference an active canonical team entity.",
                )
            )
    for membership in memberships:
        if membership.status == MembershipStatus.TOMBSTONED:
            continue
        if membership.person_entity_id not in known_people:
            violations.append(
                Dir02Violation(
                    kind="membership_person_missing", entity_id=membership.membership_id,
                    detail=f"membership {membership.membership_id!r} references unknown/inactive person {membership.person_entity_id!r}.",
                )
            )
        if membership.team_entity_id not in active_teams:
            violations.append(
                Dir02Violation(
                    kind="membership_team_missing", entity_id=membership.membership_id,
                    detail=f"membership {membership.membership_id!r} references unknown/inactive team {membership.team_entity_id!r}.",
                )
            )
    return tuple(violations)


@dataclass(frozen=True, slots=True)
class Dir06Violation:
    kind: str  # "manager_cycle" | "team_parent_cycle"
    entity_id: str
    detail: str


def find_manager_and_team_hierarchy_cycles(
    *, people: tuple[PersonDirectory, ...], teams: tuple[Team, ...]
) -> tuple[Dir06Violation, ...]:
    violations: list[Dir06Violation] = []

    manager_by_id = {p.entity_id: p.manager_entity_id for p in people if p.entity_id}
    bound = len(manager_by_id) + 1
    for person in people:
        if not person.entity_id:
            continue
        seen: set[str] = set()
        current = person.manager_entity_id
        steps = 0
        while current is not None:
            if current in seen or current == person.entity_id:
                violations.append(
                    Dir06Violation(
                        kind="manager_cycle", entity_id=person.entity_id,
                        detail=f"person {person.entity_id!r}'s manager chain contains a cycle through {current!r}.",
                    )
                )
                break
            seen.add(current)
            current = manager_by_id.get(current)
            steps += 1
            if steps > bound:
                violations.append(
                    Dir06Violation(
                        kind="manager_cycle", entity_id=person.entity_id,
                        detail=f"person {person.entity_id!r}'s manager chain exceeds the collection size without terminating.",
                    )
                )
                break

    parent_by_id = {t.entity_id: t.parent_team_id for t in teams if t.entity_id}
    bound = len(parent_by_id) + 1
    for team in teams:
        if not team.entity_id:
            continue
        seen = set()
        current = team.parent_team_id
        steps = 0
        while current is not None:
            if current in seen or current == team.entity_id:
                violations.append(
                    Dir06Violation(
                        kind="team_parent_cycle", entity_id=team.entity_id,
                        detail=f"team {team.entity_id!r}'s parent chain contains a cycle through {current!r}.",
                    )
                )
                break
            seen.add(current)
            current = parent_by_id.get(current)
            steps += 1
            if steps > bound:
                violations.append(
                    Dir06Violation(
                        kind="team_parent_cycle", entity_id=team.entity_id,
                        detail=f"team {team.entity_id!r}'s parent chain exceeds the collection size without terminating.",
                    )
                )
                break

    return tuple(violations)


def _normalize_alias(value: str) -> str:
    return value.strip().casefold()


@dataclass(frozen=True, slots=True)
class Dir04Violation:
    kind: str  # "unresolved" | "no_directory_record" | "inactive_status"
    program_id: str
    alias: str
    detail: str


def find_stakeholder_lifecycle_violations(
    *,
    program_stakeholder_aliases: dict[str, frozenset[str]],
    entities: tuple[CanonicalEntity, ...],
    people: tuple[PersonDirectory, ...],
    redirects: tuple[EntityRedirect, ...] = (),
) -> tuple[Dir04Violation, ...]:
    """DIR-04: for every known program's `ProgramContext.stakeholder_aliases`
    (§7.2's canonical namespace bridge resolves the same way regardless
    of which program the alias came from -- the shared registry is
    single, not per-program), flags an alias that does not resolve to any
    canonical person, one that resolves but has no `people_directory.yaml`
    record ("ambiguous identity," PPL-W5a.5's own established term for
    this exact condition), or one that resolves to a person whose status
    is not `ACTIVE`."""
    people_by_entity_id = {person.entity_id: person for person in people}
    violations: list[Dir04Violation] = []
    for program_id in sorted(program_stakeholder_aliases):
        for alias in sorted(program_stakeholder_aliases[program_id]):
            if not alias:
                continue
            resolution = resolve_ref_to_canonical_entity_id(alias, entities=entities, redirects=redirects)
            if resolution.canonical_entity_id is None:
                violations.append(
                    Dir04Violation(
                        kind="unresolved", program_id=program_id, alias=alias,
                        detail=f"{program_id}: stakeholder alias {alias!r} does not resolve to any canonical person in the shared registry.",
                    )
                )
                continue
            person = people_by_entity_id.get(resolution.canonical_entity_id)
            if person is None:
                violations.append(
                    Dir04Violation(
                        kind="no_directory_record", program_id=program_id, alias=alias,
                        detail=f"{program_id}: stakeholder alias {alias!r} resolves to {resolution.canonical_entity_id!r}, which has no people_directory.yaml record (ambiguous identity).",
                    )
                )
                continue
            if person.status is not PersonStatus.ACTIVE:
                violations.append(
                    Dir04Violation(
                        kind="inactive_status", program_id=program_id, alias=alias,
                        detail=f"{program_id}: stakeholder alias {alias!r} resolves to {resolution.canonical_entity_id!r}, whose status is {person.status.value!r}, not active.",
                    )
                )
    return tuple(violations)


@dataclass(frozen=True, slots=True)
class Dir12Finding:
    conflict_id: str
    entity_id: str
    has_active_accountability_reference: bool
    detail: str


def find_conflict_accountability_findings(
    *,
    open_conflicts: tuple[ConflictQueryEntry, ...],
    all_program_stakeholder_aliases: frozenset[str],
    entities: tuple[CanonicalEntity, ...],
) -> tuple[Dir12Finding, ...]:
    """DIR-12A/DIR-12B: an open conflict "with" an active accountability
    reference (DIR-12A, more urgent -- something currently depends on
    this exact person for RACI/ownership) vs "without" one (DIR-12B,
    still open but nothing currently depends on it). A conflict with no
    `entity_id` (a non-entity-scoped journal record) is not this check's
    concern and is skipped."""
    alias_by_entity_id = {
        entity.entity_id: frozenset(_normalize_alias(alias.value) for alias in entity.aliases) for entity in entities
    }
    normalized_program_aliases = frozenset(_normalize_alias(alias) for alias in all_program_stakeholder_aliases)
    findings: list[Dir12Finding] = []
    for conflict in open_conflicts:
        if conflict.entity_id is None:
            continue
        entity_aliases = alias_by_entity_id.get(conflict.entity_id, frozenset())
        has_reference = bool(entity_aliases & normalized_program_aliases)
        findings.append(
            Dir12Finding(
                conflict_id=conflict.conflict_id, entity_id=conflict.entity_id,
                has_active_accountability_reference=has_reference,
                detail=(
                    f"open conflict {conflict.conflict_id!r} for {conflict.entity_id!r} "
                    f"{'has' if has_reference else 'has no'} active accountability reference."
                ),
            )
        )
    return tuple(findings)
