from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.action_tracker import get_actions_path
from src.core.action_tracker import load_actions
from src.core.assumption_tracker import load_assumptions
from src.core.archive_store import load_skipped_issues_for_program
from src.core.claim_tracker import load_claim_status_updates, load_open_claims, load_open_decision_asks
from src.core.archive_store import read_archive_index
from src.core.claim_tracker import get_claims_path
from src.core.decision_register import load_decisions
from src.core.decision_register import get_decisions_path
from src.core.dependency_graph import get_dependencies_path
from src.core.dependency_graph import load_dependencies
from src.core.milestone_engine import get_milestones_path
from src.core.milestone_engine import load_milestones
from src.core.program_fact_store import load_program_facts
from src.core.program_fact_store import project_action_items
from src.core.program_fact_store import project_assumptions
from src.core.program_fact_store import project_baseline_trust_events
from src.core.program_fact_store import project_claim_entries
from src.core.program_fact_store import project_claim_status_updates
from src.core.program_fact_store import project_decision_asks
from src.core.program_fact_store import project_decision_entries
from src.core.program_fact_store import project_dependencies
from src.core.program_fact_store import project_milestones
from src.core.program_fact_store import project_risk_entries
from src.core.program_fact_store import project_skip_issues
from src.core.program_fact_store import project_workstream_associations
from src.core.program_fact_store import project_workstreams
from src.core.program_fact_store import resolve_fact_sor_mode
from src.core.reality_store import get_program_reality_db_path
from src.core.risk_register_engine import get_risk_register_path
from src.core.risk_register_engine import load_risk_register
from src.core.trusted_baseline_store import load_trusted_baseline_for_program
from src.core.models_v2 import Workstream
from src.core.workstream_association_store import read_workstream_association_records


def run_flip_status_doctor(
    *,
    edition_name: str,
    program_id: str,
    programs_root: Path,
    reality_db_root: Path,
) -> DoctorReport:
    fact_store_db_path = get_program_reality_db_path(program_id, db_root=reality_db_root)
    accepted_revision_count, proposed_revision_count, snapshot_pin_count = _load_fact_store_counts(fact_store_db_path)
    legacy_mutable_paths = _legacy_mutable_paths(program_id, programs_root=programs_root)
    sor_mode = resolve_fact_sor_mode(program_id=program_id, programs_root=programs_root)
    shim_mode = "disabled" if sor_mode == "primary" else "enabled"
    flip_status = _derive_flip_status(
        accepted_revision_count=accepted_revision_count,
        proposed_revision_count=proposed_revision_count,
        snapshot_pin_count=snapshot_pin_count,
        legacy_mutable_paths=legacy_mutable_paths,
    )
    doctor_status = "ok" if flip_status == "fact-store" else "warn"
    legacy_labels = ", ".join(path.name for path in legacy_mutable_paths) or "none"
    detail = (
        f"status={flip_status}; accepted_revisions={accepted_revision_count}; proposed_revisions={proposed_revision_count}; "
        f"snapshot_pins={snapshot_pin_count}; legacy_mutable_paths={len(legacy_mutable_paths)} ({legacy_labels}); "
        f"sor_mode={sor_mode}; shim_mode={shim_mode}"
    )
    return DoctorReport(
        edition=edition_name,
        checks=(
            DoctorCheck(
                label="Fact Store Flip",
                status=doctor_status,
                detail=detail,
                metadata={
                    "accepted_revision_count": accepted_revision_count,
                    "fact_store_db_exists": fact_store_db_path.exists(),
                    "fact_store_db_path": str(fact_store_db_path),
                    "flip_status": flip_status,
                    "legacy_mutable_paths": tuple(str(path) for path in legacy_mutable_paths),
                    "proposed_revision_count": proposed_revision_count,
                    "shim_mode": shim_mode,
                    "snapshot_pin_count": snapshot_pin_count,
                    "sor_mode": sor_mode,
                },
            ),
        ),
    )


