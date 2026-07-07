from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError

log = logging.getLogger(__name__)


def compute_and_persist_plane1_changes(
    program_id: str,
    programs_root: Path,
    gathered_at: datetime,
    *,
    load_program_facts,
    project_milestones,
    project_risk_entries,
    project_decision_entries,
    project_assumptions,
    project_workstreams,
    load_plane1_last_seen,
    compute_plane1_changes,
    append_plane1_changes,
    build_plane1_snapshot,
    shadow_write_plane1_snapshot,
    persist_program_fact_snapshot,
    write_plane1_last_seen,
) -> None:
    db_root = programs_root.parent / "vertex-db"
    try:
        current_facts = load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=(
                "action.item",
                "dependency.link",
                "milestone.entry",
                "risk.entry",
                "decision.entry",
                "assumption.entry",
                "workstream.entry",
            ),
        )
        milestones = list(project_milestones(current_facts))
        risks = list(project_risk_entries(current_facts))
        decisions = list(project_decision_entries(current_facts))
        assumptions = list(project_assumptions(current_facts))
        workstreams = list(project_workstreams(current_facts))
    except (ConfigError, OSError, yaml.YAMLError, KeyError, TypeError, ValueError):
        return

    last_seen = load_plane1_last_seen(program_id, programs_root=programs_root)
    gather_run_id = gathered_at.strftime("%Y%m%dT%H%M%SZ")
    changes = compute_plane1_changes(
        program_id,
        milestones,
        risks,
        workstreams,
        decisions,
        assumptions,
        last_seen,
        gather_run_id=gather_run_id,
        gathered_at=gathered_at,
    )
    if changes:
        append_plane1_changes(program_id, changes, programs_root=programs_root)
    current_snapshot = build_plane1_snapshot(milestones, risks, workstreams, decisions, assumptions)

    # Guard: never overwrite a non-empty last-seen snapshot with an empty one.
    # An empty current_snapshot means no Plane 1 entities were projected (e.g. fact
    # store not yet populated). Without this guard a gather on an un-initialised
    # program would silently erase the prior baseline, causing every subsequent run
    # to treat all entities as newly added.
    if not current_snapshot and last_seen:
        log.warning(
            "plane1: skipping plane1_last_seen write — current snapshot is empty "
            "but prior snapshot had %d entries (program_id=%s). "
            "Run `vertex gather` once the fact store is populated.",
            len(last_seen),
            program_id,
        )
        # Still persist the shadow / fact snapshots — only the last_seen write is skipped.
        shadow_write_plane1_snapshot(
            program_id,
            current_snapshot,
            recorded_at=gathered_at,
            db_root=db_root,
        )
        persist_program_fact_snapshot(
            current_facts,
            recorded_at=gathered_at,
            db_root=db_root,
        )
        return

    shadow_write_plane1_snapshot(
        program_id,
        current_snapshot,
        recorded_at=gathered_at,
        db_root=db_root,
    )
    persist_program_fact_snapshot(
        current_facts,
        recorded_at=gathered_at,
        db_root=db_root,
    )
    write_plane1_last_seen(program_id, current_snapshot, programs_root=programs_root)
