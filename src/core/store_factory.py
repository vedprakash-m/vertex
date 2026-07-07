from __future__ import annotations

from pathlib import Path

from src.core.edition_resolver import load_program
from src.core.exceptions import ConfigError
from src.core.file_stores import build_file_signal_store, build_file_trajectory_store
from src.core.journal import PROGRAMS_ROOT, read_review_log
from src.core.models_v2 import Program, SignalReviewDecision
from src.core.sqlite_stores import build_sqlite_signal_store, build_sqlite_trajectory_store, read_sqlite_signal_review_log
from src.core.store_protocols import SignalStore, TrajectoryStore


def resolve_storage_backend(storage_backend: str | None) -> str:
    if storage_backend is None:
        return "file"
    normalized = storage_backend.strip().lower()
    if normalized in {"file", "sqlite"}:
        return normalized
    raise ConfigError(f"Unsupported storage_backend '{storage_backend}'. Expected 'file' or 'sqlite'.")


def resolve_sor_aware_backend(
    program_id: str | None,
    *,
    explicit_backend: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> str:
    """Return the effective storage backend for a program, respecting the SoR flip.

    Resolution order:
    1. explicit_backend (caller override) wins if provided.
    2. No program_id → default to "file".
    3. SoR mode "primary" → "sqlite".
    4. SoR mode "legacy" → program config's storage_backend (defaults to "file").
    5. SoR resolution failure → fall back to "file".
    """
    if explicit_backend is not None:
        return resolve_storage_backend(explicit_backend)
    if program_id is None:
        return "file"
    try:
        from src.core import program_fact_store as _program_fact_store

        mode = _program_fact_store.resolve_fact_sor_mode(
            program_id=program_id, programs_root=programs_root
        )
    except Exception:
        return "file"
    if mode == "primary":
        return "sqlite"
    program = load_program(program_id, programs_root=programs_root)
    return resolve_storage_backend(program.storage_backend if program is not None else None)


def build_signal_store(
    *,
    storage_backend: str | None = None,
    program_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> SignalStore:
    backend = resolve_sor_aware_backend(
        program_id, explicit_backend=storage_backend, programs_root=programs_root
    )
    if backend == "sqlite":
        return build_sqlite_signal_store(programs_root=programs_root)
    return build_file_signal_store(programs_root=programs_root)


def build_trajectory_store(
    *,
    storage_backend: str | None = None,
    program_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> TrajectoryStore:
    backend = resolve_sor_aware_backend(
        program_id, explicit_backend=storage_backend, programs_root=programs_root
    )
    if backend == "sqlite":
        return build_sqlite_trajectory_store(programs_root=programs_root)
    return build_file_trajectory_store(programs_root=programs_root)


def build_program_signal_store(program: Program, *, programs_root: Path = PROGRAMS_ROOT) -> SignalStore:
    return build_signal_store(storage_backend=program.storage_backend, programs_root=programs_root)


def build_program_trajectory_store(program: Program, *, programs_root: Path = PROGRAMS_ROOT) -> TrajectoryStore:
    return build_trajectory_store(storage_backend=program.storage_backend, programs_root=programs_root)


def build_signal_store_for_program_id(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> SignalStore:
    program = load_program(program_id, programs_root=programs_root)
    return build_signal_store(
        storage_backend=program.storage_backend if program is not None else None,
        programs_root=programs_root,
    )


def build_trajectory_store_for_program_id(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> TrajectoryStore:
    program = load_program(program_id, programs_root=programs_root)
    return build_trajectory_store(
        storage_backend=program.storage_backend if program is not None else None,
        programs_root=programs_root,
    )


def read_signal_review_log_for_program_id(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[SignalReviewDecision, ...]:
    program = load_program(program_id, programs_root=programs_root)
    if resolve_storage_backend(program.storage_backend if program is not None else None) == "sqlite":
        return read_sqlite_signal_review_log(program_id, programs_root=programs_root)
    return tuple(
        entry
        for entry in read_review_log(program_id, programs_root=programs_root)
        if isinstance(entry, SignalReviewDecision)
    )
