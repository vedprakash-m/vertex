from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer

from src.commands.doctor_checks.fact_store_flip_checks import run_flip_parity_doctor, run_flip_status_doctor
from src.commands.doctor_checks.storage_checks import run_storage_doctor
from src.core.archive_store import read_archive_index
from src.core.models_v2 import Workstream
from src.core.checkpoint_store import create_checkpoint_snapshot
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, resolve_edition
from src.core.exceptions import StateError
from src.core.fact_sor_state import (
    AUTHORITY_FAMILIES,
    load_fact_sor_state,
    load_family_clean_cycles,
    record_family_clean_cycle,
    save_fact_sor_state,
)
from src.core.snapshot_store import ARCHIVE_ROOT


_SUPPORTED_FAMILY_SET = (
    "actions",
    "claims",
    "claim_status_updates",
    "decision_asks",
    "assumptions",
    "decisions",
    "risks",
    "dependencies",
    "milestones",
    "workstreams",
    "workstream_associations",
    "baseline_trust_events",
    "skip_issues",
)
_PENDING_FAMILY_SET: tuple[str, ...] = ()

# WI-5.3: clean-cycle gate — per §6.7 `facts.flip_clean_cycles: 5`.
_FAMILY_FLIP_CLEAN_CYCLES_REQUIRED = 5


@dataclass(frozen=True, slots=True)
class FamilyFlipResult:
    program_id: str
    family: str
    previous_mode: str
    next_mode: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class FactStoreFlipParityWindowResult:
    issue_number: int
    passed: bool
    mismatched_families: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactStoreFlipPreviewArtifacts:
    program_id: str
    edition_name: str
    target_authority: str
    consecutive_parity_passes: int
    required_consecutive_parity_passes: int
    parity_window: tuple[FactStoreFlipParityWindowResult, ...]
    current_flip_status: str
    current_sor_mode: str
    current_storage_authority: str
    shadow_write_retention: str
    ready_for_execution: bool
    supported_families: tuple[str, ...]
    pending_families: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FactStoreFlipTransitionArtifacts:
    program_id: str
    edition_name: str
    previous_mode: str
    next_mode: str
    recorded_at: datetime
    checkpoint_path: Path | None = None