def run_flip_parity_doctor(
    *,
    edition_name: str,
    program_id: str,
    issue_number: int,
    programs_root: Path,
    reality_db_root: Path,
    archive_root: Path,
    resolved_workstreams: tuple[Workstream, ...],
) -> DoctorReport:
    issue_generated_at = _load_issue_generated_at(
        edition_name=edition_name,
        issue_number=issue_number,
        archive_root=archive_root,
    )
    if issue_generated_at is None:
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    label="Flip Parity",
                    status="fail",
                    detail=f"issue={issue_number} is not present in the confirmed archive index.",
                    metadata={
                        "issue_number": issue_number,
                        "matched_families": (),
                        "mismatched_families": (),
                    },
                ),
            ),
        )

    family_results = _compute_family_parity(
        edition_name=edition_name,
        program_id=program_id,
        issue_generated_at=issue_generated_at,
        programs_root=programs_root,
        reality_db_root=reality_db_root,
        archive_root=archive_root,
        resolved_workstreams=resolved_workstreams,
    )
    matched_families = tuple(result["family"] for result in family_results if result["matches"])
    mismatched_families = tuple(result["family"] for result in family_results if not result["matches"])
    parity_status = "ok" if not mismatched_families else "fail"
    mismatch_detail = ", ".join(
        f"{result['family']}(legacy={result['legacy_count']}, fact_store={result['fact_store_count']})"
        for result in family_results
        if not result["matches"]
    ) or "none"
    return DoctorReport(
        edition=edition_name,
        checks=(
            DoctorCheck(
                label="Flip Parity Anchor",
                status="info",
                detail=f"issue={issue_number}; generated_at={issue_generated_at.isoformat()}",
                metadata={
                    "generated_at": issue_generated_at.isoformat(),
                    "issue_number": issue_number,
                },
            ),
            DoctorCheck(
                label="Flip Parity",
                status=parity_status,
                detail=(
                    f"issue={issue_number}; matched={len(matched_families)}/{len(family_results)} families; "
                    f"mismatches={mismatch_detail}"
                ),
                metadata={
                    "family_results": family_results,
                    "generated_at": issue_generated_at.isoformat(),
                    "issue_number": issue_number,
                    "matched_families": matched_families,
                    "mismatched_families": mismatched_families,
                },
            ),
        ),
    )


def _derive_flip_status(
    *,
    accepted_revision_count: int,
    proposed_revision_count: int,
    snapshot_pin_count: int,
    legacy_mutable_paths: tuple[Path, ...],
) -> str:
    if accepted_revision_count == 0 and proposed_revision_count == 0 and snapshot_pin_count == 0:
        return "legacy"
    if legacy_mutable_paths:
        return "dual"
    return "fact-store"


def _legacy_mutable_paths(program_id: str, *, programs_root: Path) -> tuple[Path, ...]:
    candidates = (
        get_actions_path(program_id, programs_root),
        get_claims_path(program_id, programs_root),
        get_decisions_path(program_id, programs_root),
        get_dependencies_path(program_id, programs_root),
        get_milestones_path(program_id, programs_root),
        get_risk_register_path(program_id, programs_root),
    )
    return tuple(path for path in candidates if path.exists())


def _load_fact_store_counts(db_path: Path) -> tuple[int, int, int]:
    if not db_path.exists():
        return (0, 0, 0)
    with sqlite3.connect(db_path) as connection:
        accepted_revision_count = _count_rows(
            connection,
            "program_fact_revisions",
            "review_state = ?",
            ("accepted",),
        )
        proposed_revision_count = _count_rows(
            connection,
            "program_fact_revisions",
            "review_state = ?",
            ("proposed",),
        )
        snapshot_pin_count = _count_rows(connection, "program_fact_snapshot_pins")
    return (accepted_revision_count, proposed_revision_count, snapshot_pin_count)


