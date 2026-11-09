from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

import src.core.quality_gates.fact_drift as fact_drift
import src.core.program_fact_store as program_fact_store
from src.commands.report import generate_report_draft
from src.core.ledger.fact_bridge import run_cross_source_conflict_detection
from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore, load_program_facts
from src.core.rev.pipeline import _compute_family_divergence
from tests.integration.test_rev_e2e import _run_cycle
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


def _rev_fact_stores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> tuple[Path, ProgramFactStore, ProgramFactStore]:
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    programs_root = tmp_path / "workspace" / (
        "custom-root" if mode == "custom-root" else "programs"
    )
    if mode == "default":
        monkeypatch.setattr(program_fact_store, "PROGRAMS_ROOT", programs_root)
        resolve_path = program_fact_store.get_program_reality_db_path

        def resolve_default(
            program_id: str, *, home_root: Path | None = None, db_root: Path | None = None,
        ) -> Path:
            return resolve_path(
                program_id, home_root=home_root, db_root=db_root, programs_root=programs_root,
            )

        monkeypatch.setattr(program_fact_store, "get_program_reality_db_path", resolve_default)
        selected = ProgramFactStore("acme")
    elif mode in {"directory", "file"}:
        configured = tmp_path / ("configured.sqlite3" if mode == "file" else "configured-db")
        monkeypatch.setenv("VERTEX_DB_PATH", str(configured))
        selected = ProgramFactStore("acme")
    else:
        if mode == "empty-override":
            monkeypatch.setenv("VERTEX_DB_PATH", "")
        root = programs_root if mode == "custom-root" else programs_root.parent
        selected = ProgramFactStore("acme", db_root=root)

    decoy_root = tmp_path / "unselected" if mode in {"workspace", "empty-override"} else programs_root.parent
    return programs_root, selected, ProgramFactStore("acme", db_root=decoy_root)


def _append_rev_milestone(store: ProgramFactStore, name: str) -> None:
    store.append_fact(
        ProgramFactInput(
            fact_type="milestone.entry",
            entity_refs=(f"MILESTONE:{name}",),
            payload={"id": name, "target_date": "2026-01-01"},
            natural_key=f"rev-authority-{name}",
        )
    )


@pytest.mark.parametrize(
    "mode", ["directory", "file", "workspace", "empty-override", "custom-root", "default"],
)
def test_rev_family_divergence_uses_selected_fact_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    programs_root, selected, decoy = _rev_fact_stores(tmp_path, monkeypatch, mode)
    _append_rev_milestone(selected, "selected")
    _append_rev_milestone(decoy, "decoy-one")
    _append_rev_milestone(decoy, "decoy-two")
    decoy_bytes = decoy.db_path.read_bytes()

    assert _compute_family_divergence("acme", programs_root) == {"workitem.state": 1}
    assert decoy.db_path.read_bytes() == decoy_bytes


@pytest.mark.parametrize(
    "mode", ["directory", "file", "workspace", "empty-override", "custom-root", "default"],
)
def test_rev_conflict_detection_uses_selected_fact_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    programs_root, selected, decoy = _rev_fact_stores(tmp_path, monkeypatch, mode)
    _append_rev_milestone(selected, "selected")
    _append_rev_milestone(decoy, "decoy-one")
    _append_rev_milestone(decoy, "decoy-two")
    decoy_bytes = decoy.db_path.read_bytes()

    result = run_cross_source_conflict_detection("acme", programs_root=programs_root)

    assert result == {"conflicts": 0, "corroborations": 0, "observations": 1}
    assert decoy.db_path.read_bytes() == decoy_bytes


@pytest.mark.parametrize("mode", ["directory", "file"])
def test_rev_empty_configured_fact_store_does_not_create_parallel_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    programs_root, selected, decoy = _rev_fact_stores(tmp_path, monkeypatch, mode)

    assert _compute_family_divergence("acme", programs_root) == {}
    assert run_cross_source_conflict_detection("acme", programs_root=programs_root) == {
        "conflicts": 0, "corroborations": 0, "observations": 0,
    }
    assert selected.db_path.is_file()
    assert not decoy.db_path.exists()


@pytest.mark.parametrize("mode", ["directory", "file"])
def test_actual_rev_cycle_uses_configured_fact_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    programs_root, selected, decoy = _rev_fact_stores(tmp_path, monkeypatch, mode)
    _append_rev_milestone(selected, "selected")
    _append_rev_milestone(decoy, "decoy-one")
    _append_rev_milestone(decoy, "decoy-two")
    decoy_bytes = decoy.db_path.read_bytes()

    report = _run_cycle(program_id="acme", programs_root=programs_root, messages=())

    assert report["stop_category"] == "complete"
    assert report["family_divergence"] == {"workitem.state": 1}
    assert decoy.db_path.read_bytes() == decoy_bytes


@pytest.mark.parametrize("mode", ["directory", "file"])
def test_actual_rev_cycle_writes_conflicts_to_configured_fact_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    programs_root, selected, decoy = _rev_fact_stores(tmp_path, monkeypatch, mode)
    for source, status, precedence in (
        ("ado", "completed", FactPrecedence.VERIFIED_SYSTEM_SIGNAL),
        ("teams", "missed", FactPrecedence.ACTIVE_PM_JUDGMENT),
    ):
        selected.append_fact(
            ProgramFactInput(
                fact_type="milestone.entry",
                entity_refs=("MILESTONE:shared", f"SOURCE:{source}"),
                payload={"id": "shared", "status": status, "source": source},
                precedence=precedence,
                natural_key=f"rev-conflict-{source}",
            )
        )
    _append_rev_milestone(decoy, "decoy")
    decoy_bytes = decoy.db_path.read_bytes()

    report = _run_cycle(program_id="acme", programs_root=programs_root, messages=())

    conflicts = [fact for fact in selected.snapshot().facts if fact.fact_type == "fact.conflict"]
    assert report["stop_category"] == "complete"
    assert conflicts and "counter_source_as_of" in conflicts[0].payload
    assert decoy.db_path.read_bytes() == decoy_bytes
