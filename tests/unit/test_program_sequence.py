from __future__ import annotations

from pathlib import Path

from src.core.ledger.program_sequence import current_sequence, next_sequence


def test_first_allocation_is_one(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert next_sequence("acme", programs_root=programs_root) == 1


def test_allocations_are_strictly_increasing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    values = [next_sequence("acme", programs_root=programs_root) for _ in range(10)]
    assert values == list(range(1, 11))


def test_sequences_are_isolated_per_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert next_sequence("acme", programs_root=programs_root) == 1
    assert next_sequence("nova", programs_root=programs_root) == 1
    assert next_sequence("acme", programs_root=programs_root) == 2


def test_current_sequence_without_prior_allocation_is_zero(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert current_sequence("acme", programs_root=programs_root) == 0


def test_current_sequence_reflects_last_allocation_without_advancing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    next_sequence("acme", programs_root=programs_root)
    next_sequence("acme", programs_root=programs_root)
    assert current_sequence("acme", programs_root=programs_root) == 2
    assert current_sequence("acme", programs_root=programs_root) == 2  # idempotent, no advance
    assert next_sequence("acme", programs_root=programs_root) == 3


def test_allocation_survives_reopen(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    next_sequence("acme", programs_root=programs_root)
    next_sequence("acme", programs_root=programs_root)
    # Simulates a process restart: a fresh call still continues from 3.
    assert next_sequence("acme", programs_root=programs_root) == 3
