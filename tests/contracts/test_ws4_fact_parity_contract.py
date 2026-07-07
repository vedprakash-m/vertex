"""WS-4 contract tests: doctor --fact-parity check.

Verifies that ``run_fact_parity_doctor`` correctly:
- Returns WARN when no parity log exists
- Returns WARN when fewer than required cycles have passed
- Returns OK when enough cycles have passed
- Reads ``fact_store.dual_read_cycles`` from ``platform_state.yaml``
- Falls back to 5 when the key is absent
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.commands.doctor_checks.fact_store_flip_checks import run_fact_parity_doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_parity_log(program_dir: Path, records: list[dict]) -> None:
    log_path = program_dir / "fact_store_parity_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _make_record(*, passed: bool, cycle_index: int = 1) -> dict:
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "program_id": "test_prog",
        "cycle_index": cycle_index,
        "matched_count": 10 if passed else 8,
        "total_count": 10,
        "parity_ratio": 1.0 if passed else 0.8,
        "passed": passed,
        "zero_tolerance_failures": [],
        "mismatched_families": [] if passed else ["claims"],
        "family_results": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fact_parity_warns_when_no_log(tmp_path: Path) -> None:
    """WARN when no parity log exists at all."""
    programs_root = tmp_path / "programs"
    (programs_root / "myprog").mkdir(parents=True)
    report = run_fact_parity_doctor(
        edition_name="test_edition",
        program_id="myprog",
        programs_root=programs_root,
    )
    assert report.failures == 0
    assert report.warnings == 1
    check = report.checks[0]
    assert check.label == "Fact Parity"
    assert check.status == "warn"
    assert "dual-read-log" in check.detail


def test_fact_parity_warns_when_insufficient_cycles(tmp_path: Path) -> None:
    """WARN when fewer than required cycles have passed."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    prog_dir.mkdir(parents=True)
    # Write only 2 passed cycles, default required is 5.
    _write_parity_log(prog_dir, [_make_record(passed=True), _make_record(passed=True)])
    report = run_fact_parity_doctor(
        edition_name="test_edition",
        program_id="myprog",
        programs_root=programs_root,
    )
    assert report.warnings == 1
    check = report.checks[0]
    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["passed_cycles"] == 2
    assert check.metadata["required_cycles"] == 5


def test_fact_parity_ok_when_enough_cycles(tmp_path: Path) -> None:
    """OK when at least 5 passed cycles exist."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    prog_dir.mkdir(parents=True)
    _write_parity_log(prog_dir, [_make_record(passed=True, cycle_index=i) for i in range(1, 6)])
    report = run_fact_parity_doctor(
        edition_name="test_edition",
        program_id="myprog",
        programs_root=programs_root,
    )
    assert report.warnings == 0
    assert report.failures == 0
    check = report.checks[0]
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["passed_cycles"] == 5


def test_fact_parity_respects_platform_state_cycles(tmp_path: Path) -> None:
    """Reads fact_store.dual_read_cycles from platform_state.yaml."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    prog_dir.mkdir(parents=True)
    # Write platform_state.yaml with dual_read_cycles = 2
    (programs_root / "platform_state.yaml").write_text(
        "schema_version: '1.0'\nposition: complete\nrecorded_at: 2026-01-01T00:00:00Z\n"
        "fact_store:\n  dual_read_cycles: 2\n",
        encoding="utf-8",
    )
    # Write 2 passed cycles — should be OK with required=2
    _write_parity_log(prog_dir, [_make_record(passed=True, cycle_index=i) for i in range(1, 3)])
    report = run_fact_parity_doctor(
        edition_name="test_edition",
        program_id="myprog",
        programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["required_cycles"] == 2


def test_fact_parity_defaults_5_when_key_absent(tmp_path: Path) -> None:
    """Falls back to 5 when fact_store block is absent in platform_state.yaml."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    prog_dir.mkdir(parents=True)
    # platform_state.yaml without fact_store key
    (programs_root / "platform_state.yaml").write_text(
        "schema_version: '1.0'\nposition: complete\nrecorded_at: 2026-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    # Only 3 cycles — WARN expected since default is 5
    _write_parity_log(prog_dir, [_make_record(passed=True, cycle_index=i) for i in range(1, 4)])
    report = run_fact_parity_doctor(
        edition_name="test_edition",
        program_id="myprog",
        programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["required_cycles"] == 5


def test_fact_parity_failed_cycles_do_not_count(tmp_path: Path) -> None:
    """Failed parity cycles do not count toward the required total."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    prog_dir.mkdir(parents=True)
    # 4 passed + 3 failed = 7 total, but only 4 passed
    records = [_make_record(passed=True, cycle_index=i) for i in range(1, 5)]
    records += [_make_record(passed=False, cycle_index=i) for i in range(5, 8)]
    _write_parity_log(prog_dir, records)
    report = run_fact_parity_doctor(
        edition_name="test_edition",
        program_id="myprog",
        programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.status == "warn"  # 4 < 5 required
    assert check.metadata is not None
    assert check.metadata["passed_cycles"] == 4
    assert check.metadata["total_cycles"] == 7


def test_fact_parity_metadata_has_log_path(tmp_path: Path) -> None:
    """metadata['log_path'] is always present."""
    programs_root = tmp_path / "programs"
    (programs_root / "myprog").mkdir(parents=True)
    report = run_fact_parity_doctor(
        edition_name="test_edition",
        program_id="myprog",
        programs_root=programs_root,
    )
    check = report.checks[0]
    assert check.metadata is not None
    assert "log_path" in check.metadata


def test_fact_parity_run_doctor_has_parameter() -> None:
    """run_doctor must accept fact_parity as a keyword argument."""
    import inspect
    from src.commands.doctor import run_doctor

    sig = inspect.signature(run_doctor)
    assert "fact_parity" in sig.parameters, (
        "run_doctor must have a fact_parity parameter (--fact-parity dispatch)"
    )
    assert sig.parameters["fact_parity"].default is False
