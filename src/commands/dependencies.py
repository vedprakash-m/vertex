from __future__ import annotations

import csv
from io import StringIO
import json
from pathlib import Path
from typing import cast

import typer

from src.core.adoption_telemetry import GoldenWorkflow, record_adoption
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.dependency_graph import save_dependencies
from src.core.dependency_scout import (
    DependencyProposalStatus,
    dependency_proposal_to_dependency,
    dependency_proposal_confidence_label,
    load_dependency_proposals,
    merge_dependency_proposals,
    save_dependency_proposals,
    scout_dependency_proposals,
    update_dependency_proposal_status,
)
from src.core.models import SnapshotItem
from src.core.models_v2 import Dependency, Signal, SignalReviewDecision, TrajectoryPoint, Workstream
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, resolve_edition
from src.core.program_fact_store import load_program_facts, project_dependencies
from src.core.snapshot_store import ARCHIVE_ROOT, read_snapshot
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id


app = typer.Typer(help="Inspect and manage inferred dependency proposals.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def dependencies_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    status: str = typer.Option("proposed", "--status", help="Status filter: proposed, accepted, dismissed, or all."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    proposals = _filtered_proposals(program.strip(), status=status)
    typer.echo(_render_dependency_proposals(program.strip(), proposals, format=format), nl=False)
    raise typer.Exit(code=0)


@app.command("list")
def list_dependency_proposals_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    status: str = typer.Option("proposed", "--status", help="Status filter: proposed, accepted, dismissed, or all."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    proposals = _filtered_proposals(program.strip(), status=status)
    typer.echo(_render_dependency_proposals(program.strip(), proposals, format=format), nl=False)
    raise typer.Exit(code=0)


@app.command("scout")
def scout_dependencies_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    edition: str | None = typer.Option(None, "--edition", help="Optional edition to use for latest confirmed snapshot context."),
    lookback_days: int = typer.Option(30, "--lookback-days", help="Signal lookback window in days."),
    min_occurrences: int = typer.Option(3, "--min-occurrences", help="Minimum repeated co-mentions required for a proposal."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render proposals without writing dependency_proposals.yaml."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    program_id = program.strip()
    context = _load_scout_context(program_id, edition_name=edition.strip() if edition is not None and edition.strip() else None)
    existing_proposals = load_dependency_proposals(program_id, programs_root=PROGRAMS_ROOT)
    from src.core.edition_resolver import ResolvedEdition
    from src.core.models import Snapshot

    snap = cast("Snapshot", context["snapshot"])
    resolved_ed = cast("ResolvedEdition", context["resolved_edition"])
    generated = scout_dependency_proposals(
        program_id=program_id,
        signals=cast("tuple[Signal, ...]", context["signals"]),
        review_states=cast("dict[str, SignalReviewDecision]", context["review_states"]),
        snapshot_items=snap.items,
        workstreams=cast("tuple[Workstream, ...]", resolved_ed.workstreams),
        existing_dependencies=cast("tuple[Dependency, ...]", context["dependencies"]),
        trajectories_by_item_id=cast("dict[int, tuple[TrajectoryPoint, ...]]", context["trajectories_by_item_id"]),
        as_of=snap.generated_at,
        lookback_days=lookback_days,
        min_occurrences=min_occurrences,
    )
    proposals = merge_dependency_proposals(existing_proposals, generated)
    if not dry_run:
        save_dependency_proposals(program_id, proposals, programs_root=PROGRAMS_ROOT)
    typer.echo(_render_dependency_proposals(program_id, proposals, format=format), nl=False)
    raise typer.Exit(code=0)


@app.command("accept")
def accept_dependency_proposal_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    proposal_id: str = typer.Option(..., "--id", help="Dependency proposal id."),
    dependency_type: str | None = typer.Option(None, "--type", help="Optional override: blocks, informs, or shares_resource."),
    risk_if_broken: str | None = typer.Option(None, "--risk-if-broken", help="Optional override risk text persisted to dependencies.yaml."),
    resolution_path: str | None = typer.Option(None, "--resolution-path", help="Optional resolution path classification, for example intra_storage or cross_org_compute_pf."),
) -> None:
    program_id = program.strip()
    proposals = list(load_dependency_proposals(program_id, programs_root=PROGRAMS_ROOT))
    proposal = next((entry for entry in proposals if entry.id == proposal_id.strip()), None)
    if proposal is None:
        raise typer.BadParameter(f"Dependency proposal '{proposal_id}' was not found in {program_id}.")
    dependency = dependency_proposal_to_dependency(
        proposal,
        dependency_type=(None if dependency_type is None or not dependency_type.strip() else _parse_dependency_type(dependency_type)),
        risk_if_broken=risk_if_broken,
        resolution_path=resolution_path,
    )
    dependencies = list(_load_current_dependencies(program_id))
    if any(entry.id == dependency.id for entry in dependencies):
        raise typer.BadParameter(f"Dependency '{dependency.id}' already exists in {program_id}.")
    dependencies.append(dependency)
    save_dependencies(program_id, tuple(dependencies), programs_root=PROGRAMS_ROOT)
    updated_proposals = update_dependency_proposal_status(
        tuple(proposals),
        proposal.id,
        status=DependencyProposalStatus.ACCEPTED,
    )
    save_dependency_proposals(program_id, updated_proposals, programs_root=PROGRAMS_ROOT)
    # ADF-W5.14: accepting a dependency proposal is a real completed action on
    # the dependency half of the risk_dependency_review golden workflow.
    try:
        record_adoption(program_id, GoldenWorkflow.RISK_DEPENDENCY_REVIEW, programs_root=PROGRAMS_ROOT)
    except Exception:
        pass
    typer.echo(f"Accepted dependency proposal {proposal.id} into programs/{program_id}/dependencies.yaml.")
    raise typer.Exit(code=0)


@app.command("dismiss")
def dismiss_dependency_proposal_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    proposal_id: str = typer.Option(..., "--id", help="Dependency proposal id."),
) -> None:
    program_id = program.strip()
    proposals = load_dependency_proposals(program_id, programs_root=PROGRAMS_ROOT)
    if not any(proposal.id == proposal_id.strip() for proposal in proposals):
        raise typer.BadParameter(f"Dependency proposal '{proposal_id}' was not found in {program_id}.")
    updated = update_dependency_proposal_status(
        proposals,
        proposal_id.strip(),
        status=DependencyProposalStatus.DISMISSED,
    )
    save_dependency_proposals(program_id, updated, programs_root=PROGRAMS_ROOT)
    # ADF-W5.14: dismissing a dependency proposal is also a real completed
    # review action on the dependency half of risk_dependency_review.
    try:
        record_adoption(program_id, GoldenWorkflow.RISK_DEPENDENCY_REVIEW, programs_root=PROGRAMS_ROOT)
    except Exception:
        pass
    typer.echo(f"Dismissed dependency proposal {proposal_id.strip()} in {program_id}.")
    raise typer.Exit(code=0)


def _filtered_proposals(program_id: str, *, status: str) -> tuple:
    proposals = load_dependency_proposals(program_id, programs_root=PROGRAMS_ROOT)
    normalized = status.strip().lower()
    if normalized == "all":
        return proposals
    allowed = {
        DependencyProposalStatus.PROPOSED.value,
        DependencyProposalStatus.ACCEPTED.value,
        DependencyProposalStatus.DISMISSED.value,
    }
    if normalized not in allowed:
        raise typer.BadParameter("--status must be proposed, accepted, dismissed, or all.")
    return tuple(proposal for proposal in proposals if proposal.status.value == normalized)


def _render_dependency_proposals(program_id: str, proposals: tuple, *, format: str) -> str:
    payload = {
        "program_id": program_id,
        "proposal_count": len(proposals),
        "proposals": [
            {
                "id": proposal.id,
                "status": proposal.status.value,
                "detection_method": proposal.detection_method,
                "from_workstream_id": proposal.from_workstream_id,
                "to_workstream_id": proposal.to_workstream_id,
                "from_item_id": proposal.from_item_id,
                "to_item_id": proposal.to_item_id,
                "from_item_title": proposal.from_item_title,
                "to_item_title": proposal.to_item_title,
                "suggested_dependency_type": proposal.suggested_dependency_type.value,
                "occurrence_count": proposal.occurrence_count,
                "confidence": proposal.confidence.value,
                "first_seen_at": proposal.first_seen_at.isoformat(),
                "last_seen_at": proposal.last_seen_at.isoformat(),
                "evidence_refs": list(proposal.evidence_refs),
                "rationale": proposal.rationale,
            }
            for proposal in proposals
        ],
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow((
            "program_id",
            "proposal_id",
            "status",
            "method",
            "from_workstream_id",
            "to_workstream_id",
            "from_item_id",
            "to_item_id",
            "type",
            "occurrence_count",
            "confidence",
            "evidence_refs",
            "rationale",
        ))
        for proposal in proposals:
            writer.writerow((
                program_id,
                proposal.id,
                proposal.status.value,
                proposal.detection_method,
                proposal.from_workstream_id,
                proposal.to_workstream_id,
                proposal.from_item_id,
                proposal.to_item_id,
                proposal.suggested_dependency_type.value,
                proposal.occurrence_count,
                proposal.confidence.value,
                "|".join(proposal.evidence_refs),
                proposal.rationale,
            ))
        return buffer.getvalue()
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    lines = [f"Dependency proposals for {program_id}"]
    if not proposals:
        lines.append("- none")
        return "\n".join(lines) + "\n"
    for proposal in proposals:
        lines.append(
            f"- {proposal.id} [{proposal.status.value}] {proposal.from_workstream_id}:{proposal.from_item_id} -> {proposal.to_workstream_id}:{proposal.to_item_id} "
            f"({proposal.suggested_dependency_type.value}, {proposal.occurrence_count} approved signals, {dependency_proposal_confidence_label(proposal)})"
        )
        lines.append(f"  {proposal.from_item_title} -> {proposal.to_item_title}")
        lines.append(f"  evidence: {', '.join(proposal.evidence_refs)}")
        lines.append(f"  rationale: {proposal.rationale}")
    return "\n".join(lines) + "\n"


def _load_scout_context(program_id: str, *, edition_name: str | None) -> dict[str, object]:
    repo_root = PROGRAMS_ROOT.parent
    resolved_edition_name, latest_entry = _resolve_latest_confirmed_edition(program_id, edition_name=edition_name, archive_root=repo_root / "archive")
    if latest_entry is None or latest_entry.snapshot_path is None:
        raise typer.BadParameter(
            f"No confirmed snapshot is available for {program_id}. Confirm an issue before running dependency scout."
        )
    resolved_edition = resolve_edition(
        resolved_edition_name,
        editions_root=EDITIONS_ROOT,
        programs_root=PROGRAMS_ROOT,
    )
    if resolved_edition is None:
        raise typer.BadParameter(f"Edition '{resolved_edition_name}' could not be resolved.")
    signal_store = build_signal_store_for_program_id(program_id, programs_root=PROGRAMS_ROOT)
    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=PROGRAMS_ROOT)
    snapshot = read_snapshot(Path(latest_entry.snapshot_path))
    signals = signal_store.read(program_id)
    review_states = signal_store.read_reviews(program_id)
    dependencies = _load_current_dependencies(program_id)
    trajectories_by_item_id = {
        item.id: trajectory_store.read(program_id, item.id)
        for item in snapshot.items
    }
    return {
        "dependencies": dependencies,
        "resolved_edition": resolved_edition,
        "review_states": review_states,
        "signals": signals,
        "snapshot": snapshot,
        "trajectories_by_item_id": trajectories_by_item_id,
    }


def _load_current_dependencies(program_id: str) -> tuple[Dependency, ...]:
    return project_dependencies(
        load_program_facts(program_id, programs_root=PROGRAMS_ROOT)
    )


def _resolve_latest_confirmed_edition(
    program_id: str,
    *,
    edition_name: str | None,
    archive_root: Path,
):
    if edition_name is not None:
        index = read_archive_index(edition_name, archive_root=archive_root)
        return edition_name, find_latest_confirmed_entry(index)
    program_archive_root = PROGRAMS_ROOT / program_id / "archive"
    if not program_archive_root.exists():
        return "", None
    latest_edition_name = ""
    latest_entry = None
    for edition_dir in sorted(program_archive_root.iterdir(), key=lambda entry: entry.name.lower()):
        if not edition_dir.is_dir():
            continue
        candidate_name = edition_dir.name
        candidate_entry = find_latest_confirmed_entry(read_archive_index(candidate_name, archive_root=archive_root))
        if candidate_entry is None:
            continue
        if latest_entry is None or candidate_entry.generated_at > latest_entry.generated_at:
            latest_edition_name = candidate_name
            latest_entry = candidate_entry
    return latest_edition_name, latest_entry


def _parse_dependency_type(value: str):
    from src.core.models_v2 import DependencyType

    return DependencyType.from_string(value)