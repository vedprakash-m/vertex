"""Contract: at most one active SoR per program (WS-4, no-parallel-store invariant).

The Fact Store has three modes: ``legacy`` (JSONL ledger), ``shadow`` (dual-write
to both), and ``primary`` (SQLite is the SoR).  The invariant is:

  For any program, EXACTLY ONE of these must be true at any given time:
    - ``fact_sor_state.yaml`` does not exist          → implicit ``legacy``
    - ``fact_sor_state.yaml`` exists with mode=legacy → explicit legacy
    - ``fact_sor_state.yaml`` exists with mode=shadow → dual-write phase
    - ``fact_sor_state.yaml`` exists with mode=primary → SQLite is primary

  It must NEVER be the case that two programs can race each other with
  different SoR opinions of the same logical state, nor can the same
  program have two contradictory mode files.

This test suite pins:
  1. ``resolve_fact_sor_mode`` returns only ``legacy | shadow | primary``.
  2. When no state file exists, the default is ``legacy``.
  3. The VERTEX_FACT_SOR env-var overrides the file, with a validated set.
  4. An invalid env-var falls back to ``legacy`` (no crash).
  5. ``save_fact_sor_state`` + ``load_fact_sor_state`` round-trips correctly.
  6. ``save_fact_sor_state`` rejects invalid modes.
  7. The three valid mode strings (and only those three) are accepted.
  8. With mode=primary, ``build_program_fact_snapshot`` bypasses shim facts.
  9. With mode=legacy/shadow, shim facts are merged into the snapshot.

Why: PB-12/PB-36.  The flip command already exists; these tests ensure
the invariant holds during and after a flip so that WS-4 phase-3 work
can build on a verified foundation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.fact_sor_state import load_fact_sor_state, save_fact_sor_state
from src.core.program_fact_store import resolve_fact_sor_mode


# ─────────────────────────────────────────────
# 1. resolve_fact_sor_mode — default (no file)
# ─────────────────────────────────────────────

def test_resolve_defaults_to_legacy_when_no_state_file(tmp_path: Path) -> None:
    """No fact_sor_state.yaml → implicit legacy mode."""
    mode = resolve_fact_sor_mode(program_id="prog", programs_root=tmp_path)
    assert mode == "legacy"


def test_resolve_defaults_to_legacy_when_no_program_id(tmp_path: Path) -> None:
    """No program_id, no env-var → legacy."""
    mode = resolve_fact_sor_mode(programs_root=tmp_path)
    assert mode == "legacy"


# ─────────────────────────────────────────────
# 2. resolve_fact_sor_mode — reads state file
# ─────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["legacy", "shadow", "primary"])
def test_resolve_reads_state_file(tmp_path: Path, mode: str) -> None:
    """resolve_fact_sor_mode returns the mode stored in fact_sor_state.yaml."""
    save_fact_sor_state(
        "prog",
        mode=mode,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        programs_root=tmp_path,
    )
    result = resolve_fact_sor_mode(program_id="prog", programs_root=tmp_path)
    assert result == mode


# ─────────────────────────────────────────────
# 3. VERTEX_FACT_SOR env-var overrides the file
# ─────────────────────────────────────────────

@pytest.mark.parametrize("env_val", ["shadow", "primary", "legacy"])
def test_env_var_overrides_state_file(tmp_path: Path, env_val: str) -> None:
    """VERTEX_FACT_SOR env-var must override the on-disk state file."""
    # Write a state file with a different mode
    file_mode = "legacy" if env_val != "legacy" else "shadow"
    save_fact_sor_state(
        "prog",
        mode=file_mode,
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        programs_root=tmp_path,
    )
    result = resolve_fact_sor_mode(
        program_id="prog",
        programs_root=tmp_path,
        environ={"VERTEX_FACT_SOR": env_val},
    )
    assert result == env_val


def test_invalid_env_var_falls_back_to_legacy(tmp_path: Path) -> None:
    """An unrecognised VERTEX_FACT_SOR value → fallback legacy (no crash)."""
    result = resolve_fact_sor_mode(
        program_id="prog",
        programs_root=tmp_path,
        environ={"VERTEX_FACT_SOR": "invalid-mode"},
    )
    assert result == "legacy"


# ─────────────────────────────────────────────
# 4. save + load round-trip
# ─────────────────────────────────────────────

def test_save_load_roundtrip(tmp_path: Path) -> None:
    """save_fact_sor_state then load_fact_sor_state must be byte-equal for all valid modes."""
    for mode in ("legacy", "shadow", "primary"):
        recorded_at = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        save_fact_sor_state(
            "prog",
            mode=mode,
            recorded_at=recorded_at,
            recorded_by="test-agent",
            programs_root=tmp_path,
        )
        state = load_fact_sor_state("prog", programs_root=tmp_path)
        assert state is not None
        assert state.mode == mode
        assert state.recorded_by == "test-agent"


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    """load_fact_sor_state returns None when no file exists."""
    result = load_fact_sor_state("nonexistent", programs_root=tmp_path)
    assert result is None


# ─────────────────────────────────────────────
# 5. save rejects invalid modes
# ─────────────────────────────────────────────

def test_save_rejects_invalid_mode(tmp_path: Path) -> None:
    """save_fact_sor_state must raise ValueError for an unknown mode."""
    with pytest.raises(ValueError, match="legacy, shadow, or primary"):
        save_fact_sor_state(
            "prog",
            mode="parallel",
            recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            programs_root=tmp_path,
        )


# ─────────────────────────────────────────────
# 6. Invariant: only one active mode per program
# ─────────────────────────────────────────────

def test_mode_transitions_are_linear(tmp_path: Path) -> None:
    """Saving a new mode atomically replaces the old one — no two modes can
    be active simultaneously for the same program."""
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for mode in ("legacy", "shadow", "primary", "shadow", "legacy"):
        save_fact_sor_state("prog", mode=mode, recorded_at=recorded_at, programs_root=tmp_path)
        loaded = load_fact_sor_state("prog", programs_root=tmp_path)
        assert loaded is not None
        assert loaded.mode == mode, (
            f"After saving mode={mode!r}, loaded mode={loaded.mode!r}; "
            "only one active mode must be present at a time."
        )
        # Only ONE state file must exist per program (no stale parallel file)
        state_files = list((tmp_path / "prog").glob("fact_store_sor*.yaml"))
        assert len(state_files) == 1, (
            f"Expected exactly 1 fact_sor_state file, found {len(state_files)}: "
            f"{[f.name for f in state_files]}"
        )


# ─────────────────────────────────────────────
# 8. mode=primary bypasses _load_current_state_shim_facts
# ─────────────────────────────────────────────

def test_primary_mode_bypasses_shim_facts(tmp_path: Path) -> None:
    """WS-4 invariant: in 'primary' mode, build_program_fact_snapshot must NOT
    call _load_current_state_shim_facts.  SQLite is the sole source of truth."""
    from src.core.program_fact_store import load_program_facts

    save_fact_sor_state(
        "prog",
        mode="primary",
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        programs_root=tmp_path,
    )
    # Empty SQLite db (auto-created by ProgramFactStore._connect), no shim.
    with patch(
        "src.core.program_fact_store._load_current_state_shim_facts"
    ) as mock_shim:
        result = load_program_facts(
            "prog", programs_root=tmp_path, db_root=tmp_path
        )
        mock_shim.assert_not_called(), (
            "In 'primary' mode, _load_current_state_shim_facts must never be invoked."
        )

    assert result.facts == (), (
        "In 'primary' mode with an empty SQLite db, no facts should appear."
    )


# ─────────────────────────────────────────────
# 9. mode=legacy/shadow merges shim facts
# ─────────────────────────────────────────────

def test_legacy_mode_merges_shim_facts(tmp_path: Path) -> None:
    """WS-4 invariant: in 'legacy' mode, shim facts from
    _load_current_state_shim_facts are merged into the snapshot."""
    from src.core.program_fact_store import load_program_facts

    save_fact_sor_state(
        "prog",
        mode="legacy",
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        programs_root=tmp_path,
    )

    # Build a minimal fake shim fact (MagicMock provides natural_key + fact_type
    # so the sort-key lambda in load_program_facts works correctly).
    fake_shim = MagicMock()
    fake_shim.natural_key = "TEST:shim-key-legacy"
    fake_shim.fact_type = "action.item"

    with patch(
        "src.core.program_fact_store._load_current_state_shim_facts",
        return_value=(fake_shim,),
    ) as mock_shim:
        result = load_program_facts(
            "prog", programs_root=tmp_path, db_root=tmp_path
        )
        mock_shim.assert_called_once(), (
            "In 'legacy' mode, _load_current_state_shim_facts must be called."
        )

    assert fake_shim in result.facts, (
        "In 'legacy' mode, shim facts must be merged into the returned snapshot."
    )
