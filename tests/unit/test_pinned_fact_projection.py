from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import Barrier
from unittest.mock import Mock, patch

import pytest

from src.core import program_fact_store
from src.core.exceptions import ConfigError
from src.core.models import EditionType
from src.core.pipeline import StageContext
from src.core.program_fact_store import ProgramFactInput, ProgramFactSnapshot, ProgramFactStore
from src.core.stages import action_stage
from src.core.stages.action_stage import ActionStage
from src.core.stages.resolution_stage import ResolutionStage
from tests.unit.test_pipeline_stages import _stage_pipeline_workspace


NOW = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)


def _fact(key: str = "a1", *, text: str = "Captured action") -> ProgramFactInput:
    return ProgramFactInput(
        fact_type="action.item", entity_refs=(f"ACTION:{key}",),
        payload={
            "id": key, "program_id": "acme", "text": text, "owner_alias": "demo",
            "status": "open", "source_type": "manual", "created_at": NOW.isoformat(),
            "due_date": "2026-05-01",
        },
        source_hash="original-source", projection_history=({"version": 1},),
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProgramFactStore:
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    return ProgramFactStore("acme", db_root=tmp_path)


def _pin(store: ProgramFactStore):
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    with patch.object(program_fact_store, "datetime", clock):
        return store.pin_snapshot(created_at=NOW - timedelta(days=30))


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_exact_load_restores_superseded_member_not_current_facts(store, offset) -> None:
    original = store.append_fact(_fact(), recorded_at=NOW - timedelta(days=2)).revision
    pin = _pin(store)
    store.append_fact(_fact(text="Changed"), recorded_at=NOW + timedelta(days=offset))
    store.append_fact(_fact("new"), recorded_at=NOW)
    with patch.object(ProgramFactStore, "snapshot", side_effect=AssertionError("current read")):
        loaded = store.load_pinned_snapshot(pin.snapshot_id)
    assert loaded == ProgramFactSnapshot("acme", NOW, (original,), NOW)
    loaded.facts[0].payload["text"] = "Local mutation"
    assert store.load_pinned_snapshot(pin.snapshot_id).facts[0].payload["text"] == "Captured action"


def test_future_and_post_capture_supersessions_preserve_original_digest(store) -> None:
    first = store.append_fact(_fact(), recorded_at=NOW - timedelta(days=2)).revision
    second = store.append_fact(_fact("a2"), recorded_at=NOW - timedelta(days=2)).revision
    future = NOW + timedelta(days=1)
    store.append_fact(_fact(text="Future"), recorded_at=future)
    pin = _pin(store)
    store.append_fact(_fact("a2", text="Later"), recorded_at=NOW + timedelta(hours=1))
    loaded = store.load_pinned_snapshot(pin.snapshot_id)
    assert loaded.facts == (replace(first, superseded_at=future), second)
    assert pin.membership_version == 2
    assert program_fact_store._snapshot_content_sha256(loaded.facts) == pin.content_sha256


def test_distinct_transaction_and_valid_clocks_are_captured_from_hashed_snapshot(store) -> None:
    valid_at = NOW + timedelta(days=3)
    included = store.append_fact(
        replace(_fact(), valid_from=NOW + timedelta(days=2)), recorded_at=NOW,
    ).revision
    store.append_fact(
        replace(_fact("excluded"), valid_until=valid_at), recorded_at=NOW,
    )
    snapshot = store.snapshot(as_of=NOW, valid_at=valid_at)
    with patch.object(ProgramFactStore, "snapshot", return_value=snapshot):
        pin = store.pin_snapshot(created_at=NOW - timedelta(days=30))
    assert store.load_pinned_snapshot(pin.snapshot_id) == ProgramFactSnapshot("acme", NOW, (included,), valid_at)


@pytest.mark.parametrize("column", ["snapshot_as_of", "snapshot_valid_at"])
@pytest.mark.parametrize("value", [None, "", "bad-clock", "2026-05-05T18:00:00"])
def test_missing_or_malformed_capture_clocks_reject(store, column, value) -> None:
    pin = _pin(store)
    with store._connect() as connection:
        connection.execute(f"UPDATE program_fact_snapshot_pins SET {column} = ?", (value,))
    with pytest.raises(ConfigError, match="snapshot"):
        store.load_pinned_snapshot(pin.snapshot_id)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("payload_json", '{"text":"damaged"}'),
        ("payload_json", "[]"),
        ("source_hash", "damaged-provenance"),
        ("projection_history_json", '[{"version":2}]'),
        ("projection_history_json", "[1]"),
        ("recorded_at", None),
        ("recorded_at", "bad-clock"),
        ("recorded_at", "2026-05-05T18:00:00"),
        ("recorded_at", "2026-05-06T18:00:00+00:00"),
        ("valid_from", "2026-05-06T18:00:00+00:00"),
        ("valid_until", NOW.isoformat()),
        ("review_state", "proposed"),
        ("program_id", "other"),
    ],
)
def test_corrupt_member_cannot_be_reconstructed(store, column, value) -> None:
    store.append_fact(_fact(), recorded_at=NOW)
    pin = _pin(store)
    with store._connect() as connection:
        if value is None:
            # The schema disallows null recorded_at; empty must not trigger decoder's now fallback.
            value = ""
        connection.execute(f"UPDATE program_fact_revisions SET {column} = ?", (value,))
    with pytest.raises(ConfigError, match="snapshot"):
        store.load_pinned_snapshot(pin.snapshot_id)


