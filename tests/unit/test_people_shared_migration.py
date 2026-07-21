"""Synthetic PPL-W2B.1 coverage for shared bootstrap/migration writes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.operator_identity import OperatorIdentity
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS, read_journal_records
from src.core.people_directory_schema import (
    FieldVerification,
    PersonDirectory,
    Team,
    TeamKind,
    load_people_directory,
    load_teams,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityRedirect,
    write_entities_document,
)
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_manifest
from src.core.people_registry_governance import adopt_registry_edits
from src.core.people_registry_lease import read_registry_lease_state
from src.core.people_shared_migration import (
    apply_shared_migration,
    bootstrap_shared_factual_files,
    preview_shared_migration,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_RUNNER = CliRunner()


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


def _entity(entity_id: str, entity_type: str, alias: str) -> CanonicalEntity:
    return CanonicalEntity(
        workspace_id="workspace:synthetic",
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_name=alias.title(),
        aliases=(_alias(alias),),
        scope="org",
        created_at=_NOW,
    )


def _write_program(
    programs_root: Path,
    program_id: str,
    *,
    entities: tuple[CanonicalEntity, ...],
    people: tuple[PersonDirectory, ...],
    teams: tuple[Team, ...],
) -> None:
    knowledge = programs_root / program_id / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    write_entities_document(knowledge / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=entities))
    write_people_directory(knowledge / "people_directory.yaml", people)
    write_teams(knowledge / "teams.yaml", teams)


def _person(entity_id: str, alias: str, title: str, *, pinned: bool = False) -> PersonDirectory:
    verifications = (
        FieldVerification(
            field_name="title",
            source="synthetic_fixture",
            source_ref=None,
            observed_at=_NOW,
            verified_at=_NOW,
            recorded_at=_NOW,
            verified_by_principal="test_steward",
            pinned=pinned,
            pin_reason="synthetic fixture" if pinned else None,
        ),
    ) if pinned else ()
    return PersonDirectory(entity_id=entity_id, alias=alias, title=title, verifications=verifications)


def _team(entity_id: str, team_id: str) -> Team:
    return Team(entity_id=entity_id, id=team_id, name=team_id.title(), kind=TeamKind.ORG_TEAM)


def _seed_first_program(programs_root: Path) -> None:
    _write_program(
        programs_root,
        "first",
        entities=(_entity("person:alice", "person", "alice"), _entity("team:platform", "team", "platform")),
        # These blank IDs intentionally prove that bootstrap binds local
        # factual records to the uniquely compatible entity candidates.
        people=(_person("", "alice", "Pinned title", pinned=True),),
        teams=(_team("", "platform"),),
    )


def test_bootstrap_preview_binds_entities_and_apply_uses_staged_transaction(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_first_program(programs_root)
    knowledge_root = programs_root.parent / "knowledge"

    preview = bootstrap_shared_factual_files("first", programs_root=programs_root, actor="tester", apply=False, as_of=_NOW)

    assert preview.entities_summary.added == ("person:alice", "team:platform")
    assert preview.people_to_write[0].entity_id == "person:alice"
    assert preview.teams_to_write[0].entity_id == "team:platform"
    assert not (knowledge_root / "entities.yaml").exists()

    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    result = bootstrap_shared_factual_files("first", programs_root=programs_root, actor="tester", apply=True, as_of=_NOW)

    manifest = load_registry_manifest(knowledge_root)
    assert result.transaction_id is not None
    assert manifest is not None and manifest.transaction_id == result.transaction_id
    assert {"entities.yaml", "people_directory.yaml", "teams.yaml"} <= set(dict(manifest.source_hashes))
    assert (knowledge_root / "entities.yaml").exists()
    assert (knowledge_root / "people_directory.yaml").exists()
    assert (knowledge_root / "teams.yaml").exists()
    assert read_registry_lease_state(knowledge_root=knowledge_root) is None
    changes = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    assert changes and all(change["field"] != "__record__" for change in changes)


def test_migration_preserves_unmatched_redirects_and_pins_while_quarantining_collision(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_first_program(programs_root)
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    bootstrap_shared_factual_files("first", programs_root=programs_root, actor="tester", apply=True, as_of=_NOW)

    from src.core.people_entity_schema import load_entities_document

    shared_document = load_entities_document(knowledge_root / "entities.yaml")
    assert shared_document is not None
    redirect = EntityRedirect(
        from_entity_id="person:retired-alice",
        to_entity_id="person:alice",
        recorded_at=_NOW,
        principal_id="test_steward",
        reason="synthetic preservation fixture",
    )
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(schema_version="2.0", entities=shared_document.entities, redirects=(redirect,)),
    )
    adopt_registry_edits(
        knowledge_root,
        actor="test_steward",
        reason="preserve synthetic redirect fixture",
        apply=True,
        as_of=_NOW,
    )

    _write_program(
        programs_root,
        "second",
        entities=(
            _entity("person:alice", "person", "alice"),
            _entity("person:carol", "person", "carol"),
            _entity("person:intruder", "person", "alice"),
        ),
        people=(
            _person("person:alice", "alice", "Attempted overwrite"),
            _person("person:carol", "carol", "New"),
            _person("person:intruder", "alice", "Must not become dangling"),
        ),
        teams=(),
    )

    result = apply_shared_migration("second", programs_root=programs_root, actor="tester", as_of=_NOW)

    assert result.partial_success is True
    assert any(conflict.incoming_entity_id == "person:intruder" for conflict in result.conflicts)
    reloaded = load_entities_document(knowledge_root / "entities.yaml")
    assert reloaded is not None and reloaded.redirects == (redirect,)
    from src.core.people_directory_schema import load_people_directory, load_teams

    people = load_people_directory(knowledge_root / "people_directory.yaml")
    teams = load_teams(knowledge_root / "teams.yaml")
    assert people is not None
    assert {person.entity_id for person in people.people} == {"person:alice", "person:carol"}
    assert next(person for person in people.people if person.entity_id == "person:alice").title == "Pinned title"
    assert teams is not None and teams.teams[0].entity_id == "team:platform"
    conflicts = read_journal_records(knowledge_root, STREAM_PEOPLE_CONFLICTS)
    assert len(conflicts) == 2
    assert {conflict["decision"] for conflict in conflicts} == {"quarantined"}


# ---------------------------------------------------------------------------
# PPL-W6.4 fix: migrate-shared completes the migration by clearing migrated
# person/team entities from the program-local source (found via a real
# pilot smoke test -- migrate-shared previously left a residual DIR-11
# doctor failure on every run, since §5.6's already-ratified rule says
# person/team entities must never live in a program-scope entities.yaml).
# ---------------------------------------------------------------------------

def test_apply_shared_migration_removes_migrated_person_team_entities_from_program_local_source(tmp_path: Path) -> None:
    from src.core.people_entity_schema import load_entities_document

    programs_root = tmp_path / "programs"
    _seed_first_program(programs_root)
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)

    apply_shared_migration("first", programs_root=programs_root, actor="tester", as_of=_NOW)

    program_local = load_entities_document(programs_root / "first" / "knowledge" / "entities.yaml")
    assert program_local is not None
    # Both the person and the team entity were successfully migrated (no
    # conflicts in this fixture) -- they must be gone from the program-local
    # source now, since DIR-11 says they may never live there.
    assert program_local.entities == ()

    shared = load_entities_document(knowledge_root / "entities.yaml")
    assert shared is not None
    assert {entity.entity_id for entity in shared.entities} == {"person:alice", "team:platform"}

    # DIR-05 companion: the same completeness applies to people_directory.yaml
    # and teams.yaml -- a "shadowed program-local factual file" must not
    # persist alongside the shared root once its records are migrated.
    from src.core.people_directory_schema import load_people_directory, load_teams

    program_people = load_people_directory(programs_root / "first" / "knowledge" / "people_directory.yaml")
    assert program_people is not None and program_people.people == ()
    program_teams = load_teams(programs_root / "first" / "knowledge" / "teams.yaml")
    assert program_teams is not None and program_teams.teams == ()

    shared_people = load_people_directory(knowledge_root / "people_directory.yaml")
    assert shared_people is not None and {p.entity_id for p in shared_people.people} == {"person:alice"}
    shared_teams = load_teams(knowledge_root / "teams.yaml")
    assert shared_teams is not None and {t.entity_id for t in shared_teams.teams} == {"team:platform"}


def test_apply_shared_migration_preserves_quarantined_entities_in_program_local_source(tmp_path: Path) -> None:
    """Reuses the exact quarantine fixture above (person:intruder collides
    on alias with a different entity_id and is quarantined, not migrated)
    to prove the cleanup step is conservative: only entities CONFIRMED
    present in the shared registry's post-commit set are removed.
    Deleting a quarantined entity from the program-local source would lose
    data with no shared copy to fall back on."""
    from src.core.people_entity_schema import load_entities_document

    programs_root = tmp_path / "programs"
    _seed_first_program(programs_root)
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    bootstrap_shared_factual_files("first", programs_root=programs_root, actor="tester", apply=True, as_of=_NOW)

    _write_program(
        programs_root,
        "second",
        entities=(
            _entity("person:alice", "person", "alice"),
            _entity("person:carol", "person", "carol"),
            _entity("person:intruder", "person", "alice"),
        ),
        people=(
            _person("person:alice", "alice", "Attempted overwrite"),
            _person("person:carol", "carol", "New"),
            _person("person:intruder", "alice", "Must not become dangling"),
        ),
        teams=(),
    )

    result = apply_shared_migration("second", programs_root=programs_root, actor="tester", as_of=_NOW)
    assert result.partial_success is True

    program_local = load_entities_document(programs_root / "second" / "knowledge" / "entities.yaml")
    assert program_local is not None
    remaining_ids = {entity.entity_id for entity in program_local.entities}
    # person:carol migrated cleanly -- removed from the program-local source.
    assert "person:carol" not in remaining_ids
    # person:intruder was quarantined -- still present locally, not lost.
    assert "person:intruder" in remaining_ids
    # person:alice was a pure duplicate of the already-migrated org entity
    # (same entity_id, already present in the shared root from "first")
    # -- also removed, since it's confirmed present in the shared registry.
    assert "person:alice" not in remaining_ids

    # DIR-05 companion: the same conservatism applies to people_directory.yaml.
    from src.core.people_directory_schema import load_people_directory

    program_people = load_people_directory(programs_root / "second" / "knowledge" / "people_directory.yaml")
    assert program_people is not None
    remaining_person_ids = {p.entity_id for p in program_people.people}
    assert "person:carol" not in remaining_person_ids
    assert "person:intruder" in remaining_person_ids
    assert "person:alice" not in remaining_person_ids


def test_registry_cli_selected_bootstrap_and_migrate_help_are_distinct(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_first_program(programs_root)
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda actor: OperatorIdentity(actor=actor, principal="test_steward", machine="test", session="test"),
    )

    preview = _RUNNER.invoke(app, ["kb", "registry", "bootstrap", "--from-program", "first", "--format", "json"])
    assert preview.exit_code == 0
    assert json.loads(preview.stdout)["people"]["added"] == ["person:alice"]
    assert not (programs_root.parent / "knowledge" / "entities.yaml").exists()

    applied = _RUNNER.invoke(
        app,
        ["kb", "registry", "bootstrap", "--from-program", "first", "--customer-boundary-id", "synthetic", "--apply"],
    )
    assert applied.exit_code == 0
    assert "Applied first shared factual root" in applied.stdout
    assert "top-level `vertex bootstrap`" in _RUNNER.invoke(app, ["kb", "registry", "bootstrap", "--help"]).stdout
    assert "top-level `vertex migrate`" in _RUNNER.invoke(app, ["kb", "registry", "migrate-shared", "--help"]).stdout


# ---------------------------------------------------------------------------
# specs/bklg.md BL-E3: entity_id backfill for pre-existing shared directory
# records (knowledge/people_directory.yaml + teams.yaml populated directly,
# predating entities.yaml).
# ---------------------------------------------------------------------------

from src.core.people_entity_schema import load_entities_document
from src.core.people_shared_migration import apply_entity_id_backfill, preview_entity_id_backfill


def _write_shared_directory_directly(
    knowledge_root: Path, *, people: tuple[PersonDirectory, ...], teams: tuple[Team, ...]
) -> None:
    """Simulates the real state this backfill exists for: people_directory.yaml/
    teams.yaml populated at the SHARED root directly (not via migrate-shared),
    so every record's entity_id is blank -- entities.yaml never existed."""
    knowledge_root.mkdir(parents=True, exist_ok=True)
    write_people_directory(knowledge_root / "people_directory.yaml", people)
    write_teams(knowledge_root / "teams.yaml", teams)


