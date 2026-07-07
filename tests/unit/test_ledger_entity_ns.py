"""Tests for EntityNsMapper — §6.2 entity namespace bridge."""
from __future__ import annotations

import pytest

from src.core.ledger.entity_ns import EntityNsMapper


@pytest.fixture()
def mapper() -> EntityNsMapper:
    return EntityNsMapper()


def test_work_item_to_ledger(mapper: EntityNsMapper) -> None:
    assert mapper.work_item_to_ledger("WI:12345") == "work_item:ado-12345"


def test_work_item_to_ledger_invalid_prefix_raises(mapper: EntityNsMapper) -> None:
    with pytest.raises(ValueError, match="Not a work-item signal ref"):
        mapper.work_item_to_ledger("P:jdoe")


def test_ledger_to_work_item_known(mapper: EntityNsMapper) -> None:
    assert mapper.ledger_to_work_item("work_item:ado-12345") == "WI:12345"


def test_ledger_to_work_item_unknown_returns_none(mapper: EntityNsMapper) -> None:
    assert mapper.ledger_to_work_item("person:jdoe") is None


def test_person_to_ledger(mapper: EntityNsMapper) -> None:
    assert mapper.person_to_ledger("P:jdoe") == "person:jdoe"


def test_person_to_ledger_invalid_prefix_raises(mapper: EntityNsMapper) -> None:
    with pytest.raises(ValueError, match="Not a person signal ref"):
        mapper.person_to_ledger("WI:999")


def test_ledger_to_person_known(mapper: EntityNsMapper) -> None:
    assert mapper.ledger_to_person("person:jdoe") == "P:jdoe"


def test_ledger_to_person_unknown_returns_none(mapper: EntityNsMapper) -> None:
    assert mapper.ledger_to_person("work_item:ado-99") is None


def test_to_ledger_work_item(mapper: EntityNsMapper) -> None:
    assert mapper.to_ledger("WI:42") == "work_item:ado-42"


def test_to_ledger_person(mapper: EntityNsMapper) -> None:
    assert mapper.to_ledger("P:alice") == "person:alice"


def test_to_ledger_unknown_returns_none(mapper: EntityNsMapper) -> None:
    assert mapper.to_ledger("milestone:m1") is None


def test_from_ledger_work_item(mapper: EntityNsMapper) -> None:
    assert mapper.from_ledger("work_item:ado-42") == "WI:42"


def test_from_ledger_person(mapper: EntityNsMapper) -> None:
    assert mapper.from_ledger("person:alice") == "P:alice"


def test_from_ledger_unknown_returns_none(mapper: EntityNsMapper) -> None:
    assert mapper.from_ledger("decision:d-99") is None


def test_roundtrip_work_item(mapper: EntityNsMapper) -> None:
    original = "WI:9999"
    assert mapper.from_ledger(mapper.work_item_to_ledger(original)) == original


def test_roundtrip_person(mapper: EntityNsMapper) -> None:
    original = "P:operator"
    assert mapper.from_ledger(mapper.person_to_ledger(original)) == original
