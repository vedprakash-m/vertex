from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_delegation_schema import (
    Delegation,
    DelegationStatus,
    delegations_path,
    load_delegations,
    write_delegations,
)

_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def test_load_delegations_round_trips_the_real_example_fixture() -> None:
    delegations = load_delegations(Path("knowledge/delegations.example.yaml"))

    assert len(delegations) == 1
    delegation = delegations[0]
    assert delegation.delegation_id == "delegation:01HQ8Y4N5P6Q7R8S9T0U1V2W3X"
    assert delegation.from_person_entity_id == "person:01HQ8Y1A2B3C4D5E6F7G8H9J0K"
    assert delegation.to_person_entity_id == "person:01HQ8Y5P6Q7R8S9T0U1V2W3X4Y"
    assert delegation.surfaces == ("vertex::nudge",)
    assert delegation.program_ids == ("acme",)
    assert delegation.workstream_ids == ()
    assert delegation.status == DelegationStatus.ACTIVE


def test_missing_file_returns_empty_tuple(tmp_path: Path) -> None:
    assert load_delegations(tmp_path / "delegations.yaml") == ()


def test_write_then_load_round_trips_a_synthetic_delegation(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    path = delegations_path(knowledge_root)
    delegation = Delegation(
        delegation_id="delegation:test1", from_person_entity_id="person:a", to_person_entity_id="person:b",
        surfaces=("vertex::nudge", "vertex::review"), valid_from=_NOW, valid_until=_NOW, reason="test",
        actor_principal="ACME\\steward", program_ids=("acme",), workstream_ids=("acme.ws1",),
        status=DelegationStatus.ACTIVE,
    )

    write_delegations(path, (delegation,))
    loaded = load_delegations(path)

    assert loaded == (delegation,)


def test_wrong_schema_major_raises(tmp_path: Path) -> None:
    path = tmp_path / "delegations.yaml"
    path.write_text('schema_version: "2.0"\ndelegations: []\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="schema_version major"):
        load_delegations(path)


def test_write_delegations_sorts_by_delegation_id(tmp_path: Path) -> None:
    path = tmp_path / "delegations.yaml"
    d1 = Delegation(delegation_id="delegation:b", from_person_entity_id="person:a", to_person_entity_id="person:b", surfaces=("vertex::nudge",), valid_from=_NOW, valid_until=_NOW, reason="r", actor_principal="p")
    d2 = Delegation(delegation_id="delegation:a", from_person_entity_id="person:a", to_person_entity_id="person:b", surfaces=("vertex::nudge",), valid_from=_NOW, valid_until=_NOW, reason="r", actor_principal="p")

    write_delegations(path, (d1, d2))
    loaded = load_delegations(path)

    assert [d.delegation_id for d in loaded] == ["delegation:a", "delegation:b"]
