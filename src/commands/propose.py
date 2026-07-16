from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
from typing import Any, Callable

import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.blurb_generator import WorkstreamBlurb, generate_workstream_blurb
from src.ai.exec_summary_drafter import ExecSummaryDraft, draft_exec_summary
from src.commands import report as report_command_helpers
from src.core.ban_list_validator import find_ban_list_violations
from src.core.chronicle import ProgramEvent, append_program_event
from src.core.claim_tracker import load_open_claims
from src.core.evidence_assembler import assemble_section_evidence_brief
from src.core.gather_state_store import load_gather_state
from src.core.journal import PROGRAMS_ROOT
from src.core.knowledge_store import load_program_knowledge
from src.core.models import Confidence, ReviewStatus
from src.core.section_proposal_store import append_proposal, build_section_revision_proposal_id, get_proposals_path, supersede_pending_proposals
from src.core.models_v2 import SectionRevisionProposal, SectionRevisionStatus
from src.core.pipeline import run_pipeline
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.stages.compute_stage import ComputeStage
from src.core.stages.fetch_stage import FetchStage
from src.core.stages.narrative_stage import NarrativeStage
from src.core.stages.resolution_stage import ResolutionStage
from src.core.store_factory import build_signal_store_for_program_id
from src.commands.report_ai import _build_section_evidence_bundle
from src.core.evidence_store import load_approved_evidence_by_lane


@dataclass(frozen=True, slots=True)
class ProposalArtifacts:
    issue_number: int
    proposal_count: int
    proposals_path: Path | None
    warnings: tuple[str, ...] = ()
    preview_lines: tuple[str, ...] = ()


