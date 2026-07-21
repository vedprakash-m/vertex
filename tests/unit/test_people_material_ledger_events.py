"""PPL-W6.1/PPL-W6.2 material-ledger event registration coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

from src.core.people_directory_schema import PersonDirectory, PersonStatus, Team, TeamKind, TeamStatus, write_people_directory, write_teams
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document
from src.core.people_material_ledger_events import (
    EVENT_OWNERSHIP_CHANGED,
    EVENT_TEAM_MEMBERSHIP_CHANGED,
    enqueue_ownership_changed_event,
    enqueue_team_membership_changed_events,
)
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships
from src.core.people_registry_corrections import merge_people
from src.core.people_registry_identity import bootstrap_registry_identity
from src.core.people_registry_outbox import pending_registry_outbox_items_for_program
from src.core.people_registry_transaction import commit_registry_files_transaction, prepare_registry_files_transaction
from src.core.people_registry_writer import RegistryPatchOperation, apply_shared_registry_patch

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
    )


def _seed(knowledge_root: Path, *, with_membership: bool = True) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    entities = EntitiesDocument(
        schema_version="2.0",
        entities=(
            CanonicalEntity(workspace_id="workspace:synthetic", entity_id="person:alice", entity_type="person", canonical_name="Alice", aliases=(_alias("alice"),), scope="org", created_at=_NOW),
            CanonicalEntity(workspace_id="workspace:synthetic", entity_id="team:platform", entity_type="team", canonical_name="Platform", aliases=(_alias("platform"),), scope="org", created_at=_NOW),
        ),
    )
    person = PersonDirectory(entity_id="person:alice", alias="alice", display_name="Alice", status=PersonStatus.ACTIVE)
    team = Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM, status=TeamStatus.ACTIVE, legacy_programs=("acme",))
    memberships = (
        TeamMembership(
            membership_id="m1", person_entity_id="person:alice", team_entity_id="team:platform", role="member",
            valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW,
            status=MembershipStatus.ACTIVE,
        ),
    ) if with_membership else ()

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", entities)
        write_people_directory(staged_dir / "people_directory.yaml", (person,))
        write_teams(staged_dir / "teams.yaml", (team,))
        write_memberships(staged_dir / "memberships.yaml", memberships)

    def validate_staged(staged_dir: Path) -> None:
        pass

    prepared = prepare_registry_files_transaction(
        knowledge_root, ("entities.yaml", "people_directory.yaml", "teams.yaml", "memberships.yaml"),
        owner="seed", write_staged_files=write_staged, validate_staged_files=validate_staged, as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)


def test_enqueue_team_membership_changed_events_enqueues_per_affected_program(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    enqueued = enqueue_team_membership_changed_events(
        knowledge_root, transaction_id="tx-1", person_entity_ids=("person:alice",),
    )

    assert enqueued == {"person:alice": ("acme",)}
    pending = pending_registry_outbox_items_for_program(knowledge_root, "acme")
    assert len(pending) == 1
    payload = json.loads(pending[0].payload_json)
    assert payload["event_type"] == EVENT_TEAM_MEMBERSHIP_CHANGED
    assert payload["person_entity_id"] == "person:alice"


def test_enqueue_team_membership_changed_events_no_memberships_enqueues_nothing(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, with_membership=False)

    enqueued = enqueue_team_membership_changed_events(
        knowledge_root, transaction_id="tx-1", person_entity_ids=("person:alice",),
    )

    assert enqueued == {"person:alice": ()}
    assert pending_registry_outbox_items_for_program(knowledge_root, "acme") == ()


def test_enqueue_ownership_changed_event(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    program_ids = enqueue_ownership_changed_event(
        knowledge_root, transaction_id="tx-1", person_entity_id="person:alice", new_manager_entity_id="person:bob",
    )

    assert program_ids == ("acme",)
    pending = pending_registry_outbox_items_for_program(knowledge_root, "acme")
    assert len(pending) == 1
    payload = json.loads(pending[0].payload_json)
    assert payload["event_type"] == EVENT_OWNERSHIP_CHANGED
    assert payload["new_manager_entity_id"] == "person:bob"


def test_real_membership_patch_through_apply_shared_registry_patch_enqueues_event(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = programs_root.parent / "knowledge"
    _seed(knowledge_root, with_membership=False)

    result = apply_shared_registry_patch(
        operations=(
            RegistryPatchOperation(
                relative_path="knowledge/people_directory.yaml", action="set_fields", match_value="alice",
                fields=(("team_ids", ("platform",)),),
            ),
        ),
        programs_root=programs_root, actor="test-principal", reason="test membership add", source="test", apply=True,
    )

    assert result.transaction_id is not None
    pending = pending_registry_outbox_items_for_program(knowledge_root, "acme")
    assert len(pending) == 1
    payload = json.loads(pending[0].payload_json)
    assert payload["event_type"] == EVENT_TEAM_MEMBERSHIP_CHANGED
    assert payload["person_entity_id"] == "person:alice"


def test_real_merge_with_a_report_enqueues_ownership_changed(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    from src.core.people_registry_identity import load_registry_config, write_registry_config

    config = load_registry_config(knowledge_root)
    assert config is not None
    write_registry_config(knowledge_root / "registry.yaml", replace(config, directory_steward_principals=("test_steward",)))

    entities = EntitiesDocument(
        schema_version="2.0",
        entities=(
            CanonicalEntity(workspace_id=config.workspace_id, entity_id="person:source", entity_type="person", canonical_name="Source", aliases=(_alias("source"),), scope="org", created_at=_NOW),
            CanonicalEntity(workspace_id=config.workspace_id, entity_id="person:target", entity_type="person", canonical_name="Target", aliases=(_alias("target"),), scope="org", created_at=_NOW),
            CanonicalEntity(workspace_id=config.workspace_id, entity_id="team:platform", entity_type="team", canonical_name="Platform", aliases=(_alias("platform"),), scope="org", created_at=_NOW),
        ),
    )
    people = (
        PersonDirectory(entity_id="person:source", alias="source", status=PersonStatus.ACTIVE),
        PersonDirectory(entity_id="person:target", alias="target", status=PersonStatus.ACTIVE),
    )
    team = Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM, status=TeamStatus.ACTIVE, legacy_programs=("acme",))
    memberships = (
        TeamMembership(
            membership_id="m1", person_entity_id="person:target", team_entity_id="team:platform", role="member",
            valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW,
            status=MembershipStatus.ACTIVE,
        ),
    )

    def write_staged(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", entities)
        write_people_directory(staged_dir / "people_directory.yaml", people)
        write_teams(staged_dir / "teams.yaml", (team,))
        write_memberships(staged_dir / "memberships.yaml", memberships)

    def validate_staged(staged_dir: Path) -> None:
        pass

    prepared = prepare_registry_files_transaction(
        knowledge_root, ("entities.yaml", "people_directory.yaml", "teams.yaml", "memberships.yaml"),
        owner="seed", write_staged_files=write_staged, validate_staged_files=validate_staged, as_of=_NOW,
    )
    commit_registry_files_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)

    # A third person reports to source; merging source into target should
    # rewrite that report's manager_entity_id to target and enqueue an
    # ownership.changed event. The report needs its OWN team membership
    # (not just target's) for `find_registry_program_affiliations` to
    # resolve any program for it at all.
    report = PersonDirectory(entity_id="person:report", alias="report", status=PersonStatus.ACTIVE, manager_entity_id="person:source")
    report_entity = CanonicalEntity(workspace_id=config.workspace_id, entity_id="person:report", entity_type="person", canonical_name="Report", aliases=(_alias("report"),), scope="org", created_at=_NOW)
    memberships_with_report = (
        *memberships,
        TeamMembership(
            membership_id="m2", person_entity_id="person:report", team_entity_id="team:platform", role="member",
            valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW,
            status=MembershipStatus.ACTIVE,
        ),
    )

    def write_staged_report(staged_dir: Path) -> None:
        write_entities_document(staged_dir / "entities.yaml", replace(entities, entities=(*entities.entities, report_entity)))
        write_people_directory(staged_dir / "people_directory.yaml", (*people, report))
        write_teams(staged_dir / "teams.yaml", (team,))
        write_memberships(staged_dir / "memberships.yaml", memberships_with_report)

    prepared_report = prepare_registry_files_transaction(
        knowledge_root, ("entities.yaml", "people_directory.yaml", "teams.yaml", "memberships.yaml"),
        owner="seed", write_staged_files=write_staged_report, validate_staged_files=validate_staged, as_of=_NOW,
    )
    commit_registry_files_transaction(prepared_report, knowledge_root=knowledge_root, as_of=_NOW)

    result = merge_people(
        knowledge_root, source_ref="source", target_ref="target", reason="reviewed duplicate",
        actor="test_steward", apply=True,
    )

    assert result.transaction_id is not None
    pending = pending_registry_outbox_items_for_program(knowledge_root, "acme")
    assert len(pending) == 1
    payload = json.loads(pending[0].payload_json)
    assert payload["event_type"] == EVENT_OWNERSHIP_CHANGED
    assert payload["person_entity_id"] == "person:report"
    assert payload["new_manager_entity_id"] == "person:target"
