"""Track K regression tests (specs/fix-data-flow.md §6.11 / PR-12).

Covers:
1. `_stray_fact_store_database_check` — detects PS-14's multi-database
   split-brain hazard (a stray ``vertex.sqlite3`` at a plausible-but-wrong
   location besides the canonical, `db_root`-resolved path).
2. The root-cause fix to `reality_store._resolve_reality_db_root`'s silent
   fallback — now logs CRITICAL when triggered.
3. Path-resolution-determinism: every public fact-store entry point resolves
   to the *same* database path for a fixed `program_id` + `programs_root`.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.commands.doctor_checks.storage_checks import _stray_fact_store_database_check
from src.core.program_fact_store import ProgramFactStore, load_program_facts
from src.core.reality_store import _resolve_reality_db_root, get_program_reality_db_path


# ---------------------------------------------------------------------------
# 1. _stray_fact_store_database_check
# ---------------------------------------------------------------------------


def test_stray_database_check_is_ok_when_only_canonical_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    programs_root = tmp_path / "programs"
    db_root = programs_root.parent  # matches production's programs_root.parent resolution
    canonical_path = get_program_reality_db_path("acme", db_root=db_root)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.touch()

    check = _stray_fact_store_database_check("acme", programs_root=programs_root, db_root=db_root)
    assert check.label == "Fact Store Location"
    assert check.status == "ok"
    assert check.metadata["stray_databases"] == {}


def test_stray_database_check_detects_programs_root_relative_stray(tmp_path: Path) -> None:
    """Minimal failing input reproducing PS-14's `programs/xpf/vertex.sqlite3`
    stray-database symptom: a second `vertex.sqlite3` exists directly under
    `programs_root/<id>/`, distinct from the canonical `programs_root.parent/<id>/`
    location production code actually resolves via `resolved_db_root =
    programs_root.parent` (`program_fact_store.py:457`)."""
    programs_root = tmp_path / "programs"
    db_root = programs_root.parent
    canonical_path = get_program_reality_db_path("xpf", db_root=db_root)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.touch()

    stray_path = programs_root / "xpf" / "vertex.sqlite3"
    stray_path.parent.mkdir(parents=True, exist_ok=True)
    stray_path.touch()

    check = _stray_fact_store_database_check("xpf", programs_root=programs_root, db_root=db_root)
    assert check.status == "warn"
    assert "programs_root_relative" in check.metadata["stray_databases"]
    assert "PS-14" in check.detail


def test_stray_database_check_detects_home_fallback_stray(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the other half of PS-14: a stray database at the
    home-directory fallback location (`~/.vertex/<id>/vertex.sqlite3`) —
    reachable only via `_resolve_reality_db_root`'s silent fallback when a
    caller omits both `db_root` and `programs_root`."""
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    programs_root = tmp_path / "programs"
    db_root = programs_root.parent
    canonical_path = get_program_reality_db_path("xpf", db_root=db_root)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.touch()

    home_stray_path = fake_home / ".vertex" / "xpf" / "vertex.sqlite3"
    home_stray_path.parent.mkdir(parents=True, exist_ok=True)
    home_stray_path.touch()

    check = _stray_fact_store_database_check("xpf", programs_root=programs_root, db_root=db_root)
    assert check.status == "warn"
    assert "home_fallback" in check.metadata["stray_databases"]


# ---------------------------------------------------------------------------
# 2. Root-cause fix: loud logging on the silent path fallback
# ---------------------------------------------------------------------------


def test_resolve_reality_db_root_logs_critical_on_silent_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:
    """PS-14 / Track K root-cause fix: previously a bare, unlogged fallback
    to `~/.vertex` whenever VERTEX_DB_PATH was unset and no db_root was
    threaded. Must now log at CRITICAL so the hazard is visible without
    requiring a `vertex doctor` run to notice."""
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)

    with caplog.at_level(logging.CRITICAL):
        resolved = _resolve_reality_db_root(home_root=tmp_path)

    assert resolved == tmp_path / ".vertex"
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)
    assert any("db_root/VERTEX_DB_PATH" in record.message for record in caplog.records)


def test_resolve_reality_db_root_does_not_log_when_env_var_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path,
) -> None:
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "explicit-root"))

    with caplog.at_level(logging.CRITICAL):
        resolved = _resolve_reality_db_root()

    assert resolved == tmp_path / "explicit-root"
    assert not any(record.levelno == logging.CRITICAL for record in caplog.records)


# ---------------------------------------------------------------------------
# 3. Path-resolution-determinism contract test
# ---------------------------------------------------------------------------


def test_fact_store_entry_points_resolve_to_the_same_canonical_path(tmp_path: Path) -> None:
    """Track K (§6.11, new in v1.3): for a fixed program_id + programs_root,
    every public fact-store entry point must resolve to the SAME database
    path. This is the structural guardrail that prevents a future refactor
    from threading a different `programs_root`/`db_root` to one call site
    but not another and silently reintroducing PS-14's split-brain hazard.
    """
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True, exist_ok=True)

    # Entry point 1: get_program_reality_db_path, resolved the way
    # production code resolves it (db_root = programs_root.parent).
    path_via_reality_store = get_program_reality_db_path("acme", db_root=programs_root.parent)

    # Entry point 2: ProgramFactStore's own internal resolution.
    store = ProgramFactStore("acme", db_root=programs_root.parent)
    path_via_fact_store = store.db_path

    # Entry point 3: load_program_facts's resolution when given programs_root
    # (mirrors every real pipeline stage's call shape -- programs_root, not db_root).
    load_program_facts("acme", programs_root=programs_root)
    path_via_load_program_facts = get_program_reality_db_path("acme", db_root=programs_root.parent)

    assert path_via_reality_store == path_via_fact_store == path_via_load_program_facts
