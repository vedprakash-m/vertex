from __future__ import annotations

from datetime import datetime, timezone

from src.core.people_directory_schema import PersonDirectory, PersonStatus, Team, TeamKind
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntityAlias, EntityStatus
from src.core.people_membership_schema import MembershipStatus, TeamMembership
from src.core.people_query import ConflictQueryEntry
from src.core.people_registry_directory_checks import (
    find_conflict_accountability_findings,
    find_duplicate_identifiers,
    find_manager_and_team_hierarchy_cycles,
    find_stakeholder_lifecycle_violations,
    find_unresolved_references,
)

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
    )


def _entity(entity_id: str, *, entity_type: str = "person", aliases: tuple[str, ...] = (), status: EntityStatus = EntityStatus.ACTIVE) -> CanonicalEntity:
    return CanonicalEntity(
        workspace_id="ws-1", entity_id=entity_id, entity_type=entity_type, canonical_name=entity_id,
        aliases=tuple(_alias(a) for a in aliases), scope="org", created_at=_NOW, status=status,
    )


def test_find_duplicate_identifiers_detects_normalized_entity_id_collision() -> None:
    entities = (_entity("person:Alice"), _entity("person:alice", entity_type="team"))

    violations = find_duplicate_identifiers(entities=entities, people=(), teams=())

    assert any(v.kind == "duplicate_entity_id" for v in violations)


def test_find_duplicate_identifiers_detects_alias_collision_across_entities() -> None:
    entities = (
        _entity("person:alice", aliases=("shared",)),
        _entity("person:bob", aliases=("shared",)),
    )

    violations = find_duplicate_identifiers(entities=entities, people=(), teams=())

    assert any(v.kind == "alias_collision" and v.key == "shared" for v in violations)


def test_find_duplicate_identifiers_clean_data_has_no_violations() -> None:
    entities = (_entity("person:alice", aliases=("alice",)), _entity("person:bob", aliases=("bob",)))

    violations = find_duplicate_identifiers(entities=entities, people=(), teams=())

    assert violations == ()


def test_find_unresolved_references_flags_person_without_active_entity() -> None:
    people = (PersonDirectory(entity_id="person:ghost", alias="ghost"),)

    violations = find_unresolved_references(entities=(), people=people, teams=(), memberships=())

    assert any(v.kind == "person_entity_missing" and v.entity_id == "person:ghost" for v in violations)


def test_find_unresolved_references_accepts_person_with_active_entity() -> None:
    entities = (_entity("person:alice"),)
    people = (PersonDirectory(entity_id="person:alice", alias="alice"),)

    violations = find_unresolved_references(entities=entities, people=people, teams=(), memberships=())

    assert violations == ()


def test_find_unresolved_references_flags_membership_with_unknown_person() -> None:
    entities = (_entity("team:platform", entity_type="team"),)
    memberships = (
        TeamMembership(
            membership_id="m1", person_entity_id="person:ghost", team_entity_id="team:platform", role="member",
            valid_from=None, valid_until=None, source="test", source_ref=None,
            observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE,
        ),
    )

    violations = find_unresolved_references(entities=entities, people=(), teams=(), memberships=memberships)

    assert any(v.kind == "membership_person_missing" for v in violations)


def test_find_unresolved_references_ignores_tombstoned_memberships() -> None:
    memberships = (
        TeamMembership(
            membership_id="m1", person_entity_id="person:ghost", team_entity_id="team:ghost", role="member",
            valid_from=None, valid_until=None, source="test", source_ref=None,
            observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.TOMBSTONED,
        ),
    )

    violations = find_unresolved_references(entities=(), people=(), teams=(), memberships=memberships)

    assert violations == ()


def test_find_manager_and_team_hierarchy_cycles_detects_a_two_hop_manager_cycle() -> None:
    people = (
        PersonDirectory(entity_id="person:a", alias="a", manager_entity_id="person:b"),
        PersonDirectory(entity_id="person:b", alias="b", manager_entity_id="person:a"),
    )

    violations = find_manager_and_team_hierarchy_cycles(people=people, teams=())

    assert any(v.kind == "manager_cycle" for v in violations)


