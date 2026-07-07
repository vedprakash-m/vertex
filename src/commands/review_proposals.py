from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import webbrowser

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape
import typer

from src.commands.report_output import _write_output_text
from src.core.edition_resolver import get_program_output_dir, resolve_edition_paths
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.dependency_scout import DependencyProposal, DependencyProposalStatus, load_dependency_proposals
from src.core.exceptions import RenderError
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS
from src.core.section_proposal_store import load_proposals
from src.core.models_v2 import SectionRevisionProposal, SectionRevisionStatus, Signal
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.models import ArchiveIndex
from src.core.store_factory import build_signal_store_for_program_id


TEMPLATES_ROOT = Path(__file__).resolve().parents[2] / "templates"


@dataclass(frozen=True, slots=True)
class ProposalReviewSignal:
    signal_id: str
    anchor_id: str
    timestamp: str | None
    source: str | None
    confidence: str | None
    workstream_id: str | None
    text: str | None


@dataclass(frozen=True, slots=True)
class ProposalReviewSection:
    section_id: str
    title: str
    status: str
    status_label: str
    resolved_at: str | None
    accepted_text: str | None
    rejection_reason: str | None
    current_text: str
    proposed_text: str | None
    ado_delta_summary: str
    top_signals: tuple[ProposalReviewSignal, ...]
    kpi_summary: str | None
    stale_claims: tuple[str, ...]
    vitality_summary: str
    confidence: str
    accept_command: str | None
    accept_modified_command: str | None
    reject_command: str | None

    @property
    def proposed_text_display(self) -> str:
        proposal_text = (self.proposed_text or "").strip()
        if proposal_text:
            return proposal_text
        return "No AI proposal (evidence brief below)"


@dataclass(frozen=True, slots=True)
class ProposalDecisionSummary:
    pending_count: int
    accepted_count: int
    accepted_modified_count: int
    rejected_count: int
    superseded_count: int
    recent_resolutions: tuple[SectionRevisionProposal, ...]


@dataclass(frozen=True, slots=True)
class ProposalReviewDependencyProposal:
    proposal_id: str
    title: str
    route_summary: str
    detection_method: str
    occurrence_summary: str
    rationale: str
    confidence: str
    evidence_refs: tuple[str, ...]
    top_signals: tuple[ProposalReviewSignal, ...]
    accept_command: str
    dismiss_command: str


@dataclass(frozen=True, slots=True)
class ReviewProposalsArtifacts:
    issue_number: int
    html_path: Path
    proposal_count: int


def review_proposals_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to inspect. Defaults to the active issue."),
    section: str | None = typer.Option(None, "--section", help="Render only the pending proposal for the specified section id."),
    resolved_only: bool = typer.Option(False, "--resolved-only", help="Render resolved proposal history instead of pending proposals."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the proposal review HTML in the browser after rendering."),
) -> None:
    try:
        artifacts = generate_review_proposals(
            edition_name=edition,
            issue_number=issue,
            section_id=section,
            resolved_only=resolved_only,
            open_browser=open_browser,
        )
    except typer.BadParameter as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    typer.echo(f"Proposal review generated for Issue {artifacts.issue_number:03d}.")
    typer.echo(f"Mode: {'resolved history' if resolved_only else 'pending review'}")
    if section is not None and section.strip():
        typer.echo(f"Section filter: {section.strip()}")
    typer.echo(f"Proposal HTML: {artifacts.html_path}")
    typer.echo(
        f"Rendered {'resolved proposal history entries' if resolved_only else 'pending proposals'}: {artifacts.proposal_count}"
    )
    raise typer.Exit(code=0)


