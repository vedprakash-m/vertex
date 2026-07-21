from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.audience_freshness import filter_candidates_by_freshness
from src.core.audience_scope_resolver import AudienceCandidate
from src.core.people_directory_schema import (
    ContactKind,
    ContactPoint,
    ContactStatus,
    FieldVerification,
    PersonDirectory,
    PersonStatus,
    write_people_directory,
)
from src.core.people_membership_schema import MembershipStatus, TeamMembership, write_memberships

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _verification(field_name: str, *, age_days: int) -> FieldVerification:
    verified_at = _NOW - timedelta(days=age_days)
    return FieldVerification(
        field_name=field_name, source="test", source_ref=None, observed_at=verified_at,
        verified_at=verified_at, recorded_at=verified_at, verified_by_principal="steward",
    )


def _contact(*, age_days: int, delivery_eligible: bool = True) -> ContactPoint:
    verified_at = _NOW - timedelta(days=age_days)
    return ContactPoint(
        kind=ContactKind.PRIMARY_EMAIL, value="jdoe@example.com", status=ContactStatus.ACTIVE,
        valid_from=None, valid_until=None, source="test", source_ref=None,
        recorded_at=verified_at, verified_at=verified_at, verified_by_principal="steward",
        delivery_eligible=delivery_eligible,
    )


def _seed_person(knowledge_root: Path, *, status_age_days: int, contact_age_days: int) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    person = PersonDirectory(
        entity_id="person:jdoe", alias="jdoe", status=PersonStatus.ACTIVE,
        verifications=(_verification("status", age_days=status_age_days),),
        contacts=(_contact(age_days=contact_age_days),),
    )
    write_people_directory(knowledge_root / "people_directory.yaml", (person,))


def _seed_membership(knowledge_root: Path, *, verified_age_days: int) -> None:
    verified_at = _NOW - timedelta(days=verified_age_days)
    write_memberships(
        knowledge_root / "memberships.yaml",
        (
            TeamMembership(
                membership_id="m1", person_entity_id="person:jdoe", team_entity_id="team:platform", role="member",
                valid_from=verified_at, valid_until=None, source="test", source_ref=None,
                observed_at=verified_at, verified_at=verified_at, status=MembershipStatus.ACTIVE,
            ),
        ),
    )


def _candidate(*, source_team_entity_id: str | None = "team:platform") -> AudienceCandidate:
    return AudienceCandidate(person_entity_id="person:jdoe", source="team_expansion", source_team_entity_id=source_team_entity_id)


def test_no_threshold_is_a_true_no_op(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_person(knowledge_root, status_age_days=999, contact_age_days=999)
    _seed_membership(knowledge_root, verified_age_days=999)

    fresh, exclusions = filter_candidates_by_freshness((_candidate(),), knowledge_root=knowledge_root, require_verified_within_days=None, as_of=_NOW)

    assert fresh == (_candidate(),)
    assert exclusions == ()


def test_fresh_candidate_passes_all_checks(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_person(knowledge_root, status_age_days=1, contact_age_days=1)
    _seed_membership(knowledge_root, verified_age_days=1)

    fresh, exclusions = filter_candidates_by_freshness((_candidate(),), knowledge_root=knowledge_root, require_verified_within_days=30, as_of=_NOW)

    assert len(fresh) == 1
    assert exclusions == ()


def test_stale_status_verification_excludes_the_candidate(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_person(knowledge_root, status_age_days=100, contact_age_days=1)
    _seed_membership(knowledge_root, verified_age_days=1)

    fresh, exclusions = filter_candidates_by_freshness((_candidate(),), knowledge_root=knowledge_root, require_verified_within_days=30, as_of=_NOW)

    assert fresh == ()
    assert len(exclusions) == 1
    assert exclusions[0].field_name == "status"
    assert exclusions[0].age_days == 100


def test_stale_contact_verification_excludes_the_candidate(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_person(knowledge_root, status_age_days=1, contact_age_days=100)
    _seed_membership(knowledge_root, verified_age_days=1)

    fresh, exclusions = filter_candidates_by_freshness((_candidate(),), knowledge_root=knowledge_root, require_verified_within_days=30, as_of=_NOW)

    assert fresh == ()
    assert exclusions[0].field_name.startswith("contact:")


def test_stale_membership_verification_excludes_the_candidate(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_person(knowledge_root, status_age_days=1, contact_age_days=1)
    _seed_membership(knowledge_root, verified_age_days=100)

    fresh, exclusions = filter_candidates_by_freshness((_candidate(),), knowledge_root=knowledge_root, require_verified_within_days=30, as_of=_NOW)

    assert fresh == ()
    assert exclusions[0].field_name == "membership"
    assert exclusions[0].age_days == 100


def test_include_people_candidate_has_no_membership_to_check(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed_person(knowledge_root, status_age_days=1, contact_age_days=1)
    # No memberships.yaml at all -- this candidate came from include_people, not a team.

    candidate = _candidate(source_team_entity_id=None)
    fresh, exclusions = filter_candidates_by_freshness((candidate,), knowledge_root=knowledge_root, require_verified_within_days=30, as_of=_NOW)

    assert fresh == (candidate,)
    assert exclusions == ()
