from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.readiness_engine import is_snapshot_stale, load_readiness_config, load_readiness_snapshot, snapshot_age_days


def readiness_gate_settings(raw_program: dict[str, Any] | None) -> tuple[bool, int]:
    if not isinstance(raw_program, dict):
        return False, 7
    readiness_config = raw_program.get("readiness")
    if not isinstance(readiness_config, dict):
        return False, 7
    gate_enabled = bool(readiness_config.get("gate"))
    raw_max_age_days = readiness_config.get("snapshot_max_age_days")
    if isinstance(raw_max_age_days, int) and raw_max_age_days > 0:
        return gate_enabled, raw_max_age_days
    return gate_enabled, 7


def run_readiness_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    readiness_gate_settings_fn: Callable[[dict[str, Any] | None], tuple[bool, int]],
) -> DoctorReport:
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("Readiness", "fail", f"Edition '{edition_name}' could not be resolved."),))

    program_id = resolved.program.id
    gate_enabled, gate_max_age_days = readiness_gate_settings_fn(resolved.raw_program)
    try:
        config = load_readiness_config(program_id, programs_root=programs_root)
    except FileNotFoundError:
        if gate_enabled:
            return DoctorReport(
                edition=edition_name,
                checks=(
                    DoctorCheck(
                        "Readiness Config",
                        "fail",
                        f"programs/{program_id}/readiness.yaml is missing while program.yaml enables readiness.gate.",
                        metadata={"gate_enabled": True, "program_id": program_id},
                    ),
                ),
            )
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Readiness Config",
                    "ok",
                    "Program readiness gate is disabled and no readiness model is configured.",
                    metadata={"gate_enabled": False, "program_id": program_id},
                ),
            ),
        )
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Readiness Config",
                    "fail",
                    str(error),
                    metadata={"gate_enabled": gate_enabled, "program_id": program_id},
                ),
            ),
        )

    effective_max_age_days = gate_max_age_days if gate_enabled else config.snapshot_max_age_days
    config_check = DoctorCheck(
        "Readiness Config",
        "ok",
        f"{len(config.dimensions)} dimension(s) configured; confirm gate {'enabled' if gate_enabled else 'disabled'}; snapshot max age {effective_max_age_days} day(s).",
        metadata={
            "dimension_count": len(config.dimensions),
            "gate_enabled": gate_enabled,
            "program_id": program_id,
            "snapshot_max_age_days": effective_max_age_days,
        },
    )

    snapshot_result = load_readiness_snapshot(program_id, programs_root=programs_root)
    if snapshot_result.snapshot is None:
        detail = snapshot_result.warnings[0] if snapshot_result.warnings else f"Readiness snapshot is missing for program '{program_id}'."
        return DoctorReport(
            edition=edition_name,
            checks=(
                config_check,
                DoctorCheck(
                    "Readiness Snapshot",
                    "warn",
                    detail,
                    metadata={"gate_enabled": gate_enabled, "program_id": program_id, "snapshot_max_age_days": effective_max_age_days},
                ),
            ),
        )

    snapshot = snapshot_result.snapshot
    age_days = snapshot_age_days(snapshot)
    status = "ok"
    detail = (
        f"Readiness snapshot fetched {snapshot.fetched_at.date().isoformat()} is within the {effective_max_age_days}-day threshold "
        f"({snapshot.passed_count}/{snapshot.total_count} dimensions green)."
    )
    if is_snapshot_stale(snapshot, max_age_days=effective_max_age_days):
        status = "warn"
        detail = (
            f"Readiness snapshot is {age_days} day(s) old (fetched {snapshot.fetched_at.date().isoformat()}); "
            f"threshold is {effective_max_age_days} day(s)."
        )

    return DoctorReport(
        edition=edition_name,
        checks=(
            config_check,
            DoctorCheck(
                "Readiness Snapshot",
                status,
                detail,
                metadata={
                    "age_days": age_days,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "passed_count": snapshot.passed_count,
                    "program_id": program_id,
                    "snapshot_max_age_days": effective_max_age_days,
                    "total_count": snapshot.total_count,
                },
            ),
        ),
    )
