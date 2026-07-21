"""PPL-W5b.3 delegation overlap-conflict detection coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.people_delegation_conflicts import find_overlapping_delegations, has_overlapping_delegation
from src.core.people_delegation_schema import Delegation, DelegationStatus

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _delegation(
    delegation_id: str,
    *,
    from_id: str = "person:alice",
    to_id: str = "person:bob",
    surfaces: tuple[str, ...] = ("vertex::nudge",),
    valid_from: datetime = _NOW,
    valid_until: datetime = _NOW + timedelta(days=14),
    program_ids: tuple[str, ...] = (),
    workstream_ids: tuple[str, ...] = (),
    status: DelegationStatus = DelegationStatus.ACTIVE,
) -> Delegation:
    return Delegation(
        delegation_id=delegation_id, from_person_entity_id=from_id, to_person_entity_id=to_id, surfaces=surfaces,
        valid_from=valid_from, valid_until=valid_until, reason="x", actor_principal="test_steward",
        program_ids=program_ids, workstream_ids=workstream_ids, status=status,
    )


def test_same_person_surface_overlapping_dates_and_empty_scopes_conflict() -> None:
    candidate = _delegation("d1")
    existing = (_delegation("d2"),)

    conflicts = find_overlapping_delegations(candidate, existing)

    assert len(conflicts) == 1
    assert conflicts[0].other.delegation_id == "d2"
    assert conflicts[0].overlapping_surfaces == ("vertex::nudge",)
    assert has_overlapping_delegation(candidate, existing)


def test_different_surface_does_not_conflict() -> None:
    candidate = _delegation("d1", surfaces=("vertex::nudge",))
    existing = (_delegation("d2", surfaces=("vertex::review",)),)

    assert find_overlapping_delegations(candidate, existing) == ()


def test_non_overlapping_dates_do_not_conflict() -> None:
    candidate = _delegation("d1", valid_from=_NOW, valid_until=_NOW + timedelta(days=7))
    existing = (_delegation("d2", valid_from=_NOW + timedelta(days=8), valid_until=_NOW + timedelta(days=20)),)

    assert find_overlapping_delegations(candidate, existing) == ()


def test_touching_dates_do_conflict_inclusive_boundary() -> None:
    candidate = _delegation("d1", valid_from=_NOW, valid_until=_NOW + timedelta(days=7))
    existing = (_delegation("d2", valid_from=_NOW + timedelta(days=7), valid_until=_NOW + timedelta(days=20)),)

    assert has_overlapping_delegation(candidate, existing)


def test_disjoint_program_scopes_do_not_conflict() -> None:
    candidate = _delegation("d1", program_ids=("acme",))
    existing = (_delegation("d2", program_ids=("globex",)),)

    assert find_overlapping_delegations(candidate, existing) == ()


def test_empty_scope_intersects_any_populated_scope() -> None:
    candidate = _delegation("d1", program_ids=())
    existing = (_delegation("d2", program_ids=("acme",)),)

    assert has_overlapping_delegation(candidate, existing)


def test_overlapping_program_but_disjoint_workstream_does_not_conflict() -> None:
    candidate = _delegation("d1", program_ids=("acme",), workstream_ids=("ws-1",))
    existing = (_delegation("d2", program_ids=("acme",), workstream_ids=("ws-2",)),)

    assert find_overlapping_delegations(candidate, existing) == ()


def test_different_source_person_does_not_conflict() -> None:
    candidate = _delegation("d1", from_id="person:alice")
    existing = (_delegation("d2", from_id="person:carol"),)

    assert find_overlapping_delegations(candidate, existing) == ()


def test_revoked_delegation_does_not_conflict() -> None:
    candidate = _delegation("d1")
    existing = (_delegation("d2", status=DelegationStatus.REVOKED),)

    assert find_overlapping_delegations(candidate, existing) == ()


def test_self_comparison_does_not_conflict() -> None:
    candidate = _delegation("d1")
    existing = (candidate,)

    assert find_overlapping_delegations(candidate, existing) == ()


def test_multiple_conflicts_are_all_reported() -> None:
    candidate = _delegation("d1")
    existing = (_delegation("d2"), _delegation("d3"))

    conflicts = find_overlapping_delegations(candidate, existing)

    assert {conflict.other.delegation_id for conflict in conflicts} == {"d2", "d3"}