def generate_review_proposals(
    *,
    edition_name: str,
    issue_number: int | None = None,
    section_id: str | None = None,
    resolved_only: bool = False,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    output_root: Path | None = None,
    open_browser: bool = False,
) -> ReviewProposalsArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=resolved_reports_root.parent / "programs",
    )
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=resolved_reports_root.parent / "programs",
    )
    if resolved_paths is None:
        raise typer.BadParameter(f"Unknown edition '{edition_name}'.")
    resolved_issue_number = issue_number if issue_number is not None else _resolve_default_issue_number(
        edition_name=edition_name,
        program_id=resolved_paths.program_id,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
    )
    all_proposals = load_proposals(
        resolved_paths.program_id,
        resolved_issue_number,
        programs_root=resolved_reports_root.parent / "programs",
    )
    dependency_proposals = (
        tuple(
            proposal
            for proposal in load_dependency_proposals(
                resolved_paths.program_id,
                programs_root=resolved_reports_root.parent / "programs",
            )
            if proposal.status == DependencyProposalStatus.PROPOSED
        )
        if not resolved_only
        else ()
    )
    pending_proposals = tuple(
        proposal for proposal in all_proposals if proposal.status == SectionRevisionStatus.PENDING
    )
    if not all_proposals and not dependency_proposals:
        raise typer.BadParameter(
            f"No proposals found for Issue {resolved_issue_number:03d}. Run `vertex propose --edition {edition_name}` first."
        )
    resolved_proposals = tuple(
        proposal for proposal in all_proposals if proposal.status != SectionRevisionStatus.PENDING
    )
    if not resolved_only and not pending_proposals and not dependency_proposals:
        raise typer.BadParameter(
            f"No pending proposals found for Issue {resolved_issue_number:03d}. Run `vertex propose --edition {edition_name}` first."
        )
    if resolved_only and not resolved_proposals:
        raise typer.BadParameter(
            f"No resolved proposals found for Issue {resolved_issue_number:03d}. Review history is unavailable until proposals are accepted, modified, rejected, or superseded."
        )
    decision_summary = _build_decision_summary(all_proposals)

    filtered_proposals = (
        tuple(
            sorted(
                resolved_proposals,
                key=lambda proposal: (proposal.resolved_at or proposal.generated_at, proposal.section_id),
                reverse=True,
            )
        )
        if resolved_only
        else pending_proposals
    )
    normalized_section_id = section_id.strip() if section_id is not None else None
    if normalized_section_id:
        filtered_proposals = tuple(
            proposal for proposal in filtered_proposals if proposal.section_id == normalized_section_id
        )
        if not filtered_proposals:
            target_label = "resolved" if resolved_only else "pending"
            known = ", ".join(sorted(proposal.section_id for proposal in (resolved_proposals if resolved_only else pending_proposals)))
            raise typer.BadParameter(
                f"No {target_label} proposal found for section '{normalized_section_id}' in Issue {resolved_issue_number:03d}. "
                f"Known {target_label} sections: {known}"
            )

    signal_store = build_signal_store_for_program_id(
        resolved_paths.program_id,
        programs_root=resolved_reports_root.parent / "programs",
    )
    signal_map = {
        signal.id: signal
        for signal in signal_store.read(resolved_paths.program_id)
    }

    sections = tuple(
        _build_review_section(
            edition_name=edition_name,
            proposal=proposal,
            signal_map=signal_map,
            include_commands=not resolved_only,
        )
        for proposal in filtered_proposals
    )
    review_dependency_proposals = tuple(
        _build_review_dependency_proposal(
            proposal=proposal,
            program_id=resolved_paths.program_id,
            signal_map=signal_map,
        )
        for proposal in dependency_proposals
    )
    html = _render_proposals_review_html(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        sections=sections,
        dependency_proposals=review_dependency_proposals,
        section_id=normalized_section_id,
        decision_summary=decision_summary,
        resolved_only=resolved_only,
    )
    target_path = _write_output_text(
        get_program_output_dir(edition_name, programs_root=resolved_reports_root.parent / "programs") / "review" / "proposals_review.html",
        html,
    )
    if open_browser:
        webbrowser.open(target_path.resolve().as_uri())
    return ReviewProposalsArtifacts(
        issue_number=resolved_issue_number,
        html_path=target_path,
        proposal_count=len(sections) + len(review_dependency_proposals),
    )


