from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.audience_scope_recipients import merge_audience_scope_recipients, resolve_audience_scope_recipients
from src.core.exceptions import ConfigError
from src.core.audience_scopes import audience_scopes_path_for_program
from src.core.nudge_models import NudgeAudiencePolicy, NudgeDeliveryConfig, ResolvedRecipient
from src.core.people_delegation_schema import Delegation, DelegationStatus, delegations_path, write_delegations
from src.core.people_directory_schema import (
    ContactKind,
    ContactPoint,
    ContactStatus,
    PersonDirectory,
    PersonStatus,
    Team,
    TeamKind,
    TeamStatus,
    write_people_directory,
    write_teams,
)
from src.core.people_entity_schema import AliasStatus, CanonicalEntity, EntitiesDocument, EntityAlias, write_entities_document
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships
from src.core.people_registry_identity import bootstrap_registry_identity
from src.core.people_registry_modes import set_registry_flag

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _alias(value: str) -> EntityAlias:
    return EntityAlias(
        value=value, kind="vertex::alias", status=AliasStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward",
    )


def _contact(email: str) -> ContactPoint:
    return ContactPoint(
        kind=ContactKind.PRIMARY_EMAIL, value=email, status=ContactStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward", delivery_eligible=True,
    )


def _seed(programs_root: Path, *, audience_scopes_enabled: bool = True) -> Path:
    knowledge_root = programs_root.parent / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    if audience_scopes_enabled:
        set_registry_flag(knowledge_root, "audience_scopes_enabled", True, actor="test-principal")
    write_entities_document(
        knowledge_root / "entities.yaml",
        EntitiesDocument(
            schema_version="2.0",
            entities=(
                CanonicalEntity(workspace_id="ws", entity_id="team:platform", entity_type="team", canonical_name="Platform", aliases=(_alias("platform"),), scope="org", created_at=_NOW),
                CanonicalEntity(workspace_id="ws", entity_id="person:jdoe", entity_type="person", canonical_name="Jane", aliases=(_alias("jdoe"),), scope="org", created_at=_NOW),
            ),
        ),
    )
    write_teams(knowledge_root / "teams.yaml", (Team(entity_id="team:platform", id="platform", name="Platform", kind=TeamKind.ORG_TEAM, status=TeamStatus.ACTIVE),))
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(membership_id="m1", person_entity_id="person:jdoe", team_entity_id="team:platform", role="member", valid_from=_NOW, valid_until=None, source="test", source_ref=None, observed_at=_NOW, verified_at=_NOW, status=MembershipStatus.ACTIVE),
        ),
    )
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (PersonDirectory(entity_id="person:jdoe", alias="jdoe", status=PersonStatus.ACTIVE, contacts=(_contact("jdoe@acme.com"),)),),
    )
    scope_path = audience_scopes_path_for_program("acme", programs_root=programs_root)
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text('schema_version: "1.0"\naudience_scopes:\n  engineering_hygiene:\n    team_refs: [platform]\n', encoding="utf-8")
    return knowledge_root


def _delivery(*, audience_scope_ids: tuple[str, ...] = ()) -> NudgeDeliveryConfig:
    return NudgeDeliveryConfig(recipient="pm@acme.com", delivery_mode="broadcast", cadence_days=7, audience_scope_ids=audience_scope_ids)


def test_empty_audience_scope_ids_is_a_true_no_op(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)

    recipients = resolve_audience_scope_recipients(
        program_id="acme", delivery=_delivery(), audience_policy=None, programs_root=programs_root,
        is_valid_email=lambda email: True,
    )

    assert recipients == []


def test_resolves_a_real_team_expansion_scope_to_a_recipient(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)

    recipients = resolve_audience_scope_recipients(
        program_id="acme", delivery=_delivery(audience_scope_ids=("engineering_hygiene",)),
        audience_policy=None, programs_root=programs_root, is_valid_email=lambda email: True,
    )

    assert len(recipients) == 1
    assert recipients[0] == ResolvedRecipient(alias="jdoe", email="jdoe@acme.com", display_name="jdoe")


