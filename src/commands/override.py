from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import typer

from src.commands.confirm import _deserialize_items, _load_draft_state
from src.commands.report import _build_scorecard_packets
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.ai_proposal_store import load_ai_proposals, update_ai_proposal_status
from src.core.edition_resolver import resolve_edition
from src.core.models_v2 import AIProposal, AIProposalStatus
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides, get_overrides_path, load_overrides, save_overrides
from src.core.snapshot_store import ARCHIVE_ROOT
from src.commands.report import _load_previous_snapshot
from src.core.models import RiskLevel
from src.core.trusted_baseline_store import load_trusted_baseline_issue


@dataclass(frozen=True, slots=True)
class OverrideCandidate:
    scorecard_name: str
    dimension_name: str
    override: DimensionOverride
    packet: object
    workstream_id: str | None = None
    ai_proposal: AIProposal | None = None


@dataclass(frozen=True, slots=True)
class ProposalStatusUpdate:
    proposal_id: str
    new_status: AIProposalStatus


@dataclass(frozen=True, slots=True)
class OverrideResult:
    issue_number: int
    changed_count: int
    kept_count: int
    skipped_count: int
    saved_path: Path
    backup_path: Path


def override_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    dimension: str | None = typer.Option(None, "--dimension", help="Optional single dimension name to edit."),
) -> None:
    result = run_override(
        edition_name=edition,
        dimension_filter=dimension,
    )
    typer.echo(f"OVERRIDE COMPLETE — {result.changed_count + result.kept_count}/{result.changed_count + result.kept_count + result.skipped_count} dimensions set")
    typer.echo(f"  Changed: {result.changed_count}  |  Kept: {result.kept_count}  |  Skipped: {result.skipped_count}")
    typer.echo(f"  Saved to: {result.saved_path}")
    typer.echo(f"  Backup: {result.backup_path}")
    typer.echo("\n  Run `vertex report --dry-run` to preview the updated published output.")