def _load_issue_generated_at(*, edition_name: str, issue_number: int, archive_root: Path) -> datetime | None:
    index = read_archive_index(edition_name, archive_root=archive_root)
    for entry in index.issues:
        if entry.kind != "confirmed":
            continue
        if entry.issue_number == issue_number:
            return entry.generated_at
    return None


def _compute_family_parity(
    *,
    edition_name: str,
    program_id: str,
    issue_generated_at: datetime,
    programs_root: Path,
    reality_db_root: Path,
    archive_root: Path,
    resolved_workstreams: tuple[Workstream, ...],
) -> list[dict[str, Any]]:
    fact_snapshot = load_program_facts(
        program_id,
        as_of=issue_generated_at,
        db_root=reality_db_root,
        programs_root=programs_root,
    )
    trusted_baseline = load_trusted_baseline_for_program(program_id, programs_root=programs_root)
    skip_issues = tuple(
        entry
        for entry in load_skipped_issues_for_program(
            program_id,
            archive_root=archive_root,
        )
        if entry.edition_id == edition_name and entry.generated_at <= issue_generated_at
    )
    families = (
        (
            "actions",
            load_actions(program_id, programs_root=programs_root),
            project_action_items(fact_snapshot),
        ),
        (
            "claims",
            load_open_claims(program_id, programs_root=programs_root),
            project_claim_entries(fact_snapshot),
        ),
        (
            "claim_status_updates",
            tuple(
                entry
                for entry in load_claim_status_updates(program_id, programs_root=programs_root)
                if entry.updated_at <= issue_generated_at
            ),
            tuple(
                entry
                for entry in project_claim_status_updates(fact_snapshot)
                if entry.updated_at <= issue_generated_at
            ),
        ),
        (
            "decision_asks",
            load_open_decision_asks(program_id, programs_root=programs_root),
            project_decision_asks(fact_snapshot),
        ),
        (
            "assumptions",
            load_assumptions(program_id, programs_root=programs_root),
            project_assumptions(fact_snapshot),
        ),
        (
            "decisions",
            load_decisions(program_id, programs_root=programs_root),
            project_decision_entries(fact_snapshot),
        ),
        (
            "risks",
            load_risk_register(program_id, programs_root=programs_root),
            project_risk_entries(fact_snapshot),
        ),
        (
            "dependencies",
            load_dependencies(program_id, programs_root=programs_root),
            project_dependencies(fact_snapshot),
        ),
        (
            "milestones",
            load_milestones(program_id, programs_root=programs_root),
            project_milestones(fact_snapshot),
        ),
        (
            "workstreams",
            resolved_workstreams,
            project_workstreams(fact_snapshot),
        ),
        (
            "workstream_associations",
            tuple(
                record
                for record in read_workstream_association_records(program_id, programs_root=programs_root)
                if record.edition == edition_name and record.recorded_at <= issue_generated_at
            ),
            tuple(
                record
                for record in project_workstream_associations(fact_snapshot)
                if record.edition == edition_name and record.recorded_at <= issue_generated_at
            ),
        ),
        (
            "baseline_trust_events",
            (() if trusted_baseline is None else trusted_baseline.history),
            project_baseline_trust_events(fact_snapshot),
        ),
        (
            "skip_issues",
            tuple(entry for entry in skip_issues if entry.edition_id == edition_name),
            tuple(entry for entry in project_skip_issues(fact_snapshot) if entry.edition_id == edition_name),
        ),
    )
    results: list[dict[str, Any]] = []
    for family_name, legacy_items, fact_store_items in families:
        normalized_legacy = _canonicalize_items(legacy_items)
        normalized_fact_store = _canonicalize_items(fact_store_items)
        results.append(
            {
                "family": family_name,
                "matches": normalized_legacy == normalized_fact_store,
                "legacy_count": len(legacy_items),
                "fact_store_count": len(fact_store_items),
            }
        )
    return results


def _canonicalize_items(items: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            json.dumps(_normalize_value(item), sort_keys=True)
            for item in items
        )
    )