def generate_section_revision_proposals(
    *,
    edition_name: str,
    ai: bool = False,
    dry_run: bool = False,
    offline: bool = False,
    steering: str | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    programs_root: Path | None = None,
    as_of: datetime | None = None,
    work_item_loader: Callable[..., Any] | None = None,
    kusto_query_executor: Callable[..., Any] | None = None,
    create_ai_client: Callable[..., Any] | None = None,
    draft_exec_summary_runner: Callable[..., ExecSummaryDraft | None] | None = None,
    generate_workstream_blurb_runner: Callable[..., WorkstreamBlurb | None] | None = None,
) -> ProposalArtifacts:
    request_ctx = report_command_helpers._build_stage_request_context(
        edition_name=edition_name,
        issue_number=None,
        reseed=False,
        no_seed=False,
        dry_run=True,
        offline=offline,
        diff_mode=False,
        as_of=as_of,
        edition_type_override=None,
        lookback_range=None,
        section_filter_ids=(),
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=work_item_loader,
        kusto_query_executor=kusto_query_executor,
        open_browser=False,
    )
    ctx = run_pipeline(
        (ResolutionStage(), FetchStage(), ComputeStage(), NarrativeStage()),
        request_ctx,
    )

    if (
        ctx.bundle is None
        or ctx.programs_root is None
        or ctx.resolved_issue_number is None
        or ctx.data_as_of is None
        or ctx.started_at is None
        or ctx.exec_summary_text is None
        or ctx.workstream_blurbs is None
        or ctx.evidence_by_item is None
        or ctx.scorecards is None
        or ctx.scorecard_packets is None
        or ctx.overrides_document is None
    ):
        raise RuntimeError("Proposal generation requires resolved report context through NarrativeStage.")

    program_id = ctx.resolved_v2.program.id if ctx.resolved_v2 is not None else ctx.bundle.config.program_id
    signal_store = build_signal_store_for_program_id(program_id, programs_root=ctx.programs_root)
    journal_signals = signal_store.read(program_id, end=ctx.data_as_of)
    review_states = signal_store.read_reviews(program_id)
    evidence_window_start = ctx.data_as_of - timedelta(days=ctx.bundle.config.ado.date_window_days)
    approved_signals = tuple(
        signal
        for signal in journal_signals
        if signal.timestamp >= evidence_window_start
        and signal_is_approved_for_evidence(signal, review_states)
    )
    vitality_scores = ()
    if ctx.resolved_v2 is not None:
        vitality_snapshot, _ = report_command_helpers._build_v2_vitality_snapshot(
            resolved_v2=ctx.resolved_v2,
            items=ctx.items,
            as_of=ctx.data_as_of,
            programs_root=ctx.programs_root,
        )
        vitality_scores = vitality_snapshot.scores
    claims = load_open_claims(program_id, programs_root=ctx.programs_root)
    signal_people_directory = load_program_knowledge(program_id, programs_root=ctx.programs_root).people_directory
    signal_source_confidence_order = ctx.resolved_v2.program.source_confidence_order if ctx.resolved_v2 is not None else ()

    workstream_data = report_command_helpers._build_workstream_data(
        issue_number=ctx.resolved_issue_number,
        bundle=ctx.bundle,
        edition_type=ctx.resolved_edition_type,
        items=ctx.items,
        scorecards=ctx.scorecards,
        scorecard_packets=ctx.scorecard_packets,
        overrides_document=ctx.overrides_document,
        workstream_blurbs=ctx.workstream_blurbs,
        dependency_cascades=(ctx.signal_context.dependency_cascades if ctx.signal_context is not None else ()),
        review_status=ReviewStatus(issue_number=ctx.resolved_issue_number, sections=()),
        evidence_by_item=ctx.evidence_by_item,
        item_urls=report_command_helpers._build_item_urls(ctx.bundle, ctx.items),
        eta_forecasts=ctx.eta_forecasts,
        approved_signals=approved_signals,
        workstreams=(ctx.resolved_v2.workstreams if ctx.resolved_v2 is not None else ()),
    )
    if ctx.section_filter_ids:
        # Explicit --sections flag: filter to only the requested workstream sections.
        section_filter_set = set(ctx.section_filter_ids)
        workstream_data = tuple(workstream for workstream in workstream_data if workstream.section_id in section_filter_set)
    elif ctx.continuity_chapters:
        # Chapter-based layout: ctx.visible_section_ids holds chapter IDs, not workstream IDs.
        # Use workstream_blurbs keys, which are correctly populated with bridge-visible workstream IDs.
        workstream_blurb_keys = set(ctx.workstream_blurbs) if ctx.workstream_blurbs else set()
        workstream_data = tuple(workstream for workstream in workstream_data if workstream.section_id in workstream_blurb_keys)
    elif ctx.visible_section_ids is not None:
        workstream_data = tuple(workstream for workstream in workstream_data if workstream.section_id in ctx.visible_section_ids)

    exec_evidence = assemble_section_evidence_brief(
        "exec_summary",
        None,
        current_items=ctx.items,
        previous_snapshot=ctx.previous_snapshot,
        journal_signals=journal_signals,
        vitality_scores=vitality_scores,
        kpi_tiles=(),
        claims=claims,
        issue_number=ctx.resolved_issue_number,
        as_of=ctx.data_as_of,
        people_directory=signal_people_directory,
        source_confidence_order=signal_source_confidence_order,
    )
    proposals = [
        SectionRevisionProposal(
            proposal_id=build_section_revision_proposal_id(
                edition_name,
                ctx.resolved_issue_number,
                section_id="exec_summary",
                generated_at=ctx.started_at,
            ),
            edition_id=edition_name,
            issue_number=ctx.resolved_issue_number,
            section_id="exec_summary",
            current_text=ctx.exec_summary_text,
            proposed_text=None,
            evidence_brief=exec_evidence,
            status=SectionRevisionStatus.PENDING,
            generated_at=ctx.started_at,
            source_hash=_source_hash(ctx.exec_summary_text),
        )
    ]
    for workstream in workstream_data:
        section_workstream_id = report_command_helpers._section_workstream_id(
            workstream,
            workstreams=(ctx.resolved_v2.workstreams if ctx.resolved_v2 is not None else ()),
        )
        current_text = ctx.workstream_blurbs.get(workstream.section_id, workstream.blurb)
        evidence_brief = assemble_section_evidence_brief(
            workstream.section_id,
            section_workstream_id,
            current_items=workstream.items,
            previous_snapshot=ctx.previous_snapshot,
            journal_signals=journal_signals,
            vitality_scores=vitality_scores,
            kpi_tiles=workstream.kpi_tiles,
            claims=claims,
            issue_number=ctx.resolved_issue_number,
            as_of=ctx.data_as_of,
            people_directory=signal_people_directory,
            source_confidence_order=signal_source_confidence_order,
        )
        proposals.append(
            SectionRevisionProposal(
                proposal_id=build_section_revision_proposal_id(
                    edition_name,
                    ctx.resolved_issue_number,
                    section_id=workstream.section_id,
                    generated_at=ctx.started_at,
                ),
                edition_id=edition_name,
                issue_number=ctx.resolved_issue_number,
                section_id=workstream.section_id,
                current_text=current_text,
                proposed_text=None,
                evidence_brief=evidence_brief,
                status=SectionRevisionStatus.PENDING,
                generated_at=ctx.started_at,
                source_hash=_source_hash(current_text),
            )
        )

    warnings: list[str] = []
    # FR-SG-47: record PM steering theme as a ProgramEvent for provenance (always, regardless of ai.enabled)
    if steering and not dry_run:
        append_program_event(
            program_id,
            ProgramEvent(
                event_type="pm_steering",
                event_date=ctx.started_at,
                description=f"PM steering: {steering}",
                source="vertex propose --steering",
                actors=(),
                linked_dimensions=(),
                event_id=None,
            ),
            programs_root=ctx.programs_root,
        )
    gather_warning = _build_stale_gather_warning(
        program_id=program_id,
        programs_root=ctx.programs_root,
        as_of=ctx.data_as_of,
    )
    if gather_warning is not None:
        warnings.append(gather_warning)

    if ai:
        proposals, ai_warnings = _populate_ai_proposal_text(  # type: ignore[assignment]
            proposals=tuple(proposals),
            ctx=ctx,
            workstream_data=workstream_data,
            program_id=program_id,
            approved_signals=approved_signals,
            create_ai_client=(create_ai_client or report_command_helpers._create_ai_client),
            draft_exec_summary_runner=(draft_exec_summary_runner or draft_exec_summary),
            generate_workstream_blurb_runner=(generate_workstream_blurb_runner or generate_workstream_blurb),
        )
        warnings.extend(ai_warnings)
    preview_lines = tuple(
        _build_preview_line(proposal)
        for proposal in proposals
    )
    if dry_run:
        return ProposalArtifacts(
            issue_number=ctx.resolved_issue_number,
            proposal_count=len(proposals),
            proposals_path=None,
            warnings=tuple(warnings),
            preview_lines=preview_lines,
        )

    superseded = supersede_pending_proposals(
        program_id,
        ctx.resolved_issue_number,
        programs_root=ctx.programs_root,
        resolved_at=ctx.started_at,
    )
    if superseded:
        warnings.append(f"Superseded {len(superseded)} pending proposal(s) before writing new proposals.")
    proposals_path: Path | None = None
    for proposal in proposals:
        proposals_path = append_proposal(
            proposal,
            program_id,
            ctx.resolved_issue_number,
            programs_root=ctx.programs_root,
        )
    return ProposalArtifacts(
        issue_number=ctx.resolved_issue_number,
        proposal_count=len(proposals),
        proposals_path=(proposals_path or get_proposals_path(program_id, ctx.resolved_issue_number, programs_root=ctx.programs_root)),
        warnings=tuple(warnings),
        preview_lines=preview_lines,
    )


