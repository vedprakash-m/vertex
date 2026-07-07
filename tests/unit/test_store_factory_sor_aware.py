"""GAP-16: SoR-aware store factory must route to SQLite when program is primary."""
from __future__ import annotations

from pathlib import Path

from src.core import program_fact_store, store_factory
from src.core.store_factory import (
    build_signal_store,
    build_trajectory_store,
    resolve_sor_aware_backend,
)


def test_resolve_sor_aware_backend_explicit_backend_wins(monkeypatch, tmp_path: Path) -> None:
    """Explicit backend always wins, regardless of SoR mode."""
    monkeypatch.setattr(
        program_fact_store, "resolve_fact_sor_mode", lambda *a, **k: "primary"
    )
    backend = resolve_sor_aware_backend("acme", explicit_backend="file", programs_root=tmp_path)
    assert backend == "file"


def test_resolve_sor_aware_backend_primary_returns_sqlite(monkeypatch, tmp_path: Path) -> None:
    """Program in 'primary' SoR mode → sqlite backend."""
    monkeypatch.setattr(
        program_fact_store, "resolve_fact_sor_mode", lambda *a, **k: "primary"
    )
    backend = resolve_sor_aware_backend("acme", programs_root=tmp_path)
    assert backend == "sqlite"


def test_resolve_sor_aware_backend_legacy_uses_program_config(monkeypatch, tmp_path: Path) -> None:
    """Legacy program → fall through to its own storage_backend config."""
    from src.core.models_v2 import Program

    monkeypatch.setattr(
        program_fact_store, "resolve_fact_sor_mode", lambda *a, **k: "legacy"
    )

    def _fake_load(pid, programs_root=None):
        return Program(id=pid, name=pid, schema_version="1.0", storage_backend="file", ado=None)

    monkeypatch.setattr(store_factory, "load_program", _fake_load)
    backend = resolve_sor_aware_backend("acme", programs_root=tmp_path)
    assert backend == "file"


def test_resolve_sor_aware_backend_no_program_id_defaults_to_file(monkeypatch, tmp_path: Path) -> None:
    """No program_id → no SoR check → default to file."""
    backend = resolve_sor_aware_backend(None, programs_root=tmp_path)
    assert backend == "file"


def test_resolve_sor_aware_backend_handles_sor_resolution_failure(
    monkeypatch, tmp_path: Path
) -> None:
    """If SoR resolution raises, fall back to legacy file path."""
    def _explode(*a, **k):
        raise RuntimeError("no SoR state on disk")

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", _explode)
    backend = resolve_sor_aware_backend("acme", programs_root=tmp_path)
    assert backend == "file"


def test_build_signal_store_honors_sor_primary(monkeypatch, tmp_path: Path) -> None:
    """build_signal_store routes to SQLite when SoR is primary."""
    calls: list[Path] = []

    def _fake_sqlite_signal(programs_root=None):
        calls.append(programs_root or tmp_path)
        return object()

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", lambda *a, **k: "primary")
    monkeypatch.setattr(store_factory, "build_sqlite_signal_store", _fake_sqlite_signal)
    store = build_signal_store(program_id="acme", programs_root=tmp_path)
    assert store is not None
    assert len(calls) == 1


def test_build_trajectory_store_honors_sor_primary(monkeypatch, tmp_path: Path) -> None:
    """build_trajectory_store routes to SQLite when SoR is primary."""
    calls: list[Path] = []

    def _fake_sqlite_traj(programs_root=None):
        calls.append(programs_root or tmp_path)
        return object()

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", lambda *a, **k: "primary")
    monkeypatch.setattr(store_factory, "build_sqlite_trajectory_store", _fake_sqlite_traj)
    store = build_trajectory_store(program_id="acme", programs_root=tmp_path)
    assert store is not None
    assert len(calls) == 1
