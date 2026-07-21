from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.audience_manifest import DISPOSITION_EXCLUDED_EXPLICIT, DISPOSITION_EXCLUDED_FRESHNESS, DISPOSITION_INCLUDED, build_audience_manifest
from src.core.audience_scopes import AudienceScope
from src.core.people_directory_schema import (
    ContactKind,
    ContactPoint,
    ContactStatus,
    PersonDirectory,
    PersonStatus,
    Team,
    TeamKind,
    TeamStatus,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
    )


def _contact(*, age_days: int) -> ContactPoint:
    verified_at = _NOW - timedelta(days=age_days)
    return ContactPoint(
        kind=ContactKind.PRIMARY_EMAIL, value="x@example.com", status=ContactStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=verified_at, verified_at=verified_at, verified_by_principal="steward",
        delivery_eligible=True,
    )


def _seed(knowledge_root: Path) -> None:
    """team:platform has three members: jdoe (fresh), asmith (stale
    contact -- excluded by freshness), bwong (present but named in
    exclude_people -- excluded explicitly)."""
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
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM, status=TeamStatus.ACTIVE),))
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(membership_id="m1", person_entity_id="person:jdoe", team_entity_id="team:platform", role="member", valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE),
            TeamMembership(membership_id="m2", person_entity_id="person:asmith", team_entity_id="team:platform", role="member", valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE),
            TeamMembership(membership_id="m3", person_entity_id="person:bwong", team_entity_id="team:platform", role="member", valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE),
        ),
    )
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (
            PersonDirectory(entity_id="person:jdoe", alias="jdoe", status=PersonStatus.ACTIVE, contacts=(_contact(age_days=1),)),
            PersonDirectory(entity_id="person:asmith", alias="asmith", status=PersonStatus.ACTIVE, contacts=(_contact(age_days=100),)),
            PersonDirectory(entity_id="person:bwong", alias="bwong", status=PersonStatus.ACTIVE, contacts=(_contact(age_days=1),)),
        ),
    )


def test_manifest_names_included_freshness_excluded_and_explicit_excluded(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope = AudienceScope(
        id="engineering_hygiene", team_entity_ids=("team:platform",),
        require_verified_within_days=30, exclude_person_entity_ids=("person:bwong",),
    )

    manifest = build_audience_manifest((scope,), program_id="acme", knowledge_root=knowledge_root, as_of=_NOW)

    by_person = {entry.person_entity_id: entry for entry in manifest.entries}
    assert by_person["person:jdoe"].disposition == DISPOSITION_INCLUDED
    assert by_person["person:asmith"].disposition == DISPOSITION_EXCLUDED_FRESHNESS
    assert "contact:" in by_person["person:asmith"].reason
    assert by_person["person:bwong"].disposition == DISPOSITION_EXCLUDED_EXPLICIT
    assert manifest.included_person_entity_ids == ("person:jdoe",)
    assert manifest.program_id == "acme"
    assert manifest.generated_at == _NOW


def test_manifest_is_empty_for_no_scopes(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    manifest = build_audience_manifest((), program_id="acme", knowledge_root=knowledge_root, as_of=_NOW)

    assert manifest.entries == ()
    assert manifest.included_person_entity_ids == ()


def test_manifest_combines_multiple_scopes(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    scope_a = AudienceScope(id="a", team_entity_ids=("team:platform",), require_verified_within_days=30, exclude_person_entity_ids=("person:bwong",))
    scope_b = AudienceScope(id="b", include_person_entity_ids=("person:bwong",))

    manifest = build_audience_manifest((scope_a, scope_b), program_id="acme", knowledge_root=knowledge_root, as_of=_NOW)

    scope_ids_for_bwong = {entry.scope_id: entry.disposition for entry in manifest.entries if entry.person_entity_id == "person:bwong"}
    assert scope_ids_for_bwong == {"a": DISPOSITION_EXCLUDED_EXPLICIT, "b": DISPOSITION_INCLUDED}
