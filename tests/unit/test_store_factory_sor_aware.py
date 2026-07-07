"""GAP-16: SoR-aware store factory must route to SQLite when program is primary."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core import program_fact_store, store_factory
from src.core.exceptions import ConfigError
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
    """Authority resolution failure must not select an alternate store."""
    def _explode(*a, **k):
        raise RuntimeError("no SoR state on disk")

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", _explode)
    with pytest.raises(RuntimeError, match="no SoR state on disk"):
        resolve_sor_aware_backend("acme", programs_root=tmp_path)


@pytest.mark.parametrize("entrypoint", ["resolve_sor_aware_backend", "build_signal_store", "build_trajectory_store"])
@pytest.mark.parametrize("error", [ConfigError("invalid authority"), OSError("unreadable authority"), RuntimeError("resolver bug")])
def test_authority_failure_prevents_any_backend_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entrypoint: str, error: Exception
) -> None:
    def fail_resolution(*_args: object, **_kwargs: object) -> str:
        raise error

    def forbidden_backend(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Authority resolution failed, but a backend was still constructed.")

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", fail_resolution)
    for builder in (
        "build_file_signal_store", "build_sqlite_signal_store",
        "build_file_trajectory_store", "build_sqlite_trajectory_store",
    ):
        monkeypatch.setattr(store_factory, builder, forbidden_backend)

    with pytest.raises(type(error)) as raised:
        getattr(store_factory, entrypoint)(program_id="fixture_program", programs_root=tmp_path)
    assert raised.value is error


@pytest.mark.parametrize("entrypoint", ["build_signal_store", "build_trajectory_store"])
def test_malformed_authority_state_fails_before_backend_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entrypoint: str
) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    program_dir = tmp_path / "fixture_program"
    program_dir.mkdir()
    (program_dir / "fact_store_sor.yaml").write_text("schema_version: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        getattr(store_factory, entrypoint)(program_id="fixture_program", programs_root=tmp_path)
    assert not tuple(tmp_path.rglob("*.db"))
    assert not tuple(tmp_path.rglob("*.jsonl"))


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
@pytest.mark.parametrize("backend", ["file", "sqlite", None])
def test_valid_nonprimary_modes_preserve_configured_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, backend: str | None
) -> None:
    from src.core.models_v2 import Program

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", lambda **_kwargs: mode)
    monkeypatch.setattr(
        store_factory, "load_program",
        lambda program_id, **_kwargs: None if backend is None else Program(
            id=program_id, name="Synthetic program", schema_version="1.0", storage_backend=backend, ado=None
        ),
    )
    assert resolve_sor_aware_backend("fixture_program", programs_root=tmp_path) == (backend or "file")


@pytest.mark.parametrize("backend", ["file", "sqlite"])
def test_explicit_backend_does_not_attempt_authority_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    def forbidden_resolution(**_kwargs: object) -> str:
        pytest.fail("An explicit backend override must not perform authority resolution.")

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", forbidden_resolution)
    assert resolve_sor_aware_backend(
        "fixture_program", explicit_backend=backend, programs_root=tmp_path
    ) == backend


@pytest.mark.parametrize("entrypoint", ["resolve_sor_aware_backend", "build_signal_store", "build_trajectory_store"])
def test_invalid_explicit_backend_is_rejected_without_authority_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entrypoint: str
) -> None:
    def forbidden_resolution(**_kwargs: object) -> str:
        pytest.fail("An invalid explicit backend must be rejected before authority lookup.")

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", forbidden_resolution)
    backend_argument = "explicit_backend" if entrypoint == "resolve_sor_aware_backend" else "storage_backend"
    with pytest.raises(ConfigError, match="Unsupported storage_backend"):
        getattr(store_factory, entrypoint)(
            program_id="fixture_program", programs_root=tmp_path, **{backend_argument: "invalid"}
        )


@pytest.mark.parametrize("entrypoint", ["build_program_signal_store", "build_program_trajectory_store"])
@pytest.mark.parametrize("backend", ["file", "sqlite"])
def test_program_object_builders_preserve_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entrypoint: str, backend: str
) -> None:
    from src.core.file_stores import FileSignalStore, FileTrajectoryStore
    from src.core.models_v2 import Program
    from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore

    def forbidden_resolution(**_kwargs: object) -> str:
        pytest.fail("Program-object helpers retain their existing explicit configuration contract.")

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", forbidden_resolution)
    program = Program(schema_version="1.0", id="fixture_program", name="Synthetic program", storage_backend=backend)
    store = getattr(store_factory, entrypoint)(program, programs_root=tmp_path)
    expected_types = (
        (FileSignalStore, FileTrajectoryStore) if backend == "file"
        else (SQLiteSignalStore, SQLiteTrajectoryStore)
    )
    assert isinstance(store, expected_types)


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
