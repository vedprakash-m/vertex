"""specs/people.md §7.4, PPL-W5a.2: audience-scope resolution engine.

Expands an `AudienceScope` (PPL-W5a.1) into candidate canonical person
entity IDs. Reuses the registry's existing query surfaces directly rather
than re-walking membership data: `people_query.find_team`/`team_members`
(PPL-W3.1) for team lookup and roster, `TeamKind` (`people_directory_schema.py`)
for the org_team gate.

Resolution order, matching §7.4's own text:
1. Team expansion -- only `org_team`-kind teams expand by default
   ("Only `org_team` memberships may expand audiences by default.
   `program_group`/`virtual_group` expansion requires an explicit
   edition opt-in" -- `allow_non_org_team_expansion` is that opt-in,
   threaded from the caller's edition config, not decided here).
   `membership_roles` (if non-empty) filters the expanded roster; role
   strings are already normalized at write time by
   `people_membership_schema.py::observe_membership`, so a case-folded
   direct comparison is correct without re-normalizing.
2. `include_people` adds explicit candidates outside any team.
3. `exclude_people` removes candidates -- applied LAST, so an exclusion
   always wins over both team expansion and an explicit include. This
   is the conservative choice: for an audience list, "accidentally
   still includes someone the operator explicitly excluded" is a worse
   failure than the reverse, and it matches this phase's own §7.4
   precedence chain (PPL-W5a.5), where "explicit exclusions" is stage
   one -- ordering exclusion last here keeps this resolver consistent
   with that later, more complete pipeline rather than contradicting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.audience_scopes import AudienceScope
from src.core.people_directory_schema import TeamKind
from src.core.people_membership_schema import MembershipStatus
from src.core.people_query import find_team, team_members

CANDIDATE_SOURCE_TEAM_EXPANSION = "team_expansion"
CANDIDATE_SOURCE_INCLUDE_PEOPLE = "include_people"


@dataclass(frozen=True, slots=True)
class AudienceCandidate:
    person_entity_id: str
    source: str  # CANDIDATE_SOURCE_TEAM_EXPANSION | CANDIDATE_SOURCE_INCLUDE_PEOPLE
    source_team_entity_id: str | None = None


def _build_pre_exclusion_candidates(
    scope: AudienceScope, *, knowledge_root: Path, allow_non_org_team_expansion: bool,
) -> dict[str, AudienceCandidate]:
    candidates: dict[str, AudienceCandidate] = {}
    allowed_roles = {role.casefold() for role in scope.membership_roles}

    for team_entity_id in scope.team_entity_ids:
        team_result = find_team(team_entity_id, knowledge_root=knowledge_root)
        if team_result is None or team_result.team is None:
            continue
        if team_result.team.kind is not TeamKind.ORG_TEAM and not allow_non_org_team_expansion:
            continue
        members_result = team_members(team_entity_id, knowledge_root=knowledge_root)
        if members_result is None:
            continue
        for membership in members_result.members:
            if membership.status is not MembershipStatus.ACTIVE:
                continue
            if allowed_roles and (membership.role or "unknown").casefold() not in allowed_roles:
                continue
            candidates[membership.person_entity_id] = AudienceCandidate(
                person_entity_id=membership.person_entity_id,
                source=CANDIDATE_SOURCE_TEAM_EXPANSION,
                source_team_entity_id=team_entity_id,
            )

    for person_entity_id in scope.include_person_entity_ids:
        candidates.setdefault(
            person_entity_id,
            AudienceCandidate(person_entity_id=person_entity_id, source=CANDIDATE_SOURCE_INCLUDE_PEOPLE),
        )

    return candidates


def resolve_audience_scope(
    scope: AudienceScope,
    *,
    knowledge_root: Path,
    allow_non_org_team_expansion: bool = False,
) -> tuple[AudienceCandidate, ...]:
    candidates = _build_pre_exclusion_candidates(scope, knowledge_root=knowledge_root, allow_non_org_team_expansion=allow_non_org_team_expansion)
    for person_entity_id in scope.exclude_person_entity_ids:
        candidates.pop(person_entity_id, None)
    return tuple(candidates.values())


def resolve_audience_scope_with_exclusions(
    scope: AudienceScope,
    *,
    knowledge_root: Path,
    allow_non_org_team_expansion: bool = False,
) -> tuple[tuple[AudienceCandidate, ...], tuple[str, ...]]:
    """PPL-W5a.4: the same resolution as `resolve_audience_scope`, but also
    reports WHICH candidates `exclude_people` actually removed (only
    entries that were real candidates before exclusion -- an
    `exclude_people` entry that never matched any candidate is not
    reported, since nothing was actually excluded by it)."""
    candidates = _build_pre_exclusion_candidates(scope, knowledge_root=knowledge_root, allow_non_org_team_expansion=allow_non_org_team_expansion)
    excluded = tuple(
        person_entity_id for person_entity_id in scope.exclude_person_entity_ids if person_entity_id in candidates
    )
    for person_entity_id in excluded:
        candidates.pop(person_entity_id, None)
    return tuple(candidates.values()), excluded