def _build_review_section(
    *,
    edition_name: str,
    proposal: SectionRevisionProposal,
    signal_map: dict[str, Signal],
    include_commands: bool,
) -> ProposalReviewSection:
    return ProposalReviewSection(
        section_id=proposal.section_id,
        title=_section_title(proposal.section_id),
        status=proposal.status.value,
        status_label=_status_label(proposal.status.value),
        resolved_at=proposal.resolved_at.isoformat() if proposal.resolved_at is not None else None,
        accepted_text=proposal.accepted_text,
        rejection_reason=proposal.rejection_reason,
        current_text=proposal.current_text,
        proposed_text=proposal.proposed_text,
        ado_delta_summary=proposal.evidence_brief.ado_delta_summary,
        top_signals=tuple(_build_review_signal(signal_id, signal_map=signal_map) for signal_id in proposal.evidence_brief.top_signals),
        kpi_summary=proposal.evidence_brief.kpi_summary,
        stale_claims=proposal.evidence_brief.stale_claims,
        vitality_summary=proposal.evidence_brief.vitality_summary,
        confidence=proposal.evidence_brief.confidence.value,
        accept_command=(
            f"vertex apply-proposals --edition {edition_name} --accept {proposal.section_id}"
            if include_commands
            else None
        ),
        accept_modified_command=(
            (
                f"vertex apply-proposals --edition {edition_name} --accept-modified "
                f"{proposal.section_id}=<edited_text>"
            )
            if include_commands
            else None
        ),
        reject_command=(
            f"vertex apply-proposals --edition {edition_name} --reject {proposal.section_id}"
            if include_commands
            else None
        ),
    )


def _build_review_dependency_proposal(
    *,
    proposal: DependencyProposal,
    program_id: str,
    signal_map: dict[str, Signal],
) -> ProposalReviewDependencyProposal:
    return ProposalReviewDependencyProposal(
        proposal_id=proposal.id,
        title=f"{proposal.from_item_title} -> {proposal.to_item_title}",
        route_summary=(
            f"{proposal.from_workstream_id}:{proposal.from_item_id} -> "
            f"{proposal.to_workstream_id}:{proposal.to_item_id} "
            f"({proposal.suggested_dependency_type.value})"
        ),
        detection_method=proposal.detection_method,
        occurrence_summary=f"{proposal.occurrence_count} signal(s)",
        rationale=proposal.rationale,
        confidence=proposal.confidence.value,
        evidence_refs=proposal.evidence_refs,
        top_signals=tuple(
            _build_review_signal(signal_id, signal_map=signal_map)
            for signal_id in proposal.evidence_refs
            if signal_id in signal_map
        ),
        accept_command=f"vertex dependencies accept --program {program_id} --id {proposal.id}",
        dismiss_command=f"vertex dependencies dismiss --program {program_id} --id {proposal.id}",
    )


def _render_proposals_review_html(
    *,
    edition_name: str,
    issue_number: int,
    sections: tuple[ProposalReviewSection, ...],
    dependency_proposals: tuple[ProposalReviewDependencyProposal, ...],
    section_id: str | None,
    decision_summary: ProposalDecisionSummary,
    resolved_only: bool,
) -> str:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES_ROOT)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)
    try:
        template = environment.get_template("proposals_review.j2")
    except TemplateNotFound as exc:
        raise RenderError("Missing template: proposals_review.j2") from exc
    return (
        template.render(
            title=f"{edition_name} proposal review",
            subtitle=(
                f"Issue {issue_number:03d} proposal {'history' if resolved_only else 'review'} pane"
                f"{' filtered to ' + section_id if section_id is not None else ''}. This view is read-only"
                + (
                    "."
                    if resolved_only
                    else "; record section decisions via `vertex apply-proposals` and dependency decisions via `vertex dependencies accept|dismiss`."
                )
            ),
            edition_name=edition_name,
            issue_number=issue_number,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            mode_label=("Resolved History" if resolved_only else "Pending Review"),
            rendered_count_label=("Rendered Resolved Proposals" if resolved_only else "Rendered Pending Proposals"),
            pending_count_label=("Remaining Pending Proposals" if resolved_only else "Pending Proposals"),
            accepted_modified_label="Accepted With Edits",
            summary_mix_label=("Resolved Decision Mix" if resolved_only else "Pending Confidence Mix"),
            summary_mix=(
                _summarize_decision_mix(sections)
                if resolved_only
                else _summarize_confidence_mix(sections, dependency_proposals=dependency_proposals)
            ),
            signal_details=_collect_signal_details(sections, dependency_proposals=dependency_proposals),
            decision_summary=_decision_summary_for_render(
                decision_summary,
                sections=sections,
                section_id=section_id,
                resolved_only=resolved_only,
            ),
            sections=sections,
            dependency_proposals=dependency_proposals,
            resolved_only=resolved_only,
            accept_all_command=f"vertex apply-proposals --edition {edition_name} --accept-all",
            interactive_command=f"vertex apply-proposals --edition {edition_name} --interactive",
            section_filter=section_id,
        ).strip()
        + "\n"
    )


