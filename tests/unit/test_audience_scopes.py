from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.audience_scopes import audience_scopes_path_for_program, find_audience_scope, load_audience_scopes
from src.core.exceptions import ConfigError
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
    )


def _seed_entities(programs_root: Path) -> None:
    knowledge_root = programs_root.parent / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                CanonicalEntity(
                    workspace_id="workspace:acme", entity_id="team:platform", entity_type="team",
                    canonical_name="Platform", aliases=(_alias("platform"),), scope="org", created_at=_NOW,
                ),
                CanonicalEntity(
                    workspace_id="workspace:acme", entity_id="person:jdoe", entity_type="person",
                    canonical_name="Jane Doe", aliases=(_alias("jdoe"),), scope="org", created_at=_NOW,
                ),
                CanonicalEntity(
                    workspace_id="workspace:acme", entity_id="person:asmith", entity_type="person",
                    canonical_name="Amy Smith", aliases=(_alias("asmith"),), scope="org", created_at=_NOW,
                ),
            ),
        ),
    )


def test_missing_file_returns_empty_tuple(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert load_audience_scopes(program_id="acme", programs_root=programs_root) == ()


def test_scope_resolves_alias_and_canonical_id_forms(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_entities(programs_root)
    path = audience_scopes_path_for_program("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            'schema_version: "1.0"\n'
            "audience_scopes:\n"
            "  engineering_hygiene:\n"
            "    team_refs: [platform]\n"
            "    membership_roles: [member, lead]\n"
            "    require_verified_within_days: 30\n"
            "    include_people: [person:asmith]\n"
            "    exclude_people: [jdoe]\n"
            "    allow_external_guests: false\n"
        ),
        encoding="utf-8",
    )

    scopes = load_audience_scopes(program_id="acme", programs_root=programs_root)

    assert len(scopes) == 1
    scope = scopes[0]
    assert scope.id == "engineering_hygiene"
    assert scope.team_entity_ids == ("team:platform",)
    assert scope.membership_roles == ("member", "lead")
    assert scope.require_verified_within_days == 30
    assert scope.include_person_entity_ids == ("person:asmith",)
    assert scope.exclude_person_entity_ids == ("person:jdoe",)
    assert scope.allow_external_guests is False


def test_unresolvable_team_ref_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_entities(programs_root)
    path = audience_scopes_path_for_program("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('schema_version: "1.0"\naudience_scopes:\n  bad:\n    team_refs: [nonexistent]\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="does not resolve"):
        load_audience_scopes(program_id="acme", programs_root=programs_root)


def test_scope_with_no_optional_fields_uses_defaults(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_entities(programs_root)
    path = audience_scopes_path_for_program("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('schema_version: "1.0"\naudience_scopes:\n  minimal: {}\n', encoding="utf-8")

    scopes = load_audience_scopes(program_id="acme", programs_root=programs_root)

    scope = scopes[0]
    assert scope.team_entity_ids == ()
    assert scope.require_verified_within_days is None
    assert scope.allow_external_guests is False


def test_find_audience_scope_returns_none_for_unknown_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_entities(programs_root)
    path = audience_scopes_path_for_program("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('schema_version: "1.0"\naudience_scopes:\n  known: {}\n', encoding="utf-8")

    scopes = load_audience_scopes(program_id="acme", programs_root=programs_root)

    assert find_audience_scope(scopes, "known") is not None
    assert find_audience_scope(scopes, "unknown") is None


def test_wrong_schema_major_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_entities(programs_root)
    path = audience_scopes_path_for_program("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('schema_version: "2.0"\naudience_scopes: {}\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="schema_version major"):
        load_audience_scopes(program_id="acme", programs_root=programs_root)
