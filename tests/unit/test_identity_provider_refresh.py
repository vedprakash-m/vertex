from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.identity_provider_refresh import refresh_people_from_provider
from src.core.people_change_journal import (
    STREAM_PEOPLE_CHANGES,
    STREAM_PEOPLE_CONFLICTS,
    STREAM_PEOPLE_REFRESH_TELEMETRY,
    find_refresh_telemetry_record,
    read_journal_records,
)
from src.core.people_directory_schema import PersonDirectory, PersonStatus, Team, TeamKind, TeamStatus, load_people_directory, load_teams, write_teams
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, load_entities_document, write_entities_document
from src.core.people_membership_schema import MembershipStatus, TeamMembership, read_all_memberships, write_memberships
from src.core.people_registry_identity import bootstrap_registry_identity
from src.core.people_registry_modes import set_registry_flag
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction
from src.core.people_directory_schema import write_people_directory

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="synthetic_fixture", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="test-principal",
    )


def _seed_active_registry(programs_root: Path, *, provider_refresh_enabled: bool = True) -> Path:
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    entities = EntitiesDocument(
        schema_version="2.0",
        entities=(
            CanonicalEntity(
                workspace_id="workspace:synthetic", entity_id="person:jdoe", entity_type="person",
                canonical_name="Jane Doe", aliases=(_alias("jdoe"),), scope="org", created_at=_NOW,
            ),
        ),
    )
    person = PersonDirectory(entity_id="person:jdoe", alias="jdoe", display_name="Jane", title="Old Title", status=PersonStatus.ACTIVE)

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", entities)
        write_people_directory(staged_dir / "people_directory.yaml", (person,))

    def validate_staged(staged_dir: Path) -> None:
        assert load_entities_document(staged_dir / "entities.yaml") is not None
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None

    prepared = prepare_registry_files_transaction(
        knowledge_root, ("entities.yaml", "people_directory.yaml"), owner="test-principal",
        write_staged_files=write_staged, validate_staged_files=validate_staged, as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)
    if provider_refresh_enabled:
        set_registry_flag(knowledge_root, "provider_refresh_enabled", True, actor="test-principal")
    return knowledge_root


def _write_provider_config(knowledge_root: Path, *, enabled: bool = True) -> None:
    (knowledge_root / "identity_providers.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            "providers:\n"
            '  - name: "acme_directory_export"\n'
            '    provider_type: "local_directory_export"\n'
            '    tenant_id: "acme-tenant"\n'
            '    capability_contract_version: "1.0"\n'
            "    allowed_fields:\n"
            "      - display_name\n"
            "      - title\n"
            "      - department\n"
            "      - contacts\n"
            f"    enabled: {str(enabled).lower()}\n"
        ),
        encoding="utf-8",
    )


def _write_export_csv(tmp_path: Path, *, title: str = "New Title") -> Path:
    path = tmp_path / "export.csv"
    path.write_text(
        "alias,display_name,title,department,manager_alias,email,teams\n"
        f"jdoe,Jane Doe,{title},Platform,mgr1,jdoe@example.com,\n",
        encoding="utf-8",
    )
    return path


def _seed_team_fixture(programs_root: Path, knowledge_root: Path) -> None:
    """Extends `_seed_active_registry`'s jdoe person with a `team:platform`
    canonical team, an `asmith` second person, and jdoe's existing ACTIVE
    membership in platform -- so a membership-refresh diff has something
    real to add and remove."""
    entities_document = load_entities_document(knowledge_root / "entities.yaml")
    entities = entities_document.entities + (
        CanonicalEntity(
            workspace_id="workspace:synthetic", entity_id="team:platform", entity_type="team",
            canonical_name="Platform", aliases=(_alias("platform"),), scope="org", created_at=_NOW,
        ),
        CanonicalEntity(
            workspace_id="workspace:synthetic", entity_id="person:asmith", entity_type="person",
            canonical_name="Amy Smith", aliases=(_alias("asmith"),), scope="org", created_at=_NOW,
        ),
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=entities))
        people = load_people_directory(knowledge_root / "people_directory.yaml").people + (
            PersonDirectory(entity_id="person:asmith", alias="asmith", display_name="Amy", status=PersonStatus.ACTIVE),
        )
        write_people_directory(staged_dir / "people_directory.yaml", people)
        write_teams(staged_dir / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM, status=TeamStatus.ACTIVE),))
        write_memberships(
            staged_dir / "memberships.yaml",
            (
                TeamMembership(
                    membership_id="m1", person_entity_id="person:jdoe", team_entity_id="team:platform", role="member",
                    valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW,
                    status=MembershipStatus.ACTIVE,
                ),
            ),
        )

    def validate_staged(staged_dir: Path) -> None:
        assert load_entities_document(staged_dir / "entities.yaml") is not None
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None
        assert load_teams(staged_dir / "teams.yaml") is not None

    prepared = prepare_registry_files_transaction(
        knowledge_root, ("entities.yaml", "people_directory.yaml", "teams.yaml", "memberships.yaml"), owner="test-principal",
        write_staged_files=write_staged, validate_staged_files=validate_staged, as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def _write_team_export_csv(tmp_path: Path, *, jdoe_teams: str = "", asmith_teams: str = "platform") -> Path:
    path = tmp_path / "team_export.csv"
    path.write_text(
        "alias,display_name,title,department,manager_alias,email,teams\n"
        f"jdoe,Jane Doe,Old Title,Platform,,jdoe@example.com,{jdoe_teams}\n"
        f"asmith,Amy Smith,EM,Growth,,asmith@example.com,{asmith_teams}\n",
        encoding="utf-8",
    )
    return path


def test_kill_switch_off_is_a_true_no_op(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root, provider_refresh_enabled=False)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path)

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
    )

    assert result.kill_switch_engaged is True
    assert result.accepted == () and result.write_result is None
    person = load_people_directory(knowledge_root / "people_directory.yaml").people[0]
    assert person.title == "Old Title"


