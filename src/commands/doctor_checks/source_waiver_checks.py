"""Source-waiver governance sub-check (D-32 / spec §SourceWaivers).

Validates ``programs/<program_id>/source_waivers.yaml`` against the
canonical schema at ``vertex/policies/source_waivers.schema.yaml``.
Surfaces:

  * missing file   -> INFO (waivers are optional, gate runs strict)
  * expired waiver -> WARN (still treated as active until removed)
  * malformed row  -> FAIL (program-level contract violation)

Registered in ``src/commands/doctor.py`` via the new
``--source-waivers`` flag.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.exceptions import ConfigError
from src.core.source_waiver_store import (
    SourceWaiverSchema,
    load_source_waivers,
    load_source_waivers_schema,
    validate_waiver_against_schema,
)


def run_source_waiver_doctor(
    *,
    programs_root: Path,
    policies_root: Path | None = None,
    program_ids: tuple[str, ...] | None = None,
    today: date | None = None,
    load_source_waivers_fn: Callable[..., Any] | None = None,
    load_source_waivers_schema_fn: Callable[..., SourceWaiverSchema] | None = None,
    enumerate_program_ids_fn: Callable[[Path], tuple[str, ...]] | None = None,
) -> DoctorReport:
    """Return a DoctorReport that audits all program source_waivers.yaml files.

    Parameters
    ----------
    programs_root
        The ``programs/`` directory to scan.
    policies_root
        Optional override for the policies root (defaults to
        ``<repo>/vertex/policies``).
    program_ids
        Optional explicit list of program_ids to audit. When ``None``,
        ``enumerate_program_ids_fn`` is used.
    today
        Override for the "is this waiver expired?" comparison. Defaults
        to ``date.today()``.
    load_source_waivers_fn
        Dependency seam for tests; defaults to the runtime loader.
    load_source_waivers_schema_fn
        Dependency seam for tests; defaults to the runtime loader.
    enumerate_program_ids_fn
        Dependency seam for tests; defaults to scanning ``programs_root``
        for sub-directories that contain a ``program.yaml``.
    """

    resolved_today = today if today is not None else date.today()
    schema_loader = load_source_waivers_schema_fn or load_source_waivers_schema
    waiver_loader = load_source_waivers_fn or load_source_waivers
    enum_programs = enumerate_program_ids_fn or _default_enumerate_program_ids

    try:
        schema = schema_loader(policies_root=policies_root)
    except ConfigError as error:
        return DoctorReport(
            edition="fleet",
            checks=(DoctorCheck("Source Waivers", "fail", f"Schema materialization failed: {error}"),),
        )

    if program_ids is None:
        try:
            program_ids = enum_programs(programs_root)
        except (FileNotFoundError, OSError) as error:
            return DoctorReport(
                edition="fleet",
                checks=(
                    DoctorCheck(
                        "Source Waivers",
                        "fail",
                        f"Could not enumerate programs under {programs_root}: {error}",
                    ),
                ),
            )

    if not program_ids:
        return DoctorReport(
            edition="fleet",
            checks=(
                DoctorCheck(
                    "Source Waivers",
                    "info",
                    f"No program directories discovered under {programs_root}; nothing to audit.",
                    metadata={"program_count": 0},
                ),
            ),
        )

    checks: list[DoctorCheck] = []
    summary: dict[str, Any] = {
        "program_count": len(program_ids),
        "waiver_count": 0,
        "expired_count": 0,
        "missing_count": 0,
        "malformed_count": 0,
        "programs": [],
    }
    overall_status = "ok"
    for program_id in program_ids:
        waiver_path = programs_root / program_id / "source_waivers.yaml"
        program_entry: dict[str, Any] = {
            "program_id": program_id,
            "waiver_path": str(waiver_path),
            "status": "ok",
            "errors": [],
            "warnings": [],
            "waivers": [],
        }
        if not waiver_path.exists():
            program_entry["status"] = "info"
            summary["missing_count"] += 1
            checks.append(
                DoctorCheck(
                    "Source Waivers",
                    "info",
                    f"program '{program_id}': no source_waivers.yaml (optional, gate runs strict).",
                )
            )
            summary["programs"].append(program_entry)
            continue

        try:
            waivers = waiver_loader(program_id, programs_root=programs_root)
        except ConfigError as error:
            program_entry["status"] = "fail"
            program_entry["errors"].append(str(error))
            summary["malformed_count"] += 1
            overall_status = _worst_status(overall_status, "fail")
            checks.append(
                DoctorCheck(
                    "Source Waivers",
                    "fail",
                    f"program '{program_id}': {error}",
                )
            )
            summary["programs"].append(program_entry)
            continue

        summary["waiver_count"] += len(waivers)
        program_expired = 0
        program_errors: list[str] = []
        program_warnings: list[str] = []
        for waiver in waivers:
            errors, warnings = validate_waiver_against_schema(
                waiver,
                schema=schema,
                today=resolved_today,
            )
            program_entry["waivers"].append(
                {
                    "contract_id": waiver.contract_id,
                    "role": waiver.role,
                    "owner": waiver.owner,
                    "granted": waiver.granted.isoformat(),
                    "expires": waiver.expires.isoformat(),
                    "errors": list(errors),
                    "warnings": list(warnings),
                }
            )
            if errors:
                program_errors.append(
                    f"{waiver.contract_id}:{waiver.role}: " + "; ".join(errors)
                )
            if warnings:
                program_warnings.append(
                    f"{waiver.contract_id}:{waiver.role}: " + "; ".join(warnings)
                )
                program_expired += len(warnings)
        summary["expired_count"] += program_expired
        if program_errors:
            program_entry["status"] = "fail"
            program_entry["errors"].extend(program_errors)
            summary["malformed_count"] += 1
            overall_status = _worst_status(overall_status, "fail")
            checks.append(
                DoctorCheck(
                    "Source Waivers",
                    "fail",
                    f"program '{program_id}': " + " | ".join(program_errors[:3])
                    + ("; +more" if len(program_errors) > 3 else ""),
                )
            )
        elif program_warnings:
            program_entry["status"] = "warn"
            program_entry["warnings"].extend(program_warnings)
            overall_status = _worst_status(overall_status, "warn")
            checks.append(
                DoctorCheck(
                    "Source Waivers",
                    "warn",
                    f"program '{program_id}': " + " | ".join(program_warnings[:3])
                    + ("; +more" if len(program_warnings) > 3 else ""),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "Source Waivers",
                    "ok",
                    f"program '{program_id}': {len(waivers)} waiver(s) valid against schema "
                    f"({schema.schema_id} v{schema.schema_version}).",
                )
            )
        summary["programs"].append(program_entry)

    detail = (
        f"Audited {summary['program_count']} program(s) against {schema.schema_id} v{schema.schema_version}: "
        f"{summary['waiver_count']} waiver(s), {summary['expired_count']} expired, "
        f"{summary['missing_count']} missing file(s), {summary['malformed_count']} malformed."
    )
    summary_check = DoctorCheck(
        "Source Waivers",
        overall_status,
        detail,
        metadata=summary,
    )
    return DoctorReport(edition="fleet", checks=(summary_check, *tuple(checks)))


def _default_enumerate_program_ids(programs_root: Path) -> tuple[str, ...]:
    if not programs_root.exists():
        raise FileNotFoundError(programs_root)
    return tuple(
        sorted(
            child.name
            for child in programs_root.iterdir()
            if child.is_dir() and (child / "program.yaml").exists()
        )
    )


def _worst_status(current: str, candidate: str) -> str:
    ordering = {"ok": 0, "info": 1, "warn": 2, "fail": 3}
    return candidate if ordering.get(candidate, 0) > ordering.get(current, 0) else current