def _section_title(section_id: str) -> str:
    if section_id == "exec_summary":
        return "Executive Summary"
    normalized = section_id.removeprefix("ws:").replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in normalized.split()) or section_id


def _summarize_confidence_mix(
    sections: tuple[ProposalReviewSection, ...],
    *,
    dependency_proposals: tuple[ProposalReviewDependencyProposal, ...] = (),
) -> str:
    counts: dict[str, int] = {}
    for section in sections:
        confidence = section.confidence.strip().lower() or "unknown"
        counts[confidence] = counts.get(confidence, 0) + 1
    for proposal in dependency_proposals:
        confidence = proposal.confidence.strip().lower() or "unknown"
        counts[confidence] = counts.get(confidence, 0) + 1
    ordered_confidences = sorted(counts, key=lambda value: (_confidence_sort_key(value), value))
    return ", ".join(f"{confidence}={counts[confidence]}" for confidence in ordered_confidences) or "none"


def _summarize_decision_mix(sections: tuple[ProposalReviewSection, ...]) -> str:
    counts: dict[str, int] = {}
    for section in sections:
        status = section.status.strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1
    ordered_statuses = sorted(counts, key=lambda value: (_decision_sort_key(value), value))
    return ", ".join(f"{status}={counts[status]}" for status in ordered_statuses) or "none"


def _confidence_sort_key(confidence: str) -> int:
    if confidence == "high":
        return 0
    if confidence == "medium":
        return 1
    if confidence == "low":
        return 2
    return 3


def _decision_sort_key(status: str) -> int:
    if status == "accepted":
        return 0
    if status == "accepted_modified":
        return 1
    if status == "rejected":
        return 2
    if status == "superseded":
        return 3
    return 4


def _status_label(status: str) -> str:
    if status == SectionRevisionStatus.PENDING.value:
        return "Pending"
    if status == SectionRevisionStatus.ACCEPTED.value:
        return "Accepted"
    if status == SectionRevisionStatus.ACCEPTED_MODIFIED.value:
        return "Accepted With Edits"
    if status == SectionRevisionStatus.REJECTED.value:
        return "Rejected"
    if status == SectionRevisionStatus.SUPERSEDED.value:
        return "Superseded"
    return status.replace("_", " ").title()


def _build_review_signal(signal_id: str, *, signal_map: dict[str, Signal]) -> ProposalReviewSignal:
    signal = signal_map.get(signal_id)
    if signal is None:
        return ProposalReviewSignal(
            signal_id=signal_id,
            anchor_id=_signal_anchor_id(signal_id),
            timestamp=None,
            source=None,
            confidence=None,
            workstream_id=None,
            text=None,
        )
    return ProposalReviewSignal(
        signal_id=signal_id,
        anchor_id=_signal_anchor_id(signal_id),
        timestamp=signal.timestamp.isoformat(),
        source=signal.source,
        confidence=signal.confidence.value.lower(),
        workstream_id=signal.workstream_id,
        text=signal.text,
    )


