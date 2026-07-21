"""PPL-W5b.4 delegation resolution engine coverage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.people_delegation_resolution import resolve_active_delegation
from src.core.people_delegation_schema import Delegation, DelegationStatus, delegations_path, write_delegations

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


def _seed(knowledge_root: Path, delegations: tuple[Delegation, ...]) -> None:
    write_delegations(delegations_path(knowledge_root), delegations)


def test_active_unconflicted_in_scope_delegation_resolves_to_the_delegate(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1")
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved == delegation


def test_no_delegation_resolves_to_none(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _seed(knowledge_root, ())

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None


def test_expired_delegation_resolves_to_none(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1", valid_from=_NOW - timedelta(days=30), valid_until=_NOW - timedelta(days=1))
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None


def test_revoked_delegation_resolves_to_none(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1", status=DelegationStatus.REVOKED)
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None


def test_wrong_surface_resolves_to_none(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1", surfaces=("vertex::review",))
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None


def test_out_of_scope_program_resolves_to_none(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1", program_ids=("globex",))
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", program_id="acme", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None


def test_matching_program_scope_resolves(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1", program_ids=("acme",))
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", program_id="acme", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved == delegation


def test_scoped_delegation_does_not_apply_without_program_context(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1", program_ids=("acme",))
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None


def test_conflicting_pair_resolves_to_none(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    first = _delegation("d1", to_id="person:bob")
    second = _delegation("d2", to_id="person:carol")
    _seed(knowledge_root, (first, second))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None


def test_delegate_own_outbound_delegation_is_never_followed(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    a_to_b = _delegation("d1", from_id="person:alice", to_id="person:bob")
    b_to_c = _delegation("d2", from_id="person:bob", to_id="person:carol")
    _seed(knowledge_root, (a_to_b, b_to_c))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is not None
    assert resolved.to_person_entity_id == "person:bob"


def test_different_source_person_delegation_is_ignored(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    delegation = _delegation("d1", from_id="person:carol")
    _seed(knowledge_root, (delegation,))

    resolved = resolve_active_delegation(
        "person:alice", surface="vertex::nudge", knowledge_root=knowledge_root, as_of=_NOW,
    )

    assert resolved is None