def test_preview_entity_id_backfill_requires_bootstrapped_registry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = programs_root.parent / "knowledge"
    _write_shared_directory_directly(knowledge_root, people=(_person("", "alice", "TPM"),), teams=())
    try:
        preview_entity_id_backfill(programs_root=programs_root, as_of=_NOW)
        assert False, "expected ConfigError before bootstrap"
    except Exception as exc:
        assert "has not been bootstrapped" in str(exc)


def test_entity_id_backfill_mints_entities_for_orphaned_directory_records(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = programs_root.parent / "knowledge"
    _write_shared_directory_directly(
        knowledge_root,
        people=(_person("", "alice", "Senior TPM"),),
        teams=(_team("", "platform"),),
    )
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)

    preview = preview_entity_id_backfill(programs_root=programs_root, as_of=_NOW)
    assert preview.people_backfilled == ("alice",)
    assert preview.teams_backfilled == ("platform",)
    assert len(preview.new_entity_ids) == 2
    assert not (knowledge_root / "entities.yaml").exists(), "preview must not write anything"

    result = apply_entity_id_backfill(programs_root=programs_root, actor="tester", as_of=_NOW)
    assert result.people_backfilled == ("alice",)
    assert result.teams_backfilled == ("platform",)
    assert result.transaction_id is not None

    entities_doc = load_entities_document(knowledge_root / "entities.yaml")
    assert entities_doc is not None
    assert {e.entity_type for e in entities_doc.entities} == {"person", "team"}
    person_entity = next(e for e in entities_doc.entities if e.entity_type == "person")
    team_entity = next(e for e in entities_doc.entities if e.entity_type == "team")
    assert person_entity.canonical_name == "Senior TPM" or person_entity.canonical_name == "alice"
    assert {alias.value for alias in person_entity.aliases} == {"alice"}
    assert {alias.value for alias in team_entity.aliases} == {"platform"}

    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    assert people_result is not None
    alice = next(p for p in people_result.people if p.alias == "alice")
    assert alice.entity_id == person_entity.entity_id
    assert alice.title == "Senior TPM"  # untouched field proves this is a pure entity_id patch

    teams_result = load_teams(knowledge_root / "teams.yaml")
    assert teams_result is not None
    platform = next(t for t in teams_result.teams if t.id == "platform")
    assert platform.entity_id == team_entity.entity_id


