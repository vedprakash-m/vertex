"""PPL-W5b.2 delegation lifecycle write path coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.core.exceptions import ConfigError
from src.core.operator_identity import OperatorIdentity
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, read_journal_records
from src.core.people_delegation_lifecycle import create_delegation, list_delegations, revoke_delegation
from src.core.people_delegation_schema import DelegationStatus, delegations_path, load_delegations
from src.core.people_directory_schema import PersonDirectory, PersonStatus, write_people_directory
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_config, write_registry_config
from src.core.people_registry_modes import set_registry_flag
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
_RUNNER = CliRunner()


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="test_steward",
    )


def _seed(knowledge_root: Path, *, delegation_enabled: bool = True) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    config = load_registry_config(knowledge_root)
    assert config is not None
    write_registry_config(knowledge_root / "registry.yaml", replace(config, directory_steward_principals=("test_steward",)))
    if delegation_enabled:
        set_registry_flag(knowledge_root, "delegation_enabled", True, actor="test_steward")

    entities = (
        CanonicalEntity(
            workspace_id=config.workspace_id, entity_id="person:alice", entity_type="person", canonical_name="Alice",
            aliases=(_alias("alice"),), scope="org", created_at=_NOW,
        ),
        CanonicalEntity(
            workspace_id=config.workspace_id, entity_id="person:bob", entity_type="person", canonical_name="Bob",
            aliases=(_alias("bob"),), scope="org", created_at=_NOW,
        ),
    )
    people = (
        PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),
        PersonDirectory(entity_id="person:bob", alias="bob", status=PersonStatus.ACTIVE),
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=entities))
        write_people_directory(staged_dir / "people_directory.yaml", people)

    def validate_staged(staged_dir: Path) -> None:
        pass

    prepared = prepare_registry_files_transaction(
        knowledge_root, ("entities.yaml", "people_directory.yaml"), owner="seed",
        write_staged_files=write_staged, validate_staged_files=validate_staged, as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def _window() -> tuple[datetime, datetime]:
    return _NOW, _NOW + timedelta(days=14)


def test_preview_does_not_require_kill_switch_or_steward_and_does_not_write(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, delegation_enabled=False)
    valid_from, valid_until = _window()

    delegation = create_delegation(
        knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
        valid_from=valid_from, valid_until=valid_until, reason="Alice on leave.",
        actor="<preview>", apply=False,
    )

    assert delegation.from_person_entity_id == "person:alice"
    assert delegation.to_person_entity_id == "person:bob"
    assert not delegations_path(knowledge_root).exists()


def test_create_with_kill_switch_off_is_rejected_before_any_write(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, delegation_enabled=False)
    valid_from, valid_until = _window()

    with pytest.raises(ConfigError, match="disabled"):
        create_delegation(
            knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
            valid_from=valid_from, valid_until=valid_until, reason="Alice on leave.",
            actor="test_steward", apply=True,
        )
    assert not delegations_path(knowledge_root).exists()


def test_create_by_non_steward_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    valid_from, valid_until = _window()

    with pytest.raises(ConfigError, match="not an authorized directory steward"):
        create_delegation(
            knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
            valid_from=valid_from, valid_until=valid_until, reason="Alice on leave.",
            actor="rando", apply=True,
        )


def test_create_applied_produces_a_journaled_delegation_readable_via_list(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    valid_from, valid_until = _window()

    delegation = create_delegation(
        knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
        valid_from=valid_from, valid_until=valid_until, reason="Alice on leave.",
        actor="test_steward", apply=True, program_ids=("acme",),
    )

    assert delegation.status is DelegationStatus.ACTIVE
    persisted = load_delegations(delegations_path(knowledge_root))
    assert persisted == (delegation,)

    listed = list_delegations(knowledge_root)
    assert listed == (delegation,)

    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    matching = [record for record in records if record.get("entity_id") == delegation.delegation_id]
    assert len(matching) == 1
    assert matching[0]["operation"] == "delegation_create"


def test_self_delegation_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    valid_from, valid_until = _window()

    with pytest.raises(ConfigError, match="cannot delegate to themselves"):
        create_delegation(
            knowledge_root, from_ref="alice", to_ref="alice", surfaces=("vertex::nudge",),
            valid_from=valid_from, valid_until=valid_until, reason="x",
            actor="test_steward", apply=True,
        )


def test_unresolvable_ref_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    valid_from, valid_until = _window()

    with pytest.raises(ConfigError, match="does not resolve"):
        create_delegation(
            knowledge_root, from_ref="nonexistent", to_ref="bob", surfaces=("vertex::nudge",),
            valid_from=valid_from, valid_until=valid_until, reason="x",
            actor="test_steward", apply=True,
        )


def test_valid_until_before_valid_from_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    with pytest.raises(ConfigError, match="valid_until must be after valid_from"):
        create_delegation(
            knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
            valid_from=_NOW, valid_until=_NOW - timedelta(days=1), reason="x",
            actor="test_steward", apply=True,
        )


def test_revoke_applied_flips_status_and_journals(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    valid_from, valid_until = _window()
    delegation = create_delegation(
        knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
        valid_from=valid_from, valid_until=valid_until, reason="Alice on leave.",
        actor="test_steward", apply=True,
    )

    revoked = revoke_delegation(
        knowledge_root, delegation_id=delegation.delegation_id, reason="Alice is back.",
        actor="test_steward", apply=True,
    )

    assert revoked.status is DelegationStatus.REVOKED
    persisted = load_delegations(delegations_path(knowledge_root))
    assert persisted == (revoked,)
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    matching = [record for record in records if record.get("operation") == "delegation_revoke"]
    assert len(matching) == 1


def test_revoke_nonexistent_delegation_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    with pytest.raises(ConfigError, match="does not exist"):
        revoke_delegation(knowledge_root, delegation_id="delegation:missing", reason="x", actor="test_steward", apply=True)


def test_revoke_already_revoked_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    valid_from, valid_until = _window()
    delegation = create_delegation(
        knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
        valid_from=valid_from, valid_until=valid_until, reason="x", actor="test_steward", apply=True,
    )
    revoke_delegation(knowledge_root, delegation_id=delegation.delegation_id, reason="x", actor="test_steward", apply=True)

    with pytest.raises(ConfigError, match="already revoked"):
        revoke_delegation(knowledge_root, delegation_id=delegation.delegation_id, reason="x", actor="test_steward", apply=True)


def test_list_active_only_excludes_revoked_and_out_of_window(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    active = create_delegation(
        knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
        valid_from=_NOW, valid_until=_NOW + timedelta(days=14), reason="x", actor="test_steward", apply=True,
    )
    expired = create_delegation(
        knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::brief",),
        valid_from=_NOW - timedelta(days=30), valid_until=_NOW - timedelta(days=1), reason="x",
        actor="test_steward", apply=True,
    )
    revoke_delegation(knowledge_root, delegation_id=expired.delegation_id, reason="x", actor="test_steward", apply=True)

    listed = list_delegations(knowledge_root, active_only=True, as_of=_NOW)

    assert listed == (active,)


def test_cli_delegate_create_is_preview_by_default_then_applies_as_authenticated_steward(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda command: OperatorIdentity(actor=command, principal="test_steward", machine="test", session="test"),
    )
    create_args = [
        "kb", "people", "delegate", "create",
        "--from", "alice", "--to", "bob", "--surface", "vertex::nudge",
        "--valid-from", "2026-07-20T00:00:00+00:00", "--valid-until", "2026-08-03T00:00:00+00:00",
        "--reason", "Alice on leave.",
    ]

    preview = _RUNNER.invoke(app, create_args)
    assert preview.exit_code == 0
    assert "Preview: would create" in preview.stdout
    assert not delegations_path(knowledge_root).exists()

    applied = _RUNNER.invoke(app, [*create_args, "--apply", "--format", "json"])
    assert applied.exit_code == 0
    payload = json.loads(applied.stdout)
    assert payload["from_person_entity_id"] == "person:alice"
    assert payload["to_person_entity_id"] == "person:bob"
    assert payload["status"] == "active"
    persisted = load_delegations(delegations_path(knowledge_root))
    assert len(persisted) == 1


def test_cli_delegate_revoke_and_list(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda command: OperatorIdentity(actor=command, principal="test_steward", machine="test", session="test"),
    )
    delegation = create_delegation(
        knowledge_root, from_ref="alice", to_ref="bob", surfaces=("vertex::nudge",),
        valid_from=_NOW, valid_until=_NOW + timedelta(days=14), reason="x", actor="test_steward", apply=True,
    )

    listed = _RUNNER.invoke(app, ["kb", "people", "delegate", "list", "--format", "json"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)[0]["delegation_id"] == delegation.delegation_id

    revoked = _RUNNER.invoke(
        app,
        ["kb", "people", "delegate", "revoke", "--delegation-id", delegation.delegation_id, "--reason", "back", "--apply", "--format", "json"],
    )
    assert revoked.exit_code == 0
    assert json.loads(revoked.stdout)["status"] == "revoked"
