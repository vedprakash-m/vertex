from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.audience_precedence import (
    STAGE_INACTIVE_STATUS,
    STAGE_OPT_OUTS,
    STAGE_TENANT_GUEST_POLICY,
    DelegatedRouting,
    apply_precedence_pipeline,
)
from src.core.people_delegation_schema import Delegation, DelegationStatus, delegations_path, write_delegations
from src.core.people_directory_schema import ContactKind, ContactPoint, ContactStatus, PersonDirectory, PersonStatus, write_people_directory
from src.core.people_registry_identity import bootstrap_registry_identity
from src.core.people_registry_modes import set_registry_flag

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _contact(email: str) -> ContactPoint:
    return ContactPoint(
        kind=ContactKind.PRIMARY_EMAIL, value=email, status=ContactStatus.ACTIVE, valid_from=None, valid_until=None,
        source="test", source_ref=None, recorded_at=_NOW, verified_at=_NOW, verified_by_principal="steward", delivery_eligible=True,
    )


def _seed(knowledge_root: Path) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    write_people_directory(
        knowledge_root / "people_directory.yaml",
        (
            PersonDirectory(entity_id="person:fresh", alias="fresh", status=PersonStatus.ACTIVE, contacts=(_contact("fresh@acme.com"),)),
            PersonDirectory(entity_id="person:external", alias="external", status=PersonStatus.ACTIVE, contacts=(_contact("external@other.com"),)),
            PersonDirectory(entity_id="person:optedout", alias="optedout", status=PersonStatus.ACTIVE, contacts=(_contact("optedout@acme.com"),)),
            PersonDirectory(entity_id="person:inactive", alias="inactive", status=PersonStatus.DEPARTED, contacts=(_contact("inactive@acme.com"),)),
            PersonDirectory(entity_id="person:externalinactive", alias="externalinactive", status=PersonStatus.DEPARTED, contacts=(_contact("externalinactive@other.com"),)),
        ),
    )


def test_fresh_candidate_passes_every_stage(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:fresh",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(),
    )

    assert remaining == ("person:fresh",)
    assert exclusions == ()


def test_external_domain_excluded_at_tenant_guest_stage(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:external",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(),
    )

    assert remaining == ()
    assert exclusions[0].stage == STAGE_TENANT_GUEST_POLICY


def test_external_domain_passes_when_scope_allows_external_guests(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:external",), knowledge_root=knowledge_root, scope_allow_external_guests=True,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(),
    )

    assert remaining == ("person:external",)
    assert exclusions == ()


def test_opted_out_person_excluded_at_opt_outs_stage(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:optedout",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset({"optedout"}),
    )

    assert remaining == ()
    assert exclusions[0].stage == STAGE_OPT_OUTS


def test_inactive_person_excluded_at_inactive_status_stage(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:inactive",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(),
    )

    assert remaining == ()
    assert exclusions[0].stage == STAGE_INACTIVE_STATUS


def test_ambiguous_identity_excluded_when_no_directory_record_exists(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:ghost",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(),
    )

    assert remaining == ()
    assert exclusions[0].stage == "ambiguous_identity"


def test_a_candidate_matching_two_stages_is_excluded_only_at_the_earlier_one(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:externalinactive",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(),
    )

    assert remaining == ()
    assert len(exclusions) == 1
    assert exclusions[0].stage == STAGE_TENANT_GUEST_POLICY  # stage 2, before stage 5's inactive check


def test_multiple_candidates_are_independently_evaluated(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, _ = apply_precedence_pipeline(
        ("person:fresh", "person:external", "person:inactive"), knowledge_root=knowledge_root,
        scope_allow_external_guests=False, allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(),
    )

    assert remaining == ("person:fresh",)
    assert len(exclusions) == 2


def _bootstrap(knowledge_root: Path, *, delegation_enabled: bool = True) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)
    if delegation_enabled:
        set_registry_flag(knowledge_root, "delegation_enabled", True, actor="test_steward")


def _write_delegation(knowledge_root: Path, *, status: DelegationStatus = DelegationStatus.ACTIVE) -> None:
    write_delegations(
        delegations_path(knowledge_root),
        (
            Delegation(
                delegation_id="delegation:1", from_person_entity_id="person:fresh", to_person_entity_id="person:external",
                surfaces=("vertex::nudge",), valid_from=_NOW, valid_until=_NOW + timedelta(days=14),
                reason="Alice on leave.", actor_principal="test_steward", status=status,
            ),
        ),
    )


def test_active_delegation_routes_without_excluding_the_original_person(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    _bootstrap(knowledge_root)
    _write_delegation(knowledge_root)

    remaining, exclusions, routings = apply_precedence_pipeline(
        ("person:fresh",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(), as_of=_NOW,
    )

    assert remaining == ("person:fresh",)
    assert exclusions == ()
    assert routings == (DelegatedRouting(person_entity_id="person:fresh", delegate_entity_id="person:external", delegation_id="delegation:1"),)


def test_kill_switch_off_leaves_stage_three_a_true_pass_through(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    _bootstrap(knowledge_root, delegation_enabled=False)
    _write_delegation(knowledge_root)

    remaining, exclusions, routings = apply_precedence_pipeline(
        ("person:fresh",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(), as_of=_NOW,
    )

    assert remaining == ("person:fresh",)
    assert routings == ()


def test_unbootstrapped_registry_leaves_stage_three_a_true_pass_through(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)

    remaining, exclusions, routings = apply_precedence_pipeline(
        ("person:fresh",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(), as_of=_NOW,
    )

    assert remaining == ("person:fresh",)
    assert routings == ()


def test_revoked_delegation_produces_no_routing(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root)
    _bootstrap(knowledge_root)
    _write_delegation(knowledge_root, status=DelegationStatus.REVOKED)

    remaining, exclusions, routings = apply_precedence_pipeline(
        ("person:fresh",), knowledge_root=knowledge_root, scope_allow_external_guests=False,
        allowed_domains=frozenset({"acme.com"}), opt_out_aliases=frozenset(), as_of=_NOW,
    )

    assert remaining == ("person:fresh",)
    assert routings == ()