def test_entity_id_backfill_preserves_already_canonical_records(tmp_path: Path) -> None:
    """A mixed real-world state: one record already has a valid entity_id
    (already resolved via some earlier path), one doesn't. Only the
    orphaned one should be touched."""
    programs_root = tmp_path / "programs"
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(schema_version="2.0", entities=(_entity("person:already-canonical", "person", "bob"),)),
    )
    _write_shared_directory_directly(
        knowledge_root,
        people=(
            PersonDirectory(entity_id="person:already-canonical", alias="bob", title="Already canonical"),
            _person("", "carol", "Orphaned"),
        ),
        teams=(),
    )

    result = apply_entity_id_backfill(programs_root=programs_root, actor="tester", as_of=_NOW)
    assert result.people_backfilled == ("carol",)

    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    assert people_result is not None
    bob = next(p for p in people_result.people if p.alias == "bob")
    carol = next(p for p in people_result.people if p.alias == "carol")
    assert bob.entity_id == "person:already-canonical"  # untouched
    assert carol.entity_id and carol.entity_id != ""

    entities_doc = load_entities_document(knowledge_root / "entities.yaml")
    assert entities_doc is not None
    assert len(entities_doc.entities) == 2  # bob's pre-existing entity + carol's newly minted one


def test_entity_id_backfill_is_idempotent_noop_on_second_run(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = programs_root.parent / "knowledge"
    _write_shared_directory_directly(knowledge_root, people=(_person("", "alice", "TPM"),), teams=())
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)

    first = apply_entity_id_backfill(programs_root=programs_root, actor="tester", as_of=_NOW)
    assert first.people_backfilled == ("alice",)

    second = apply_entity_id_backfill(programs_root=programs_root, actor="tester", as_of=_NOW)
    assert second.is_noop is True
    assert second.transaction_id is None  # no transaction was opened for a no-op

    entities_doc = load_entities_document(knowledge_root / "entities.yaml")
    assert entities_doc is not None
    assert len(entities_doc.entities) == 1  # no duplicate minted on the second run


def test_entity_id_backfill_appends_journal_change_records(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = programs_root.parent / "knowledge"
    _write_shared_directory_directly(knowledge_root, people=(_person("", "alice", "TPM"),), teams=())
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)

    apply_entity_id_backfill(programs_root=programs_root, actor="tester", as_of=_NOW)

    changes = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    entity_id_changes = [c for c in changes if c["field"] == "entity_id"]
    assert entity_id_changes, "expected at least one entity_id field-change record"
    assert any(c["after"] and c["after"] != "" for c in entity_id_changes)
