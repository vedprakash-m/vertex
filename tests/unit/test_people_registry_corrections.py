"""PPL-W2B.3 steward correction coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
import yaml

from cli import app
from src.core.exceptions import ConfigError
from src.core.operator_identity import OperatorIdentity
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS, read_journal_records
from src.core.people_directory_schema import PersonDirectory, PersonStatus, load_people_directory, write_people_directory
from src.core.people_entity_schema import (
    AliasStatus,
    CanonicalEntity,
    EntitiesDocument,
    EntityAlias,
    EntityIdentifier,
    EntityRedirect,
    EntityStatus,
    load_entities_document,
    write_entities_document,
)
from src.core.people_membership_schema import TeamMembership, load_memberships, write_memberships
from src.core.people_namespace_bridge import resolve_ref_to_canonical_entity_id
from src.core.people_registry_corrections import bind_person_identifier, merge_people, split_person, unmerge_people
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_config, write_registry_config
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction
from src.core.profile_encryption import encrypt_people_profiles_file, inspect_people_profiles_file

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
_RUNNER = CliRunner()


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


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


def _seed_registry(
    knowledge_root: Path,
    *,
    include_mutables: bool = True,
    source_alias: str = "source",
    target_alias: str = "target",
) -> None:
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
            aliases=(_alias(source_alias), _alias("source-alt")),
            scope="org",
            created_at=_NOW,
            identifiers=(EntityIdentifier(provider="graph", kind="provider_subject", subject_id="source-stable"),),
        ),
        CanonicalEntity(
            workspace_id=config.workspace_id,
            entity_id="person:target",
            entity_type="person",
            canonical_name="Target",
            aliases=(_alias(target_alias),),
            scope="org",
            created_at=_NOW,
            identifiers=(EntityIdentifier(provider="graph", kind="provider_subject", subject_id="target-stable"),),
        ),
    )
    people = (
        PersonDirectory(entity_id="person:source", alias=source_alias, title="TPM", status=PersonStatus.ACTIVE),
        PersonDirectory(entity_id="person:target", alias=target_alias, status=PersonStatus.ACTIVE),
    )
    memberships = (
        TeamMembership(
            membership_id="membership:source",
            person_entity_id="person:source",
            team_entity_id="team:platform",
            role="member",
            valid_from=None,
            valid_until=None,
            source="synthetic",
            source_ref=None,
            observed_at=_NOW,
            verified_at=_NOW,
        ),
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=entities))
        write_people_directory(staged_dir / "people_directory.yaml", people)
        if include_mutables:
            write_memberships(staged_dir / "memberships.yaml", memberships)
            (staged_dir / "people_profiles.yaml").write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "2.0",
                        "profiles": [{"entity_id": "person:source", "alias": "source", "comm_style": "brief"}],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (staged_dir / "delegations.yaml").write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0",
                        "delegations": [
                            {
                                "delegation_id": "delegation:source",
                                "from_person_entity_id": "person:source",
                                "to_person_entity_id": "person:target",
                            }
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (staged_dir / ".cache").mkdir(parents=True)
            (staged_dir / ".cache" / "program_affiliation_cache.yaml").write_text(
                yaml.safe_dump({"entries": [{"person_entity_id": "person:source"}]}, sort_keys=False),
                encoding="utf-8",
            )

    def validate_staged(staged_dir: Path) -> None:
        assert load_entities_document(staged_dir / "entities.yaml") is not None
        assert load_people_directory(staged_dir / "people_directory.yaml") is not None
        if include_mutables:
            assert len(load_memberships(staged_dir / "memberships.yaml")) == 1

    paths = ("entities.yaml", "people_directory.yaml")
    if include_mutables:
        paths += ("memberships.yaml", "people_profiles.yaml", "delegations.yaml", ".cache/program_affiliation_cache.yaml")
    prepared = prepare_registry_files_transaction(
        knowledge_root,
        paths,
        owner="seed",
        write_staged_files=write_staged,
        validate_staged_files=validate_staged,
        as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def test_merge_redirects_current_profile_membership_delegation_and_affiliation_cache(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)

    preview = merge_people(
        knowledge_root,
        source_ref="source",
        target_ref="target",
        reason="reviewed duplicate",
        actor="<preview>",
        apply=False,
        as_of=_NOW,
    )
    assert preview.transaction_id is None
    assert "people_profiles.yaml" in preview.affected_paths

    result = merge_people(
        knowledge_root,
        source_ref="person:source",
        target_ref="person:target",
        reason="reviewed duplicate",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )

    document = load_entities_document(knowledge_root / "entities.yaml")
    assert document is not None
    source = next(entity for entity in document.entities if entity.entity_id == "person:source")
    target = next(entity for entity in document.entities if entity.entity_id == "person:target")
    assert source.status is EntityStatus.TOMBSTONED
    assert {(item.provider, item.subject_id) for item in target.identifiers} == {
        ("graph", "source-stable"),
        ("graph", "target-stable"),
    }
    assert resolve_ref_to_canonical_entity_id("person:source", entities=document.entities, redirects=document.redirects).canonical_entity_id == "person:target"
    assert [person.entity_id for person in load_people_directory(knowledge_root / "people_directory.yaml").people] == ["person:target"]
    assert load_memberships(knowledge_root / "memberships.yaml")[0].person_entity_id == "person:target"
    assert yaml.safe_load((knowledge_root / "people_profiles.yaml").read_text(encoding="utf-8"))["profiles"][0]["entity_id"] == "person:target"
    delegation = yaml.safe_load((knowledge_root / "delegations.yaml").read_text(encoding="utf-8"))["delegations"][0]
    assert delegation["from_person_entity_id"] == delegation["to_person_entity_id"] == "person:target"
    assert yaml.safe_load((knowledge_root / ".cache" / "program_affiliation_cache.yaml").read_text(encoding="utf-8"))["entries"][0]["person_entity_id"] == "person:target"
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    assert any(record["operation"] == "merge" and record["transaction_id"] == result.transaction_id for record in records)


def test_bind_rejects_stable_identifier_owned_by_another_person(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root, include_mutables=False)

    result = bind_person_identifier(
        knowledge_root,
        person_ref="target",
        provider="graph",
        subject_id="source-stable",
        reason="must not auto-merge same alias/stable-ID conflict",
        actor="<preview>",
        apply=False,
        as_of=_NOW,
    )

    assert result.conflicts[0].kind == "stable_identifier_already_bound"
    with pytest.raises(ConfigError, match="authorized directory steward"):
        bind_person_identifier(
            knowledge_root,
            person_ref="target",
            provider="ado",
            subject_id="new-id",
            reason="reviewed",
            actor="not-a-steward",
            apply=True,
            as_of=_NOW,
        )


def test_same_alias_with_distinct_stable_identifiers_never_auto_selects_a_merge_source(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root, include_mutables=False, source_alias="shared", target_alias="shared")
    document = load_entities_document(knowledge_root / "entities.yaml")
    assert document is not None
    assert {
        identifier.subject_id
        for entity in document.entities
        for identifier in entity.identifiers
    } == {"source-stable", "target-stable"}

    with pytest.raises(ConfigError, match="must resolve to exactly one"):
        merge_people(
            knowledge_root,
            source_ref="shared",
            target_ref="person:target",
            reason="a shared alias is insufficient evidence",
            actor="<preview>",
            apply=False,
            as_of=_NOW,
        )


def test_merge_rewrites_an_encrypted_profile_without_downgrading_its_envelope(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    fake_keyring = _FakeKeyring()
    monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
    encrypt_people_profiles_file(knowledge_root / "people_profiles.yaml")

    merge_people(
        knowledge_root,
        source_ref="source",
        target_ref="target",
        reason="reviewed encrypted-profile duplicate",
        actor="test_steward",
        apply=True,
        as_of=_NOW,
    )

    assert inspect_people_profiles_file(knowledge_root / "people_profiles.yaml").storage == "encrypted"
    profile_document = yaml.safe_load((knowledge_root / "people_profiles.yaml").read_text(encoding="utf-8"))
    assert profile_document["storage"] == "encrypted"
    merge_record = next(record for record in read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES) if record["operation"] == "merge")
    assert merge_record["before"]["restorable_paths"] == [
        ".cache/program_affiliation_cache.yaml",
        "delegations.yaml",
        "entities.yaml",
        "memberships.yaml",
        "people_directory.yaml",
        "people_profiles.yaml",
    ]
    assert "brief" not in json.dumps(merge_record)


def test_split_requires_complete_partition_and_reports_ambiguous_authored_references(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root, include_mutables=False)
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    (programs_root / "demo" / "program.yaml").write_text("owner_entity_id: person:source\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="explicit.*partition"):
        split_person(
            knowledge_root,
            person_ref="source",
            aliases_for_new_person=("source-alt",),
            aliases_retained_by_source=(),
            identifiers_for_new_person=(),
            identifiers_retained_by_source=(),
            reason="incomplete partition",
            actor="<preview>",
            apply=False,
            programs_root=programs_root,
            as_of=_NOW,
        )

    preview = split_person(
        knowledge_root,
        person_ref="source",
        aliases_for_new_person=("source-alt",),
        aliases_retained_by_source=("source",),
        identifiers_for_new_person=(),
        identifiers_retained_by_source=("graph:source-stable",),
        new_entity_id="person:split",
        reason="reviewed identity separation",
        actor="<preview>",
        apply=False,
        programs_root=programs_root,
        as_of=_NOW,
    )
    assert preview.conflicts[0].kind == "ambiguous_authored_reference"
    assert preview.authored_references[0].field_path == "owner_entity_id"
    applied = split_person(
        knowledge_root,
        person_ref="source",
        aliases_for_new_person=("source-alt",),
        aliases_retained_by_source=("source",),
        identifiers_for_new_person=(),
        identifiers_retained_by_source=("graph:source-stable",),
        new_entity_id="person:split",
        reason="reviewed identity separation",
        actor="test_steward",
        apply=True,
        programs_root=programs_root,
        as_of=_NOW,
    )
    assert applied.transaction_id is not None
    assert {person.entity_id for person in load_people_directory(knowledge_root / "people_directory.yaml").people} == {
        "person:source",
        "person:target",
        "person:split",
    }
    assert any(record["decision"] == "conflict" for record in read_journal_records(knowledge_root, STREAM_PEOPLE_CONFLICTS))


def test_cli_merge_is_preview_by_default_then_applies_as_authenticated_steward(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda command: OperatorIdentity(actor=command, principal="test_steward", machine="test", session="test"),
    )

    preview = _RUNNER.invoke(
        app,
        ["kb", "people", "merge", "--from", "source", "--into", "target", "--reason", "reviewed duplicate"],
    )
    assert preview.exit_code == 0
    assert "Preview: would apply merge" in preview.stdout
    assert load_entities_document(knowledge_root / "entities.yaml").entities[0].status is EntityStatus.ACTIVE

    applied = _RUNNER.invoke(
        app,
        ["kb", "people", "merge", "--from", "source", "--into", "target", "--reason", "reviewed duplicate", "--apply", "--format", "json"],
    )
    assert applied.exit_code == 0
    payload = json.loads(applied.stdout)
    assert payload["operation"] == "merge"
    assert payload["transaction_id"]


def test_cli_bind_split_and_unmerge_preview_routes(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_registry(knowledge_root)
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda command: OperatorIdentity(actor=command, principal="test_steward", machine="test", session="test"),
    )

    bind = _RUNNER.invoke(
        app,
        ["kb", "people", "bind", "--person", "target", "--provider", "ado", "--subject-id", "target-ado", "--reason", "reviewed"],
    )
    split = _RUNNER.invoke(
        app,
        [
            "kb",
            "people",
            "split",
            "--person",
            "source",
            "--alias",
            "source-alt",
            "--retain-alias",
            "source",
            "--retain-identifier",
            "graph:source-stable",
            "--reason",
            "reviewed",
        ],
    )
    merged = _RUNNER.invoke(
        app,
        ["kb", "people", "merge", "--from", "source", "--into", "target", "--reason", "reviewed", "--apply"],
    )
    unmerge = _RUNNER.invoke(
        app,
        ["kb", "people", "unmerge", "--from", "person:source", "--reason", "reviewed"],
    )

    assert bind.exit_code == split.exit_code == merged.exit_code == unmerge.exit_code == 0
    assert "Preview: would apply bind" in bind.stdout
    assert "Preview: would apply split" in split.stdout
    assert "Applied merge" in merged.stdout
    assert "Preview: would apply unmerge" in unmerge.stdout
