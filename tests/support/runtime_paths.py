"""Sanctioned test-fixture helper for runtime-artifact paths
(specs/declutter.md §12 Phase 0.5: "Test fixture allowlist/helper plan exists
for intentional temporary runtime-store paths").

Why this exists
---------------
The R-1a inline-construction ban
(``tests/contracts/test_runtime_path_construction.py``) scans ``src/`` and
``scripts/`` only — production code. Test fixtures are intentionally exempt,
because many tests must construct a runtime-artifact path (or place data at the
legacy root vs the canonical ``runtime/`` location) to exercise the
transitional read-resolver fallback and the DC-02 split-brain signal. To keep
that exemption intentional rather than ad-hoc, this module is the single
sanctioned place for test fixtures to:

* resolve a runtime-artifact path (legacy root, canonical ``runtime/``, or the
  read resolver), and
* seed a runtime artifact on disk at one or both locations.

Tests that need a runtime-artifact path should import from here (or directly
from ``src.core.program_paths``) rather than writing
``programs_root / program_id / "channel_registry.sqlite3"`` inline. The
allowlist rationale and the list of runtime artifacts live in
``src/core/program_paths.py`` (``RUNTIME_ARTIFACTS``); this helper never
re-declares a filename — it delegates to that registry.
"""
from __future__ import annotations

from pathlib import Path

from src.core.program_paths import (
    RUNTIME_ARTIFACTS_BY_NAME,
    RUNTIME_SUBDIR,
    get_runtime_dir,
    resolve_channel_registry_path_for_read,
    resolve_dedup_drop_log_path_for_read,
    resolve_gather_state_path_for_read,
    resolve_m365_registry_path_for_read,
    resolve_program_analytics_store_path_for_read,
    resolve_readiness_snapshot_path_for_read,
    resolve_run_telemetry_path_for_read,
)

__all__ = [
    "RUNTIME_ARTIFACT_NAMES",
    "legacy_runtime_artifact_path",
    "canonical_runtime_artifact_path",
    "resolver_runtime_artifact_path",
    "seed_legacy_runtime_artifact",
    "seed_canonical_runtime_artifact",
    "seed_split_brain_runtime_artifact",
]

# The sanctioned set of runtime-artifact names test fixtures may construct.
RUNTIME_ARTIFACT_NAMES: frozenset[str] = frozenset(RUNTIME_ARTIFACTS_BY_NAME)


def _artifact(name: str) -> str:
    if name not in RUNTIME_ARTIFACTS_BY_NAME:
        raise KeyError(
            f"{name!r} is not a registered runtime artifact. Known: "
            f"{sorted(RUNTIME_ARTIFACTS_BY_NAME)}"
        )
    return RUNTIME_ARTIFACTS_BY_NAME[name].filename


def legacy_runtime_artifact_path(programs_root: Path, program_id: str, name: str) -> Path:
    """Path of a runtime artifact at its legacy root location (pre-Phase-1-E)."""
    return programs_root / program_id / _artifact(name)


def canonical_runtime_artifact_path(programs_root: Path, program_id: str, name: str) -> Path:
    """Path of a runtime artifact at its canonical ``runtime/`` location."""
    return get_runtime_dir(program_id, programs_root=programs_root) / _artifact(name)


_RESOLVERS = {
    "gather_state": resolve_gather_state_path_for_read,
    "run_telemetry": resolve_run_telemetry_path_for_read,
    "dedup_drop_log": resolve_dedup_drop_log_path_for_read,
    "m365_registry": resolve_m365_registry_path_for_read,
    "channel_registry": resolve_channel_registry_path_for_read,
    "vertex_analytics": resolve_program_analytics_store_path_for_read,
    "readiness_snapshot": resolve_readiness_snapshot_path_for_read,
}


def resolver_runtime_artifact_path(programs_root: Path, program_id: str, name: str) -> Path:
    """Path the transitional read resolver returns for a runtime artifact.

    Phase 1-B: canonical-first (``runtime/``), falls back to the legacy root
    only when the canonical file does not yet exist. Tests that exercise the
    resolver's behaviour should use this rather than hard-coding a location.
    """
    _artifact(name)  # validate the name is registered
    resolver = _RESOLVERS[name]
    return resolver(program_id, programs_root=programs_root)


def seed_legacy_runtime_artifact(
    programs_root: Path, program_id: str, name: str, *, content: bytes | str = b""
) -> Path:
    """Create a runtime artifact at its legacy root location; return the path."""
    path = legacy_runtime_artifact_path(programs_root, program_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, content)
    return path


def seed_canonical_runtime_artifact(
    programs_root: Path, program_id: str, name: str, *, content: bytes | str = b""
) -> Path:
    """Create a runtime artifact at its canonical ``runtime/`` location."""
    path = canonical_runtime_artifact_path(programs_root, program_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, content)
    return path


def seed_split_brain_runtime_artifact(
    programs_root: Path, program_id: str, name: str, *, content: bytes | str = b""
) -> tuple[Path, Path]:
    """Seed a runtime artifact at BOTH legacy root and canonical runtime/.

    Use to exercise the DC-02 split-brain detection and the read resolver's
    canonical-first ordering. Returns ``(legacy_path, canonical_path)``.
    """
    legacy = seed_legacy_runtime_artifact(programs_root, program_id, name, content=content)
    canonical = seed_canonical_runtime_artifact(programs_root, program_id, name, content=content)
    return legacy, canonical


def _write(path: Path, content: bytes | str) -> None:
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)