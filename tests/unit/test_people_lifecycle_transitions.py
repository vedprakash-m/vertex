"""PPL-W6.3a/PPL-W6.3b lifecycle-status transition + identity.lifecycle_changed
event coverage."""

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
from src.core.people_directory_schema import PersonDirectory, PersonStatus, write_people_directory
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document
from src.core.people_lifecycle_transitions import transition_person_lifecycle_status
from src.core.people_material_ledger_events import EVENT_IDENTITY_LIFECYCLE_CHANGED
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships
from src.core.people_directory_schema import Team, TeamKind, TeamStatus, write_teams
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_config, write_registry_config
from src.core.people_registry_outbox import pending_registry_outbox_items_for_program
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction

_NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)
_RUNNER = CliRunner()


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="test_steward",
    )


def _seed(knowledge_root: Path, *, with_team_membership: bool = False, program_id: str = "acme") -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    config = load_registry_config(knowledge_root)
    assert config is not None
    write_registry_config(knowledge_root / "registry.yaml", replace(config, directory_steward_principals=("test_steward",)))

    entities = [
        CanonicalEntity(
            workspace_id=config.workspace_id, entity_id="person:alice", entity_type="person", canonical_name="Alice",
            aliases=(_alias("alice"),), scope="org", created_at=_NOW,
        ),
    ]
    memberships = ()
    if with_team_membership:
        entities.append(
            CanonicalEntity(
                workspace_id=config.workspace_id, entity_id="team:platform", entity_type="team", canonical_name="Platform",
                aliases=(_alias("platform"),), scope="org", created_at=_NOW,
            ),
        )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", EntitiesDocument(schema_version="2.0", entities=tuple(entities)))
        write_people_directory(staged_dir / "people_directory.yaml", (PersonDirectory(entity_id="person:alice", alias="alice", status=PersonStatus.ACTIVE),))
        if with_team_membership:
            write_teams(staged_dir / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM, status=TeamStatus.ACTIVE, legacy_programs=(program_id,)),))
            write_memberships(
                staged_dir / "memberships.yaml",
                (
                    TeamMembership(
                        membership_id="m1", person_entity_id="person:alice", team_entity_id="team:platform", role="member",
                        valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW,
                        status=MembershipStatus.ACTIVE,
                    ),
                ),
            )

    def validate_staged(staged_dir: Path) -> None:
        pass

    paths = ("entities.yaml", "people_directory.yaml")
    if with_team_membership:
        paths += ("teams.yaml", "memberships.yaml")
    prepared = prepare_registry_files_transaction(
        knowledge_root, paths, owner="seed", write_staged_files=write_staged, validate_staged_files=validate_staged, as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def test_preview_does_not_require_steward_and_does_not_write(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    result = transition_person_lifecycle_status(
        knowledge_root, person_ref="alice", new_status=PersonStatus.DEPARTED, reason="left the company",
        actor="<preview>", apply=False,
    )

    assert result.entity_id == "person:alice"
    assert result.from_status is PersonStatus.ACTIVE
    assert result.to_status is PersonStatus.DEPARTED
    assert result.transaction_id is None


def test_apply_by_non_steward_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    with pytest.raises(ConfigError, match="not an authorized directory steward"):
        transition_person_lifecycle_status(
            knowledge_root, person_ref="alice", new_status=PersonStatus.DEPARTED, reason="left the company",
            actor="rando", apply=True,
        )


def test_apply_transitions_status_and_journals(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    result = transition_person_lifecycle_status(
        knowledge_root, person_ref="alice", new_status=PersonStatus.DEPARTED, reason="left the company",
        actor="test_steward", apply=True, as_of=_NOW,
    )

    assert result.transaction_id is not None
    from src.core.people_directory_schema import load_people_directory
    directory = load_people_directory(knowledge_root / "people_directory.yaml")
    assert directory is not None
    alice = next(p for p in directory.people if p.entity_id == "person:alice")
    assert alice.status is PersonStatus.DEPARTED
    assert alice.departed_at == _NOW

    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    matching = [r for r in records if r.get("entity_id") == "person:alice" and r.get("operation") == "lifecycle_transition"]
    assert len(matching) == 1
    assert matching[0]["field"] == "status"


def test_rehire_clears_departed_at(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    transition_person_lifecycle_status(
        knowledge_root, person_ref="alice", new_status=PersonStatus.DEPARTED, reason="left", actor="test_steward", apply=True, as_of=_NOW,
    )

    transition_person_lifecycle_status(
        knowledge_root, person_ref="alice", new_status=PersonStatus.ACTIVE, reason="rehired",
        actor="test_steward", apply=True, as_of=_NOW + timedelta(days=30),
    )

    from src.core.people_directory_schema import load_people_directory
    directory = load_people_directory(knowledge_root / "people_directory.yaml")
    assert directory is not None
    alice = next(p for p in directory.people if p.entity_id == "person:alice")
    assert alice.status is PersonStatus.ACTIVE
    assert alice.departed_at is None


def test_same_status_transition_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    with pytest.raises(ConfigError, match="already has status"):
        transition_person_lifecycle_status(
            knowledge_root, person_ref="alice", new_status=PersonStatus.ACTIVE, reason="noop", actor="test_steward", apply=True,
        )


def test_unresolvable_person_ref_is_rejected(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    with pytest.raises(ConfigError, match="must resolve to exactly one"):
        transition_person_lifecycle_status(
            knowledge_root, person_ref="ghost", new_status=PersonStatus.DEPARTED, reason="x", actor="test_steward", apply=True,
        )


def test_apply_enqueues_identity_lifecycle_changed_event(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, with_team_membership=True, program_id="acme")

    transition_person_lifecycle_status(
        knowledge_root, person_ref="alice", new_status=PersonStatus.DEPARTED, reason="left the company",
        actor="test_steward", apply=True, as_of=_NOW,
    )

    pending = pending_registry_outbox_items_for_program(knowledge_root, "acme")
    assert len(pending) == 1
    payload = json.loads(pending[0].payload_json)
    assert payload["event_type"] == EVENT_IDENTITY_LIFECYCLE_CHANGED
    assert payload["from_status"] == "active"
    assert payload["to_status"] == "departed"


def test_apply_with_no_program_affiliation_enqueues_nothing(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, with_team_membership=False)

    transition_person_lifecycle_status(
        knowledge_root, person_ref="alice", new_status=PersonStatus.DEPARTED, reason="left the company",
        actor="test_steward", apply=True, as_of=_NOW,
    )

    assert pending_registry_outbox_items_for_program(knowledge_root, "acme") == ()


def test_cli_lifecycle_set_is_preview_by_default_then_applies_as_authenticated_steward(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.kb.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.kb.capture_operator_identity",
        lambda command: OperatorIdentity(actor=command, principal="test_steward", machine="test", session="test"),
    )

    preview = _RUNNER.invoke(
        app, ["kb", "people", "lifecycle-set", "--person", "alice", "--status", "departed", "--reason", "left the company"],
    )
    assert preview.exit_code == 0
    assert "Preview: would apply" in preview.stdout

    applied = _RUNNER.invoke(
        app,
        ["kb", "people", "lifecycle-set", "--person", "alice", "--status", "departed", "--reason", "left the company", "--apply", "--format", "json"],
    )
    assert applied.exit_code == 0
    payload = json.loads(applied.stdout)
    assert payload["from_status"] == "active"
    assert payload["to_status"] == "departed"
    assert payload["transaction_id"]


def test_cli_lifecycle_set_rejects_an_invalid_status(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    result = _RUNNER.invoke(app, ["kb", "people", "lifecycle-set", "--person", "alice", "--status", "retired", "--reason", "x"])

    assert result.exit_code != 0
