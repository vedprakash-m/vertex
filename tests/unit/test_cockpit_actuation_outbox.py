from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import src.commands.cockpit as cockpit
import src.core.cockpit_builder as builder
import src.core.ledger.durable_outbox_store as store
from src.commands.cockpit_tui import run_cockpit_tui_loop
from src.core._db import open_program_db
from src.core.actuation_outbox import outbox_db_path
from src.core.cockpit_html import render_cockpit_html
from src.core.cockpit_models import CockpitSnapshot, cockpit_snapshot_from_json_dict, cockpit_snapshot_to_json_dict
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN

_NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
_CANARY = "synthetic-private-canary"


def _record(db: Path, identifier: str, final: str) -> None:
    domain = ACTUATION_DISPATCH_DOMAIN
    store.enqueue(db, identifier, domain=domain, correlation_id=identifier, payload_json=f'{{"content":"{_CANARY}"}}')
    if final == "pending":
        return
    leased = store.lease_next(db, domain, owner="synthetic", ttl_seconds=120)
    assert leased is not None and leased.outbox_id == identifier
    args = {"owner": "synthetic", "fencing_token": leased.lease_fencing_token}
    if final == "leased":
        return
    if final == "failed_retryable":
        store.mark_failed_retryable(
            db, identifier, reason=_CANARY, next_attempt_at=datetime.now(timezone.utc) + timedelta(days=1), **args,
        )
        return
    if final == "dead_letter":
        store.mark_dead_letter(db, identifier, reason=_CANARY, **args)
        return
    if final == "failed_terminal":
        store.mark_failed_terminal(db, identifier, reason=_CANARY, **args)
        return
    if final == "manually_resolved":
        store.mark_failed_terminal(db, identifier, reason=_CANARY, **args)
        store.mark_manually_resolved(db, identifier, resolved_by="synthetic", reason=_CANARY)
        return
    store.mark_dispatched(db, identifier, **args)
    if final == "dispatched":
        return
    if final == "uncertain_remote_state":
        store.mark_uncertain_remote_state(db, identifier, reason=_CANARY, **args)
        return
    store.mark_succeeded(db, identifier, **args)
    if final == "succeeded":
        return
    if final == "compensation_required":
        store.mark_compensation_required(db, identifier, reason=_CANARY, **args)
        return
    store.mark_audited(db, identifier, **args)
    if final == "audited":
        return
    assert final == "completed"
    store.mark_completed(db, identifier, **args)


def _mixed_journal(root: Path) -> Path:
    db = outbox_db_path("fixture", programs_root=root)
    for status in (
        "uncertain_remote_state", "dead_letter", "failed_terminal", "compensation_required",
        "failed_retryable", "leased", "dispatched", "succeeded", "audited", "completed",
        "manually_resolved", "pending",
    ):
        _record(db, f"fixture:synthetic/project:create_task.v1:new:{status}", status)
    store.enqueue(db, "other-domain", domain="other", correlation_id="other", payload_json=_CANARY)
    _record(outbox_db_path("decoy", programs_root=root), "decoy", "uncertain_remote_state")
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    return db


def _raw_status(db: Path, status: str) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as connection:
        connection.executescript(store._SCHEMA_SQL)
        connection.execute(
            "INSERT INTO outbox (outbox_id, domain, correlation_id, payload_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("synthetic", ACTUATION_DISPATCH_DOMAIN, "synthetic", "{}", status, _NOW.isoformat(), _NOW.isoformat()),
        )


def _show(root: Path, monkeypatch: pytest.MonkeyPatch, *, output_format: str = "json") -> str:
    monkeypatch.setattr(cockpit, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(
        cockpit.app, ["show", "--program", "fixture", "--format", output_format, "--no-persist"]
    )
    assert result.exit_code == 0, repr(result.exception)
    return result.stdout


def _counts(snapshot: CockpitSnapshot) -> tuple[int | None, int | None, int | None]:
    reliability = snapshot.reliability_summary
    return reliability.outbox_pending, reliability.uncertain_remote_state, reliability.dead_letter_count


def test_real_cli_and_cockpit_surface_observed_terminal_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    db = _mixed_journal(root)
    before = db.read_bytes()

    text = _show(root, monkeypatch)
    payload = json.loads(text)
    summary = payload["reliability_summary"]
    assert (summary["outbox_pending"], summary["uncertain_remote_state"], summary["dead_letter_count"]) == (1, 1, 1)
    findings = {row["finding_id"]: row for row in payload["findings"]}
    for status in ("uncertain_remote_state", "dead_letter", "failed_terminal", "compensation_required"):
        finding = findings[f"reliability.outbox.{status}"]
        assert finding["status"] == "warn"
        assert finding["next_command"] == f"vertex cockpit explain --program fixture --finding reliability.outbox.{status}"
    assert "reliability.outbox.not_wired" not in findings
    assert _CANARY not in text and str(root) not in text
    assert db.read_bytes() == before

    explain = CliRunner().invoke(
        cockpit.app, ["explain", "--program", "fixture", "--finding", "reliability.outbox.uncertain_remote_state"]
    )
    assert explain.exit_code == 0, repr(explain.exception)
    assert "unknown" in explain.stdout.lower()
    assert _CANARY not in explain.stdout and str(root) not in explain.stdout

    output: list[str] = []
    run_cockpit_tui_loop("fixture", programs_root=root, input_fn=lambda _: "q", output_fn=output.append)
    assert "unknown" in "\n".join(output).lower()
    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)
    html = render_cockpit_html(snapshot, retained=True)
    assert "reliability.outbox.uncertain_remote_state" in html
    assert _CANARY not in html and str(root) not in html
    assert db.read_bytes() == before