def test_preview_mode_makes_no_write(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path)

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test", apply=False, as_of=_NOW,
    )

    assert len(result.accepted) >= 1
    person = load_people_directory(knowledge_root / "people_directory.yaml").people[0]
    assert person.title == "Old Title"


def test_apply_writes_accepted_fields_through_the_canonical_writer(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path, title="New Title")

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test refresh", apply=True, as_of=_NOW,
    )

    assert result.write_result is not None
    assert result.write_result.transaction_id is not None
    person = load_people_directory(knowledge_root / "people_directory.yaml").people[0]
    assert person.title == "New Title"
    assert person.department == "Platform"
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    assert any(record.get("source") == "provider_refresh" for record in records)


def test_manager_alias_is_unresolved_and_never_written(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    (knowledge_root / "identity_providers.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            "providers:\n"
            '  - name: "acme_directory_export"\n'
            '    provider_type: "local_directory_export"\n'
            '    tenant_id: "acme-tenant"\n'
            '    capability_contract_version: "1.0"\n'
            "    allowed_fields:\n"
            "      - display_name\n"
            "      - manager_alias\n"
            "    enabled: true\n"
        ),
        encoding="utf-8",
    )
    export_path = _write_export_csv(tmp_path)

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
    )

    assert len(result.unresolved) == 1
    assert result.unresolved[0].field_name == "manager_alias"


def test_low_confidence_quarantines_and_does_not_write(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    (knowledge_root / "policies").mkdir(exist_ok=True)
    (knowledge_root / "policies" / "identity_source_authority.yaml").write_text(
        "identity_source_authority_override:\n  auto_accept_confidence_threshold: 2.0\n", encoding="utf-8",
    )
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path, title="New Title")

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
    )

    assert result.accepted == ()
    assert len(result.quarantined) >= 1
    person = load_people_directory(knowledge_root / "people_directory.yaml").people[0]
    assert person.title == "Old Title"
    conflicts = read_journal_records(knowledge_root, STREAM_PEOPLE_CONFLICTS)
    assert any(record.get("decision") == "quarantined" for record in conflicts)


def test_unknown_provider_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path)

    with pytest.raises(ConfigError, match="not configured"):
        refresh_people_from_provider(
            programs_root=programs_root, provider_name="nonexistent", person_refs=("jdoe",),
            import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
        )


def test_disabled_provider_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root, enabled=False)
    export_path = _write_export_csv(tmp_path)

    with pytest.raises(ConfigError, match="not enabled"):
        refresh_people_from_provider(
            programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
            import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
        )


def test_unresolvable_person_ref_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path)

    with pytest.raises(ConfigError, match="must resolve to exactly one"):
        refresh_people_from_provider(
            programs_root=programs_root, provider_name="acme_directory_export", person_refs=("ghost",),
            import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
        )


def test_apply_produces_exactly_one_retrievable_telemetry_record(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path, title="New Title")

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
    )

    records = read_journal_records(knowledge_root, STREAM_PEOPLE_REFRESH_TELEMETRY)
    assert len(records) == 1
    assert records[0]["refresh_run_id"] == result.refresh_run_id
    assert records[0]["provider"] == "acme_directory_export"
    assert records[0]["requested_count"] == 1
    assert records[0]["accepted_count"] == len(result.accepted)
    assert records[0]["kill_switch_engaged"] is False
    assert records[0]["authenticated_principal"] == "steward@example.com"

    found = find_refresh_telemetry_record(knowledge_root, refresh_run_id=result.refresh_run_id)
    assert found is not None


def test_preview_produces_no_telemetry_record(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path)

    refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test", apply=False, as_of=_NOW,
    )

    assert read_journal_records(knowledge_root, STREAM_PEOPLE_REFRESH_TELEMETRY) == ()


def test_kill_switch_off_produces_no_telemetry_record(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root, provider_refresh_enabled=False)
    _write_provider_config(knowledge_root)
    export_path = _write_export_csv(tmp_path)

    refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
        import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
    )

    assert read_journal_records(knowledge_root, STREAM_PEOPLE_REFRESH_TELEMETRY) == ()


def test_missing_import_file_option_raises_for_local_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _write_provider_config(knowledge_root)

    with pytest.raises(ConfigError, match="--import-file"):
        refresh_people_from_provider(
            programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",),
            import_file=None, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
        )