def fact_store_flip_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    to: str = typer.Option(..., "--to", help="Edition id to assess for fact-store flip readiness."),
    execute: bool = typer.Option(False, "--execute", help="Create a pre-flip checkpoint and persist SoR mode to shadow."),
    commit: bool = typer.Option(False, "--commit", help="Promote a previously executed shadow flip to primary SoR mode."),
    family: str | None = typer.Option(None, "--family", help="Authority family to flip to primary mode (WI-5.3)."),
    editions_root: Path = typer.Option(EDITIONS_ROOT, hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
    archive_root: Path = typer.Option(ARCHIVE_ROOT, hidden=True),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
) -> None:
    if family is not None:
        # WI-5.3 per-family flip path
        if commit:
            raise typer.BadParameter("--commit is not supported with --family. Per-family flips are single-step.")
        normalized_family = family.strip().lower()
        if normalized_family not in AUTHORITY_FAMILIES:
            raise typer.BadParameter(
                f"Unknown authority family {family!r}. Known families: {', '.join(AUTHORITY_FAMILIES)}"
            )
        if execute:
            result = flip_family_to_primary(
                program_id=program,
                family=normalized_family,
                programs_root=programs_root,
            )
            typer.echo(
                f"Family flip complete: program={result.program_id} | family={result.family} | "
                f"{result.previous_mode} → {result.next_mode}"
            )
        else:
            # Preview: show cycle status for this family
            cycles = load_family_clean_cycles(program, programs_root=programs_root)
            count = cycles.get(normalized_family, 0)
            ready = count >= _FAMILY_FLIP_CLEAN_CYCLES_REQUIRED
            current_state = load_fact_sor_state(program, programs_root=programs_root)
            current_fm = (current_state.family_modes or {}) if current_state else {}
            current_family_mode = current_fm.get(normalized_family, current_state.mode if current_state else "legacy")
            typer.echo(
                f"Family flip preview: program={program} | family={normalized_family} | "
                f"current_mode={current_family_mode} | clean_cycles={count}/"
                f"{_FAMILY_FLIP_CLEAN_CYCLES_REQUIRED} | ready={'yes' if ready else 'no'}"
            )
            if not ready:
                typer.echo(
                    f"Not ready: need {_FAMILY_FLIP_CLEAN_CYCLES_REQUIRED} clean cycles for '{normalized_family}'; "
                    f"have {count}. Run gather cycles and let parity checks accumulate."
                )
        raise typer.Exit(code=0)

    if execute and commit:
        raise typer.BadParameter("Use either --execute or --commit, not both.")

    artifacts = run_fact_store_flip_preview(
        program_id=program,
        edition_name=to,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=db_root,
    )
    typer.echo(
        f"Fact-store flip preview for {artifacts.program_id}/{artifacts.edition_name} | "
        f"target_authority={artifacts.target_authority} | parity_window={artifacts.consecutive_parity_passes}/"
        f"{artifacts.required_consecutive_parity_passes} consecutive | current_status={artifacts.current_flip_status} | "
        f"current_authority={artifacts.current_storage_authority} | shadow_write_retention={artifacts.shadow_write_retention} | "
        f"ready_for_execution={'yes' if artifacts.ready_for_execution else 'no'}"
    )
    if artifacts.parity_window:
        typer.echo(
            "Recent parity issues: "
            + ", ".join(
                (
                    f"#{result.issue_number}=ok"
                    if result.passed
                    else f"#{result.issue_number}=fail({','.join(result.mismatched_families) or 'unknown'})"
                )
                for result in artifacts.parity_window
            )
        )
    else:
        typer.echo("Recent parity issues: none")
    typer.echo(
        "Family coverage: "
        f"supported={','.join(artifacts.supported_families)} | pending={','.join(artifacts.pending_families) or 'none'}"
    )
    typer.echo(f"Blockers: {', '.join(artifacts.blockers) if artifacts.blockers else 'none'}")
    if execute:
        transition = execute_fact_store_flip(
            program_id=program,
            edition_name=to,
            editions_root=editions_root,
            programs_root=programs_root,
            archive_root=archive_root,
            reality_db_root=db_root,
        )
        typer.echo(
            "Execution complete: "
            f"sor_mode={transition.next_mode}; checkpoint={transition.checkpoint_path}"
        )
        raise typer.Exit(code=0)
    if commit:
        transition = commit_fact_store_flip(
            program_id=program,
            edition_name=to,
            editions_root=editions_root,
            programs_root=programs_root,
            archive_root=archive_root,
            reality_db_root=db_root,
        )
        typer.echo(f"Commit complete: sor_mode={transition.next_mode}")
        raise typer.Exit(code=0)
    raise typer.Exit(code=0)