@pytest.mark.parametrize("value", ["bad-clock", "2026-05-05T18:00:00", NOW.isoformat(), (NOW + timedelta(days=1)).isoformat()])
def test_corrupt_captured_supersession_rejects(store, value) -> None:
    store.append_fact(_fact(), recorded_at=NOW)
    pin = _pin(store)
    with store._connect() as connection:
        connection.execute("UPDATE program_fact_snapshot_members SET captured_superseded_at = ?", (value,))
    with pytest.raises(ConfigError, match="snapshot"):
        store.load_pinned_snapshot(pin.snapshot_id)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("pinned_revision_count", -1), ("pinned_revision_count", 2),
        ("pinned_revision_count", "bad"), ("content_sha256", None),
        ("content_sha256", "z" * 64), ("content_sha256", "0" * 64),
        ("membership_version", None), ("membership_version", 1), ("membership_version", 99),
    ],
)
def test_corrupt_pin_rejects(store, column, value) -> None:
    pin = _pin(store)
    with store._connect() as connection:
        connection.execute(f"UPDATE program_fact_snapshot_pins SET {column} = ?", (value,))
    with pytest.raises(ConfigError, match="snapshot"):
        store.load_pinned_snapshot(pin.snapshot_id)


def test_missing_members_and_purge_invalidate_capture_and_remove_metadata(store) -> None:
    store.append_fact(_fact(), recorded_at=NOW)
    pin = _pin(store)
    assert store.purge_facts_after(NOW - timedelta(seconds=1)) == 1
    with store._connect() as connection:
        assert connection.execute("SELECT * FROM program_fact_snapshot_members").fetchall() == []
    with pytest.raises(ConfigError, match="membership"):
        store.load_pinned_snapshot(pin.snapshot_id)


def test_purge_removes_captured_timestamp_but_retains_unaffected_members(store) -> None:
    store.append_fact(_fact("retained"), recorded_at=NOW - timedelta(days=2))
    removed = store.append_fact(_fact(), recorded_at=NOW).revision
    future = NOW + timedelta(days=1)
    store.append_fact(_fact(text="Future"), recorded_at=future)
    pin = _pin(store)
    with store._connect() as connection:
        assert connection.execute(
            "SELECT captured_superseded_at FROM program_fact_snapshot_members WHERE revision_id = ?",
            (removed.revision_id,),
        ).fetchone()[0] == future.isoformat()
    assert store.purge_facts_after(NOW - timedelta(days=1)) == 2
    with store._connect() as connection:
        members = connection.execute("SELECT revision_id, captured_superseded_at FROM program_fact_snapshot_members").fetchall()
        assert len(members) == 1
        assert members[0]["revision_id"] != removed.revision_id
    with pytest.raises(ConfigError, match="membership"):
        store.load_pinned_snapshot(pin.snapshot_id)


def test_deleting_pin_cascades_its_captured_member_metadata(store) -> None:
    store.append_fact(_fact(), recorded_at=NOW)
    pin = _pin(store)
    with store._connect() as connection:
        connection.execute("DELETE FROM program_fact_snapshot_pins WHERE snapshot_id = ?", (pin.snapshot_id,))
        assert connection.execute("SELECT * FROM program_fact_snapshot_members").fetchall() == []
    assert len(store.snapshot().facts) == 1


