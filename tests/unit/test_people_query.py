from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.people_change_journal import append_people_conflict_record
from src.core.people_directory_schema import (
    ContactKind,
    ContactPoint,
    ContactStatus,
    FieldVerification,
    PersonDirectory,
    Team,
    TeamKind,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import (
    ENTITIES_SCHEMA_VERSION,
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityStatus,
    write_entities_document,
)
from src.core.people_membership_schema import (
    MembershipStatus,
    TeamMembership,
    write_memberships,
)
from src.core.people_query import (
    find_person,
    find_registry_program_affiliations,
    find_team,
    list_conflicts,
    list_stale_people,
    paginate,
    search_people,
    team_members,
)

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _alias(value: str, *, as_of: datetime = _NOW) -> EntityAlias:
    return EntityAlias(
        value=value, kind="alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=as_of, verified_at=as_of, verified_by_principal="steward",
    )


def _entity(entity_id: str, *, entity_type: str, name: str, aliases: tuple[str, ...] = (), as_of: datetime = _NOW) -> CanonicalEntity:
    return CanonicalEntity(
        workspace_id="ws-1", entity_id=entity_id, entity_type=entity_type, canonical_name=name,
        aliases=tuple(_alias(a, as_of=as_of) for a in aliases), scope="org", created_at=as_of, status=EntityStatus.ACTIVE,
    )


def _seed_registry(knowledge_root: Path) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    entities = (
        _entity("person:alice", entity_type="person", name="Alice Adams", aliases=("alice", "aadams")),
        _entity("person:bob", entity_type="person", name="Bob Brown", aliases=("bob",)),
        _entity("team:platform", entity_type="team", name="Platform Team", aliases=("platform",)),
    )
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version=ENTITIES_SCHEMA_VERSION, entities=entities))

    stale_verified_at = _NOW - timedelta(days=200)
    fresh_verified_at = _NOW - timedelta(days=1)
    people = (
        PersonDirectory(
            entity_id="person:alice", alias="alice", display_name="Alice Adams",
            contacts=(
                ContactPoint(
                    kind=ContactKind.PRIMARY_EMAIL, value="alice@example.com", status=ContactStatus.ACTIVE,
                    valid_from=None, valid_until=None, source="test", source_ref=None,
                    recorded_at=stale_verified_at, verified_at=stale_verified_at, verified_by_principal="steward",
                    delivery_eligible=True,
                ),
            ),
            verifications=(
                FieldVerification(
                    field_name="title", source="test", source_ref=None, observed_at=stale_verified_at,
                    verified_at=stale_verified_at, recorded_at=stale_verified_at, verified_by_principal="steward",
                ),
            ),
        ),
        PersonDirectory(
            entity_id="person:bob", alias="bob", display_name="Bob Brown",
            verifications=(
                FieldVerification(
                    field_name="title", source="test", source_ref=None, observed_at=fresh_verified_at,
                    verified_at=fresh_verified_at, recorded_at=fresh_verified_at, verified_by_principal="steward",
                ),
            ),
        ),
    )
    write_people_directory(knowledge_root / "people_directory.yaml", people)

    teams = (
        Team(entity_id="team:platform", id="platform", name="Platform Team", kind=TeamKind.ORG_TEAM, legacy_programs=("xpf",)),
    )
    write_teams(knowledge_root / "teams.yaml", teams)

    memberships = (
        TeamMembership(
            membership_id="m1", person_entity_id="person:alice", team_entity_id="team:platform", role="member",
            valid_from=_NOW - timedelta(days=30), valid_until=None, source="test", source_ref=None,
            observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE,
        ),
    )
    write_memberships(knowledge_root / "memberships.yaml", memberships)