def test_status_read_is_coherent_selected_domain_and_does_not_read_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    db = _mixed_journal(root)
    before = db.read_bytes()
    statements: list[str] = []
    real_open = store.open_program_db

    def trace_open(*args: object, **kwargs: object):
        unit = real_open(*args, **kwargs)
        unit.connection.set_trace_callback(statements.append)
        return unit

    opened = Mock(side_effect=trace_open)
    monkeypatch.setattr(store, "open_program_db", opened)

    counts = store.read_status_counts(db, ACTUATION_DISPATCH_DOMAIN)

    assert counts is not None
    assert counts == {
        status: 1 for status in (
            "uncertain_remote_state", "dead_letter", "failed_terminal", "compensation_required",
            "failed_retryable", "leased", "dispatched", "succeeded", "audited", "completed",
            "manually_resolved", "pending",
        )
    }
    opened.assert_called_once_with(db, read_only=True)
    assert statements[0] == "BEGIN"
    status_queries = [statement for statement in statements if statement.startswith("SELECT status, COUNT(*)")]
    assert len(status_queries) == 1
    assert "payload_json" not in " ".join(statements)
    assert "failure_reason" not in " ".join(statements)
    assert db.read_bytes() == before


def test_missing_journal_is_unknown_and_not_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    db = outbox_db_path("fixture", programs_root=root)
    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)
    assert _counts(snapshot) == (None, None, None)
    assert next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.unavailable").status == "info"
    assert not db.exists()
    assert json.loads(_show(root, monkeypatch))["reliability_summary"]["uncertain_remote_state"] is None
    assert not db.exists()


def test_initialized_empty_journal_is_observed_empty(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    db = outbox_db_path("fixture", programs_root=root)
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.executescript(store._SCHEMA_SQL)

    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)

    assert _counts(snapshot) == (0, 0, 0)
    assert not any(f.finding_id == "reliability.outbox.unavailable" for f in snapshot.findings)
    assert not any(f.status == "warn" and f.finding_id.startswith("reliability.outbox.") for f in snapshot.findings)


def test_percent_encoded_name_does_not_read_another_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    selected = outbox_db_path("fixture%20x", programs_root=root)
    decoy = outbox_db_path("fixture x", programs_root=root)
    _raw_status(selected, "pending")
    _raw_status(decoy, "uncertain_remote_state")

    counts = builder._read_actuation_status_counts("fixture%20x", programs_root=root)

    assert counts == {"pending": 1}
    monkeypatch.setattr(cockpit, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(
        cockpit.app, ["show", "--program", "fixture%20x", "--format", "json", "--no-persist"],
    )
    assert result.exit_code == 0, repr(result.exception)
    assert json.loads(result.stdout)["reliability_summary"]["uncertain_remote_state"] == 0


@pytest.mark.parametrize("name", ["programs#scope", "programs%25scope", "programs%20scope"])
def test_sqlite_read_only_paths_preserve_literal_reserved_characters(tmp_path: Path, name: str) -> None:
    root = tmp_path / name
    db = outbox_db_path("fixture", programs_root=root)
    _raw_status(db, "pending")

    with open_program_db(db, read_only=True) as connection:
        assert connection.execute("SELECT status FROM outbox").fetchone()[0] == "pending"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM outbox")
    assert store.read_status_counts(db, ACTUATION_DISPATCH_DOMAIN) == {"pending": 1}


def test_outbox_warning_reports_the_observed_count(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    db = _mixed_journal(root)
    _raw_status(db, "uncertain_remote_state")
    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)
    finding = next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.uncertain_remote_state")
    assert snapshot.reliability_summary.uncertain_remote_state == 2
    assert finding.summary.endswith(": 2.")


@pytest.mark.parametrize("definition", [
    {"type": "table", "sql": None},
    {"type": "table", "sql": "CREATE VIRTUAL TABLE outbox USING synthetic()"},
])
def test_nonordinary_table_definitions_are_not_queried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, definition: dict[str, object],
) -> None:
    db = tmp_path / "outbox.db"
    db.touch()
    connection = Mock()
    connection.execute.side_effect = [
        None,
        Mock(fetchone=Mock(return_value=definition)),
    ]
    monkeypatch.setattr(store, "open_program_db", Mock(return_value=nullcontext(connection)))

    with pytest.raises(store.OutboxError, match="ordinary table"):
        store.read_status_counts(db, ACTUATION_DISPATCH_DOMAIN)
    assert connection.execute.call_count == 2


