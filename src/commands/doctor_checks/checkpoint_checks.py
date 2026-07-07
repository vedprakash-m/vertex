from __future__ import annotations

from pathlib import Path
import re

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.checkpoint_store import CHECKPOINT_DIR_PATHS, CHECKPOINT_FILE_PATHS, checkpoint_missing_relpaths, list_checkpoints
from src.core.edition_resolver import resolve_edition


def run_checkpoint_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    archive_root: Path,
) -> DoctorReport:
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("Checkpoint Inventory", "fail", f"Edition '{edition_name}' could not be resolved."),))

    program_id = resolved.program.id
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    latest_confirmed = find_latest_confirmed_entry(archive_index)
    checkpoints = list_checkpoints(program_id, programs_root=programs_root)

    if not checkpoints:
        status = "ok" if latest_confirmed is None else "warn"
        detail = (
            f"No confirmed issues exist for program '{program_id}' yet, so no checkpoints are expected."
            if latest_confirmed is None
            else (
                f"No checkpoints found for program '{program_id}'. `vertex confirm` creates them automatically before archiving; "
                "run a non-dry-run confirm before attempting `vertex rollback`."
            )
        )
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Checkpoint Inventory",
                    status,
                    detail,
                    metadata={
                        "checkpoint_count": 0,
                        "latest_confirmed_issue": latest_confirmed.issue_number if latest_confirmed is not None else None,
                        "program_id": program_id,
                    },
                ),
            ),
        )

    latest_checkpoint = checkpoints[0]
    latest_checkpoint_issue = checkpoint_issue_number(latest_checkpoint)
    inventory_status = "ok"
    inventory_parts = [f"{len(checkpoints)} checkpoint(s) found; newest is {latest_checkpoint.name}."]
    if latest_confirmed is not None:
        inventory_parts.append(f"Latest confirmed archive issue is {latest_confirmed.issue_number:03d}.")
        if latest_checkpoint_issue is None:
            inventory_status = "warn"
            inventory_parts.append("Newest checkpoint name does not encode an issue number.")
        elif latest_checkpoint_issue < latest_confirmed.issue_number:
            inventory_status = "warn"
            inventory_parts.append(
                f"Newest checkpoint is tagged issue {latest_checkpoint_issue:03d}, which predates the latest confirmed archive issue."
            )

    live_mutable_paths = checkpoint_live_relpaths(program_id, programs_root=programs_root)
    missing_relpaths = checkpoint_missing_relpaths(program_id, latest_checkpoint, programs_root=programs_root)
    if not live_mutable_paths:
        coverage_check = DoctorCheck(
            "Checkpoint Coverage",
            "warn",
            f"{latest_checkpoint.name} exists, but none of the mutable checkpointed stores currently exist under programs/{program_id}/.",
            metadata={"checkpoint_name": latest_checkpoint.name, "live_mutable_paths": [], "missing_relpaths": []},
        )
    elif missing_relpaths:
        coverage_check = DoctorCheck(
            "Checkpoint Coverage",
            "fail",
            f"{latest_checkpoint.name} is missing {len(missing_relpaths)} live mutable path(s): {', '.join(missing_relpaths)}.",
            metadata={
                "checkpoint_name": latest_checkpoint.name,
                "live_mutable_paths": live_mutable_paths,
                "missing_relpaths": list(missing_relpaths),
            },
        )
    else:
        coverage_check = DoctorCheck(
            "Checkpoint Coverage",
            "ok",
            f"{latest_checkpoint.name} captures all {len(live_mutable_paths)} currently present mutable rollback path(s).",
            metadata={
                "checkpoint_name": latest_checkpoint.name,
                "live_mutable_paths": live_mutable_paths,
                "missing_relpaths": [],
            },
        )

    inventory_check = DoctorCheck(
        "Checkpoint Inventory",
        inventory_status,
        " ".join(inventory_parts),
        metadata={
            "checkpoint_count": len(checkpoints),
            "latest_checkpoint_issue": latest_checkpoint_issue,
            "latest_checkpoint_name": latest_checkpoint.name,
            "latest_confirmed_issue": latest_confirmed.issue_number if latest_confirmed is not None else None,
            "program_id": program_id,
        },
    )
    return DoctorReport(
        edition=edition_name,
        checks=(inventory_check, coverage_check),
    )


def checkpoint_issue_number(checkpoint_path: Path) -> int | None:
    match = re.fullmatch(r"issue_(\d+)_\d{8}T\d{6}Z", checkpoint_path.name)
    if match is None:
        return None
    return int(match.group(1))


def checkpoint_live_relpaths(program_id: str, *, programs_root: Path) -> list[str]:
    program_dir = programs_root / program_id
    relpaths = [rel_path for rel_path in CHECKPOINT_FILE_PATHS if (program_dir / rel_path).exists()]
    relpaths.extend(f"{rel_dir}/" for rel_dir in CHECKPOINT_DIR_PATHS if (program_dir / rel_dir).exists())
    return relpaths
