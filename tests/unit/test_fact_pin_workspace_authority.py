from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

import src.core.quality_gates.fact_drift as fact_drift
from src.commands.report import generate_report_draft
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore, load_program_facts
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_confirm import (
    _isolate_fact_store_db as _isolate_fact_store_db,
    test_confirm_issue_archives_outputs_and_resets_active_state as _exercise_actual_confirm,
)
from tests.unit.test_commands_report import _sample_items
from tests.support.synthetic_report_source import synthetic_report_root as repo_root


@pytest.mark.parametrize("database_mode", ["directory", "file", "workspace"])
def test_actual_report_pin_uses_current_fact_database(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_mode: str,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    if database_mode == "workspace":
        monkeypatch.delenv("VERTEX_DB_PATH")
        db_root = tmp_path
    else:
        db_root = tmp_path / ("configured.sqlite3" if database_mode == "file" else "configured-db")
        monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    store = ProgramFactStore("acme", db_root=db_root)
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    first = store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "hold"},
            natural_key="selected-authority-first",
        ),
        recorded_at=as_of,
    )

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        programs_root=programs_root,
        archive_root=archive_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    with sqlite3.connect(store.db_path) as connection:
        pins = connection.execute("SELECT snapshot_id FROM program_fact_snapshot_pins").fetchall()
    assert len(pins) == 1
    pin_id = pins[0][0]
    pin = store.load_snapshot_pin(pin_id)
    assert pin is not None and pin.pinned_revision_count == 1
    current = load_program_facts("acme", programs_root=programs_root, archive_root=archive_root)
    assert first.revision.revision_id in {fact.revision_id for fact in current.facts}
    assert store.detect_drift(pin_id) == ()

    added = store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "ship"},
            natural_key="selected-authority-second",
        ),
        recorded_at=datetime(2026, 5, 4, 18, tzinfo=timezone.utc),
    )
    assert added.revision.revision_id in {fact.revision_id for fact in store.detect_drift(pin_id)}


def test_actual_confirm_drift_uses_configured_fact_database(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_paths: list[Path] = []

    def open_store(program_id: str, *, db_root: Path | None = None) -> ProgramFactStore:
        store = ProgramFactStore(program_id, db_root=db_root)
        observed_paths.append(store.db_path)
        return store

    monkeypatch.setattr(fact_drift, "ProgramFactStore", open_store)

    _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert observed_paths == [tmp_path / ".vertex" / "acme" / "vertex.sqlite3"] * 2


def test_explicit_fact_database_still_overrides_configured_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "configured-db"))
    explicit_root = tmp_path / "explicit-db"
    store = ProgramFactStore("acme", db_root=explicit_root)
    as_of = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)
    written = store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "explicit"},
        ),
        recorded_at=as_of,
    )

    snapshot = load_program_facts(
        "acme", as_of=as_of, db_root=explicit_root, programs_root=tmp_path / "programs",
    )

    assert [fact.revision_id for fact in snapshot.facts] == [written.revision.revision_id]
    assert not (tmp_path / "configured-db").exists()