def run_fact_store_flip_preview(
    *,
    program_id: str,
    edition_name: str,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    reality_db_root: Path | None = None,
) -> FactStoreFlipPreviewArtifacts:
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        raise typer.BadParameter(f"Edition '{edition_name}' could not be resolved.")
    if resolved.program.id != program_id:
        raise typer.BadParameter(
            f"Edition '{edition_name}' is bound to program '{resolved.program.id}', not '{program_id}'."
        )

    flip_report = run_flip_status_doctor(
        edition_name=edition_name,
        program_id=program_id,
        programs_root=programs_root,
        reality_db_root=reality_db_root or programs_root.parent,
    )
    flip_check = flip_report.checks[0]
    flip_metadata = flip_check.metadata or {}

    storage_report = run_storage_doctor(
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=reality_db_root,
    )
    authority_check = next(check for check in storage_report.checks if check.label == "Fact Store Authority")
    authority_metadata = authority_check.metadata or {}

    parity_window = _build_recent_parity_window(
        edition_name=edition_name,
        program_id=program_id,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=reality_db_root or programs_root.parent,
        resolved_workstreams=resolved.workstreams,
    )
    consecutive_parity_passes = 0
    for result in parity_window:
        if not result.passed:
            break
        consecutive_parity_passes += 1

    required_consecutive_parity_passes = 3
    shadow_write_retention = str(authority_metadata.get("shadow_write_retention", "disabled"))
    blockers: list[str] = []
    if consecutive_parity_passes < required_consecutive_parity_passes:
        blockers.append(
            f"need {required_consecutive_parity_passes} consecutive parity passes; have {consecutive_parity_passes}"
        )
        first_failed_issue = next((result for result in parity_window if not result.passed), None)
        if first_failed_issue is not None:
            blockers.append(
                "parity mismatch at issue "
                f"#{first_failed_issue.issue_number}: {','.join(first_failed_issue.mismatched_families) or 'unknown'}"
            )
    if shadow_write_retention != "enabled":
        blockers.append("shadow-write retention anchor is unavailable")
    if _PENDING_FAMILY_SET:
        blockers.append(f"unsupported families remain: {','.join(_PENDING_FAMILY_SET)}")
    ready_for_execution = not blockers

    return FactStoreFlipPreviewArtifacts(
        program_id=program_id,
        edition_name=edition_name,
        target_authority="primary",
        consecutive_parity_passes=consecutive_parity_passes,
        required_consecutive_parity_passes=required_consecutive_parity_passes,
        parity_window=parity_window,
        current_flip_status=str(flip_metadata.get("flip_status", "legacy")),
        current_sor_mode=str(flip_metadata.get("sor_mode", "legacy")),
        current_storage_authority=str(authority_metadata.get("fact_store_authority", "legacy")),
        shadow_write_retention=shadow_write_retention,
        ready_for_execution=ready_for_execution,
        supported_families=_SUPPORTED_FAMILY_SET,
        pending_families=_PENDING_FAMILY_SET,
        blockers=tuple(blockers),
    )


def execute_fact_store_flip(
    *,
    program_id: str,
    edition_name: str,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    reality_db_root: Path | None = None,
) -> FactStoreFlipTransitionArtifacts:
    preview = run_fact_store_flip_preview(
        program_id=program_id,
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=reality_db_root,
    )
    if preview.blockers:
        raise StateError(f"Fact-store flip execute is blocked: {', '.join(preview.blockers)}")

    current_state = load_fact_sor_state(program_id, programs_root=programs_root)
    current_mode = current_state.mode if current_state is not None else "legacy"
    if current_mode == "shadow":
        raise StateError(f"Fact-store flip for program '{program_id}' is already executed in shadow mode.")
    if current_mode == "primary":
        raise StateError(f"Fact-store flip for program '{program_id}' is already committed to primary mode.")

    issue_number = _latest_confirmed_issue_number(edition_name=edition_name, archive_root=archive_root)
    checkpoint_path = create_checkpoint_snapshot(program_id, issue_number, programs_root=programs_root)
    recorded_at = datetime.now(timezone.utc)
    save_fact_sor_state(
        program_id,
        mode="shadow",
        recorded_at=recorded_at,
        recorded_by="vertex admin fact-store-flip --execute",
        programs_root=programs_root,
    )
    return FactStoreFlipTransitionArtifacts(
        program_id=program_id,
        edition_name=edition_name,
        previous_mode=current_mode,
        next_mode="shadow",
        recorded_at=recorded_at,
        checkpoint_path=checkpoint_path,
    )


def commit_fact_store_flip(
    *,
    program_id: str,
    edition_name: str,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    reality_db_root: Path | None = None,
) -> FactStoreFlipTransitionArtifacts:
    current_state = load_fact_sor_state(program_id, programs_root=programs_root)
    current_mode = current_state.mode if current_state is not None else "legacy"
    if current_mode == "primary":
        raise StateError(f"Fact-store flip for program '{program_id}' is already committed to primary mode.")
    if current_mode != "shadow":
        raise StateError(
            f"Fact-store flip for program '{program_id}' must be executed to shadow mode before commit."
        )

    preview = run_fact_store_flip_preview(
        program_id=program_id,
        edition_name=edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=reality_db_root,
    )
    if preview.blockers:
        raise StateError(f"Fact-store flip commit is blocked: {', '.join(preview.blockers)}")

    recorded_at = datetime.now(timezone.utc)
    save_fact_sor_state(
        program_id,
        mode="primary",
        recorded_at=recorded_at,
        recorded_by="vertex admin fact-store-flip --commit",
        programs_root=programs_root,
    )
    return FactStoreFlipTransitionArtifacts(
        program_id=program_id,
        edition_name=edition_name,
        previous_mode=current_mode,
        next_mode="primary",
        recorded_at=recorded_at,
    )