def test_unknown_and_other_program_pin_reject(store, monkeypatch) -> None:
    pin = _pin(store)
    with pytest.raises(ConfigError, match="snapshot"):
        store.load_pinned_snapshot("missing")
    monkeypatch.setenv("VERTEX_DB_PATH", str(store.db_path))
    other = ProgramFactStore("other")
    assert other.db_path == store.db_path
    with pytest.raises(ConfigError, match="snapshot"):
        other.load_pinned_snapshot(pin.snapshot_id)


@pytest.mark.parametrize("version", [None, 1, 2])
def test_old_pins_cannot_exact_load_but_drift_stays_compatible(store, version) -> None:
    pin = _pin(store)
    with store._connect() as connection:
        connection.execute(
            "UPDATE program_fact_snapshot_pins SET membership_version = ?, snapshot_as_of = NULL, snapshot_valid_at = NULL",
            (version,),
        )
    with pytest.raises(ConfigError, match="snapshot"):
        store.load_pinned_snapshot(pin.snapshot_id)
    assert store.detect_drift(pin.snapshot_id) == ()


def test_empty_capture_reopens_with_actual_clocks(store, tmp_path) -> None:
    pin = _pin(store)
    assert ProgramFactStore("acme", db_root=tmp_path).load_pinned_snapshot(pin.snapshot_id) == ProgramFactSnapshot("acme", NOW, (), NOW)