def _collect_signal_details(
    sections: tuple[ProposalReviewSection, ...],
    *,
    dependency_proposals: tuple[ProposalReviewDependencyProposal, ...] = (),
) -> tuple[ProposalReviewSignal, ...]:
    seen: set[str] = set()
    details: list[ProposalReviewSignal] = []
    for section in sections:
        for signal in section.top_signals:
            if signal.signal_id in seen:
                continue
            seen.add(signal.signal_id)
            details.append(signal)
    for proposal in dependency_proposals:
        for signal in proposal.top_signals:
            if signal.signal_id in seen:
                continue
            seen.add(signal.signal_id)
            details.append(signal)
    return tuple(details)


def _signal_anchor_id(signal_id: str) -> str:
    return "signal-" + re.sub(r"[^a-zA-Z0-9_-]", "-", signal_id)


def _build_decision_summary(proposals: tuple[SectionRevisionProposal, ...]) -> ProposalDecisionSummary:
    accepted = tuple(proposal for proposal in proposals if proposal.status == SectionRevisionStatus.ACCEPTED)
    accepted_modified = tuple(
        proposal for proposal in proposals if proposal.status == SectionRevisionStatus.ACCEPTED_MODIFIED
    )
    rejected = tuple(proposal for proposal in proposals if proposal.status == SectionRevisionStatus.REJECTED)
    superseded = tuple(proposal for proposal in proposals if proposal.status == SectionRevisionStatus.SUPERSEDED)
    pending = tuple(proposal for proposal in proposals if proposal.status == SectionRevisionStatus.PENDING)
    recent_resolutions = tuple(
        sorted(
            (
                proposal
                for proposal in proposals
                if proposal.status != SectionRevisionStatus.PENDING and proposal.resolved_at is not None
            ),
            key=lambda proposal: (proposal.resolved_at, proposal.section_id),
            reverse=True,
        )[:5]
    )
    return ProposalDecisionSummary(
        pending_count=len(pending),
        accepted_count=len(accepted),
        accepted_modified_count=len(accepted_modified),
        rejected_count=len(rejected),
        superseded_count=len(superseded),
        recent_resolutions=recent_resolutions,
    )


def _decision_summary_for_render(
    decision_summary: ProposalDecisionSummary,
    *,
    sections: tuple[ProposalReviewSection, ...],
    section_id: str | None,
    resolved_only: bool,
) -> ProposalDecisionSummary:
    if not resolved_only or section_id is None:
        return decision_summary
    accepted_count = sum(1 for section in sections if section.status == SectionRevisionStatus.ACCEPTED.value)
    accepted_modified_count = sum(
        1 for section in sections if section.status == SectionRevisionStatus.ACCEPTED_MODIFIED.value
    )
    rejected_count = sum(1 for section in sections if section.status == SectionRevisionStatus.REJECTED.value)
    superseded_count = sum(1 for section in sections if section.status == SectionRevisionStatus.SUPERSEDED.value)
    return ProposalDecisionSummary(
        pending_count=0,
        accepted_count=accepted_count,
        accepted_modified_count=accepted_modified_count,
        rejected_count=rejected_count,
        superseded_count=superseded_count,
        recent_resolutions=tuple(
            proposal
            for proposal in decision_summary.recent_resolutions
            if proposal.section_id == section_id
        ),
    )


def _next_issue_number(archive_index: ArchiveIndex) -> int:
    latest = find_latest_confirmed_entry(archive_index)
    if latest is None:
        return 1
    return latest.issue_number + 1


def _resolve_default_issue_number(
    *,
    edition_name: str,
    program_id: str,
    reports_root: Path,
    archive_root: Path,
) -> int:
    narratives_root = reports_root.parent / "programs" / program_id / "narratives"
    latest_issue_with_proposals: int | None = None
    if narratives_root.exists():
        for path in narratives_root.glob("issue_*/proposals.jsonl"):
            try:
                issue_number = int(path.parent.name.removeprefix("issue_"))
            except ValueError:
                continue
            latest_issue_with_proposals = issue_number if latest_issue_with_proposals is None else max(latest_issue_with_proposals, issue_number)
    if latest_issue_with_proposals is not None:
        return latest_issue_with_proposals
    return _next_issue_number(read_archive_index(edition_name, archive_root=archive_root))