def test_team_refresh_complete_snapshot_adds_and_removes_memberships(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _seed_team_fixture(programs_root, knowledge_root)
    _write_provider_config(knowledge_root)
    export_path = _write_team_export_csv(tmp_path, jdoe_teams="", asmith_teams="platform")

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=(), team_refs=("platform",),
        import_file=export_path, actor="steward@example.com", reason="membership sync", apply=True, as_of=_NOW,
    )

    assert len(result.team_membership_diffs) == 1
    diff = result.team_membership_diffs[0]
    assert diff.complete is True
    assert diff.added_person_aliases == ("asmith",)
    assert diff.removed_person_aliases == ("jdoe",)

    memberships = read_all_memberships(knowledge_root)
    jdoe_active = [m for m in memberships if m.person_entity_id == "person:jdoe" and m.team_entity_id == "team:platform" and m.status == MembershipStatus.ACTIVE]
    asmith_active = [m for m in memberships if m.person_entity_id == "person:asmith" and m.team_entity_id == "team:platform" and m.status == MembershipStatus.ACTIVE]
    assert jdoe_active == []
    assert len(asmith_active) == 1


def test_team_refresh_preview_makes_no_membership_write(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _seed_team_fixture(programs_root, knowledge_root)
    _write_provider_config(knowledge_root)
    export_path = _write_team_export_csv(tmp_path, jdoe_teams="", asmith_teams="platform")

    refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=(), team_refs=("platform",),
        import_file=export_path, actor="steward@example.com", reason="membership sync", apply=False, as_of=_NOW,
    )

    memberships = read_all_memberships(knowledge_root)
    jdoe_active = [m for m in memberships if m.person_entity_id == "person:jdoe" and m.status == MembershipStatus.ACTIVE]
    assert len(jdoe_active) == 1  # untouched


def test_team_refresh_partial_snapshot_never_changes_memberships(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _seed_team_fixture(programs_root, knowledge_root)
    _write_provider_config(knowledge_root)
    export_path = tmp_path / "malformed.json"
    export_path.write_text('[{"display_name": "No Alias Person"}]', encoding="utf-8")

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=(), team_refs=("platform",),
        import_file=export_path, actor="steward@example.com", reason="membership sync", apply=True, as_of=_NOW,
    )

    assert result.team_membership_diffs[0].complete is False
    memberships = read_all_memberships(knowledge_root)
    jdoe_active = [m for m in memberships if m.person_entity_id == "person:jdoe" and m.status == MembershipStatus.ACTIVE]
    assert len(jdoe_active) == 1  # untouched despite the incomplete snapshot


def test_team_refresh_unresolved_provider_alias_is_skipped_not_created(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _seed_team_fixture(programs_root, knowledge_root)
    _write_provider_config(knowledge_root)
    export_path = tmp_path / "team_export.csv"
    export_path.write_text(
        "alias,display_name,title,department,manager_alias,email,teams\n"
        "jdoe,Jane Doe,Old Title,Platform,,jdoe@example.com,platform\n"
        "ghost,Ghost Person,,,,,platform\n",
        encoding="utf-8",
    )

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=(), team_refs=("platform",),
        import_file=export_path, actor="steward@example.com", reason="membership sync", apply=True, as_of=_NOW,
    )

    diff = result.team_membership_diffs[0]
    assert diff.unresolved_provider_aliases == ("ghost",)
    assert "ghost" not in diff.added_person_aliases
    people = load_people_directory(knowledge_root / "people_directory.yaml").people
    assert not any(person.alias == "ghost" for person in people)


def test_team_ref_must_resolve_or_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _seed_team_fixture(programs_root, knowledge_root)
    _write_provider_config(knowledge_root)
    export_path = _write_team_export_csv(tmp_path)

    with pytest.raises(ConfigError, match="must resolve to exactly one"):
        refresh_people_from_provider(
            programs_root=programs_root, provider_name="acme_directory_export", person_refs=(), team_refs=("nonexistent-team",),
            import_file=export_path, actor="steward@example.com", reason="test", apply=True, as_of=_NOW,
        )


def test_person_and_team_refresh_can_run_in_the_same_call(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed_active_registry(programs_root)
    _seed_team_fixture(programs_root, knowledge_root)
    _write_provider_config(knowledge_root)
    export_path = _write_team_export_csv(tmp_path, jdoe_teams="platform", asmith_teams="platform")

    result = refresh_people_from_provider(
        programs_root=programs_root, provider_name="acme_directory_export", person_refs=("jdoe",), team_refs=("platform",),
        import_file=export_path, actor="steward@example.com", reason="combined refresh", apply=True, as_of=_NOW,
    )

    assert result.requested_person_count == 1
    assert result.requested_team_count == 1
    assert len(result.accepted) >= 1
    assert len(result.team_membership_diffs) == 1
    people = load_people_directory(knowledge_root / "people_directory.yaml").people
    jdoe = next(person for person in people if person.entity_id == "person:jdoe")
    assert jdoe.title == "Old Title"