@pytest.mark.parametrize(
    ("table", "column"), [
        ("program_fact_snapshot_pins", "snapshot_as_of"),
        ("program_fact_snapshot_pins", "snapshot_valid_at"),
        ("program_fact_snapshot_members", "captured_superseded_at"),
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
def test_capture_migration_only_ignores_concurrent_duplicate_column(store, table, column, duplicate) -> None:
    with store._connect() as connection:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        proxy = Mock(wraps=connection)

        def execute(sql, *args):
            if sql == f"ALTER TABLE {table} ADD COLUMN {column} TEXT":
                if duplicate:
                    connection.execute(sql, *args)
                raise sqlite3.OperationalError(f"duplicate column name: {column}" if duplicate else "synthetic migration failure")
            return connection.execute(sql, *args)

        proxy.execute.side_effect = execute
        if duplicate:
            store._initialize_schema(proxy)
        else:
            with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
                store._initialize_schema(proxy)


def test_concurrent_upgrade_creates_exact_loadable_new_pins_without_upgrading_old_capture(store) -> None:
    old = _pin(store)
    with store._connect() as connection:
        connection.execute("ALTER TABLE program_fact_snapshot_pins DROP COLUMN snapshot_as_of")
        connection.execute("ALTER TABLE program_fact_snapshot_pins DROP COLUMN snapshot_valid_at")
        connection.execute("ALTER TABLE program_fact_snapshot_members DROP COLUMN captured_superseded_at")
    barrier = Barrier(3)

    def capture(_):
        barrier.wait(timeout=10)
        return store.pin_snapshot()

    with ThreadPoolExecutor(max_workers=3) as workers:
        pins = tuple(workers.map(capture, range(3)))
    assert len({pin.snapshot_id for pin in pins}) == 3
    for pin in pins:
        assert store.load_pinned_snapshot(pin.snapshot_id).facts == ()
    with pytest.raises(ConfigError, match="snapshot"):
        store.load_pinned_snapshot(old.snapshot_id)
    assert store.detect_drift(old.snapshot_id) == ()


def test_capture_insert_failure_rolls_back_pin_and_members(store) -> None:
    store.append_fact(_fact(), recorded_at=NOW)
    with store._connect() as connection:
        connection.execute("""
            CREATE TRIGGER reject_capture BEFORE INSERT ON program_fact_snapshot_members
            WHEN NEW.captured_superseded_at IS NULL
            BEGIN SELECT RAISE(ABORT, 'synthetic capture failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic capture failure"):
        _pin(store)
    with store._connect() as connection:
        assert connection.execute("SELECT * FROM program_fact_snapshot_pins").fetchall() == []
        assert connection.execute("SELECT * FROM program_fact_snapshot_members").fetchall() == []


def _resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "primary") -> StageContext:
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.setenv("VERTEX_FACT_SOR", mode)
    reports_root = _stage_pipeline_workspace(tmp_path)
    return ResolutionStage().execute(StageContext(
        edition_name="acme_weekly", programs_root=tmp_path / "programs",
        reports_root=reports_root, archive_root=tmp_path / "archive", data_as_of=NOW,
    ))


def test_real_resolution_and_primary_action_consume_original_members_without_current_read(tmp_path, monkeypatch) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(_fact(), recorded_at=NOW)
    ctx = _resolved(tmp_path, monkeypatch)
    assert ctx.fact_snapshot is not None
    assert ctx.fact_snapshot_pin is not None
    assert ctx.fact_snapshot.program_id == "acme"
    store.append_fact(_fact(text="Changed after resolution"), recorded_at=NOW + timedelta(seconds=1))
    with (
        patch.object(action_stage, "load_program_facts", side_effect=AssertionError("current read")),
        patch.object(ProgramFactStore, "snapshot", side_effect=AssertionError("current read")),
    ):
        result = ActionStage().execute(ctx)
    assert [action.text for action in result.actions] == ["Captured action"]
    assert result.overdue_action_ids == ("a1",)
    assert result.action_warnings == ()


def test_primary_resolved_report_missing_capture_cannot_current_read(tmp_path, monkeypatch) -> None:
    ctx = replace(_resolved(tmp_path, monkeypatch), fact_snapshot=None, fact_snapshot_pin=None)
    with patch.object(action_stage, "load_program_facts", side_effect=AssertionError("current read")):
        with pytest.raises(ConfigError, match="snapshot"):
            ActionStage().execute(ctx)


def test_primary_action_rejects_other_program_snapshot(tmp_path, monkeypatch) -> None:
    ctx = _resolved(tmp_path, monkeypatch)
    ctx = replace(ctx, fact_snapshot=ProgramFactSnapshot("other", NOW, (), NOW))
    with pytest.raises(ConfigError, match="program"):
        ActionStage().execute(ctx)


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_nonprimary_actions_preserve_authored_loader_path(tmp_path, monkeypatch, mode) -> None:
    ctx = _resolved(tmp_path, monkeypatch, mode)
    journal = tmp_path / "programs" / "acme" / "journal" / "actions.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(_fact("authored", text="Authored action").payload) + "\n", encoding="utf-8")
    assert ProgramFactStore("acme", db_root=tmp_path).snapshot().facts == ()
    assert [action.text for action in ActionStage().execute(ctx).actions] == ["Authored action"]


def test_primary_standalone_missing_capture_retains_legacy_helper_compatibility(tmp_path, monkeypatch) -> None:
    ctx = replace(_resolved(tmp_path, monkeypatch), fact_snapshot=None, fact_snapshot_pin=None, bundle=None)
    with patch.object(action_stage, "_load_current_actions", return_value=()) as load:
        assert ActionStage().execute(ctx).actions == ()
    load.assert_called_once()


def test_lookback_and_prepopulated_actions_do_not_acquire_current_facts(tmp_path, monkeypatch) -> None:
    ctx = _resolved(tmp_path, monkeypatch)
    with patch.object(action_stage, "load_program_facts", side_effect=AssertionError("current read")):
        assert ActionStage().execute(replace(ctx, resolved_edition_type=EditionType.LOOKBACK)).actions == ()
        seeded = replace(ctx, actions=())
        assert ActionStage().execute(seeded) is seeded


def test_missing_context_and_invalid_action_keep_existing_stage_outcomes(tmp_path, monkeypatch) -> None:
    assert ActionStage().name() == "action"
    assert ActionStage().execute(StageContext(edition_name="standalone")).actions == ()
    ctx = _resolved(tmp_path, monkeypatch)
    bad = ProgramFactStore("acme", db_root=tmp_path).append_fact(
        replace(_fact(), payload={}), recorded_at=NOW,
    ).revision
    result = ActionStage().execute(replace(ctx, fact_snapshot=ProgramFactSnapshot("acme", NOW, (bad,), NOW)))
    assert result.actions == ()
    assert result.action_warnings == ("Action register skipped: 'created_at'",)