def test_unreadable_file_stat_does_not_become_a_missing_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "outbox.db"
    original = Path.stat

    def stat(path: Path, *args: object, **kwargs: object):
        if path == db:
            raise PermissionError(_CANARY)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(PermissionError):
        store.read_status_counts(db, ACTUATION_DISPATCH_DOMAIN)


@pytest.mark.parametrize(
    "damage", ["corrupt", "missing_table", "incomplete_schema", "view", "unknown_status", "binary_status"],
)
def test_invalid_journal_is_unknown_and_redacted(tmp_path: Path, damage: str) -> None:
    root = tmp_path / "programs"
    db = outbox_db_path("fixture", programs_root=root)
    db.parent.mkdir(parents=True)
    if damage == "corrupt":
        db.write_text(_CANARY, encoding="utf-8")
    elif damage == "missing_table":
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
    elif damage == "incomplete_schema":
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE outbox (domain TEXT, status TEXT)")
    elif damage == "view":
        with sqlite3.connect(db) as connection:
            connection.executescript(store._SCHEMA_SQL)
            connection.execute("ALTER TABLE outbox RENAME TO original_outbox")
            connection.execute("CREATE VIEW outbox AS SELECT * FROM original_outbox")
    else:
        _record(db, "intent", "pending")
        with sqlite3.connect(db) as connection:
            status = sqlite3.Binary(_CANARY.encode()) if damage == "binary_status" else _CANARY
            connection.execute("UPDATE outbox SET status = ?", (status,))
    before = db.read_bytes()

    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)

    assert _counts(snapshot) == (None, None, None)
    finding = next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.unavailable")
    assert finding.status == "warn"
    text = json.dumps(cockpit_snapshot_to_json_dict(snapshot))
    assert _CANARY not in text and str(root) not in text
    assert db.read_bytes() == before


@pytest.mark.parametrize("error", [
    PermissionError("synthetic-private-canary"), sqlite3.DatabaseError("synthetic-private-canary"),
    store.OutboxError("synthetic-private-canary"),
])
def test_expected_read_failures_are_explicit_and_unexpected_failures_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(builder, "read_status_counts", fail)
    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=tmp_path / "programs", now=_NOW)
    assert _counts(snapshot) == (None, None, None)
    assert next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.unavailable").status == "warn"
    assert _CANARY not in json.dumps(cockpit_snapshot_to_json_dict(snapshot))

    monkeypatch.setattr(builder, "read_status_counts", Mock(side_effect=RuntimeError("implementation defect")))
    with pytest.raises(RuntimeError, match="implementation defect"):
        builder.build_cockpit_snapshot("fixture", programs_root=tmp_path / "programs", now=_NOW)


def test_wal_mode_journal_is_unavailable_without_opening_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    db = _mixed_journal(root)
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    before = db.read_bytes()
    sidecars = {
        suffix: (db.with_name(db.name + suffix).read_bytes() if db.with_name(db.name + suffix).exists() else None)
        for suffix in ("-wal", "-shm", "-journal")
    }
    opener = Mock(wraps=store.open_program_db)
    monkeypatch.setattr(store, "open_program_db", opener)

    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)

    assert _counts(snapshot) == (None, None, None)
    finding = next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.unavailable")
    assert finding.status == "warn"
    opener.assert_not_called()
    assert db.read_bytes() == before
    assert {
        suffix: (db.with_name(db.name + suffix).read_bytes() if db.with_name(db.name + suffix).exists() else None)
        for suffix in sidecars
    } == sidecars


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_existing_wal_sidecar_is_unavailable_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    root = tmp_path / "programs"
    db = outbox_db_path("fixture", programs_root=root)
    _raw_status(db, "pending")
    sidecar = db.with_name(db.name + suffix)
    sidecar.write_bytes(b"synthetic-sidecar")
    before = db.read_bytes()
    sidecar_before = sidecar.read_bytes()
    opener = Mock(wraps=store.open_program_db)
    monkeypatch.setattr(store, "open_program_db", opener)

    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)

    assert _counts(snapshot) == (None, None, None)
    assert next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.unavailable").status == "warn"
    opener.assert_not_called()
    assert db.read_bytes() == before
    assert sidecar.read_bytes() == sidecar_before


