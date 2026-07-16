"""ADF-W1.9 done-check: QG-37 State Authority.

Ambiguous fact-store path fixture blocks mutation (via
``assert_state_authority_or_raise``); unambiguous fixture allows read-only
evaluation to pass cleanly. Also covers the doctor-fail wiring
(``vertex doctor --storage`` escalates to ``fail`` when ambiguous) and the
gate-registry reservation collision this gate's implementation must not
reintroduce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.commands.doctor_checks.storage_checks import _state_authority_gate_check
from src.core.quality_gates.gate_registry import RESERVED_GATE_IDS, assert_no_reservation_collisions
from src.core.quality_gates.state_authority import (
    GATE_ID,
    StateAuthorityAmbiguousError,
    assert_state_authority_or_raise,
    evaluate_state_authority_gate,
    find_stray_fact_store_databases,
)


def test_qg_37_is_no_longer_reserved_once_implemented() -> None:
    assert "QG-37" not in RESERVED_GATE_IDS
    assert_no_reservation_collisions()  # must not raise


def test_unambiguous_fixture_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    canonical_path = db_root / "xpf" / "vertex.sqlite3"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.touch()

    evaluation = evaluate_state_authority_gate("xpf", programs_root=programs_root, db_root=db_root)
    assert evaluation.passed
    assert evaluation.gate_id == GATE_ID
    assert_state_authority_or_raise("xpf", programs_root=programs_root, db_root=db_root)  # must not raise


def test_ambiguous_fixture_blocks_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    canonical_path = db_root / "xpf" / "vertex.sqlite3"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.touch()

    stray_path = fake_home / ".vertex" / "xpf" / "vertex.sqlite3"
    stray_path.parent.mkdir(parents=True, exist_ok=True)
    stray_path.touch()

    stray = find_stray_fact_store_databases("xpf", programs_root=programs_root, db_root=db_root)
    assert "home_fallback" in stray

    evaluation = evaluate_state_authority_gate("xpf", programs_root=programs_root, db_root=db_root)
    assert not evaluation.passed
    assert evaluation.exit_code == 1
    assert not evaluation.forceable
    assert "home_fallback" in evaluation.message

    with pytest.raises(StateAuthorityAmbiguousError):
        assert_state_authority_or_raise("xpf", programs_root=programs_root, db_root=db_root)


def test_ambiguous_fixture_read_only_evaluation_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading the gate's verdict (evaluate_*) is always safe -- only the
    explicit assert_*_or_raise call blocks. A read-only command (doctor,
    cockpit) must be able to report ambiguity without itself failing."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    stray_path = fake_home / ".vertex" / "xpf" / "vertex.sqlite3"
    stray_path.parent.mkdir(parents=True, exist_ok=True)
    stray_path.touch()

    evaluation = evaluate_state_authority_gate("xpf", programs_root=programs_root, db_root=db_root)
    assert not evaluation.passed  # verdict is correct...
    # ...but evaluating it did not raise, unlike assert_state_authority_or_raise.


def test_doctor_check_fails_when_fact_store_path_is_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Section 12.1 "...and fails doctor" half: run_storage_doctor's
    QG-37 check (distinct from the pre-existing informational "Fact Store
    Location" warn-only check) carries status="fail" -- which DoctorReport's
    `.failures`/`.overall` aggregation already treats as blocking, per its
    own docstring on the "fail"/"error" severity contract."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    canonical_path = db_root / "fixture_prog" / "vertex.sqlite3"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.touch()
    stray_path = fake_home / ".vertex" / "fixture_prog" / "vertex.sqlite3"
    stray_path.parent.mkdir(parents=True, exist_ok=True)
    stray_path.touch()

    check = _state_authority_gate_check("fixture_prog", programs_root=programs_root, db_root=db_root)
    assert check.label == "QG-37 State Authority"
    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["gate_id"] == GATE_ID
    assert check.metadata["passed"] is False


def test_doctor_check_ok_when_fact_store_path_is_unambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    canonical_path = db_root / "fixture_prog" / "vertex.sqlite3"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.touch()

    check = _state_authority_gate_check("fixture_prog", programs_root=programs_root, db_root=db_root)
    assert check.status == "ok"
    assert check.metadata["passed"] is True
