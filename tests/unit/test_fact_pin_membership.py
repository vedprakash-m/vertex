from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Barrier
from unittest.mock import Mock, patch

import pytest

from src.core import program_fact_store
from src.core.exceptions import ConfigError
from src.core.program_fact_store import FactPrecedence, FactSnapshotPin, ProgramFactInput, ProgramFactStore
from src.core.quality_gates.fact_drift import evaluate_program_fact_drift_from_draft


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


def _fact(
    scope: str, *, status: str = "open", valid_until: datetime | None = None,
) -> ProgramFactInput:
    return ProgramFactInput(
        fact_type="action", scope=scope, entity_refs=(scope,),
        payload={"status": status}, valid_until=valid_until,
        precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
    )


@pytest.mark.parametrize("offset", [0, -1])
@pytest.mark.parametrize("replace", [False, True])
def test_new_pin_detects_equal_or_backdated_accepted_changes(tmp_path, offset, replace) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    store.append_fact(_fact("WI:1"), recorded_at=NOW)
    pin = store.pin_snapshot(metadata={"edition": "fixture"})
    changed = store.append_fact(
        _fact("WI:1" if replace else "WI:2", status="closed"),
        recorded_at=NOW + timedelta(days=offset),
    )

    reopened = ProgramFactStore("fixture", db_root=tmp_path)
    drift = reopened.detect_drift(pin.snapshot_id)
    assert tuple(fact.revision_id for fact in drift) == (changed.revision.revision_id,)
    gate = evaluate_program_fact_drift_from_draft(
        draft_state={"program_fact_snapshot": {"snapshot_id": pin.snapshot_id}},
        program_id="fixture", db_root=tmp_path,
    )
    assert gate.results[0].passed is False
    assert gate.results[0].exit_code == 3
    assert reopened.load_snapshot_pin(pin.snapshot_id).metadata == {"edition": "fixture"}


