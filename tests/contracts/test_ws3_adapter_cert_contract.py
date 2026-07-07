"""Contract tests for WS-3 adapter-cert doctor check.

Verifies:
  A-1  adapter_cert_checks.py exists with run_adapter_cert_doctor().
  A-2  run_adapter_cert_doctor returns DoctorReport for an empty program.
  A-3  Disabled channels produce INFO checks (not warn/fail).
  A-4  Enabled uncertified channel produces WARN check.
  A-5  Enabled certified channel produces OK check.
  A-6  WorkIQ probe: available → ok; unavailable → warn.
  A-7  Cert file is written (or updated) on run (write_cert=True).
  A-8  doctor.py dispatches --adapter-cert to _run_adapter_cert_doctor.
  A-9  Credential expiry approaching (≤14 days) → warn check emitted.
  A-10 Credential already expired → fail check emitted.
  A-11 Credential far in the future (>14 days) → no expiry check emitted.
"""

from __future__ import annotations

import importlib
import inspect
from datetime import date, timedelta
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ADAPTER_CERT_MODULE = "src.commands.doctor_checks.adapter_cert_checks"


def _make_programs_root(tmp_path: Path, program_id: str = "testprog") -> Path:
    programs_root = tmp_path / "programs"
    (programs_root / program_id).mkdir(parents=True)
    return programs_root


# ---------------------------------------------------------------------------
# A-1  Module + function exist
# ---------------------------------------------------------------------------


def test_a1_adapter_cert_checks_module_has_run_function() -> None:
    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    assert hasattr(mod, "run_adapter_cert_doctor"), (
        "run_adapter_cert_doctor must be defined in adapter_cert_checks.py"
    )
    assert callable(mod.run_adapter_cert_doctor)


# ---------------------------------------------------------------------------
# A-2  Returns DoctorReport for an empty program (all channels disabled)
# ---------------------------------------------------------------------------


def test_a2_returns_doctor_report_for_empty_program(tmp_path: Path) -> None:
    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    programs_root = _make_programs_root(tmp_path)

    report = mod.run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: False, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: False,
        write_cert=False,
    )

    from src.commands.doctor_checks.models import DoctorReport
    assert isinstance(report, DoctorReport)
    assert report.edition == "test_weekly"
    assert len(report.checks) > 0


# ---------------------------------------------------------------------------
# A-3  Disabled channels → INFO
# ---------------------------------------------------------------------------


def test_a3_disabled_channels_produce_info(tmp_path: Path) -> None:
    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    programs_root = _make_programs_root(tmp_path)

    report = mod.run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: False, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: True,
        write_cert=False,
    )

    channel_checks = [c for c in report.checks if c.label.startswith("Adapter Cert:")]
    assert len(channel_checks) == 4
    for check in channel_checks:
        assert check.status == "info", f"Expected info for disabled channel, got {check.status}: {check.label}"


# ---------------------------------------------------------------------------
# A-4  Enabled uncertified channel → WARN
# ---------------------------------------------------------------------------


def test_a4_enabled_uncertified_channel_warns(tmp_path: Path) -> None:
    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    programs_root = _make_programs_root(tmp_path)

    report = mod.run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: True, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: True,
        write_cert=False,
    )

    ado_check = next(c for c in report.checks if "ADO" in c.label)
    assert ado_check.status == "warn", f"Expected warn for uncertified enabled ADO, got: {ado_check.status}"


# ---------------------------------------------------------------------------
# A-5  Enabled certified channel → OK
# ---------------------------------------------------------------------------


def test_a5_enabled_certified_channel_ok(tmp_path: Path) -> None:
    import yaml

    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    programs_root = _make_programs_root(tmp_path)

    # Pre-populate adapter_cert.yaml with certified ADO.
    cert_data = {"channels": {"ado": {"status": "certified", "version": "1.0"}}}
    cert_path = programs_root / "testprog" / "adapter_cert.yaml"
    cert_path.write_text(yaml.dump(cert_data), encoding="utf-8")

    report = mod.run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: True, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: True,
        write_cert=False,
    )

    ado_check = next(c for c in report.checks if "ADO" in c.label)
    assert ado_check.status == "ok", f"Expected ok for certified ADO, got: {ado_check.status}"


# ---------------------------------------------------------------------------
# A-6  WorkIQ probe: available → ok; unavailable → warn
# ---------------------------------------------------------------------------


def test_a6_workiq_probe_available_ok(tmp_path: Path) -> None:
    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    programs_root = _make_programs_root(tmp_path)

    report = mod.run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={k: lambda: False for k in ("ado", "kusto", "teams", "icm")},
        workiq_probe_fn=lambda: True,
        write_cert=False,
    )

    probe_check = next(c for c in report.checks if c.label == "WorkIQ Probe")
    assert probe_check.status == "ok"
    assert probe_check.metadata is not None
    assert probe_check.metadata["available"] is True


def test_a6_workiq_probe_unavailable_warn(tmp_path: Path) -> None:
    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    programs_root = _make_programs_root(tmp_path)

    report = mod.run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={k: lambda: False for k in ("ado", "kusto", "teams", "icm")},
        workiq_probe_fn=lambda: False,
        write_cert=False,
    )

    probe_check = next(c for c in report.checks if c.label == "WorkIQ Probe")
    assert probe_check.status == "warn"
    assert probe_check.metadata is not None
    assert probe_check.metadata["available"] is False


# ---------------------------------------------------------------------------
# A-7  write_cert=True creates adapter_cert.yaml
# ---------------------------------------------------------------------------