def test_find_manager_and_team_hierarchy_cycles_accepts_a_clean_chain() -> None:
    people = (
        PersonDirectory(entity_id="person:a", alias="a", manager_entity_id="person:b"),
        PersonDirectory(entity_id="person:b", alias="b", manager_entity_id=None),
    )

    violations = find_manager_and_team_hierarchy_cycles(people=people, teams=())

    assert violations == ()


def test_find_manager_and_team_hierarchy_cycles_detects_a_team_parent_cycle() -> None:
    teams = (
        Team(entity_id="team:a", id="a", name="A", kind=TeamKind.ORG_TEAM, parent_team_id="team:b"),
        Team(entity_id="team:b", id="b", name="B", kind=TeamKind.ORG_TEAM, parent_team_id="team:a"),
    )

    violations = find_manager_and_team_hierarchy_cycles(people=(), teams=teams)

    assert any(v.kind == "team_parent_cycle" for v in violations)


def test_find_stakeholder_lifecycle_violations_flags_unresolved_alias() -> None:
    violations = find_stakeholder_lifecycle_violations(
        program_stakeholder_aliases={"acme": frozenset({"ghost"})}, entities=(), people=(),
    )

    assert len(violations) == 1
    assert violations[0].kind == "unresolved"
    assert violations[0].program_id == "acme"


def test_find_stakeholder_lifecycle_violations_flags_ambiguous_identity_with_no_directory_record() -> None:
    entities = (_entity("person:jdoe", aliases=("jdoe",)),)

    violations = find_stakeholder_lifecycle_violations(
        program_stakeholder_aliases={"acme": frozenset({"jdoe"})}, entities=entities, people=(),
    )

    assert len(violations) == 1
    assert violations[0].kind == "no_directory_record"


def test_find_stakeholder_lifecycle_violations_flags_departed_person() -> None:
    entities = (_entity("person:jdoe", aliases=("jdoe",)),)
    people = (PersonDirectory(entity_id="person:jdoe", alias="jdoe", status=PersonStatus.DEPARTED),)

    violations = find_stakeholder_lifecycle_violations(
        program_stakeholder_aliases={"acme": frozenset({"jdoe"})}, entities=entities, people=people,
    )

    assert len(violations) == 1
    assert violations[0].kind == "inactive_status"


def test_find_stakeholder_lifecycle_violations_accepts_an_active_person() -> None:
    entities = (_entity("person:jdoe", aliases=("jdoe",)),)
    people = (PersonDirectory(entity_id="person:jdoe", alias="jdoe", status=PersonStatus.ACTIVE),)

    violations = find_stakeholder_lifecycle_violations(
        program_stakeholder_aliases={"acme": frozenset({"jdoe"})}, entities=entities, people=people,
    )

    assert violations == ()


def _conflict(conflict_id: str, entity_id: str | None) -> ConflictQueryEntry:
    return ConflictQueryEntry(
        conflict_id=conflict_id, decision="conflict", entity_id=entity_id, reason="test",
        recorded_at=_NOW, sequence=1, status="open",
    )


def test_find_conflict_accountability_findings_true_when_entity_alias_is_referenced() -> None:
    entities = (_entity("person:jdoe", aliases=("jdoe",)),)

    findings = find_conflict_accountability_findings(
        open_conflicts=(_conflict("c1", "person:jdoe"),),
        all_program_stakeholder_aliases=frozenset({"jdoe"}),
        entities=entities,
    )

    assert len(findings) == 1
    assert findings[0].has_active_accountability_reference is True


def test_find_conflict_accountability_findings_false_when_entity_alias_is_not_referenced() -> None:
    entities = (_entity("person:jdoe", aliases=("jdoe",)),)

    findings = find_conflict_accountability_findings(
        open_conflicts=(_conflict("c1", "person:jdoe"),),
        all_program_stakeholder_aliases=frozenset({"someone-else"}),
        entities=entities,
    )

    assert len(findings) == 1
    assert findings[0].has_active_accountability_reference is False


def test_find_conflict_accountability_findings_skips_conflicts_without_entity_id() -> None:
    findings = find_conflict_accountability_findings(
        open_conflicts=(_conflict("c1", None),), all_program_stakeholder_aliases=frozenset(), entities=(),
    )

    assert findings == ()
