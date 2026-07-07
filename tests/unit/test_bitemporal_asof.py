"""GAP-36d: Bitemporal ``as_of`` — read at past validity windows."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.program_fact_store import (
    FactPrecedence,
    ProgramFactInput,
    ProgramFactStore,
)


def _fact(*, natural_key: str, fact_type: str = "test.event") -> ProgramFactInput:
    return ProgramFactInput(
        fact_type=fact_type,
        entity_refs=(f"test:{natural_key}",),
        payload={"natural_key": natural_key},
        scope="program",
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        natural_key=natural_key,
    )


def test_bitemporal_filters_rows_outside_validity_window(tmp_path: Path) -> None:
    """A fact whose validity window excludes ``as_of`` is excluded.

    Seeding: write a fact with valid_from=t2, valid_until=t3.
    Read at t1 (before t2): excluded.
    Read at t2 (start): included.
    Read at t4 (after t3): excluded.
    """
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t3 = datetime(2026, 12, 1, tzinfo=timezone.utc)
    t4 = datetime(2027, 6, 1, tzinfo=timezone.utc)

    store = ProgramFactStore("acme", db_root=tmp_path)
    fact = _fact(natural_key="test:bitemporal-window")
    fact_with_window = ProgramFactInput(
        fact_type=fact.fact_type,
        entity_refs=fact.entity_refs,
        payload=fact.payload,
        scope=fact.scope,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        natural_key=fact.natural_key,
        valid_from=t2,
        valid_until=t3,
    )
    store.append_fact(fact_with_window, recorded_at=t2)

    snap_t1 = store.snapshot(as_of=t1)
    snap_t2 = store.snapshot(as_of=t2)
    snap_t3 = store.snapshot(as_of=t3 - timedelta(seconds=1))
    snap_t4 = store.snapshot(as_of=t4)
    assert len(snap_t1.facts) == 0
    assert len(snap_t2.facts) == 1
    assert len(snap_t3.facts) == 1
    assert len(snap_t4.facts) == 0


def test_bitemporal_legacy_rows_without_validity_remain_ever_valid(tmp_path: Path) -> None:
    """A fact with no valid_from/valid_until is treated as ever-valid.

    The legacy contract (pre-GAP-36d) treated all rows as ever-valid.
    Existing facts without a validity window must continue to be
    returned at every as_of.
    """
    t_past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    t_future = datetime(2030, 1, 1, tzinfo=timezone.utc)

    store = ProgramFactStore("acme", db_root=tmp_path)
    # No valid_from/valid_until on the fact.
    store.append_fact(_fact(natural_key="test:legacy"), recorded_at=t_past)

    snap_past = store.snapshot(as_of=t_past)
    snap_future = store.snapshot(as_of=t_future)
    assert len(snap_past.facts) == 1
    assert len(snap_future.facts) == 1


def test_bitemporal_open_ended_validity(tmp_path: Path) -> None:
    """A fact with valid_from=t1 and valid_until=None is valid from t1 onward."""
    t0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t_future = datetime(2030, 1, 1, tzinfo=timezone.utc)

    store = ProgramFactStore("acme", db_root=tmp_path)
    fact = _fact(natural_key="test:open-ended")
    fact_with_open_end = ProgramFactInput(
        fact_type=fact.fact_type,
        entity_refs=fact.entity_refs,
        payload=fact.payload,
        scope=fact.scope,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        natural_key=fact.natural_key,
        valid_from=t1,
        valid_until=None,
    )
    store.append_fact(fact_with_open_end, recorded_at=t1)

    snap_before = store.snapshot(as_of=t0)
    snap_at = store.snapshot(as_of=t1)
    snap_later = store.snapshot(as_of=t_future)
    assert len(snap_before.facts) == 0
    assert len(snap_at.facts) == 1
    assert len(snap_later.facts) == 1
