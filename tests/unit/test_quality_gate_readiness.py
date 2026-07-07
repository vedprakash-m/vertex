"""Direct coverage for the extracted launch-readiness gates.

Guards the D-09 / Phase 3 peel of the readiness cluster from the
``src/core/quality_gates`` package into
``src/core/quality_gates/readiness.py`` (re-exported from ``__init__``). The
readiness-engine loaders are monkeypatched in the submodule namespace so the
gate branching can be exercised without on-disk config/snapshots.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.core.quality_gates import evaluate_readiness_gates
from src.core.quality_gates import readiness


def _dimension(gate_id: str, name: str, passed: bool = True, summary: str = "ok"):
    return SimpleNamespace(gate_id=gate_id, name=name, passed=passed, summary=summary)


def _patch_config(monkeypatch, pairs):
    monkeypatch.setattr(
        readiness,
        "load_readiness_config",
        lambda program_id, programs_root: SimpleNamespace(
            dimensions=tuple(_dimension(gid, nm) for gid, nm in pairs)
        ),
    )


def _patch_snapshot(monkeypatch, snapshot, *, stale=False, age=1, warnings=()):
    monkeypatch.setattr(
        readiness,
        "load_readiness_snapshot",
        lambda program_id, programs_root: SimpleNamespace(snapshot=snapshot, warnings=tuple(warnings)),
    )
    monkeypatch.setattr(readiness, "is_snapshot_stale", lambda snap, max_age_days=None: stale)
    monkeypatch.setattr(readiness, "snapshot_age_days", lambda snap: age)


def test_no_program_is_noop() -> None:
    assert evaluate_readiness_gates(program_id=None).results == ()


def test_missing_snapshot_emits_unavailable_per_gate(monkeypatch) -> None:
    _patch_config(monkeypatch, [("RG-1", "Security"), ("RG-2", "Privacy")])
    _patch_snapshot(monkeypatch, snapshot=None, warnings=("snap missing",))
    report = evaluate_readiness_gates(program_id="acme", programs_root=Path("/tmp"))
    assert [r.gate_id for r in report.results] == ["RG-1", "RG-2"]
    assert all(not r.passed and r.exit_code == 1 for r in report.results)
    assert "snap missing" in report.results[0].message


def test_stale_snapshot_blocks_all_gates(monkeypatch) -> None:
    _patch_config(monkeypatch, [("RG-1", "Security")])
    snapshot = SimpleNamespace(dimensions=(_dimension("RG-1", "Security"),), snapshot_max_age_days=7)
    _patch_snapshot(monkeypatch, snapshot=snapshot, stale=True, age=10)
    report = evaluate_readiness_gates(program_id="acme", programs_root=Path("/tmp"))
    gate = report.results[0]
    assert gate.passed is False
    assert "stale (10d old; max 7d)" in gate.message


def test_pass_and_fail_dimensions(monkeypatch) -> None:
    _patch_config(monkeypatch, [("RG-1", "Security"), ("RG-2", "Privacy")])
    snapshot = SimpleNamespace(
        dimensions=(
            _dimension("RG-1", "Security", passed=True, summary="all good"),
            _dimension("RG-2", "Privacy", passed=False, summary="needs DPIA"),
        ),
        snapshot_max_age_days=7,
    )
    _patch_snapshot(monkeypatch, snapshot=snapshot)
    report = evaluate_readiness_gates(program_id="acme", programs_root=Path("/tmp"))
    by_id = {r.gate_id: r for r in report.results}
    assert by_id["RG-1"].passed is True and "passed" in by_id["RG-1"].message
    assert by_id["RG-2"].passed is False and "needs DPIA" in by_id["RG-2"].message


def test_configured_gate_missing_from_snapshot(monkeypatch) -> None:
    _patch_config(monkeypatch, [("RG-9", "NewDim")])
    snapshot = SimpleNamespace(dimensions=(), snapshot_max_age_days=7)
    _patch_snapshot(monkeypatch, snapshot=snapshot)
    report = evaluate_readiness_gates(program_id="acme", programs_root=Path("/tmp"))
    gate = report.results[0]
    assert gate.gate_id == "RG-9" and gate.passed is False
    assert "snapshot does not include gate 'RG-9'" in gate.message


def test_unconfigured_snapshot_dimensions_are_appended(monkeypatch) -> None:
    _patch_config(monkeypatch, [])  # no configured gates
    snapshot = SimpleNamespace(
        dimensions=(_dimension("RG-X", "Extra", passed=True, summary="fine"),),
        snapshot_max_age_days=7,
    )
    _patch_snapshot(monkeypatch, snapshot=snapshot)
    report = evaluate_readiness_gates(program_id="acme", programs_root=Path("/tmp"))
    assert [r.gate_id for r in report.results] == ["RG-X"]
    assert report.results[0].passed is True


def test_config_load_failure_falls_back_to_defaults(monkeypatch) -> None:
    def _raise(program_id, programs_root):
        raise FileNotFoundError("no readiness.yaml")

    monkeypatch.setattr(readiness, "load_readiness_config", _raise)
    monkeypatch.setattr(
        readiness,
        "DEFAULT_READINESS_DIMENSIONS",
        {"sec": ("Security", "RG-1")},
    )
    _patch_snapshot(monkeypatch, snapshot=None)
    report = evaluate_readiness_gates(program_id="acme", programs_root=Path("/tmp"))
    # Default gate pair (RG-1) is used and reported unavailable.
    assert report.results[0].gate_id == "RG-1" and report.results[0].passed is False
