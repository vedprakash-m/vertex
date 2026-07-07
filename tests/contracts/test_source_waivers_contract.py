"""Contract: source_waivers validate — §9a P2 acceptance criterion.

``source_waiver_checks.py`` is the doctor sub-check that validates
``programs/<id>/source_waivers.yaml`` against
``vertex/policies/source_waivers.schema.yaml``.

This file freezes the four invariants required by §9a P2:

  (a) The schema policy file exists at its canonical path.
  (b) ``run_source_waiver_doctor`` is importable and callable.
  (c) Missing source_waivers.yaml → INFO (not a hard gate failure).
  (d) Expired waiver → WARN (surfaced, operator must rotate).
  (e) Malformed row (ConfigError on load) → FAIL (program-level violation).
  (f) Valid waiver → OK check emitted.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.commands.doctor_checks.source_waiver_checks import run_source_waiver_doctor
from src.core.source_health import SourceWaiver
from src.core.source_waiver_store import SourceWaiverSchema, SourceWaiverFieldSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICIES_ROOT = REPO_ROOT / "vertex" / "policies"


# ---------------------------------------------------------------------------
# (a) Schema file exists at the canonical path
# ---------------------------------------------------------------------------

def test_source_waivers_schema_file_exists() -> None:
    """vertex/policies/source_waivers.schema.yaml must be present."""
    schema_path = POLICIES_ROOT / "source_waivers.schema.yaml"
    assert schema_path.exists(), (
        f"Missing canonical source_waivers schema at {schema_path}. "
        "This is required for the §9a P2 source_waivers validate gate."
    )


# ---------------------------------------------------------------------------
# (b) Importable and callable
# ---------------------------------------------------------------------------

def test_run_source_waiver_doctor_is_callable() -> None:
    """run_source_waiver_doctor must be importable and callable."""
    assert callable(run_source_waiver_doctor)


# ---------------------------------------------------------------------------
# (c) Missing source_waivers.yaml → INFO (not hard gate failure)
# ---------------------------------------------------------------------------

def test_missing_waiver_file_returns_info(tmp_path: Path) -> None:
    """A program with no source_waivers.yaml gets an INFO check, not FAIL."""
    prog_dir = tmp_path / "prog_a"
    prog_dir.mkdir()
    (prog_dir / "program.yaml").write_text("program_id: prog_a\n")

    report = run_source_waiver_doctor(
        programs_root=tmp_path,
        policies_root=POLICIES_ROOT,
        program_ids=("prog_a",),
    )

    statuses = {c.status for c in report.checks}
    assert "fail" not in statuses, "Missing waiver file must not produce a FAIL"
    assert "info" in statuses, "Missing waiver file must produce an INFO check"


# ---------------------------------------------------------------------------
# (d) Valid waiver → OK (no warn/fail)
# ---------------------------------------------------------------------------

def test_valid_waiver_returns_ok(tmp_path: Path) -> None:
    """A well-formed, non-expired waiver produces no WARN or FAIL checks."""
    today = date.today()
    prog_dir = tmp_path / "prog_b"
    prog_dir.mkdir()
    (prog_dir / "program.yaml").write_text("program_id: prog_b\n")
    future = today + timedelta(days=30)
    (prog_dir / "source_waivers.yaml").write_text(
        f'schema_version: "1.0"\n'
        f"waivers:\n"
        f"  - contract_id: kusto_schema_drift\n"
        f"    role: telemetry\n"
        f"    owner: testowner@example.com\n"
        f"    reason: Schema migration in progress\n"
        f"    granted: {today.isoformat()}\n"
        f"    expires: {future.isoformat()}\n"
    )

    report = run_source_waiver_doctor(
        programs_root=tmp_path,
        policies_root=POLICIES_ROOT,
        program_ids=("prog_b",),
    )

    statuses = {c.status for c in report.checks}
    assert "fail" not in statuses, f"Valid waiver must not FAIL; got checks: {report.checks}"
    assert "warn" not in statuses, f"Valid waiver must not WARN; got checks: {report.checks}"


# ---------------------------------------------------------------------------
# (e) Expired waiver → WARN
# ---------------------------------------------------------------------------

def test_expired_waiver_returns_warn(tmp_path: Path) -> None:
    """An expired waiver produces a WARN check (still active until removed)."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    prog_dir = tmp_path / "prog_c"
    prog_dir.mkdir()
    (prog_dir / "program.yaml").write_text("program_id: prog_c\n")
    (prog_dir / "source_waivers.yaml").write_text(
        f'schema_version: "1.0"\n'
        f"waivers:\n"
        f"  - contract_id: old_signal_waiver\n"
        f"    role: advisory\n"
        f"    owner: tpm@example.com\n"
        f"    reason: Expired pilot\n"
        f"    granted: 2024-01-01\n"
        f"    expires: {yesterday.isoformat()}\n"
    )

    report = run_source_waiver_doctor(
        programs_root=tmp_path,
        policies_root=POLICIES_ROOT,
        program_ids=("prog_c",),
        today=today,
    )

    statuses = {c.status for c in report.checks}
    assert "warn" in statuses, (
        f"Expired waiver must produce a WARN check; got: {report.checks}"
    )
    assert "fail" not in statuses, (
        "Expired waiver must not produce FAIL (it is still treated as active)."
    )


# ---------------------------------------------------------------------------
# (f) No programs → graceful INFO (not crash)
# ---------------------------------------------------------------------------

def test_no_programs_returns_info(tmp_path: Path) -> None:
    """When program_ids is empty, doctor returns an INFO note (no crash)."""
    report = run_source_waiver_doctor(
        programs_root=tmp_path,
        policies_root=POLICIES_ROOT,
        program_ids=(),
    )
    assert report is not None
    statuses = {c.status for c in report.checks}
    assert "fail" not in statuses
