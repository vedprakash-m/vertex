"""Read-only confirm pre-validation helpers.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). This cluster
resolves the confirming author, validates the decision-strip acknowledgement,
and evaluates stale-approval / stale-proposed-decision conditions. Every
function is read-only — it inspects review status, ADO evidence, and decision-
register state and returns warnings/failures — and none of them write state, so
they are safe to lift out of the confirm transaction module ahead of the write
path. ``confirm.py`` imports the public entry points it calls under their
historical private aliases; the predicate helpers are internal to this module.
"""

from __future__ import annotations

import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core.decision_register import assess_proposed_decision_staleness
from src.core.edition_resolver import resolve_edition
from src.core.models import ReportData, ReviewState, ReviewStatus, WorkItem
from src.core.overrides_store import OverridesDocument
from src.core.program_fact_store import load_current_decision_entries
from src.core.view_models import WorkstreamData


def read_confirming_author() -> str | None:
    override = os.environ.get("VERTEX_AUTHOR")
    if override and override.strip():
        return override.strip()
    try:
        completed = subprocess.run(
            ["git", "config", "user.name"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        candidate = completed.stdout.strip()
        if candidate:
            return candidate
    username = os.environ.get("USERNAME")
    if username and username.strip():
        return username.strip()
    return None


def ack_word_count(reason: str | None) -> int:
    if reason is None:
        return 0
    return len(reason.split())


def validate_decision_strip_ack(overrides_document: OverridesDocument) -> tuple[str, ...]:
    ack = overrides_document.decision_strip_ack
    if ack is None or not ack.no_leadership_ask:
        return ()
    reason_words = ack_word_count(ack.reason)
    if ack.reason is None or reason_words < 12 or reason_words > 40:
        return ("Decision Strip acknowledgement reason must be 12-40 words.",)
    return ()


def evaluate_stale_approvals(
    *,
    review_status: ReviewStatus,
    report: ReportData,
    workstream_data: tuple[WorkstreamData, ...],
    evidence_by_item: dict[int, Any],
    current_manifest_id: str,
    ack_stale_approval: bool,
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    section_items = {"exec_summary": report.items, **{f"ws:{workstream.section_id}": workstream.items for workstream in workstream_data}}
    section_labels = {"exec_summary": "Executive Summary", **{f"ws:{workstream.section_id}": workstream.title for workstream in workstream_data}}

    warnings: list[str] = []
    failures: list[str] = []
    override_applied = False

    for section in review_status.sections:
        if section.state != ReviewState.APPROVED or not section.manifest_id or section.manifest_id == current_manifest_id:
            continue
        label = section_labels.get(section.section_id, section.section_id)
        warnings.append(
            f"[STALE APPROVAL] {label} was approved against manifest {section.manifest_id}, current manifest {current_manifest_id}."
        )
        if section.updated_at is None:
            continue
        if section_has_post_approval_data_change(section_items.get(section.section_id, ()), evidence_by_item, section.updated_at):
            if ack_stale_approval:
                override_applied = True
            else:
                failures.append(
                    f"BLOCKED: Stale approval + data changed for {label}. Re-run confirm with --ack-stale-approval after reviewing updated ADO data."
                )

    return tuple(warnings), tuple(failures), override_applied


def build_stale_proposed_decision_warnings(
    *,
    edition_name: str,
    as_of: date,
    reports_root: Path,
) -> tuple[str, ...]:
    repo_root = reports_root.parent
    programs_root = repo_root / "programs"
    editions_root = repo_root / "editions"
    resolved_v2 = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved_v2 is None:
        return ()

    stale_entries = tuple(
        entry
        for entry in load_current_decision_entries(resolved_v2.program.id, programs_root=programs_root)
        if assess_proposed_decision_staleness(entry, as_of)
    )
    if not stale_entries:
        return ()

    preview = ", ".join(f"{entry.id} ({entry.title})" for entry in stale_entries[:3])
    if len(stale_entries) > 3:
        preview = f"{preview}, +{len(stale_entries) - 3} more"
    return (
        f"[DECISIONS] {len(stale_entries)} proposed decision entr{'y' if len(stale_entries) == 1 else 'ies'} pending >14 days: "
        f"{preview}. Review vertex decisions list --program {resolved_v2.program.id} --status proposed.",
    )


def section_has_post_approval_data_change(
    items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, Any],
    approved_at: datetime,
) -> bool:
    for item in items:
        evidence = evidence_by_item.get(item.id)
        if evidence is None:
            continue
        for revision in evidence.revisions:
            if revision.changed_date > approved_at and any(is_stale_approval_field(field_name) for field_name in revision.fields_changed):
                return True
    return False


def is_stale_approval_field(field_name: str) -> bool:
    normalized = field_name.strip().lower()
    return normalized in {"state", "system.state", "risk", "risklevel", "risk_level"} or "risk" in normalized