def test_new_pin_detects_validity_expiry_without_later_revision(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    clock = Mock(wraps=datetime)
    with patch.object(program_fact_store, "datetime", clock):
        clock.now.return_value = NOW
        original = store.append_fact(
            _fact("WI:1", valid_until=NOW + timedelta(days=1)), recorded_at=NOW,
        )
        pin = store.pin_snapshot()
        clock.now.return_value = NOW + timedelta(days=2)
        assert tuple(fact.revision_id for fact in store.detect_drift(pin.snapshot_id)) == (
            original.revision.revision_id,
        )
        gate = evaluate_program_fact_drift_from_draft(
            draft_state={"program_fact_snapshot": {"snapshot_id": pin.snapshot_id}},
            program_id="fixture", db_root=tmp_path,
        )
        assert gate.results[0].passed is False


def test_missing_persisted_membership_is_not_successful_no_drift(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    store.append_fact(_fact("WI:1"), recorded_at=NOW)
    pin = store.pin_snapshot()
    with store._connect() as connection:
        connection.execute(
            "DELETE FROM program_fact_snapshot_members WHERE snapshot_id = ?",
            (pin.snapshot_id,),
        )
    with pytest.raises(ConfigError, match="membership"):
        store.detect_drift(pin.snapshot_id)


@pytest.mark.parametrize("empty", [False, True])
def test_legacy_schema_upgrade_retains_watermark_pin_behavior(tmp_path, empty) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    if not empty:
        store.append_fact(_fact("WI:1"), recorded_at=NOW)
    pin = store.pin_snapshot(metadata={"edition": "legacy"})
    with store._connect() as connection:
        connection.execute("DROP TABLE program_fact_snapshot_members")
        connection.execute("ALTER TABLE program_fact_snapshot_pins DROP COLUMN membership_version")
        connection.execute("ALTER TABLE program_fact_snapshot_pins DROP COLUMN content_sha256")

    reopened = ProgramFactStore("fixture", db_root=tmp_path)
    loaded = reopened.load_snapshot_pin(pin.snapshot_id)
    assert loaded.membership_version is None
    assert loaded.metadata == {"edition": "legacy"}
    assert reopened.detect_drift(pin.snapshot_id) == ()
    added = reopened.append_fact(_fact("WI:2"), recorded_at=NOW + timedelta(days=1))
    assert tuple(fact.revision_id for fact in reopened.detect_drift(pin.snapshot_id)) == (
        added.revision.revision_id,
    )
    assert reopened.pin_snapshot().membership_version == 2


def test_unsupported_membership_version_is_not_a_legacy_fallback(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    pin = store.pin_snapshot()
    with store._connect() as connection:
        connection.execute(
            "UPDATE program_fact_snapshot_pins SET membership_version = 99 WHERE snapshot_id = ?",
            (pin.snapshot_id,),
        )
    with pytest.raises(ConfigError, match="version"):
        store.detect_drift(pin.snapshot_id)


def test_pin_membership_insert_failure_rolls_back_entire_pin(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    store.append_fact(_fact("WI:1"), recorded_at=NOW)
    with store._connect() as connection:
        connection.execute("""
            CREATE TRIGGER reject_member BEFORE INSERT ON program_fact_snapshot_members
            BEGIN SELECT RAISE(ABORT, 'synthetic member failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic member failure"):
        store.pin_snapshot()
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM program_fact_snapshot_pins").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM program_fact_snapshot_members").fetchone()[0] == 0


def test_rollback_purge_invalidates_pin_without_preventing_fact_purge(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    store.append_fact(_fact("WI:1"), recorded_at=NOW)
    pin = store.pin_snapshot()
    assert store.purge_facts_after(NOW - timedelta(days=1)) == 1
    with pytest.raises(ConfigError, match="membership"):
        store.detect_drift(pin.snapshot_id)


def test_pin_lookup_cannot_cross_program_in_shared_database(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    pin = store.pin_snapshot()
    other = ProgramFactStore("other", db_root=tmp_path)
    other._db_path = store._db_path
    assert other.load_snapshot_pin(pin.snapshot_id) is None
    with pytest.raises(KeyError, match="Unknown snapshot pin"):
        other.detect_drift(pin.snapshot_id)


def test_concurrent_legacy_schema_upgrade_and_pin_creation(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    store.append_fact(_fact("WI:1"), recorded_at=NOW)
    with store._connect() as connection:
        connection.execute("DROP TABLE program_fact_snapshot_members")
        connection.execute("ALTER TABLE program_fact_snapshot_pins DROP COLUMN membership_version")
        connection.execute("ALTER TABLE program_fact_snapshot_pins DROP COLUMN content_sha256")
    barrier = Barrier(4)

    def pin_concurrently() -> FactSnapshotPin:
        barrier.wait(timeout=10)
        return ProgramFactStore("fixture", db_root=tmp_path).pin_snapshot()

    with ThreadPoolExecutor(max_workers=4) as workers:
        pins = tuple(workers.map(lambda _: pin_concurrently(), range(4)))
    assert len({pin.snapshot_id for pin in pins}) == 4
    for pin in pins:
        assert pin.membership_version == 2
        assert store.detect_drift(pin.snapshot_id) == ()


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize(
    ("column", "declaration"),
    [("membership_version", "INTEGER"), ("content_sha256", "TEXT")],
)
def test_membership_migration_only_tolerates_concurrent_duplicate_column(
    tmp_path, duplicate, column, declaration,
) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    with store._connect() as connection:
        connection.execute("DROP TABLE program_fact_snapshot_members")
        connection.execute(f"ALTER TABLE program_fact_snapshot_pins DROP COLUMN {column}")
        proxy = Mock(wraps=connection)

        def execute(sql: str, *args):
            if sql == f"ALTER TABLE program_fact_snapshot_pins ADD COLUMN {column} {declaration}":
                if duplicate:
                    connection.execute(sql, *args)
                raise sqlite3.OperationalError(
                    f"duplicate column name: {column}" if duplicate else "synthetic migration failure",
                )
            return connection.execute(sql, *args)

        proxy.execute.side_effect = execute
        if duplicate:
            store._initialize_schema(proxy)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(program_fact_snapshot_pins)")}
            assert column in columns
        else:
            with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
                store._initialize_schema(proxy)


def test_membership_join_and_purge_lookup_for_1100_facts(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    for index in range(1100):
        store.append_fact(_fact(f"WI:{index}"), recorded_at=NOW)
    pin = store.pin_snapshot()
    assert pin.pinned_revision_count == 1100
    assert store.detect_drift(pin.snapshot_id) == ()
    added = store.append_fact(_fact("WI:new"), recorded_at=NOW - timedelta(days=1))
    assert tuple(fact.revision_id for fact in store.detect_drift(pin.snapshot_id)) == (
        added.revision.revision_id,
    )
    with store._connect() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT snapshot_id FROM program_fact_snapshot_members WHERE revision_id = ?",
            (added.revision.revision_id,),
        ).fetchall()
    assert any("idx_program_fact_snapshot_member_revision" in row[3] for row in plan)
