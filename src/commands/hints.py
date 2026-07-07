from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
import typer

from src.commands import report as report_command_helpers
from src.core.config_loader import REPORTS_ROOT, load_bundle_with_mode
from src.core.archive_store import read_archive_index
from src.core.models import EditionType, Confidence
from src.core.section_proposal_store import append_hint_proposal, load_hint_proposals, HintProposal
from src.core.ado_narrative_hint_engine import generate_delta_hints, HintKind
from src.core.scorecard_trends import load_scorecard_trends, ScorecardTrend
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.overrides_store import load_overrides, merge_overrides
from src.core.evidence_engine import build_evidence
from src.core.trusted_baseline_store import load_trusted_baseline_issue

app = typer.Typer(help="Vertex narrative delta hints commands.")


@app.callback(invoke_without_command=True)
def hints_command(
    ctx: typer.Context,
    edition: str = typer.Option(..., "--edition", help="Edition name."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number. Defaults to the latest/next draft issue."),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Interactively accept/reject/modify generated hints."),
) -> None:
    """
    Generate and interactively manage narrative delta hints.
    """
    if ctx.invoked_subcommand is not None:
        return

    resolved_reports_root = REPORTS_ROOT
    resolved_archive_root = ARCHIVE_ROOT
    programs_root = resolved_reports_root.parent / "programs"
    current_time = datetime.now(timezone.utc)

    # 1. Load the program/edition bundle
    try:
        load_result = load_bundle_with_mode(
            edition,
            reports_root=resolved_reports_root,
            programs_root=programs_root,
        )
    except Exception as e:
        typer.secho(f"Failed to load bundle for edition '{edition}': {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    bundle = load_result.bundle
    program_id = bundle.program.id  # use program_id (e.g. "acme") not edition (e.g. "acme_weekly")

    # 2. Resolve issue number
    archive_index = read_archive_index(edition, archive_root=resolved_archive_root)
    resolved_issue_number = issue if issue is not None else report_command_helpers._next_issue_number(archive_index)

    typer.secho(f"Generating narrative hints for {edition} Issue {resolved_issue_number:03d}...", fg=typer.colors.GREEN, bold=True)

    # 3. Load live work items
    try:
        items, _ = report_command_helpers._load_live_work_items(bundle, current_time)
    except Exception as e:
        typer.secho(f"Failed to load live work items from ADO: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # 4. Load ETA forecasts
    try:
        eta_forecasts = report_command_helpers._load_eta_forecasts(
            edition_name=edition,
            items=items,
            as_of=current_time,
            reports_root=resolved_reports_root,
        )
    except Exception as e:
        typer.secho(f"Warning: Failed to load ETA forecasts: {e}", fg=typer.colors.YELLOW, err=True)
        eta_forecasts = {}

    # 5. Build deltas
    try:
        evidence_window_start = current_time - timedelta(days=bundle.config.ado.date_window_days)
        evidence_by_item = {item.id: build_evidence(item, evidence_window_start, current_time) for item in items}
        
        trusted_baseline_issue_number = load_trusted_baseline_issue(
            edition,
            before_issue_number=resolved_issue_number,
            programs_root=programs_root,
        )
        previous_snapshot, previous_issue_number = report_command_helpers._load_previous_snapshot(
            edition,
            resolved_issue_number,
            resolved_archive_root,
            trusted_issue_number=trusted_baseline_issue_number,
        )
        continuity_snapshot = previous_snapshot if report_command_helpers._has_usable_continuity_baseline(previous_snapshot) else None
        continuity_previous_issue_number = previous_issue_number if continuity_snapshot is not None else None
        
        deltas = report_command_helpers._build_continuity_deltas(
            current_items=items,
            previous_snapshot=continuity_snapshot,
            issue_number=resolved_issue_number,
            previous_issue_number=continuity_previous_issue_number,
            evidence_by_item=evidence_by_item,
        )
    except Exception as e:
        typer.secho(f"Failed to compute deltas: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # 6. Load Scorecard trends
    trends: dict[str | tuple[str, str], ScorecardTrend] = {}
    try:
        expected_scorecards = {
            scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
            for scorecard in bundle.config.scorecards
        }
        overrides_document, _ = merge_overrides(
            issue_number=resolved_issue_number,
            expected_scorecards=expected_scorecards,
            existing=load_overrides(edition, reports_root=resolved_reports_root, issue_number=resolved_issue_number),
        )
        scorecard_packets = report_command_helpers._build_scorecard_packets(bundle, items, continuity_snapshot)
        scorecards, _, _ = report_command_helpers._build_scorecard_data(
            bundle=bundle,
            items=items,
            evidence_by_item=evidence_by_item,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
            edition_name=edition,
            reports_root=resolved_reports_root,
        )
        
        current_dimensions = {
            (scorecard.scorecard_name, dimension.name): dimension.risk
            for scorecard in scorecards
            for dimension in scorecard.dimensions
        }
        trends = load_scorecard_trends(edition, current_dimensions, archive_root=resolved_archive_root)  # type: ignore[assignment]
    except Exception as e:
        typer.secho(f"Warning: Failed to load scorecard trends: {e}", fg=typer.colors.YELLOW, err=True)

    # 6b. Load journal signals
    try:
        from src.core.journal import read_signals
        evidence_window_start = current_time - timedelta(days=bundle.config.ado.date_window_days)
        journal_signals = read_signals(
            program_id=edition,
            start=evidence_window_start,
            end=current_time,
            programs_root=programs_root,
        )
    except Exception as e:
        typer.secho(f"Warning: Failed to load journal signals: {e}", fg=typer.colors.YELLOW, err=True)
        journal_signals = ()

    # 6c. Load governance state (current and prior)
    try:
        overrides_doc = load_overrides(
            edition,
            reports_root=resolved_reports_root,
            issue_number=resolved_issue_number,
        )
        current_governance = overrides_doc.governance if overrides_doc else None

        # Load prior issue overrides for delta detection
        if continuity_previous_issue_number is not None:
            prior_overrides_doc = load_overrides(
                edition,
                reports_root=resolved_reports_root,
                issue_number=continuity_previous_issue_number,
            )
            prior_governance = prior_overrides_doc.governance if prior_overrides_doc else None
        else:
            prior_governance = None
    except Exception as e:
        typer.secho(f"Warning: Failed to load governance state: {e}", fg=typer.colors.YELLOW, err=True)
        current_governance = None
        prior_governance = None

    # 7. Generate delta hints
    hints = generate_delta_hints(
        delta_set=deltas,
        items={wi.id: wi for wi in items},
        issue_number=resolved_issue_number,
        program=bundle.program,
        forecasts=eta_forecasts,
        trends=trends,
        signals=journal_signals,
        governance=current_governance,
        prior_governance=prior_governance,
    )

    if not hints:
        typer.secho("No narrative hints generated (no ADO or scorecard delta changes detected).", fg=typer.colors.GREEN)
        raise typer.Exit(code=0)

    # 8. Load existing decisions
    existing_proposals = load_hint_proposals(program_id, resolved_issue_number, programs_root=programs_root)
    existing_by_id = {proposal.hint_id: proposal for proposal in existing_proposals}

    # Group hints by workstream_id
    from collections import defaultdict
    hints_by_workstream = defaultdict(list)
    for hint in hints:
        hints_by_workstream[hint.workstream_id].append(hint)

    # 9. Present to user
    total_hints = len(hints)
    accepted_count = 0
    rejected_count = 0
    modified_count = 0
    pending_count = 0

    for ws_id, ws_hints in sorted(hints_by_workstream.items()):
        typer.secho(f"\n=== Workstream: {ws_id.upper()} ===", fg=typer.colors.BLUE, bold=True)
        
        for hint in ws_hints:
            typer.secho(f"\n[{hint.hint_kind.value}] (Confidence: {hint.confidence.value})", fg=typer.colors.CYAN)
            typer.secho(f"  Suggested: {hint.suggested_sentence}", fg=typer.colors.WHITE)
            
            existing = existing_by_id.get(hint.hint_id)
            if existing is not None:
                status_color = typer.colors.GREEN if existing.status in ("accepted", "modified") else typer.colors.RED
                text_to_show = existing.accepted_text if existing.status == "modified" else existing.suggested_sentence
                typer.secho(f"  Already Decided: [{existing.status.upper()}]" + (f" -> {text_to_show}" if text_to_show else ""), fg=status_color)
                
                if existing.status == "accepted":
                    accepted_count += 1
                elif existing.status == "rejected":
                    rejected_count += 1
                elif existing.status == "modified":
                    modified_count += 1
                
                if interactive:
                    redo = typer.confirm("  Change decision?", default=False)
                    if not redo:
                        continue
                else:
                    continue

            if not interactive:
                pending_count += 1
                continue

            # Prompt interactive decision
            choice = typer.prompt("  Decision? (a)ccept, (r)eject, (m)odify, (s)kip", default="a").strip().lower()
            
            if choice == "a":
                proposal = HintProposal(
                    hint_id=hint.hint_id,
                    edition=edition,
                    issue_number=resolved_issue_number,
                    workstream_id=hint.workstream_id,
                    hint_kind=hint.hint_kind.value,
                    suggested_sentence=hint.suggested_sentence,
                    status="accepted",
                    accepted_text=hint.suggested_sentence,
                )
                append_hint_proposal(proposal, program_id, resolved_issue_number, programs_root=programs_root)
                typer.secho("  Accepted!", fg=typer.colors.GREEN)
                accepted_count += 1
            elif choice == "r":
                proposal = HintProposal(
                    hint_id=hint.hint_id,
                    edition=edition,
                    issue_number=resolved_issue_number,
                    workstream_id=hint.workstream_id,
                    hint_kind=hint.hint_kind.value,
                    suggested_sentence=hint.suggested_sentence,
                    status="rejected",
                    accepted_text=None,
                )
                append_hint_proposal(proposal, program_id, resolved_issue_number, programs_root=programs_root)
                typer.secho("  Rejected!", fg=typer.colors.RED)
                rejected_count += 1
            elif choice == "m":
                modified = typer.prompt("  Enter modified sentence", default=hint.suggested_sentence).strip()
                proposal = HintProposal(
                    hint_id=hint.hint_id,
                    edition=edition,
                    issue_number=resolved_issue_number,
                    workstream_id=hint.workstream_id,
                    hint_kind=hint.hint_kind.value,
                    suggested_sentence=hint.suggested_sentence,
                    status="modified",
                    accepted_text=modified,
                )
                append_hint_proposal(proposal, program_id, resolved_issue_number, programs_root=programs_root)
                typer.secho(f"  Accepted with modification: {modified}", fg=typer.colors.GREEN)
                modified_count += 1
            else:
                typer.secho("  Skipped (remains pending)", fg=typer.colors.WHITE)
                pending_count += 1

    typer.secho("\n=== Summary ===", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"Total Hints: {total_hints}")
    typer.echo(f"Accepted: {accepted_count}")
    typer.echo(f"Modified: {modified_count}")
    typer.echo(f"Rejected: {rejected_count}")
    typer.echo(f"Pending/Skipped: {pending_count}")
    raise typer.Exit(code=0)