def test_kill_switch_off_is_a_true_no_op(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root, audience_scopes_enabled=False)

    recipients = resolve_audience_scope_recipients(
        program_id="acme", delivery=_delivery(audience_scope_ids=("engineering_hygiene",)),
        audience_policy=None, programs_root=programs_root, is_valid_email=lambda email: True,
    )

    assert recipients == []


def test_undefined_scope_id_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)

    with pytest.raises(ConfigError, match="undefined audience scope"):
        resolve_audience_scope_recipients(
            program_id="acme", delivery=_delivery(audience_scope_ids=("nonexistent",)),
            audience_policy=None, programs_root=programs_root, is_valid_email=lambda email: True,
        )


def test_opt_out_from_audience_policy_removes_the_candidate(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)
    policy = NudgeAudiencePolicy(allowed_domains=("acme.com",), opt_out=frozenset({"jdoe"}))

    recipients = resolve_audience_scope_recipients(
        program_id="acme", delivery=_delivery(audience_scope_ids=("engineering_hygiene",)),
        audience_policy=policy, programs_root=programs_root, is_valid_email=lambda email: True,
    )

    assert recipients == []


def test_invalid_email_is_skipped_not_crashed_on(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)

    recipients = resolve_audience_scope_recipients(
        program_id="acme", delivery=_delivery(audience_scope_ids=("engineering_hygiene",)),
        audience_policy=None, programs_root=programs_root, is_valid_email=lambda email: False,
    )

    assert recipients == []


def test_merge_is_additive_and_deduplicates_by_email() -> None:
    existing = [ResolvedRecipient(alias="alice", email="alice@acme.com", display_name="Alice")]
    new = [
        ResolvedRecipient(alias="alice", email="ALICE@acme.com", display_name="Alice Duplicate"),
        ResolvedRecipient(alias="bob", email="bob@acme.com", display_name="Bob"),
    ]

    merged = merge_audience_scope_recipients(existing, new)

    assert len(merged) == 2
    assert merged[0].email == "alice@acme.com"
    assert merged[1].email == "bob@acme.com"


def test_merge_with_no_new_recipients_returns_the_same_list() -> None:
    existing = [ResolvedRecipient(alias="alice", email="alice@acme.com", display_name="Alice")]

    merged = merge_audience_scope_recipients(existing, [])

    assert merged is existing


def test_active_delegation_routes_the_recipient_to_the_delegates_email(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    knowledge_root = _seed(programs_root)
    set_registry_flag(knowledge_root, "delegation_enabled", True, actor="test-principal")
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (
            PersonDirectory(entity_id="person:jdoe", alias="jdoe", status=PersonStatus.ACTIVE, contacts=(_contact("jdoe@acme.com"),)),
            PersonDirectory(entity_id="person:carol", alias="carol", status=PersonStatus.ACTIVE, contacts=(_contact("carol@acme.com"),)),
        ),
    )
    write_delegations(
        delegations_path(knowledge_root),
        (
            Delegation(
                delegation_id="delegation:1", from_person_entity_id="person:jdoe", to_person_entity_id="person:carol",
                surfaces=("vertex::nudge",), valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
                valid_until=datetime(2035, 1, 1, tzinfo=timezone.utc), reason="x", actor_principal="test-principal",
                status=DelegationStatus.ACTIVE,
            ),
        ),
    )

    recipients = resolve_audience_scope_recipients(
        program_id="acme", delivery=_delivery(audience_scope_ids=("engineering_hygiene",)),
        audience_policy=None, programs_root=programs_root, is_valid_email=lambda email: True,
    )

    assert len(recipients) == 1
    assert recipients[0] == ResolvedRecipient(alias="carol", email="carol@acme.com", display_name="carol")
