from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.people_directory_schema import Team, TeamKind, write_teams
from src.core.people_entity_schema import (
    ENTITIES_SCHEMA_VERSION,
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityStatus,
    write_entities_document,
)
from src.core.people_registry_cache import (
    cache_db_path,
    cache_manifest_path,
    ensure_cache_fresh,
    lookup_alias_in_cache,
    read_cache_status,
    rebuild_cache,
)

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _seed_entities(knowledge_root: Path) -> None:
    entities = (
        CanonicalEntity(
            workspace_id="ws-1", entity_id="person:alice", entity_type="person", canonical_name="Alice",
            aliases=(
                EntityAlias(
                    value="alice", kind="alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
                    source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
                ),
            ),
            scope="org", created_at=_NOW, status=EntityStatus.ACTIVE,
        ),
    )
    write_entities_document(knowledge_root / "entities.yaml", EntitiesDocument(schema_version=ENTITIES_SCHEMA_VERSION, entities=entities))


def test_read_cache_status_reports_missing_before_any_build(tmp_path: Path) -> None:
    status = read_cache_status(tmp_path)

    assert status.exists is False
    assert status.valid is False
    assert status.reason == "missing"


def test_rebuild_cache_creates_db_and_manifest_files(tmp_path: Path) -> None:
    _seed_entities(tmp_path)

    manifest = rebuild_cache(tmp_path, as_of=_NOW)

    assert cache_db_path(tmp_path).exists()
    assert cache_manifest_path(tmp_path).exists()
    assert manifest.source_hashes


def test_read_cache_status_valid_immediately_after_rebuild(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    rebuild_cache(tmp_path, as_of=_NOW)

    status = read_cache_status(tmp_path)

    assert status.valid is True
    assert status.reason == "valid"


def test_read_cache_status_detects_stale_source_after_a_source_edit(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    rebuild_cache(tmp_path, as_of=_NOW)

    # Mutate a source file after the cache was built -- the content hash must diverge.
    (tmp_path / "entities.yaml").write_text((tmp_path / "entities.yaml").read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")

    status = read_cache_status(tmp_path)

    assert status.valid is False
    assert status.reason == "stale_source"


def test_read_cache_status_detects_a_corrupt_manifest(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    rebuild_cache(tmp_path, as_of=_NOW)
    cache_manifest_path(tmp_path).write_text("not valid json{{{", encoding="utf-8")

    status = read_cache_status(tmp_path)

    assert status.valid is False
    assert status.reason == "corrupt_manifest"


def test_ensure_cache_fresh_rebuilds_when_stale(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    rebuild_cache(tmp_path, as_of=_NOW)
    (tmp_path / "entities.yaml").write_text((tmp_path / "entities.yaml").read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")

    manifest = ensure_cache_fresh(tmp_path)

    status_after = read_cache_status(tmp_path)
    assert status_after.valid is True
    assert manifest.source_hashes == status_after.manifest.source_hashes


def test_ensure_cache_fresh_is_a_no_op_when_already_valid(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    first = rebuild_cache(tmp_path, as_of=_NOW)

    second = ensure_cache_fresh(tmp_path)

    assert first.built_at == second.built_at


def test_lookup_alias_in_cache_finds_a_real_alias(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    rebuild_cache(tmp_path, as_of=_NOW)

    results = lookup_alias_in_cache(tmp_path, "alice")

    assert results == (("person:alice", "person"),)


def test_lookup_alias_in_cache_is_case_insensitive(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    rebuild_cache(tmp_path, as_of=_NOW)

    assert lookup_alias_in_cache(tmp_path, "ALICE") == (("person:alice", "person"),)


def test_lookup_alias_in_cache_returns_empty_for_unknown_alias(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    rebuild_cache(tmp_path, as_of=_NOW)

    assert lookup_alias_in_cache(tmp_path, "nobody") == ()


def test_lookup_alias_in_cache_returns_empty_when_no_cache_exists(tmp_path: Path) -> None:
    assert lookup_alias_in_cache(tmp_path, "alice") == ()


def test_rebuild_cache_indexes_team_id_alongside_person_alias(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    write_teams(tmp_path / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM),))

    rebuild_cache(tmp_path, as_of=_NOW)

    assert lookup_alias_in_cache(tmp_path, "platform") == (("team:platform", "team"),)


def test_rebuild_cache_never_modifies_source_yaml(tmp_path: Path) -> None:
    _seed_entities(tmp_path)
    before = (tmp_path / "entities.yaml").read_text(encoding="utf-8")

    rebuild_cache(tmp_path, as_of=_NOW)

    after = (tmp_path / "entities.yaml").read_text(encoding="utf-8")
    assert before == after


# ---------------------------------------------------------------------------
# PPL-W3.5d: preloaded entities/people/teams skip the fresh disk load
# ---------------------------------------------------------------------------

def test_rebuild_cache_with_preloaded_data_uses_it_instead_of_re_reading_disk(tmp_path: Path) -> None:
    """A caller (e.g. `registry_dir13_cache_check` reusing `kb_checks.py`'s
    own already-loaded `_SharedRegistrySnapshot`) can pass `entities`/
    `people`/`teams` directly -- `rebuild_cache` must use THAT data, not
    re-read the source files, even if their on-disk content has since
    diverged. Matches this file's own established "mutate the source,
    prove the caller's own data wins" idiom."""
    _seed_entities(tmp_path)
    from src.core.people_entity_schema import load_entities_document

    preloaded_document = load_entities_document(tmp_path / "entities.yaml")
    assert preloaded_document is not None
    preloaded_entities = preloaded_document.entities

    # Diverge the on-disk file from what was preloaded -- a real rebuild_cache
    # call (no preload) against THIS file would index "someone_else", not "alice".
    other_entities = (
        CanonicalEntity(
            workspace_id="ws-1", entity_id="person:someone_else", entity_type="person", canonical_name="Someone Else",
            aliases=(
                EntityAlias(
                    value="someone_else", kind="alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
                    source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
                ),
            ),
            scope="org", created_at=_NOW, status=EntityStatus.ACTIVE,
        ),
    )
    write_entities_document(tmp_path / "entities.yaml", EntitiesDocument(schema_version=ENTITIES_SCHEMA_VERSION, entities=other_entities))

    rebuild_cache(tmp_path, as_of=_NOW, entities=preloaded_entities, people=(), teams=())

    assert lookup_alias_in_cache(tmp_path, "alice") == (("person:alice", "person"),)
    assert lookup_alias_in_cache(tmp_path, "someone_else") == ()


def test_rebuild_cache_preloaded_none_defaults_still_load_from_disk(tmp_path: Path) -> None:
    """`entities`/`people`/`teams` all default to `None` -- a true no-op,
    identical to every pre-existing `rebuild_cache` call in this file."""
    _seed_entities(tmp_path)

    manifest = rebuild_cache(tmp_path, as_of=_NOW, entities=None, people=None, teams=None)

    assert cache_db_path(tmp_path).exists()
    assert lookup_alias_in_cache(tmp_path, "alice") == (("person:alice", "person"),)