def flip_family_to_primary(
    *,
    program_id: str,
    family: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> FamilyFlipResult:
    """Flip a single authority family to primary SoR mode (WI-5.3).

    Refuses with ``StateError`` when the family has not accumulated
    ``_FAMILY_FLIP_CLEAN_CYCLES_REQUIRED`` consecutive clean parity cycles.
    """
    normalized_family = family.strip().lower()
    if normalized_family not in AUTHORITY_FAMILIES:
        raise ValueError(f"Unknown authority family {family!r}.")

    cycles = load_family_clean_cycles(program_id, programs_root=programs_root)
    count = cycles.get(normalized_family, 0)
    if count < _FAMILY_FLIP_CLEAN_CYCLES_REQUIRED:
        raise StateError(
            f"Family flip refused for '{normalized_family}': need "
            f"{_FAMILY_FLIP_CLEAN_CYCLES_REQUIRED} clean cycles, have {count}."
        )

    current_state = load_fact_sor_state(program_id, programs_root=programs_root)
    current_fm = dict(current_state.family_modes) if current_state else {}
    previous_family_mode = current_fm.get(normalized_family, current_state.mode if current_state else "legacy")

    if previous_family_mode == "primary":
        raise StateError(f"Family '{normalized_family}' is already in primary mode.")

    new_fm = dict(current_fm)
    new_fm[normalized_family] = "primary"
    recorded_at = datetime.now(timezone.utc)
    save_fact_sor_state(
        program_id,
        mode=current_state.mode if current_state else "legacy",
        recorded_at=recorded_at,
        recorded_by=f"vertex admin fact-store-flip --family {normalized_family} --execute",
        family_modes=new_fm,
        programs_root=programs_root,
    )
    return FamilyFlipResult(
        program_id=program_id,
        family=normalized_family,
        previous_mode=previous_family_mode,
        next_mode="primary",
        recorded_at=recorded_at,
    )


def _build_recent_parity_window(
    *,
    edition_name: str,
    program_id: str,
    programs_root: Path,
    archive_root: Path,
    reality_db_root: Path,
    resolved_workstreams: tuple[Workstream, ...],
) -> tuple[FactStoreFlipParityWindowResult, ...]:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    confirmed_issue_numbers = tuple(
        entry.issue_number
        for entry in reversed(archive_index.issues)
        if entry.kind == "confirmed"
    )[:3]
    results: list[FactStoreFlipParityWindowResult] = []
    for issue_number in confirmed_issue_numbers:
        report = run_flip_parity_doctor(
            edition_name=edition_name,
            program_id=program_id,
            issue_number=issue_number,
            programs_root=programs_root,
            reality_db_root=reality_db_root,
            archive_root=archive_root,
            resolved_workstreams=resolved_workstreams,
        )
        parity_check = next(check for check in report.checks if check.label == "Flip Parity")
        parity_metadata = parity_check.metadata or {}
        results.append(
            FactStoreFlipParityWindowResult(
                issue_number=issue_number,
                passed=parity_check.status == "ok",
                mismatched_families=tuple(parity_metadata.get("mismatched_families", ())),
            )
        )
    return tuple(results)


def _latest_confirmed_issue_number(*, edition_name: str, archive_root: Path) -> int:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    confirmed_issue_numbers = [entry.issue_number for entry in archive_index.issues if entry.kind == "confirmed"]
    if not confirmed_issue_numbers:
        return 0
    return max(confirmed_issue_numbers)