def test_a7_cert_file_written_on_run(tmp_path: Path) -> None:
    mod = importlib.import_module(ADAPTER_CERT_MODULE)
    programs_root = _make_programs_root(tmp_path)
    cert_path = programs_root / "testprog" / "adapter_cert.yaml"

    assert not cert_path.exists()

    mod.run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: True, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: False,
        write_cert=True,
    )

    assert cert_path.exists(), "adapter_cert.yaml must be created when write_cert=True"


# ---------------------------------------------------------------------------
# A-8  doctor.py dispatches adapter_cert to _run_adapter_cert_doctor
# ---------------------------------------------------------------------------


def test_a8_doctor_dispatches_adapter_cert() -> None:
    import ast

    doctor_path = Path("src/commands/doctor.py")
    tree = ast.parse(doctor_path.read_text(encoding="utf-8"))

    # Find run_doctor function.
    run_doctor_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_doctor"
    )

    # Find adapter_cert branch.
    adapter_cert_branches = [
        stmt for stmt in run_doctor_node.body
        if isinstance(stmt, ast.If)
        and isinstance(stmt.test, ast.Name)
        and stmt.test.id == "adapter_cert"
    ]
    assert len(adapter_cert_branches) == 1, "Expected exactly one 'if adapter_cert:' branch in run_doctor"

    # The branch must return a call to _run_adapter_cert_doctor.
    branch = adapter_cert_branches[0]
    return_stmts = [s for s in ast.walk(branch) if isinstance(s, ast.Return)]
    assert any(
        isinstance(s.value, ast.Call)
        and isinstance(s.value.func, ast.Name)
        and s.value.func.id == "_run_adapter_cert_doctor"
        for s in return_stmts
    ), "adapter_cert branch must return _run_adapter_cert_doctor(...)"


# ---------------------------------------------------------------------------
# A-9  Credential expiry approaching (≤14 days) → warn check emitted
# ---------------------------------------------------------------------------


def test_a9_credential_expiry_approaching_warns(tmp_path: Path) -> None:
    from src.commands.doctor_checks.adapter_cert_checks import run_adapter_cert_doctor

    programs_root = _make_programs_root(tmp_path)
    # Seed adapter_cert.yaml with a credential expiring in 7 days.
    import yaml
    near_expiry = (date.today() + timedelta(days=7)).isoformat()
    cert_data = {
        "channels": {
            "ado": {"status": "certified", "version": "1.0", "credential_expiry": near_expiry}
        }
    }
    cert_path = tmp_path / "programs" / "testprog" / "adapter_cert.yaml"
    cert_path.write_text(yaml.dump(cert_data), encoding="utf-8")

    report = run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: True, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: False,
        write_cert=False,
    )

    expiry_checks = [c for c in report.checks if "Credential Expiry" in c.label]
    assert expiry_checks, "Expected at least one Credential Expiry check when expiry is approaching"
    expiry_check = expiry_checks[0]
    assert expiry_check.status == "warn", f"Expected 'warn' for approaching expiry, got {expiry_check.status!r}"
    assert "7" in expiry_check.detail or near_expiry in expiry_check.detail, (
        "Detail should mention days remaining or expiry date"
    )


# ---------------------------------------------------------------------------
# A-10 Credential already expired → fail check emitted
# ---------------------------------------------------------------------------


def test_a10_credential_already_expired_fails(tmp_path: Path) -> None:
    from src.commands.doctor_checks.adapter_cert_checks import run_adapter_cert_doctor

    programs_root = _make_programs_root(tmp_path)
    import yaml
    past_expiry = (date.today() - timedelta(days=3)).isoformat()
    cert_data = {
        "channels": {
            "ado": {"status": "certified", "version": "1.0", "credential_expiry": past_expiry}
        }
    }
    cert_path = tmp_path / "programs" / "testprog" / "adapter_cert.yaml"
    cert_path.write_text(yaml.dump(cert_data), encoding="utf-8")

    report = run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: True, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: False,
        write_cert=False,
    )

    expiry_checks = [c for c in report.checks if "Credential Expiry" in c.label]
    assert expiry_checks, "Expected a Credential Expiry check when credential is expired"
    expiry_check = expiry_checks[0]
    assert expiry_check.status == "fail", f"Expected 'fail' for expired credential, got {expiry_check.status!r}"
    assert past_expiry in expiry_check.detail or "expired" in expiry_check.detail.lower()


# ---------------------------------------------------------------------------
# A-11 Credential far in the future (>14 days) → no expiry check emitted
# ---------------------------------------------------------------------------


def test_a11_credential_far_future_no_expiry_check(tmp_path: Path) -> None:
    from src.commands.doctor_checks.adapter_cert_checks import run_adapter_cert_doctor

    programs_root = _make_programs_root(tmp_path)
    import yaml
    far_expiry = (date.today() + timedelta(days=90)).isoformat()
    cert_data = {
        "channels": {
            "ado": {"status": "certified", "version": "1.0", "credential_expiry": far_expiry}
        }
    }
    cert_path = tmp_path / "programs" / "testprog" / "adapter_cert.yaml"
    cert_path.write_text(yaml.dump(cert_data), encoding="utf-8")

    report = run_adapter_cert_doctor(
        edition_name="test_weekly",
        program_id="testprog",
        programs_root=programs_root,
        channel_enabled_fns={"ado": lambda: True, "kusto": lambda: False, "teams": lambda: False, "icm": lambda: False},
        workiq_probe_fn=lambda: False,
        write_cert=False,
    )

    expiry_checks = [c for c in report.checks if "Credential Expiry" in c.label]
    assert not expiry_checks, (
        f"No Credential Expiry check expected when expiry is 90 days away, got: {expiry_checks}"
    )