def run_override(
    edition_name: str,
    dimension_filter: str | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    output_root: Path | None = None,
) -> OverrideResult:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=resolved_reports_root.parent / "programs",
    )
    overrides_document = load_overrides(edition_name, reports_root=resolved_reports_root)
    if overrides_document is None or overrides_document.issue_number is None:
        raise typer.BadParameter("overrides.yaml is missing. Run `vertex report --dry-run` first.")

    draft_state = _load_draft_state(edition_name, overrides_document.issue_number, programs_root=resolved_reports_root.parent / "programs")
    items = _deserialize_items(tuple(draft_state.get("items", [])))
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=overrides_document.issue_number,
        programs_root=resolved_reports_root.parent / "programs",
    )
    previous_snapshot, _ = _load_previous_snapshot(
        edition_name=edition_name,
        issue_number=overrides_document.issue_number,
        archive_root=resolved_archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    scorecard_packets = _build_scorecard_packets(bundle, items, previous_snapshot)
    pending_ai_proposals = _load_pending_ai_proposals_by_dimension(
        edition_name,
        reports_root=resolved_reports_root,
    )
    candidates = _select_candidates(
        overrides_document,
        scorecard_packets,
        pending_ai_proposals,
        dimension_filter,
    )

    typer.echo(f"VERTEX INTERACTIVE OVERRIDE — {bundle.config.edition.title} (Issue {overrides_document.issue_number})")
    typer.echo("=" * 58)
    typer.echo()

    changed_count = 0
    kept_count = 0
    skipped_count = 0
    updated_document = overrides_document
    proposal_updates: list[ProposalStatusUpdate] = []

    for index, candidate in enumerate(candidates, start=1):
        typer.echo(f"Dimension {index}/{len(candidates)}: {candidate.dimension_name}")
        typer.echo("-" * 36)
        typer.echo(_format_evidence(candidate.packet))
        derived_risk = getattr(candidate.packet, "derived_risk", RiskLevel.UNKNOWN)
        typer.echo(f"Derived risk: {_format_risk(derived_risk)}")
        typer.echo(f"Current override: {_format_risk(candidate.override.risk)}")
        typer.echo(f"Prior confirmed risk: {_format_risk(candidate.packet.prior_confirmed_risk)}")  # type: ignore[attr-defined]
        typer.echo(f"Detail section hidden: {'Yes' if candidate.override.hide_details else 'No'}")
        if candidate.ai_proposal is not None:
            typer.echo(_format_ai_proposal(candidate.ai_proposal))
        typer.echo()
        typer.echo(_choice_help(candidate.ai_proposal is not None))
        choice = _prompt_choice(default="K", include_ai_proposed=candidate.ai_proposal is not None)
        if choice == "S":
            skipped_count += 1
            typer.echo()
            continue

        existing_summary = candidate.override.summary or ""
        proposal_update = _proposal_status_update_for_choice(choice, candidate.ai_proposal)
        new_risk = _resolve_choice(choice, candidate.override.risk, candidate.ai_proposal)
        summary = typer.prompt("  Summary (optional, ≤50 words)", default=existing_summary, show_default=False).strip()
        if len(summary.split()) > 50:
            raise typer.BadParameter("Summary must be 50 words or fewer.")
        hide_details = _prompt_yes_no(
            "  Hide detail section from published output/review?",
            default=candidate.override.hide_details,
        )

        if (
            new_risk == candidate.override.risk
            and summary == existing_summary
            and hide_details == candidate.override.hide_details
        ):
            kept_count += 1
            if proposal_update is not None:
                proposal_updates.append(proposal_update)
            typer.echo(
                f"\n  ✅ {candidate.dimension_name} → {_format_override_result(new_risk, derived_risk, hide_details)}{_format_keep_suffix(proposal_update)}\n"
            )
            continue

        changed_count += 1
        updated_document = _update_override(
            updated_document,
            candidate.scorecard_name,
            candidate.dimension_name,
            new_risk,
            summary or None,
            hide_details,
        )
        if proposal_update is not None:
            proposal_updates.append(proposal_update)
        typer.echo(
            f"\n  ✅ {candidate.dimension_name} → {_format_override_result(new_risk, derived_risk, hide_details)}{_format_resolution_suffix(proposal_update)}\n"
        )

    overrides_path = get_overrides_path(edition_name, reports_root=resolved_reports_root)
    backup_path = overrides_path.with_suffix(overrides_path.suffix + ".bak")
    shutil.copy2(overrides_path, backup_path)
    saved_path = save_overrides(edition_name, updated_document, reports_root=resolved_reports_root)

    resolved = resolve_edition(
        edition_name,
        programs_root=resolved_reports_root.parent / "programs",
    )
    if resolved is not None and proposal_updates:
        actor = _default_actor_identity()
        decision_time = _now_utc()
        for proposal_update in proposal_updates:
            update_ai_proposal_status(
                resolved.paths.program_id,
                proposal_update.proposal_id,
                new_status=proposal_update.new_status,
                resolved_by=actor,
                resolved_at=decision_time,
                programs_root=resolved_reports_root.parent / "programs",
            )

    return OverrideResult(
        issue_number=updated_document.issue_number or 0,
        changed_count=changed_count,
        kept_count=kept_count,
        skipped_count=skipped_count,
        saved_path=saved_path,
        backup_path=backup_path,
    )


def _select_candidates(
    overrides_document: OverridesDocument,
    scorecard_packets: dict[str, dict[str, object]],
    pending_ai_proposals: dict[tuple[str, str], AIProposal],
    dimension_filter: str | None,
) -> list[OverrideCandidate]:
    candidates = [
        OverrideCandidate(
            scorecard_name=scorecard.name,
            dimension_name=dimension.name,
            override=dimension,
            packet=scorecard_packets[scorecard.name][dimension.name],
            ai_proposal=pending_ai_proposals.get((scorecard.name, dimension.name)),
            workstream_id=(
                pending_ai_proposals.get((scorecard.name, dimension.name)).workstream_id  # type: ignore[union-attr]
                if pending_ai_proposals.get((scorecard.name, dimension.name)) is not None
                else None
            ),
        )
        for scorecard in overrides_document.scorecards
        for dimension in scorecard.dimensions
    ]
    if dimension_filter is None:
        return candidates

    normalized = dimension_filter.strip().lower()
    matches = [
        candidate
        for candidate in candidates
        if candidate.dimension_name.lower() == normalized
        or f"{candidate.scorecard_name} / {candidate.dimension_name}".lower() == normalized
    ]
    if not matches:
        raise typer.BadParameter(f"Dimension '{dimension_filter}' not found in overrides.yaml.")
    if len(matches) > 1:
        joined = ", ".join(f"{candidate.scorecard_name} / {candidate.dimension_name}" for candidate in matches)
        raise typer.BadParameter(f"Dimension '{dimension_filter}' is ambiguous. Use one of: {joined}")
    return matches


def _format_evidence(packet: object) -> str:
    items_by_risk = getattr(packet, "items_by_risk", {})
    risk_parts = ", ".join(f"{count} {risk.title()}" for risk, count in items_by_risk.items())
    summary_parts = [f"Evidence: {getattr(packet, 'total_items', 0)} items"]
    if risk_parts:
        summary_parts.append(f"({risk_parts})")
    summary_parts.append(f"derived {_format_risk(getattr(packet, 'derived_risk', RiskLevel.UNKNOWN))}")
    summary_parts.append(f"{getattr(packet, 'stale_count', 0)} stale")
    summary_parts.append(f"{getattr(packet, 'overdue_count', 0)} overdue ETA")
    if getattr(packet, "blocked_count", 0):
        summary_parts.append(f"{getattr(packet, 'blocked_count', 0)} blocked")
    return ", ".join(summary_parts)


def _format_ai_proposal(proposal: AIProposal) -> str:
    summary_parts = [
        f"AI proposal: {_format_risk(proposal.synthesis.proposed_risk)}",
        f"confidence {_format_risk_level_label(proposal.synthesis.confidence.value)}",
    ]
    lines = [" | ".join(summary_parts)]
    lines.append(f"Assessment: {proposal.synthesis.overall_assessment}")
    if proposal.synthesis.evidence_refs:
        lines.append(f"Evidence refs: {', '.join(proposal.synthesis.evidence_refs)}")
    if proposal.synthesis.recommended_actions:
        lines.append(f"Suggested actions: {'; '.join(proposal.synthesis.recommended_actions)}")
    return "\n".join(lines)


def _choice_help(include_ai_proposed: bool) -> str:
    if include_ai_proposed:
        return "  [A]I-proposed  [L]ow  [M]edium  [H]igh  [D]one  [C]lear override  [S]kip  [K]eep current"
    return "  [L]ow  [M]edium  [H]igh  [D]one  [C]lear override  [S]kip  [K]eep current"


def _prompt_choice(default: str, *, include_ai_proposed: bool) -> str:
    valid_choices = {"L", "M", "H", "D", "C", "S", "K"}
    if include_ai_proposed:
        valid_choices.add("A")
    while True:
        choice = typer.prompt("  Your choice", default=default).strip().upper()
        if choice in valid_choices:
            return choice
        typer.echo(
            "  Enter one of: A, L, M, H, D, C, S, K"
            if include_ai_proposed
            else "  Enter one of: L, M, H, D, C, S, K"
        )


def _resolve_choice(
    choice: str,
    current_risk: RiskLevel | None,
    ai_proposal: AIProposal | None,
) -> RiskLevel | None:
    if choice == "A" and ai_proposal is not None:
        return ai_proposal.synthesis.proposed_risk
    if choice == "K":
        return current_risk
    if choice == "L":
        return RiskLevel.LOW
    if choice == "M":
        return RiskLevel.MEDIUM
    if choice == "H":
        return RiskLevel.HIGH
    if choice == "D":
        return RiskLevel.DONE
    if choice == "C":
        return None
    return current_risk


def _prompt_yes_no(prompt: str, default: bool) -> bool:
    default_choice = "Y" if default else "N"
    while True:
        choice = typer.prompt(prompt, default=default_choice).strip().upper()
        if choice in {"Y", "YES"}:
            return True
        if choice in {"N", "NO"}:
            return False
        typer.echo("  Enter Y or N")


def _format_risk(risk: RiskLevel | None) -> str:
    return risk.value.title() if risk is not None else "None"


def _format_risk_level_label(value: str) -> str:
    return value.replace("_", " ").title()


def _format_override_result(
    override_risk: RiskLevel | None,
    derived_risk: RiskLevel,
    hide_details: bool,
) -> str:
    if override_risk is None:
        label = f"Derived ({derived_risk.value.title()})"
    else:
        label = override_risk.value.title()
    return f"{label} | details hidden" if hide_details else label


def _format_keep_suffix(proposal_update: ProposalStatusUpdate | None) -> str:
    if proposal_update is None:
        return " (kept)"
    return f" (kept, AI proposal {proposal_update.new_status.value})"


def _format_resolution_suffix(proposal_update: ProposalStatusUpdate | None) -> str:
    if proposal_update is None:
        return ""
    return f" (AI proposal {proposal_update.new_status.value})"


def _proposal_status_update_for_choice(
    choice: str,
    ai_proposal: AIProposal | None,
) -> ProposalStatusUpdate | None:
    if ai_proposal is None or choice == "S":
        return None
    if choice == "A":
        return ProposalStatusUpdate(proposal_id=ai_proposal.id, new_status=AIProposalStatus.ACCEPTED)
    return ProposalStatusUpdate(proposal_id=ai_proposal.id, new_status=AIProposalStatus.REJECTED)


def _load_pending_ai_proposals_by_dimension(
    edition_name: str,
    *,
    reports_root: Path,
) -> dict[tuple[str, str], AIProposal]:
    resolved = resolve_edition(
        edition_name,
        programs_root=reports_root.parent / "programs",
    )
    if resolved is None:
        return {}

    pending_by_workstream: dict[str, AIProposal] = {}
    for proposal in load_ai_proposals(
        resolved.paths.program_id,
        status=AIProposalStatus.PENDING,
        programs_root=reports_root.parent / "programs",
    ):
        pending_by_workstream[proposal.workstream_id] = proposal

    proposals_by_dimension: dict[tuple[str, str], AIProposal] = {}
    for scorecard in resolved.scorecards:
        for dimension in scorecard.dimensions:
            dim_proposal = pending_by_workstream.get(dimension.workstream_id)
            if dim_proposal is not None:
                proposals_by_dimension[(scorecard.name, dimension.name)] = dim_proposal
    return proposals_by_dimension


def _default_actor_identity() -> str:
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _update_override(
    document: OverridesDocument,
    scorecard_name: str,
    dimension_name: str,
    new_risk: RiskLevel | None,
    summary: str | None,
    hide_details: bool,
) -> OverridesDocument:
    updated_scorecards: list[ScorecardOverrides] = []
    for scorecard in document.scorecards:
        if scorecard.name != scorecard_name:
            updated_scorecards.append(scorecard)
            continue
        updated_dimensions: list[DimensionOverride] = []
        for dimension in scorecard.dimensions:
            if dimension.name != dimension_name:
                updated_dimensions.append(dimension)
                continue
            updated_dimensions.append(
                DimensionOverride(
                    name=dimension.name,
                    risk=new_risk,
                    note=dimension.note,
                    summary=summary,
                    hide_details=hide_details,
                )
            )
        updated_scorecards.append(ScorecardOverrides(name=scorecard.name, dimensions=tuple(updated_dimensions), footnote=scorecard.footnote))

    return OverridesDocument(
        issue_number=document.issue_number,
        top_3_now=document.top_3_now,
        scorecards=tuple(updated_scorecards),
        removed_dimensions=document.removed_dimensions,
    )
