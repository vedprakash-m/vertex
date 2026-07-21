"""PPL-W2B.3 contract: a safe merge reversal preserves canonical history."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_directory_schema import PersonDirectory, PersonStatus, load_people_directory, write_people_directory
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityIdentifier,
    EntityStatus,
    load_entities_document,
    write_entities_document,
)
from src.core.people_namespace_bridge import resolve_ref_to_canonical_entity_id
from src.core.people_registry_corrections import bind_person_identifier, merge_people, unmerge_people
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_config, write_registry_config
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value,
        kind="vertex::alias",
        status=AliasStatus.ACTIVE,
        valid_from=None,
        valid_until=None,
        source="synthetic_fixture",
        source_ref=None,
        recorded_at=_NOW,
        verified_at=_NOW,
        verified_by_principal="test_steward",
    )


def _seed(knowledge_root: Path) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    config = load_registry_config(knowledge_root)
    assert config is not None
    write_registry_config(knowledge_root / "registry.yaml", replace(config, directory_steward_principals=("test_steward",)))
    entities = (
        CanonicalEntity(
            workspace_id=config.workspace_id,
            entity_id="person:source",
            entity_type="person",
            canonical_name="Source",
            aliases=(_alias("source"),),
            scope="org",
            created_at=_NOW,
            identifiers=(EntityIdentifier(provider="graph", kind="provider_subject", subject_id="source"),),
        ),
        CanonicalEntity(
            workspace_id=config.workspace_id,
            entity_id="person:target",
            entity_type="person",
            canonical_name="Target",
            aliases=(_alias("target"),),
            scope="org",
            created_at=_NOW,
            identifiers=(EntityIdentifier(provider="graph", kind="provider_subject", subject_id="target"),),
        ),
    )
    people = (
        PersonDirectory(entity_id="person:source", alias="source", status=PersonStatus.ACTIVE),
        PersonDirectory(entity_id="person:target", alias="target", status=PersonStatus.ACTIVE),
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=entities))
        write_people_directory(staged_dir / "people_directory.yaml", people)

    def validate_staged(staged_dir: Path) -> None:
        assert load_entities_document(staged_dir / "entities.yaml") is not None
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None

    prepared = prepare_registry_files_transaction(
        knowledge_root,
        ("entities.yaml", "people_directory.yaml"),
        owner="seed",
        write_staged_files=write_staged,
        validate_staged_files=validate_staged,
        as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def test_merge_then_safe_unmerge_round_trip_restores_entities_and_redirect_resolution(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    merge_people(
        knowledge_root,
        source_ref="person:source",
        target_ref="person:target",
        reason="reviewed duplicate",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )
    merged = load_entities_document(knowledge_root / "entities.yaml")
    assert merged is not None
    assert resolve_ref_to_canonical_entity_id("person:source", entities=merged.entities, redirects=merged.redirects).canonical_entity_id == "person:target"

    result = unmerge_people(
        knowledge_root,
        source_ref="person:source",
        reason="reviewed correction",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )

    restored = load_entities_document(knowledge_root / "entities.yaml")
    assert result.transaction_id is not None
    assert restored is not None
    assert not restored.redirects
    assert {entity.entity_id for entity in restored.entities if entity.status is EntityStatus.ACTIVE} == {"person:source", "person:target"}
    assert resolve_ref_to_canonical_entity_id("person:source", entities=restored.entities, redirects=restored.redirects).canonical_entity_id == "person:source"


def test_unmerge_refuses_when_a_downstream_generation_consumed_the_merge(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    merge_people(
        knowledge_root,
        source_ref="person:source",
        target_ref="person:target",
        reason="reviewed duplicate",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )
    bind_person_identifier(
        knowledge_root,
        person_ref="person:target",
        provider="ado",
        subject_id="target",
        reason="later reviewed binding",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )

    with pytest.raises(ConfigError, match="downstream committed generations"):
        unmerge_people(
            knowledge_root,
            source_ref="person:source",
            reason="must not rewrite consumed correction",
            actor="test_steward",
            apply=True,
            as_of=_NOW,
        )
