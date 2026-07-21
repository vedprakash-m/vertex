"""specs/people.md Phase 2a, PPL-W2A.1: tests for entities.yaml schema
2.0 + DIR-11 org-scope enforcement (src/core/people_entity_schema.py).

specs/people.md §9.1's own verification bar: "DIR-11 contract-test case;
a schema-0 legacy `entities.yaml` produces a valid migration preview."
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.people_entity_schema import (
    ENTITIES_SCHEMA_VERSION,
    AliasStatus,
    CanonicalEntity,
    Dir11Violation,
    EntitiesDocument,
    EntityAlias,
    EntityRedirect,
    EntityStatus,
    check_dir11_compliance,
    is_legacy_schema_0_entities_document,
    load_entities_document,
    preview_entities_migration,
    write_entities_document,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value,
        kind="vertex::alias",
        status=AliasStatus.ACTIVE,
        valid_from=_NOW,
        valid_until=None,
        source="operator_assertion",
        source_ref=None,
        recorded_at=_NOW,
        verified_at=_NOW,
        verified_by_principal="ACME\\steward",
    )


def _entity(entity_id: str, entity_type: str, *, scope: str = "org", aliases: tuple[EntityAlias, ...] = ()) -> CanonicalEntity:
    return CanonicalEntity(
        workspace_id="workspace:acme",
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_name=entity_id,
        aliases=aliases,
        scope=scope,
        created_at=_NOW,
    )


def test_load_entities_document_reads_the_real_example_fixture() -> None:
    path = Path(__file__).resolve().parents[2] / "knowledge" / "entities.example.yaml"

    document = load_entities_document(path)

    assert document is not None
    assert document.schema_version == ENTITIES_SCHEMA_VERSION
    assert len(document.entities) == 2
    person = next(e for e in document.entities if e.entity_type == "person")
    assert person.aliases[0].value == "sample_owner"
    assert len(person.identifiers) == 1


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    document = EntitiesDocument(
        schema_version=ENTITIES_SCHEMA_VERSION,
        entities=(_entity("person:1", "person", aliases=(_alias("alice"),)),),
        redirects=(EntityRedirect(from_entity_id="person:old", to_entity_id="person:1", recorded_at=_NOW, principal_id="ACME\\steward", reason="rename"),),
    )

    write_entities_document(path, document)
    reloaded = load_entities_document(path)

    assert reloaded == document


def test_load_entities_document_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_entities_document(tmp_path / "entities.yaml") is None


def test_load_entities_document_rejects_wrong_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    path.write_text('schema_version: "1.0"\nentities: []\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="expected entities.yaml schema_version"):
        load_entities_document(path)


def test_is_legacy_schema_0_true_for_versionless_file(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    path.write_text(
        yaml.safe_dump({"entities": [{"id": "alice", "type": "person", "name": "Alice", "aliases": ["alice"]}]}),
        encoding="utf-8",
    )

    assert is_legacy_schema_0_entities_document(path) is True


def test_is_legacy_schema_0_false_for_schema_2_file(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    write_entities_document(path, EntitiesDocument(schema_version=ENTITIES_SCHEMA_VERSION, entities=()))

    assert is_legacy_schema_0_entities_document(path) is False


def test_is_legacy_schema_0_false_for_missing_file(tmp_path: Path) -> None:
    assert is_legacy_schema_0_entities_document(tmp_path / "entities.yaml") is False


def test_preview_entities_migration_produces_valid_preview_without_writing(tmp_path: Path) -> None:
    # specs/people.md §9.1's exact PPL-W2A.1 verification: "a schema-0 legacy
    # entities.yaml produces a valid migration preview."
    path = tmp_path / "entities.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "entities": [
                    {"id": "alice", "type": "person", "name": "Alice Smith", "aliases": ["alice", "asmith"]},
                    {"id": "team-a", "type": "team", "name": "Team A", "aliases": ["team_a"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    original_bytes = path.read_bytes()

    preview = preview_entities_migration(path, workspace_id="workspace:acme", as_of=_NOW)

    assert len(preview.would_create_entities) == 2
    alice = next(e for e in preview.would_create_entities if e.entity_id == "alice")
    assert alice.entity_type == "person"
    assert len(alice.aliases) == 2
    assert all(a.source == "legacy_migration" for a in alice.aliases)
    assert all("unverified" in a.verified_by_principal.lower() for a in alice.aliases)
    assert preview.diagnostics  # Non-empty summary diagnostic.
    # Never writes anything -- the source file is byte-for-byte unchanged.
    assert path.read_bytes() == original_bytes


def test_preview_entities_migration_requires_an_existing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No entities.yaml found"):
        preview_entities_migration(tmp_path / "entities.yaml", workspace_id="workspace:acme")


def test_preview_entities_migration_rejects_an_already_migrated_file(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    write_entities_document(path, EntitiesDocument(schema_version=ENTITIES_SCHEMA_VERSION, entities=()))

    with pytest.raises(ConfigError, match="not a legacy schema-0 document"):
        preview_entities_migration(path, workspace_id="workspace:acme")


def test_preview_entities_migration_flags_missing_entity_id(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    path.write_text(yaml.safe_dump({"entities": [{"type": "person", "name": "No ID"}]}), encoding="utf-8")

    preview = preview_entities_migration(path, workspace_id="workspace:acme", as_of=_NOW)

    assert preview.would_create_entities == ()
    assert any("missing entity_id" in d for d in preview.diagnostics)


def test_dir11_flags_a_program_scoped_person_entity() -> None:
    # specs/people.md §9.1's exact PPL-W2A.1 verification: "DIR-11 contract-test case."
    org_entities = ()
    program_entities = (_entity("person:1", "person", scope="program"),)

    violations = check_dir11_compliance(org_entities=org_entities, program_entities=program_entities)

    assert len(violations) == 1
    assert violations[0].reason == "program_scoped_org_only_type"
    assert violations[0].entity_id == "person:1"


def test_dir11_flags_a_program_scoped_team_entity() -> None:
    violations = check_dir11_compliance(org_entities=(), program_entities=(_entity("team:1", "team", scope="program"),))

    assert len(violations) == 1
    assert violations[0].reason == "program_scoped_org_only_type"


def test_dir11_flags_program_entity_id_collision_with_org_entity() -> None:
    org_entities = (_entity("widget:1", "widget"),)  # A non-org-scope-only type, so only the collision check applies.
    program_entities = (_entity("widget:1", "widget", scope="program"),)

    violations = check_dir11_compliance(org_entities=org_entities, program_entities=program_entities)

    assert len(violations) == 1
    assert violations[0].reason == "overrides_org_binding"


def test_dir11_flags_program_alias_collision_with_org_entity() -> None:
    org_entities = (_entity("widget:org-1", "widget", aliases=(_alias("shared-alias"),)),)
    program_entities = (_entity("widget:prog-1", "widget", scope="program", aliases=(_alias("shared-alias"),)),)

    violations = check_dir11_compliance(org_entities=org_entities, program_entities=program_entities)

    assert len(violations) == 1
    assert violations[0].reason == "overrides_org_binding"
    assert violations[0].entity_id == "widget:prog-1"


def test_dir11_passes_when_no_program_scope_conflicts() -> None:
    org_entities = (_entity("person:1", "person"),)
    program_entities = (_entity("widget:prog-1", "widget", scope="program", aliases=(_alias("unique-alias"),)),)

    violations = check_dir11_compliance(org_entities=org_entities, program_entities=program_entities)

    assert violations == ()


def test_dir11_flags_any_program_redeclaration_of_an_org_entity_id() -> None:
    # A program-scope document re-declaring the same entity_id an org-scope
    # document already owns is flagged even if the content is identical --
    # a program-scope document should never re-declare an org entity at
    # all, matching the spec's "or overrides an org binding" wording
    # literally rather than trying to diff record content for equivalence.
    org_entities = (_entity("widget:1", "widget"),)
    program_entities = (_entity("widget:1", "widget", scope="program"),)

    violations = check_dir11_compliance(org_entities=org_entities, program_entities=program_entities)

    assert len(violations) == 1
    assert violations[0].reason == "overrides_org_binding"


def test_dir11_empty_inputs_produce_no_violations() -> None:
    assert check_dir11_compliance(org_entities=(), program_entities=()) == ()