def propose_command(
    edition: str = typer.Option(..., "--edition", help="Edition id, e.g. myprogram_weekly."),
    ai: bool = typer.Option(False, "--ai/--no-ai", help="Enable AI text generation for proposals."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview proposal briefs without writing proposals.jsonl."),
    offline: bool = typer.Option(False, "--offline/--no-offline", help="Use cached snapshot instead of live ADO fetch."),
    steering: str = typer.Option(None, "--steering", help="PM strategic theme to apply as narrative emphasis (recorded as a ProgramEvent for provenance)."),
) -> None:
    artifacts = generate_section_revision_proposals(
        edition_name=edition,
        ai=ai,
        dry_run=dry_run,
        offline=offline,
        steering=steering or None,
    )
    if dry_run:
        for line in artifacts.preview_lines:
            typer.echo(line)
        typer.echo(f"Dry-run: generated {artifacts.proposal_count} proposal briefs. No file written.")
    typer.echo(f"Issue {artifacts.issue_number:03d} | proposals: {artifacts.proposal_count}")
    for warning in artifacts.warnings:
        typer.echo(f"Warning: {warning}")
    if artifacts.proposals_path is None:
        typer.echo("Dry-run: proposals.jsonl not written.")
    else:
        typer.echo(f"Proposals: {artifacts.proposals_path}")
    raise typer.Exit(code=0)


def _source_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _build_stale_gather_warning(*, program_id: str, programs_root: Path, as_of: datetime) -> str | None:
    gather_state = load_gather_state(program_id, programs_root=programs_root)
    if gather_state is None:
        return None
    age = as_of - gather_state.gathered_at
    if age <= timedelta(hours=24):
        return None
    hours_old = max(24, int(age.total_seconds() // 3600))
    return (
        f"Last gather was {hours_old} hours ago. Consider running 'vertex gather --program {program_id}' "
        "before proposing to get fresh signals."
    )


def _populate_ai_proposal_text(
    *,
    proposals: tuple[SectionRevisionProposal, ...],
    ctx: Any,
    workstream_data: tuple[Any, ...],
    program_id: str,
    approved_signals: tuple[Any, ...],
    create_ai_client: Callable[..., Any],
    draft_exec_summary_runner: Callable[..., ExecSummaryDraft | None],
    generate_workstream_blurb_runner: Callable[..., WorkstreamBlurb | None],
) -> tuple[tuple[SectionRevisionProposal, ...], tuple[str, ...]]:
    if get_ai_mode() == AIMode.DISABLED:
        return proposals, ("AI skipped: invocation AI is disabled by --no-ai / AIMode.DISABLED.",)
    if not getattr(ctx.bundle.config.ai, "enabled", False):
        return proposals, ("AI skipped: program AI is disabled; storing evidence briefs only.",)

    budget_per_client = _ai_budget_per_client(ctx.bundle)
    temperature = ctx.bundle.config.ai.temperature
    exec_client = create_ai_client(
        deployment=ctx.bundle.config.ai.exec_summary_deployment,
        temperature=temperature,
        budget_usd=budget_per_client,
    )
    blurb_client = create_ai_client(
        deployment=ctx.bundle.config.ai.blurb_deployment,
        temperature=temperature,
        budget_usd=budget_per_client,
    )
    workstream_by_section = {workstream.section_id: workstream for workstream in workstream_data}
    approved_signal_ids = {signal.id for signal in approved_signals}
    m365_evidence_by_lane = load_approved_evidence_by_lane(
        program_id,
        programs_root=ctx.programs_root,
        approved_signal_ids=approved_signal_ids,
    )
    warnings: list[str] = []
    populated: list[SectionRevisionProposal] = []

    for proposal in proposals:
        if proposal.evidence_brief.confidence is Confidence.LOW:
            warnings.append(f"AI skipped for {proposal.section_id}: insufficient evidence (confidence=low).")
            populated.append(proposal)
            continue
        if proposal.section_id == "exec_summary":
            draft = draft_exec_summary_runner(
                client=exec_client,
                items=ctx.items,
                deltas=ctx.deltas,
                editorial_rules=ctx.bundle.editorial_rules,
                program_id=program_id,
                edition_type=ctx.resolved_edition_type,
                program_context=ctx.bundle.program_context,
                supplemental_context=(
                    f"ADO delta summary: {proposal.evidence_brief.ado_delta_summary}",
                    *tuple(f"Signal: {signal}" for signal in proposal.evidence_brief.top_signals),
                    *(() if proposal.evidence_brief.kpi_summary is None else (f"KPI summary: {proposal.evidence_brief.kpi_summary}",)),
                    f"Vitality summary: {proposal.evidence_brief.vitality_summary}",
                ),
                programs_root=ctx.programs_root,
            )
            if draft is None:
                populated.append(proposal)
            else:
                populated.append(
                    _with_ai_text_or_warning(
                        proposal=proposal,
                        proposed_text=draft.text,
                        prompt_version=draft.prompt_version,
                        editorial_rules=ctx.bundle.editorial_rules,
                        warnings=warnings,
                    )
                )
            continue

        workstream = workstream_by_section.get(proposal.section_id)
        if workstream is None:
            populated.append(proposal)
            continue
        section_workstream_id = report_command_helpers._section_workstream_id(
            workstream,
            workstreams=(ctx.resolved_v2.workstreams if ctx.resolved_v2 is not None else ()),
        )
        ai_blurb = generate_workstream_blurb_runner(
            client=blurb_client,
            workstream_name=workstream.title,
            items=workstream.items,
            evidence_by_item=ctx.evidence_by_item,
            deltas=report_command_helpers._relevant_item_deltas(ctx.deltas, {item.id for item in workstream.items}),
            editorial_rules=ctx.bundle.editorial_rules,
            program_id=program_id,
            edition_type=ctx.resolved_edition_type,
            program_context=ctx.bundle.program_context,
            supplemental_context=(
                f"ADO delta summary: {proposal.evidence_brief.ado_delta_summary}",
                *tuple(f"Signal: {signal}" for signal in proposal.evidence_brief.top_signals),
                *(() if proposal.evidence_brief.kpi_summary is None else (f"KPI summary: {proposal.evidence_brief.kpi_summary}",)),
                f"Vitality summary: {proposal.evidence_brief.vitality_summary}",
            ),
            workstream_evidence_bundle=(
                _build_section_evidence_bundle(
                    lane_id=section_workstream_id,
                    ai_context=ctx.signal_context,
                    m365_evidence_by_lane=m365_evidence_by_lane,
                    item_ids={item.id for item in workstream.items},
                )
                if ctx.signal_context is not None and section_workstream_id
                else None
            ),
            programs_root=ctx.programs_root,
        )
        if ai_blurb is None:
            populated.append(proposal)
            continue
        populated.append(
            _with_ai_text_or_warning(
                proposal=proposal,
                proposed_text=ai_blurb.text,
                prompt_version=ai_blurb.prompt_version,
                editorial_rules=ctx.bundle.editorial_rules,
                warnings=warnings,
            )
        )

    return tuple(populated), tuple(warnings)


def _with_ai_text(proposal: SectionRevisionProposal, proposed_text: str, prompt_version: str) -> SectionRevisionProposal:
    return SectionRevisionProposal(
        proposal_id=proposal.proposal_id,
        edition_id=proposal.edition_id,
        issue_number=proposal.issue_number,
        section_id=proposal.section_id,
        current_text=proposal.current_text,
        proposed_text=proposed_text,
        evidence_brief=proposal.evidence_brief,
        status=proposal.status,
        generated_at=proposal.generated_at,
        resolved_at=proposal.resolved_at,
        accepted_text=proposal.accepted_text,
        rejection_reason=proposal.rejection_reason,
        source_hash=proposal.source_hash,
        ai_model_used=prompt_version,
        ai_cost_usd=proposal.ai_cost_usd,
    )


def _with_ai_text_or_warning(
    *,
    proposal: SectionRevisionProposal,
    proposed_text: str,
    prompt_version: str,
    editorial_rules: Any,
    warnings: list[str],
) -> SectionRevisionProposal:
    violations = find_ban_list_violations({proposal.section_id: proposed_text}, editorial_rules)
    if not violations:
        return _with_ai_text(proposal, proposed_text, prompt_version)
    phrases = ", ".join(sorted({violation.phrase for violation in violations}, key=str.lower))
    warnings.append(
        f"AI skipped for {proposal.section_id}: generated text violates the editorial ban-list ({phrases})."
    )
    return proposal


def _build_preview_line(proposal: SectionRevisionProposal) -> str:
    summary = (
        f"[{proposal.section_id}] {proposal.evidence_brief.ado_delta_summary} "
        f"signals={len(proposal.evidence_brief.top_signals)} "
        f"confidence={proposal.evidence_brief.confidence.value}"
    )
    if proposal.proposed_text is None:
        return summary
    return f"{summary} | proposal={proposal.proposed_text}"


def _ai_budget_per_client(bundle: Any) -> float:
    deployments = {
        deployment
        for deployment in (
            getattr(bundle.config.ai, "exec_summary_deployment", None),
            getattr(bundle.config.ai, "blurb_deployment", None),
        )
        if deployment
    }
    return bundle.config.ai.budget_usd_per_run / max(1, len(deployments))