def test_orphaned_sqlite_sidecar_is_not_reported_as_an_absent_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    db = outbox_db_path("fixture", programs_root=root)
    db.parent.mkdir(parents=True)
    sidecar = db.with_name(db.name + "-wal")
    sidecar.write_bytes(b"synthetic-orphaned-sidecar")
    sidecar_before = sidecar.read_bytes()
    opener = Mock(wraps=store.open_program_db)
    monkeypatch.setattr(store, "open_program_db", opener)

    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)

    assert _counts(snapshot) == (None, None, None)
    assert next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.unavailable").status == "warn"
    opener.assert_not_called()
    assert not db.exists()
    assert sidecar.read_bytes() == sidecar_before


@pytest.mark.parametrize(
    "error_type", [sqlite3.ProgrammingError, sqlite3.InterfaceError, sqlite3.InternalError, sqlite3.NotSupportedError],
)
def test_sqlite_implementation_errors_are_not_treated_as_an_unavailable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[sqlite3.Error],
) -> None:
    failure = error_type("synthetic implementation defect")
    monkeypatch.setattr(builder, "read_status_counts", Mock(side_effect=failure))

    with pytest.raises(error_type) as raised:
        builder.build_cockpit_snapshot("fixture", programs_root=tmp_path / "programs", now=_NOW)

    assert raised.value is failure


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_outbox_and_sqlite_sidecars_cannot_escape_selected_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str,
) -> None:
    root = tmp_path / "programs"
    db = _mixed_journal(root)
    selected = db.with_name(db.name + suffix)
    original = Path.resolve

    def resolved(path: Path, *args: object, **kwargs: object) -> Path:
        if path == selected:
            return (root / "decoy" / "outside").resolve()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved)
    reader = Mock(wraps=store.read_status_counts)
    monkeypatch.setattr(builder, "read_status_counts", reader)
    snapshot = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)

    assert _counts(snapshot) == (None, None, None)
    assert next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.unavailable").status == "warn"
    reader.assert_not_called()


@pytest.mark.parametrize("program_id", ["", ".", "..", "../decoy", "fixture\\decoy"])
def test_invalid_program_selection_does_not_read_an_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program_id: str,
) -> None:
    reader = Mock(wraps=store.read_status_counts)
    monkeypatch.setattr(builder, "read_status_counts", reader)
    with pytest.raises(store.OutboxError):
        builder._read_actuation_status_counts(program_id, programs_root=tmp_path / "programs")
    reader.assert_not_called()


@pytest.mark.parametrize("selection", ["program_alias", "resolve_error"])
def test_invalid_resolved_selection_does_not_read_an_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    root = tmp_path / "programs"
    db = outbox_db_path("fixture", programs_root=root)
    original = Path.resolve

    def resolved(path: Path, *args: object, **kwargs: object) -> Path:
        if selection == "program_alias" and path == root / "fixture":
            return original(root / "decoy")
        if selection == "resolve_error" and path == db:
            raise RuntimeError("synthetic-private-canary")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved)
    reader = Mock(wraps=store.read_status_counts)
    monkeypatch.setattr(builder, "read_status_counts", reader)
    with pytest.raises(store.OutboxError) as raised:
        builder._read_actuation_status_counts("fixture", programs_root=root)
    assert _CANARY not in str(raised.value)
    reader.assert_not_called()


def test_unusual_program_name_is_not_interpolated_into_a_command(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    program = "sample space"
    db = outbox_db_path(program, programs_root=root)
    _record(db, "intent", "uncertain_remote_state")
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    snapshot = builder.build_cockpit_snapshot(program, programs_root=root, now=_NOW)
    finding = next(f for f in snapshot.findings if f.finding_id == "reliability.outbox.uncertain_remote_state")
    assert finding.status == "warn"
    assert finding.next_command is None


def test_selected_root_and_existing_snapshot_shape_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    db = _mixed_journal(root)
    opened = Mock(wraps=store.open_program_db)
    monkeypatch.setattr(store, "open_program_db", opened)
    first = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)
    second = builder.build_cockpit_snapshot("fixture", programs_root=root, now=_NOW)
    assert first.input_hash == second.input_hash
    assert [call.args[0] for call in opened.call_args_list] == [db, db]

    payload = cockpit_snapshot_to_json_dict(first)
    assert set(payload["reliability_summary"]) == {
        "outbox_pending", "uncertain_remote_state", "dead_letter_count", "duplicate_preventions", "audit_coverage",
    }
    assert _counts(cockpit_snapshot_from_json_dict(payload)) == (1, 1, 1)
    payload["reliability_summary"].update(
        {"outbox_pending": 0, "uncertain_remote_state": 0, "dead_letter_count": 0},
    )
    assert _counts(cockpit_snapshot_from_json_dict(payload)) == (0, 0, 0)
