from __future__ import annotations

from datetime import date
from pathlib import Path
import shutil

import pytest

from src.commands.doctor_checks.source_waiver_checks import run_source_waiver_doctor
from src.core.source_waiver_store import (
    SCHEMA_VERSION,
    SourceWaiver,
    SourceWaiverSchema,
    load_source_waivers_schema,
    validate_waiver_against_schema,
)
from src.core.exceptions import ConfigError


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "vertex" / "policies" / "source_waivers.schema.yaml"


def test_schema_file_exists_and_loads() -> None:
    assert SCHEMA_PATH.exists(), (
        "D-32 requires vertex/policies/source_waivers.schema.yaml to be materialized; "
        "this file is the governance contract for programs/<id>/source_waivers.yaml."
    )
    schema = load_source_waivers_schema()
    assert isinstance(schema, SourceWaiverSchema)
    assert schema.schema_id == "vertex.source_waivers"
    assert schema.schema_version == SCHEMA_VERSION
    field_names = {spec.field_name for spec in schema.waiver_fields}
    assert {"contract_id", "role", "owner", "reason", "granted", "expires"} <= field_names
    assert "telemetry" in schema.allowed_roles
    assert "advisory" in schema.allowed_roles
    assert "unbacked" in schema.allowed_roles


def test_schema_file_is_rejected_when_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_source_waivers_schema(policies_root=tmp_path)
    assert "missing" in str(error.value).lower()


def test_validate_waiver_against_schema_accepts_valid_waiver() -> None:
    schema = load_source_waivers_schema()
    waiver = SourceWaiver(
        contract_id="acme.kusto.os_compliance",
        role="telemetry",
        owner="owner@example.com",
        reason="Pending telemetry upgrade window",
        granted=date(2026, 5, 1),
        expires=date(2026, 7, 31),
    )
    errors, warnings = validate_waiver_against_schema(waiver, schema=schema, today=date(2026, 6, 7))
    assert errors == []
    assert warnings == []


def test_validate_waiver_against_schema_rejects_bad_role() -> None:
    schema = load_source_waivers_schema()
    waiver = SourceWaiver(
        contract_id="acme.kusto.os_compliance",
        role="made_up_role",
        owner="owner@example.com",
        reason="Pending telemetry upgrade window",
        granted=date(2026, 5, 1),
        expires=date(2026, 7, 31),
    )
    errors, _warnings = validate_waiver_against_schema(waiver, schema=schema, today=date(2026, 6, 7))
    assert any("role" in err for err in errors)


def test_validate_waiver_against_schema_rejects_non_email_owner() -> None:
    schema = load_source_waivers_schema()
    waiver = SourceWaiver(
        contract_id="acme.kusto.os_compliance",
        role="telemetry",
        owner="not-an-email",
        reason="Pending telemetry upgrade window",
        granted=date(2026, 5, 1),
        expires=date(2026, 7, 31),
    )
    errors, _warnings = validate_waiver_against_schema(waiver, schema=schema, today=date(2026, 6, 7))
    assert any("email" in err.lower() for err in errors)


def test_validate_waiver_against_schema_warns_on_expired_waiver() -> None:
    schema = load_source_waivers_schema()
    waiver = SourceWaiver(
        contract_id="acme.kusto.os_compliance",
        role="telemetry",
        owner="owner@example.com",
        reason="Pending telemetry upgrade window",
        granted=date(2026, 1, 1),
        expires=date(2026, 4, 1),
    )
    errors, warnings = validate_waiver_against_schema(waiver, schema=schema, today=date(2026, 6, 7))
    assert errors == []
    assert any("expired" in warn.lower() for warn in warnings)


def test_validate_waiver_against_schema_rejects_expires_before_granted() -> None:
    schema = load_source_waivers_schema()
    waiver = SourceWaiver(
        contract_id="acme.kusto.os_compliance",
        role="telemetry",
        owner="owner@example.com",
        reason="Pending telemetry upgrade window",
        granted=date(2026, 6, 1),
        expires=date(2026, 5, 1),
    )
    errors, _warnings = validate_waiver_against_schema(waiver, schema=schema, today=date(2026, 6, 7))
    assert any("expires" in err.lower() and "granted" in err.lower() for err in errors)


def test_run_source_waiver_doctor_passes_for_valid_programs(tmp_path: Path) -> None:
    programs_root = _seed_two_programs(tmp_path)
    report = run_source_waiver_doctor(programs_root=programs_root, today=date(2026, 6, 7))
    statuses = [check.status for check in report.checks]
    assert "fail" not in statuses
    summary = report.checks[0]
    assert summary.metadata is not None
    assert summary.metadata["program_count"] == 2
    assert summary.metadata["waiver_count"] == 2
    assert summary.metadata["malformed_count"] == 0


