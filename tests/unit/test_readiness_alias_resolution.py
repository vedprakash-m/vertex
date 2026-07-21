"""specs/people.md Phase 2a, PPL-W2A.5: tests for the additive canonical-
resolution layer on `src.commands.readiness._alias_exists`.

specs/people.md §7.9's exact directive: "Migrate `people_directory`
dimension and `alias_exists()` to canonical resolution without changing
readiness outcome for equivalent data." These tests prove the migration
is additive (OR, never a replacement): the legacy check still succeeds
for legacy-only data (zero regression), and canonical resolution ALSO
succeeds once schema-2.0 `entities.yaml` data exists (new capability).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.commands.readiness import _alias_exists, _canonical_alias_exists
from src.core.people_entity_schema import (
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    AliasStatus,
    write_entities_document,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _write_legacy_people_directory(program_dir: Path, aliases: list[str]) -> None:
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "people_directory.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0", "people": [{"alias": alias} for alias in aliases]}),
        encoding="utf-8",
    )
    (knowledge_dir / "people_profiles.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "profiles": []}), encoding="utf-8")
    (knowledge_dir / "teams.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "teams": []}), encoding="utf-8")
    (knowledge_dir / "products.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "products": []}), encoding="utf-8")
    (knowledge_dir / "golden_queries.yaml").write_text(yaml.safe_dump({"schema_version": "1.0", "queries": []}), encoding="utf-8")


def test_alias_exists_true_for_legacy_directory_alias_unchanged_by_migration(tmp_path: Path) -> None:
    # Zero-regression proof: existing legacy-only data resolves exactly as before.
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    _write_legacy_people_directory(program_dir, ["alice"])

    assert _alias_exists("acme", "alice", programs_root=programs_root) is True
    assert _alias_exists("acme", "ALICE", programs_root=programs_root) is True  # Case-insensitive, as before.
    assert _alias_exists("acme", "nobody", programs_root=programs_root) is False


def test_canonical_alias_exists_returns_false_with_no_schema2_entities(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    _write_legacy_people_directory(program_dir, [])

    # Production has no schema-2.0 entities.yaml anywhere today -- must be a safe no-op.
    assert _canonical_alias_exists("acme", "alice", programs_root=programs_root) is False


def test_alias_exists_additionally_resolves_via_canonical_entities_when_present(tmp_path: Path) -> None:
    # New capability: an alias NOT in the legacy people_directory.yaml but
    # present in a schema-2.0 org-scope entities.yaml is now found too.
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    _write_legacy_people_directory(program_dir, [])  # Legacy directory has nothing.
    knowledge_root = programs_root.parent / "knowledge"
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                CanonicalEntity(
                    workspace_id="workspace:acme",
                    entity_id="person:01ABC",
                    entity_type="person",
                    canonical_name="Bob",
                    aliases=(
                        EntityAlias(
                            value="bob",
                            kind="vertex::alias",
                            status=AliasStatus.ACTIVE,
                            valid_from=_NOW,
                            valid_until=None,
                            source="operator_assertion",
                            source_ref=None,
                            recorded_at=_NOW,
                            verified_at=_NOW,
                            verified_by_principal="ACME\\steward",
                        ),
                    ),
                    scope="org",
                    created_at=_NOW,
                ),
            ),
        ),
    )

    assert _alias_exists("acme", "bob", programs_root=programs_root) is True
    assert _canonical_alias_exists("acme", "bob", programs_root=programs_root) is True


def test_canonical_alias_exists_ignores_a_legacy_shaped_entities_yaml_gracefully(tmp_path: Path) -> None:
    # A program-scope legacy (schema-0) entities.yaml must not crash this check.
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    _write_legacy_people_directory(program_dir, [])
    (program_dir / "knowledge" / "entities.yaml").write_text(
        yaml.safe_dump({"entities": [{"id": "alice", "type": "person", "aliases": ["alice"]}]}),
        encoding="utf-8",
    )

    assert _canonical_alias_exists("acme", "alice", programs_root=programs_root) is False