def test_find_person_resolves_by_bare_alias(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    result = find_person("alice", knowledge_root=knowledge_root)

    assert result is not None
    assert result.entity.entity_id == "person:alice"
    assert result.directory is not None and result.directory.alias == "alice"
    assert len(result.memberships) == 1
    assert result.memberships[0].team_entity_id == "team:platform"


def test_find_person_resolves_by_canonical_ref_form(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    result = find_person("person:aadams", knowledge_root=knowledge_root)

    assert result is not None
    assert result.entity.entity_id == "person:alice"


def test_find_person_returns_none_for_unknown_ref(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    assert find_person("nobody", knowledge_root=knowledge_root) is None


def test_find_person_records_legacy_reference_for_bare_alias_but_not_canonical_ref(tmp_path: Path) -> None:
    """WO-6 (specs/backlog.md, schema-3.0 horizon): a bare-alias lookup is
    the legacy compatibility path and must be counted; an already-canonical
    ref must not be."""
    from src.core.people_legacy_reference_metrics import summarize_legacy_reference_log

    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    find_person("person:alice", knowledge_root=knowledge_root)
    assert summarize_legacy_reference_log(knowledge_root).legacy_reference_count == 0

    find_person("alice", knowledge_root=knowledge_root)
    summary = summarize_legacy_reference_log(knowledge_root)
    assert summary.legacy_reference_count == 1
    assert summary.sample_refs == ("alice",)


def test_find_person_rejects_a_team_ref(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    assert find_person("platform", knowledge_root=knowledge_root) is None


def test_find_team_and_team_members(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    team_result = find_team("platform", knowledge_root=knowledge_root)
    assert team_result is not None
    assert team_result.team is not None and team_result.team.name == "Platform Team"

    members = team_members("platform", knowledge_root=knowledge_root)
    assert members is not None
    assert {m.person_entity_id for m in members.members} == {"person:alice"}


def test_team_members_as_of_excludes_a_membership_before_its_valid_from(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    members = team_members("platform", knowledge_root=knowledge_root, as_of=_NOW - timedelta(days=60))

    assert members is not None
    assert members.members == ()


def test_search_people_ranks_exact_over_prefix_over_substring(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    candidates = search_people("ali", knowledge_root=knowledge_root)

    assert candidates[0].alias == "alice"
    assert candidates[0].match_kind == "prefix"


def test_search_people_exact_alias_match_scores_highest(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    candidates = search_people("alice", knowledge_root=knowledge_root)

    assert candidates[0].alias == "alice"
    assert candidates[0].match_kind == "exact"
    assert candidates[0].score == 1.0


def test_search_people_empty_query_returns_nothing(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    assert search_people("   ", knowledge_root=knowledge_root) == ()


def test_list_stale_people_flags_only_records_older_than_threshold(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    stale = list_stale_people(knowledge_root=knowledge_root, as_of=_NOW)

    stale_aliases = {entry.alias for entry in stale}
    assert "alice" in stale_aliases
    assert "bob" not in stale_aliases


def test_list_stale_people_respects_custom_freshness_window(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    stale = list_stale_people(knowledge_root=knowledge_root, as_of=_NOW, freshness_days=1000)

    assert stale == ()


def test_list_conflicts_open_vs_resolved(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)
    earlier = _NOW - timedelta(hours=2)
    later = _NOW - timedelta(hours=1)

    append_people_conflict_record(
        knowledge_root, workspace_id="ws-1", conflict_id="c-1", decision="quarantined",
        authenticated_principal="steward", reason="alias collision", entity_id="person:alice", as_of=earlier,
    )
    append_people_conflict_record(
        knowledge_root, workspace_id="ws-1", conflict_id="c-2", decision="quarantined",
        authenticated_principal="steward", reason="alias collision", entity_id="person:bob", as_of=earlier,
    )
    append_people_conflict_record(
        knowledge_root, workspace_id="ws-1", conflict_id="c-1-resolved", decision="merge",
        authenticated_principal="steward", reason="resolved via merge", entity_id="person:alice", as_of=later,
    )

    all_conflicts = list_conflicts(knowledge_root=knowledge_root)
    assert {c.conflict_id for c in all_conflicts} == {"c-1", "c-2"}

    open_conflicts = list_conflicts(knowledge_root=knowledge_root, status="open")
    assert {c.conflict_id for c in open_conflicts} == {"c-2"}

    resolved_conflicts = list_conflicts(knowledge_root=knowledge_root, status="resolved")
    assert {c.conflict_id for c in resolved_conflicts} == {"c-1"}


def test_list_conflicts_with_no_journal_returns_empty(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    assert list_conflicts(knowledge_root=knowledge_root) == ()


def test_paginate_returns_bounded_page_and_next_cursor() -> None:
    items = tuple(range(10))

    page, next_cursor = paginate(items, limit=3, cursor=None)
    assert page == (0, 1, 2)
    assert next_cursor == "3"

    page2, next_cursor2 = paginate(items, limit=3, cursor=next_cursor)
    assert page2 == (3, 4, 5)
    assert next_cursor2 == "6"


def test_paginate_last_page_has_no_next_cursor() -> None:
    items = tuple(range(5))

    page, next_cursor = paginate(items, limit=10, cursor=None)

    assert page == items
    assert next_cursor is None


def test_find_registry_program_affiliations_emits_legacy_team_program_edges(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    edges = find_registry_program_affiliations("alice", knowledge_root=knowledge_root)

    assert len(edges) == 1
    assert edges[0].program_id == "xpf"
    assert edges[0].relation_type == "legacy_team_program"
    assert edges[0].alias == "alice"


def test_find_registry_program_affiliations_returns_empty_for_unknown_ref(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    assert find_registry_program_affiliations("nobody", knowledge_root=knowledge_root) == ()


def test_find_registry_program_affiliations_returns_empty_when_team_has_no_legacy_programs(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    # Overwrite the fixture team with no legacy_programs to prove the empty case.
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform Team", kind=TeamKind.ORG_TEAM),))

    assert find_registry_program_affiliations("alice", knowledge_root=knowledge_root) == ()