def test_run_source_waiver_doctor_surfaces_expired_waiver_as_warn(tmp_path: Path) -> None:
    programs_root = _seed_two_programs(tmp_path, alpha_waiver_expired=True)
    report = run_source_waiver_doctor(programs_root=programs_root, today=date(2026, 6, 7))
    statuses = [check.status for check in report.checks]
    assert "fail" not in statuses
    assert "warn" in statuses
    summary = report.checks[0]
    assert summary.metadata is not None
    assert summary.metadata["expired_count"] >= 1
    assert summary.metadata["malformed_count"] == 0


def test_run_source_waiver_doctor_fails_on_malformed_waiver(tmp_path: Path) -> None:
    programs_root = _seed_two_programs(tmp_path, alpha_waiver_role="bogus_role")
    report = run_source_waiver_doctor(programs_root=programs_root, today=date(2026, 6, 7))
    statuses = [check.status for check in report.checks]
    assert "fail" in statuses
    summary = report.checks[0]
    assert summary.metadata is not None
    assert summary.metadata["malformed_count"] >= 1


def test_run_source_waiver_doctor_reports_missing_file_as_info(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "empty_prog").mkdir(parents=True)
    (programs_root / "empty_prog" / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    report = run_source_waiver_doctor(programs_root=programs_root, today=date(2026, 6, 7))
    statuses = [check.status for check in report.checks]
    assert "info" in statuses
    assert "fail" not in statuses
    summary = report.checks[0]
    assert summary.metadata is not None
    assert summary.metadata["missing_count"] == 1


def test_run_source_waiver_doctor_handles_missing_programs_root(tmp_path: Path) -> None:
    # When programs_root does not exist, the default enumerator surfaces a
    # FileNotFoundError; run_source_waiver_doctor catches that and returns
    # a fail DoctorReport rather than propagating the error.
    report = run_source_waiver_doctor(
        programs_root=tmp_path / "does-not-exist",
        program_ids=None,
        today=date(2026, 6, 7),
    )
    assert report.checks[0].status == "fail"
    assert "Could not enumerate" in report.checks[0].detail or "Schema" in report.checks[0].detail


def test_run_source_waiver_doctor_explicit_program_ids_bypass_enum(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "alpha").mkdir(parents=True)
    (programs_root / "alpha" / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    (programs_root / "alpha" / "source_waivers.yaml").write_text(
        """
schema_version: '1.0'
waivers:
  - contract_id: alpha.slice
    role: telemetry
    owner: owner@example.com
    reason: Pending telemetry upgrade window
    granted: 2026-05-01
    expires: 2026-07-31
""".strip(),
        encoding="utf-8",
    )
    report = run_source_waiver_doctor(
        programs_root=programs_root,
        program_ids=("alpha",),
        today=date(2026, 6, 7),
    )
    summary = report.checks[0]
    assert summary.metadata is not None
    assert summary.metadata["program_count"] == 1
    assert summary.metadata["waiver_count"] == 1
    assert summary.status == "ok"


def _seed_two_programs(
    tmp_path: Path,
    *,
    alpha_waiver_expired: bool = False,
    alpha_waiver_role: str = "telemetry",
) -> Path:
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    programs_root = tmp_path / "programs"
    (programs_root / "alpha").mkdir(parents=True)
    (programs_root / "alpha" / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    granted = "2026-01-01" if alpha_waiver_expired else "2026-05-01"
    expires = "2026-04-01" if alpha_waiver_expired else "2026-07-31"
    (programs_root / "alpha" / "source_waivers.yaml").write_text(
        f"""
schema_version: '1.0'
waivers:
  - contract_id: alpha.slice
    role: {alpha_waiver_role}
    owner: owner@example.com
    reason: Pending telemetry upgrade window
    granted: {granted}
    expires: {expires}
""".strip(),
        encoding="utf-8",
    )
    (programs_root / "beta").mkdir(parents=True)
    (programs_root / "beta" / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    (programs_root / "beta" / "source_waivers.yaml").write_text(
        """
schema_version: '1.0'
waivers:
  - contract_id: beta.slice
    role: advisory
    owner: another@example.com
    reason: Advisory role pending contract refresh
    granted: 2026-05-15
    expires: 2026-08-15
""".strip(),
        encoding="utf-8",
    )
    return programs_root
