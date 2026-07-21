from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.audience_scope_resolver import (
    CANDIDATE_SOURCE_INCLUDE_PEOPLE,
    CANDIDATE_SOURCE_TEAM_EXPANSION,
    resolve_audience_scope,
    resolve_audience_scope_with_exclusions,
)
from src.core.audience_scopes import AudienceScope
from src.core.people_directory_schema import Team, TeamKind, TeamStatus, write_teams
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
    )


def _seed(knowledge_root: Path, *, team_kind: TeamKind = TeamKind.ORG_TEAM) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                CanonicalEntity(workspace_id="ws", entity_id="team:platform", entity_type="team", canonical_name="Platform", aliases=(_alias("platform"),), scope="org", created_at=_NOW),
                CanonicalEntity(workspace_id="ws", entity_id="person:jdoe", entity_type="person", canonical_name="Jane", aliases=(_alias("jdoe"),), scope="org", created_at=_NOW),
                CanonicalEntity(workspace_id="ws", entity_id="person:asmith", entity_type="person", canonical_name="Amy", aliases=(_alias("asmith"),), scope="org", created_at=_NOW),
                CanonicalEntity(workspace_id="ws", entity_id="person:bwong", entity_type="person", canonical_name="Bob", aliases=(_alias("bwong"),), scope="org", created_at=_NOW),
            ),
        ),
    )
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=team_kind, status=TeamStatus.ACTIVE),))
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(membership_id="m1", person_entity_id="person:jdoe", team_entity_id="team:platform", role="member", valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE),
            TeamMembership(membership_id="m2", person_entity_id="person:asmith", team_entity_id="team:platform", role="lead", valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE),
            TeamMembership(membership_id="m3", person_entity_id="person:bwong", team_entity_id="team:platform", role="member", valid_from=_NOW, valid_until=_NOW, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.TOMBSTONED),
        ),
    )


def test_org_team_expands_by_default(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="engineering_hygiene", team_entity_ids=("team:platform",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root)

    person_ids = {c.person_entity_id for c in candidates}
    assert person_ids == {"person:jdoe", "person:asmith"}  # bwong is tombstoned, excluded
    assert all(c.source == CANDIDATE_SOURCE_TEAM_EXPANSION for c in candidates)


def test_program_group_team_does_not_expand_without_opt_in(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, team_kind=TeamKind.PROGRAM_GROUP)
    scope = AudienceScope(id="s", team_entity_ids=("team:platform",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root)

    assert candidates == ()


def test_program_group_team_expands_with_explicit_opt_in(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, team_kind=TeamKind.PROGRAM_GROUP)
    scope = AudienceScope(id="s", team_entity_ids=("team:platform",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root, allow_non_org_team_expansion=True)

    assert {c.person_entity_id for c in candidates} == {"person:jdoe", "person:asmith"}


def test_membership_roles_filters_the_roster(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="s", team_entity_ids=("team:platform",), membership_roles=("lead",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root)

    assert {c.person_entity_id for c in candidates} == {"person:asmith"}


def test_include_people_adds_a_candidate_outside_any_team(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="s", include_person_entity_ids=("person:bwong",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root)

    assert len(candidates) == 1
    assert candidates[0].person_entity_id == "person:bwong"
    assert candidates[0].source == CANDIDATE_SOURCE_INCLUDE_PEOPLE


def test_exclude_people_removes_a_team_expanded_candidate(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="s", team_entity_ids=("team:platform",), exclude_person_entity_ids=("person:jdoe",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root)

    assert {c.person_entity_id for c in candidates} == {"person:asmith"}


def test_exclude_wins_over_include_for_the_same_person(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="s", include_person_entity_ids=("person:bwong",), exclude_person_entity_ids=("person:bwong",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root)

    assert candidates == ()


def test_unknown_team_entity_id_is_skipped_not_an_error(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="s", team_entity_ids=("team:nonexistent",))

    candidates = resolve_audience_scope(scope, knowledge_root=knowledge_root)

    assert candidates == ()


def test_with_exclusions_reports_who_was_actually_removed(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="s", team_entity_ids=("team:platform",), exclude_person_entity_ids=("person:jdoe",))

    candidates, excluded = resolve_audience_scope_with_exclusions(scope, knowledge_root=knowledge_root)

    assert {c.person_entity_id for c in candidates} == {"person:asmith"}
    assert excluded == ("person:jdoe",)


def test_with_exclusions_does_not_report_a_no_op_exclusion(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(id="s", team_entity_ids=("team:platform",), exclude_person_entity_ids=("person:bwong",))

    candidates, excluded = resolve_audience_scope_with_exclusions(scope, knowledge_root=knowledge_root)

    assert {c.person_entity_id for c in candidates} == {"person:jdoe", "person:asmith"}
    assert excluded == ()
