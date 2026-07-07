"""Contract tests for WS-4 SQLite rollback parity (PB-36).

Verifies:
  R-1  ProgramFactStore.purge_facts_after() deletes rows recorded after cutoff.
  R-2  purge_facts_after() does NOT delete rows recorded at or before cutoff.
  R-3  purge_facts_after() returns correct deleted-row count.
  R-4  rollback.py has _parse_checkpoint_timestamp() that parses the canonical name.
  R-5  rollback.py has _purge_fact_store_after_checkpoint() helper.
  R-6  _parse_checkpoint_timestamp returns None for non-conforming names.
  R-7  purge_facts_after on absent DB returns 0 (non-fatal).
  R-8  rollback._purge_fact_store_after_checkpoint returns 0 when DB absent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path, program_id: str = "testprog"):
    from src.core.program_fact_store import ProgramFactStore  # noqa: PLC0415
    db_root = tmp_path / "vertex-db"
    return ProgramFactStore(program_id, db_root=db_root)


def _append_fact(store, *, recorded_at: datetime, fact_type: str = "test_fact"):
    from src.core.program_fact_store import ProgramFactInput, FactPrecedence  # noqa: PLC0415
    fact = ProgramFactInput(
        fact_type=fact_type,
        entity_refs={"id": "1"},
        payload={"value": "test"},
        scope="test",
        source_signal_ids=[],
        confidence=None,
        precedence=FactPrecedence.RAW_TELEMETRY,
    )
    return store.append_fact(fact, recorded_at=recorded_at)


# ---------------------------------------------------------------------------
# R-1  purge_facts_after deletes rows recorded after cutoff
# ---------------------------------------------------------------------------


def test_r1_purge_facts_after_deletes_post_cutoff_rows(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    cutoff = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    after = cutoff + timedelta(hours=1)

    _append_fact(store, recorded_at=after, fact_type="post_cutoff_fact")

    snapshot_before = store.snapshot()
    assert len(snapshot_before.facts) == 1

    deleted = store.purge_facts_after(cutoff)
    assert deleted == 1

    snapshot_after = store.snapshot()
    assert len(snapshot_after.facts) == 0


# ---------------------------------------------------------------------------
# R-2  purge_facts_after does NOT delete rows at-or-before cutoff
# ---------------------------------------------------------------------------


def test_r2_purge_facts_after_preserves_rows_at_or_before_cutoff(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    cutoff = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    at_cutoff = cutoff
    before = cutoff - timedelta(hours=1)

    _append_fact(store, recorded_at=before, fact_type="before_fact")
    _append_fact(store, recorded_at=at_cutoff, fact_type="at_cutoff_fact")

    deleted = store.purge_facts_after(cutoff)
    assert deleted == 0

    snapshot = store.snapshot()
    assert len(snapshot.facts) == 2


# ---------------------------------------------------------------------------
# R-3  purge_facts_after returns correct count
# ---------------------------------------------------------------------------


def test_r3_purge_facts_after_returns_correct_count(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    cutoff = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

    # 2 before, 3 after
    for i in range(2):
        _append_fact(store, recorded_at=cutoff - timedelta(days=i + 1), fact_type=f"before_{i}")
    for i in range(3):
        _append_fact(store, recorded_at=cutoff + timedelta(hours=i + 1), fact_type=f"after_{i}")

    deleted = store.purge_facts_after(cutoff)
    assert deleted == 3

    snapshot = store.snapshot()
    assert len(snapshot.facts) == 2


# ---------------------------------------------------------------------------
# R-4  rollback._parse_checkpoint_timestamp parses canonical name
# ---------------------------------------------------------------------------


def test_r4_parse_checkpoint_timestamp_canonical() -> None:
    from src.commands.rollback import _parse_checkpoint_timestamp  # noqa: PLC0415

    cp_path = Path("programs/testprog/checkpoints/issue_042_20250601T120000Z")
    ts = _parse_checkpoint_timestamp(cp_path)

    assert ts is not None
    assert ts.year == 2025
    assert ts.month == 6
    assert ts.day == 1
    assert ts.hour == 12
    assert ts.tzinfo is not None


# ---------------------------------------------------------------------------
# R-5  rollback._purge_fact_store_after_checkpoint is exported
# ---------------------------------------------------------------------------


def test_r5_purge_fact_store_helper_exists() -> None:
    import importlib  # noqa: PLC0415
    mod = importlib.import_module("src.commands.rollback")
    assert hasattr(mod, "_purge_fact_store_after_checkpoint"), (
        "_purge_fact_store_after_checkpoint must be defined in rollback.py"
    )
    assert callable(mod._purge_fact_store_after_checkpoint)


# ---------------------------------------------------------------------------
# R-6  _parse_checkpoint_timestamp returns None for non-conforming names
# ---------------------------------------------------------------------------


def test_r6_parse_checkpoint_timestamp_none_for_bad_name() -> None:
    from src.commands.rollback import _parse_checkpoint_timestamp  # noqa: PLC0415

    assert _parse_checkpoint_timestamp(Path("some_dir_without_timestamp")) is None
    assert _parse_checkpoint_timestamp(Path("issue_001")) is None
    assert _parse_checkpoint_timestamp(Path("issue_001_badformat")) is None


# ---------------------------------------------------------------------------
# R-7  purge_facts_after on fresh (empty) DB returns 0
# ---------------------------------------------------------------------------


def test_r7_purge_facts_after_empty_db_returns_zero(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc)
    deleted = store.purge_facts_after(cutoff)
    assert deleted == 0


# ---------------------------------------------------------------------------
# R-8  rollback._purge_fact_store_after_checkpoint returns 0 when DB absent
# ---------------------------------------------------------------------------


def test_r8_purge_helper_returns_zero_when_db_absent(tmp_path: Path) -> None:
    from src.commands.rollback import _purge_fact_store_after_checkpoint  # noqa: PLC0415

    programs_root = tmp_path / "programs"
    (programs_root / "testprog" / "checkpoints").mkdir(parents=True)
    cp_path = programs_root / "testprog" / "checkpoints" / "issue_001_20250601T120000Z"
    cp_path.mkdir()

    # No vertex-db directory / no sqlite DB.
    result = _purge_fact_store_after_checkpoint(
        "testprog",
        cp_path,
        programs_root=programs_root,
    )
    assert result == 0