def _normalize_value(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _normalize_value(getattr(value, field.name))
            for field in fields(value)
            if field.name not in {"fact_id", "last_validated_at"}
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_normalize_value(item) for item in value]
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _count_rows(
    connection: sqlite3.Connection,
    table_name: str,
    where_clause: str | None = None,
    parameters: tuple[object, ...] = (),
) -> int:
    table_row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if table_row is None:
        return 0
    query = f"SELECT COUNT(*) FROM {table_name}"
    if where_clause:
        query = f"{query} WHERE {where_clause}"
    row = connection.execute(query, parameters).fetchone()
    return int(row[0]) if row is not None else 0


# --------------------------------------------------------------------------
# Fact parity doctor (WS-4: doctor --fact-parity)
# --------------------------------------------------------------------------

_DEFAULT_DUAL_READ_CYCLES = 5


def _load_dual_read_cycles(*, programs_root: Path) -> int:
    """Read ``fact_store.dual_read_cycles`` from ``platform_state.yaml``.

    Falls back to 5 when the file is absent, unparseable, or the key is missing.
    """
    path = programs_root / "platform_state.yaml"
    if not path.exists():
        return _DEFAULT_DUAL_READ_CYCLES
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return _DEFAULT_DUAL_READ_CYCLES
    if not isinstance(doc, dict):
        return _DEFAULT_DUAL_READ_CYCLES
    fs_block = doc.get("fact_store")
    if not isinstance(fs_block, dict):
        return _DEFAULT_DUAL_READ_CYCLES
    value = fs_block.get("dual_read_cycles", _DEFAULT_DUAL_READ_CYCLES)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return _DEFAULT_DUAL_READ_CYCLES


def run_fact_parity_doctor(
    *,
    edition_name: str,
    program_id: str,
    programs_root: Path,
) -> DoctorReport:
    """Report whether enough dual-read parity cycles have been logged for the program.

    Reads ``fact_store.dual_read_cycles`` from ``platform_state.yaml`` (default 5)
    and counts passed cycles in ``programs/<prog>/fact_store_parity_log.jsonl``.
    Returns WARN if fewer than the required cycles have passed, OK otherwise.
    """
    required_cycles = _load_dual_read_cycles(programs_root=programs_root)
    log_path = programs_root / program_id / "fact_store_parity_log.jsonl"

    if not log_path.exists():
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    label="Fact Parity",
                    status="warn",
                    detail=(
                        f"No parity log found at {log_path.name}. "
                        f"Run: vertex facts dual-read-log --program {program_id} --cycles {required_cycles}"
                    ),
                    metadata={
                        "log_path": str(log_path),
                        "required_cycles": required_cycles,
                        "passed_cycles": 0,
                        "total_cycles": 0,
                    },
                ),
            ),
        )

    records: list[dict[str, Any]] = []
    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            from src.core.jsonl_utils import parse_jsonl_line
            records.append(parse_jsonl_line(stripped))
        except (json.JSONDecodeError, ValueError):
            continue

    passed_cycles = sum(1 for r in records if r.get("passed", False))
    total_cycles = len(records)

    if passed_cycles >= required_cycles:
        status = "ok"
        detail = (
            f"parity_cycles_passed={passed_cycles}/{total_cycles}; "
            f"required={required_cycles}; log={log_path.name}"
        )
    else:
        status = "warn"
        detail = (
            f"Only {passed_cycles}/{total_cycles} cycles passed; required={required_cycles}. "
            f"Run: vertex facts dual-read-log --program {program_id} --cycles {required_cycles}"
        )

    return DoctorReport(
        edition=edition_name,
        checks=(
            DoctorCheck(
                label="Fact Parity",
                status=status,
                detail=detail,
                metadata={
                    "log_path": str(log_path),
                    "required_cycles": required_cycles,
                    "passed_cycles": passed_cycles,
                    "total_cycles": total_cycles,
                },
            ),
        ),
    )
