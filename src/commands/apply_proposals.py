from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil

import typer

from src.core.ban_list_validator import find_ban_list_violations
from src.core.archive_store import find_latest_confirmed_entry, is_issue_confirmed, read_archive_index
from src.core.models import ArchiveIndex
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.edition_resolver import resolve_edition_paths
from src.core.models_v2 import SectionRevisionProposal, SectionRevisionStatus
from src.core.narrative_store import get_narratives_dir, load_narratives, narrative_filename_for_section, write_narrative_section
from src.core.section_proposal_store import load_proposals, update_proposal_status
from src.core.snapshot_store import ARCHIVE_ROOT


@dataclass(frozen=True, slots=True)
class ApplyProposalsArtifacts:
    issue_number: int
    accepted_count: int
    rejected_count: int
    backup_path: Path | None
    accepted_sections: tuple[str, ...]
    accepted_modified_sections: tuple[str, ...]
    rejected_sections: tuple[str, ...]

    @property
    def applied_sections(self) -> tuple[str, ...]:
        return self.accepted_sections + self.accepted_modified_sections


@dataclass(frozen=True, slots=True)
class UndoProposalsArtifacts:
    issue_number: int
    restored_backup_path: Path
    available_backups: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class InteractiveProposalDecisions:
    issue_number: int
    accept: tuple[str, ...]
    accept_modified: tuple[tuple[str, str], ...]
    reject: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AcceptAllPreview:
    issue_number: int
    accepted_count: int
    confidence_summary: str


def apply_proposals_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to update. Defaults to the latest issue with proposals."),
    accept: list[str] = typer.Option([], "--accept", help="Section ids to accept."),
    accept_modified: list[str] = typer.Option([], "--accept-modified", help="Section edits to accept as <section_id>=<text>."),
    reject: list[str] = typer.Option([], "--reject", help="Section ids to reject."),
    accept_all: bool = typer.Option(False, "--accept-all", help="Accept all pending proposals."),
    interactive: bool = typer.Option(False, "--interactive", help="Prompt through pending proposals one section at a time."),
    undo: bool = typer.Option(False, "--undo", help="Restore the most recent narrative backup for the issue."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompts for --undo and --accept-all."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview apply/reject actions without mutating files or proposal status."),
) -> None:
    if undo:
        if accept or accept_modified or reject or accept_all or interactive or dry_run:
            typer.echo("--undo cannot be combined with --accept, --accept-modified, --reject, --accept-all, --interactive, or --dry-run.")
            raise typer.Exit(code=2)
        try:
            undo_preview = preview_section_revision_proposals_undo(
                edition_name=edition,
                issue_number=issue,
            )
        except typer.BadParameter as error:
            typer.echo(str(error))
            raise typer.Exit(code=2)
        if not yes and not typer.confirm(
            f"Restore latest narrative backup for Issue {undo_preview.issue_number:03d} from {undo_preview.restored_backup_path}?",
            default=False,
        ):
            typer.echo("Restore cancelled.")
            raise typer.Exit(code=1)
        undo_artifacts = undo_section_revision_proposals(
            edition_name=edition,
            issue_number=issue,
        )
        typer.echo(
            f"Restored narratives for Issue {undo_artifacts.issue_number:03d} from backup {undo_artifacts.restored_backup_path}."
        )
        raise typer.Exit(code=0)

    accept_modified_entries = _parse_accept_modified_values(accept_modified)
    reject_sections = tuple(reject)

    if interactive and (accept or accept_modified_entries or reject or accept_all):
        typer.echo("--interactive cannot be combined with --accept, --accept-modified, --reject, or --accept-all.")
        raise typer.Exit(code=2)

    if accept_all and not dry_run and not yes:
        try:
            preview = preview_accept_all_proposals(
                edition_name=edition,
                issue_number=issue,
                accept_modified=accept_modified_entries,
                reject=reject_sections,
            )
        except typer.BadParameter as error:
            typer.echo(str(error))
            raise typer.Exit(code=2)
        if preview.accepted_count > 0 and not typer.confirm(
            (
                f"Accept all {preview.accepted_count} pending proposal(s) for Issue {preview.issue_number:03d}? "
                f"Confidence mix: {preview.confidence_summary}"
            ),
            default=False,
        ):
            typer.echo("Apply cancelled.")
            raise typer.Exit(code=1)

    accept_sections = tuple(accept)
    if interactive:
        try:
            planned = prompt_for_section_revision_actions(
                edition_name=edition,
                issue_number=issue,
            )
        except typer.BadParameter as error:
            typer.echo(str(error))
            raise typer.Exit(code=2)
        accept_sections = planned.accept
        accept_modified_entries = planned.accept_modified
        reject_sections = planned.reject
        if not accept_sections and not accept_modified_entries and not reject_sections:
            typer.echo(f"No proposal decisions recorded for Issue {planned.issue_number:03d}.")
            raise typer.Exit(code=0)

    try:
        artifacts = apply_section_revision_proposals(
            edition_name=edition,
            issue_number=issue,
            accept=accept_sections,
            accept_modified=accept_modified_entries,
            reject=reject_sections,
            accept_all=accept_all,
            dry_run=dry_run,
        )
    except typer.BadParameter as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)

    if dry_run:
        typer.echo(
            f"Dry-run: would apply {artifacts.accepted_count} proposal(s) and reject {artifacts.rejected_count} proposal(s) for Issue {artifacts.issue_number:03d}."
        )
        _echo_apply_decision_summary(artifacts, prefix="Would ")
    else:
        typer.echo(
            f"Applied {artifacts.accepted_count} proposal(s) and rejected {artifacts.rejected_count} proposal(s) for Issue {artifacts.issue_number:03d}."
        )
        _echo_apply_decision_summary(artifacts)
        if artifacts.backup_path is not None:
            typer.echo(f"Narrative backup: {artifacts.backup_path}")
        typer.echo(f"Run 'vertex report --edition {edition} --dry-run' to see the result.")
    raise typer.Exit(code=0)


def preview_section_revision_proposals_undo(
    *,
    edition_name: str,
    issue_number: int | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
) -> UndoProposalsArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=resolved_reports_root.parent / "programs",
    )
    if resolved_paths is None:
        raise typer.BadParameter(f"Unknown edition '{edition_name}'.")
    resolved_issue_number = issue_number if issue_number is not None else _resolve_default_undo_issue_number(
        edition_name=edition_name,
        program_id=resolved_paths.program_id,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
    )
    available_backups = _list_narrative_backups(
        program_id=resolved_paths.program_id,
        issue_number=resolved_issue_number,
        reports_root=resolved_reports_root,
    )
    if not available_backups:
        raise typer.BadParameter(f"No narrative backups found for Issue {resolved_issue_number:03d}.")
    return UndoProposalsArtifacts(
        issue_number=resolved_issue_number,
        restored_backup_path=available_backups[-1],
        available_backups=available_backups,
    )


def undo_section_revision_proposals(
    *,
    edition_name: str,
    issue_number: int | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
) -> UndoProposalsArtifacts:
    artifacts = preview_section_revision_proposals_undo(
        edition_name=edition_name,
        issue_number=issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
    )
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    if is_issue_confirmed(edition_name, artifacts.issue_number, archive_root=resolved_archive_root):
        raise typer.BadParameter(
            f"Issue {artifacts.issue_number:03d} is already confirmed (published) and its narrative is locked — "
            "the proposal-apply cannot be undone for a published issue."
        )
    target_dir = get_narratives_dir(edition_name, artifacts.issue_number, reports_root=resolved_reports_root)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(artifacts.restored_backup_path, target_dir)
    return artifacts


def prompt_for_section_revision_actions(
    *,
    edition_name: str,
    issue_number: int | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    prompt_fn=typer.prompt,
    echo_fn=typer.echo,
) -> InteractiveProposalDecisions:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
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
    pending_proposals = load_proposals(
        resolved_paths.program_id,
        resolved_issue_number,
        programs_root=resolved_reports_root.parent / "programs",
        status_filter={SectionRevisionStatus.PENDING},
    )
    if not pending_proposals:
        raise typer.BadParameter(
            f"No pending proposals found for Issue {resolved_issue_number:03d}. Run `vertex propose --edition {edition_name}` first."
        )

    accepted: list[str] = []
    accepted_modified: list[tuple[str, str]] = []
    rejected: list[str] = []
    for proposal in pending_proposals:
        echo_fn(f"Section: {_section_title(proposal.section_id)} ({proposal.section_id})")
        echo_fn(f"Proposed: {_proposal_preview(proposal)}")
        for line in _interactive_evidence_summary_lines(proposal):
            echo_fn(line)
        while True:
            decision = str(prompt_fn("Choose [a]ccept / [m]odify / [r]eject / [s]kip", default="s")).strip().lower()
            if decision in {"a", "accept"}:
                accepted.append(proposal.section_id)
                break
            if decision in {"m", "modify"}:
                modified_text = str(prompt_fn("Enter accepted text", default=(proposal.proposed_text or proposal.current_text or "").strip())).strip()
                if not modified_text:
                    echo_fn("Accepted text cannot be empty.")
                    continue
                accepted_modified.append((proposal.section_id, modified_text))
                break
            if decision in {"r", "reject"}:
                rejected.append(proposal.section_id)
                break
            if decision in {"s", "skip", ""}:
                break
            echo_fn("Enter 'a', 'm', 'r', or 's'.")

    return InteractiveProposalDecisions(
        issue_number=resolved_issue_number,
        accept=tuple(accepted),
        accept_modified=tuple(accepted_modified),
        reject=tuple(rejected),
    )


def preview_accept_all_proposals(
    *,
    edition_name: str,
    issue_number: int | None = None,
    accept_modified: tuple[tuple[str, str], ...] = (),
    reject: tuple[str, ...] = (),
    reports_root: Path | None = None,
    archive_root: Path | None = None,
) -> AcceptAllPreview:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
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
    pending_proposals = load_proposals(
        resolved_paths.program_id,
        resolved_issue_number,
        programs_root=resolved_reports_root.parent / "programs",
        status_filter={SectionRevisionStatus.PENDING},
    )
    if not pending_proposals:
        raise typer.BadParameter(
            f"No pending proposals found for Issue {resolved_issue_number:03d}. Run `vertex propose --edition {edition_name}` first."
        )

    reject_sections = {section.strip() for section in reject if section.strip()}
    selected_proposals = tuple(
        proposal for proposal in pending_proposals if proposal.section_id not in reject_sections
    )
    return AcceptAllPreview(
        issue_number=resolved_issue_number,
        accepted_count=len(selected_proposals),
        confidence_summary=_summarize_proposal_confidence(selected_proposals),
    )


def apply_section_revision_proposals(
    *,
    edition_name: str,
    issue_number: int | None = None,
    accept: tuple[str, ...] = (),
    accept_modified: tuple[tuple[str, str], ...] = (),
    reject: tuple[str, ...] = (),
    accept_all: bool = False,
    dry_run: bool = False,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
) -> ApplyProposalsArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
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
    if is_issue_confirmed(edition_name, resolved_issue_number, archive_root=resolved_archive_root):
        raise typer.BadParameter(
            f"Issue {resolved_issue_number:03d} is already confirmed (published) and its narrative is locked — "
            "proposals cannot be applied to a published issue."
        )
    pending_proposals = load_proposals(
        resolved_paths.program_id,
        resolved_issue_number,
        programs_root=resolved_reports_root.parent / "programs",
        status_filter={SectionRevisionStatus.PENDING},
    )
    if not pending_proposals:
        raise typer.BadParameter(
            f"No pending proposals found for Issue {resolved_issue_number:03d}. Run `vertex propose --edition {edition_name}` first."
        )

    pending_by_section = {proposal.section_id: proposal for proposal in pending_proposals}
    accept_sections = {section.strip() for section in accept if section.strip()}
    accept_modified_sections = {
        section_id.strip(): accepted_text.strip()
        for section_id, accepted_text in accept_modified
        if section_id.strip() and accepted_text.strip()
    }
    reject_sections = {section.strip() for section in reject if section.strip()}
    if accept_all:
        accept_sections.update(section_id for section_id in pending_by_section if section_id not in accept_modified_sections)
    if not accept_sections and not accept_modified_sections and not reject_sections:
        raise typer.BadParameter("Provide at least one of --accept, --accept-modified, --reject, or --accept-all.")

    overlap = (accept_sections | set(accept_modified_sections)).intersection(reject_sections)
    if overlap:
        duplicated = ", ".join(sorted(overlap))
        raise typer.BadParameter(f"Sections cannot be both accepted and rejected: {duplicated}")

    modified_overlap = accept_sections.intersection(accept_modified_sections)
    if modified_overlap:
        duplicated = ", ".join(sorted(modified_overlap))
        raise typer.BadParameter(f"Sections cannot be both accepted and accepted-modified: {duplicated}")

    unknown_sections = (accept_sections | set(accept_modified_sections) | reject_sections) - set(pending_by_section)
    if unknown_sections:
        known = ", ".join(sorted(pending_by_section))
        raise typer.BadParameter(
            f"Unknown pending proposal section(s): {', '.join(sorted(unknown_sections))}. Known pending sections: {known}"
        )

    accepted_proposals = tuple(pending_by_section[section_id] for section_id in sorted(accept_sections))
    accepted_modified_proposals = tuple(
        (pending_by_section[section_id], accept_modified_sections[section_id])
        for section_id in sorted(accept_modified_sections)
    )
    rejected_proposals = tuple(pending_by_section[section_id] for section_id in sorted(reject_sections))

    for proposal in accepted_proposals:
        _validate_source_hash(
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            proposal=proposal,
            reports_root=resolved_reports_root,
        )
    for proposal, _accepted_text in accepted_modified_proposals:
        _validate_source_hash(
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            proposal=proposal,
            reports_root=resolved_reports_root,
        )

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=resolved_reports_root.parent / "programs",
    )
    for proposal in accepted_proposals:
        if proposal.proposed_text is None:
            continue
        _validate_apply_text_against_ban_list(
            section_id=proposal.section_id,
            text=proposal.proposed_text,
            editorial_rules=bundle.editorial_rules,
        )
    for proposal, accepted_text in accepted_modified_proposals:
        _validate_apply_text_against_ban_list(
            section_id=proposal.section_id,
            text=accepted_text,
            editorial_rules=bundle.editorial_rules,
        )

    backup_path: Path | None = None
    proposals_requiring_write = tuple(proposal for proposal in accepted_proposals if proposal.proposed_text is not None)
    modified_writes = tuple(accepted_modified_proposals)
    if not dry_run and proposals_requiring_write:
        backup_path = _backup_narratives_dir(
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            program_id=resolved_paths.program_id,
            reports_root=resolved_reports_root,
        )
    if not dry_run and backup_path is None and modified_writes:
        backup_path = _backup_narratives_dir(
            edition_name=edition_name,
            issue_number=resolved_issue_number,
            program_id=resolved_paths.program_id,
            reports_root=resolved_reports_root,
        )

    if not dry_run:
        for proposal in proposals_requiring_write:
            assert proposal.proposed_text is not None
            write_narrative_section(
                edition_name,
                resolved_issue_number,
                proposal.section_id,
                proposal.proposed_text,
                reports_root=resolved_reports_root,
            )
        for proposal, accepted_text in modified_writes:
            write_narrative_section(
                edition_name,
                resolved_issue_number,
                proposal.section_id,
                accepted_text,
                reports_root=resolved_reports_root,
            )
        for proposal in accepted_proposals:
            update_proposal_status(
                proposal.proposal_id,
                SectionRevisionStatus.ACCEPTED,
                program_id=resolved_paths.program_id,
                issue_number=resolved_issue_number,
                programs_root=resolved_reports_root.parent / "programs",
            )
        for proposal, accepted_text in accepted_modified_proposals:
            update_proposal_status(
                proposal.proposal_id,
                SectionRevisionStatus.ACCEPTED_MODIFIED,
                accepted_text=accepted_text,
                program_id=resolved_paths.program_id,
                issue_number=resolved_issue_number,
                programs_root=resolved_reports_root.parent / "programs",
            )
        for proposal in rejected_proposals:
            update_proposal_status(
                proposal.proposal_id,
                SectionRevisionStatus.REJECTED,
                program_id=resolved_paths.program_id,
                issue_number=resolved_issue_number,
                programs_root=resolved_reports_root.parent / "programs",
            )

    return ApplyProposalsArtifacts(
        issue_number=resolved_issue_number,
        accepted_count=len(accepted_proposals) + len(accepted_modified_proposals),
        rejected_count=len(rejected_proposals),
        backup_path=backup_path,
        accepted_sections=tuple(proposal.section_id for proposal in accepted_proposals),
        accepted_modified_sections=tuple(proposal.section_id for proposal, _accepted_text in accepted_modified_proposals),
        rejected_sections=tuple(proposal.section_id for proposal in rejected_proposals),
    )


def _echo_apply_decision_summary(artifacts: ApplyProposalsArtifacts, *, prefix: str = "") -> None:
    if artifacts.accepted_sections:
        typer.echo(f"{prefix}accept: {_format_section_summary(artifacts.accepted_sections)}")
    if artifacts.accepted_modified_sections:
        typer.echo(f"{prefix}accept-modified: {_format_section_summary(artifacts.accepted_modified_sections)}")
    if artifacts.rejected_sections:
        typer.echo(f"{prefix}reject: {_format_section_summary(artifacts.rejected_sections)}")


def _format_section_summary(sections: tuple[str, ...]) -> str:
    return ", ".join(sections)


def _validate_source_hash(
    *,
    edition_name: str,
    issue_number: int,
    proposal: SectionRevisionProposal,
    reports_root: Path,
) -> None:
    current_narratives = load_narratives(edition_name, issue_number, reports_root=reports_root)
    filename = narrative_filename_for_section(proposal.section_id)
    current_text = (current_narratives.get(filename) or "").strip()
    hash_basis = current_text if current_text else proposal.current_text
    current_hash = _source_hash(hash_basis)
    if proposal.source_hash != current_hash:
        raise typer.BadParameter(
            f"Section {proposal.section_id} narrative has changed since proposal was generated. Re-run 'vertex propose' to refresh, or edit manually."
        )


def _backup_narratives_dir(
    *,
    edition_name: str,
    issue_number: int,
    program_id: str,
    reports_root: Path,
) -> Path:
    source_dir = get_narratives_dir(edition_name, issue_number, reports_root=reports_root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_dir = reports_root.parent / "programs" / program_id / "backups" / "narratives" / f"issue_{issue_number:03d}" / timestamp
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, target_dir)
    return target_dir


def _source_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _list_narrative_backups(
    *,
    program_id: str,
    issue_number: int,
    reports_root: Path,
) -> tuple[Path, ...]:
    backups_root = reports_root.parent / "programs" / program_id / "backups" / "narratives" / f"issue_{issue_number:03d}"
    if not backups_root.exists():
        return ()
    return tuple(sorted(path for path in backups_root.iterdir() if path.is_dir()))


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
                discovered_issue = int(path.parent.name.removeprefix("issue_"))
            except ValueError:
                continue
            latest_issue_with_proposals = discovered_issue if latest_issue_with_proposals is None else max(latest_issue_with_proposals, discovered_issue)
    if latest_issue_with_proposals is not None:
        return latest_issue_with_proposals
    return _next_issue_number(read_archive_index(edition_name, archive_root=archive_root))


def _resolve_default_undo_issue_number(
    *,
    edition_name: str,
    program_id: str,
    reports_root: Path,
    archive_root: Path,
) -> int:
    backups_root = reports_root.parent / "programs" / program_id / "backups" / "narratives"
    latest_issue_with_backups: int | None = None
    if backups_root.exists():
        for path in backups_root.glob("issue_*"):
            if not path.is_dir():
                continue
            if not any(child.is_dir() for child in path.iterdir()):
                continue
            try:
                discovered_issue = int(path.name.removeprefix("issue_"))
            except ValueError:
                continue
            latest_issue_with_backups = discovered_issue if latest_issue_with_backups is None else max(latest_issue_with_backups, discovered_issue)
    if latest_issue_with_backups is not None:
        return latest_issue_with_backups
    return _resolve_default_issue_number(
        edition_name=edition_name,
        program_id=program_id,
        reports_root=reports_root,
        archive_root=archive_root,
    )


def _proposal_preview(proposal: SectionRevisionProposal) -> str:
    preview_source = (proposal.proposed_text or proposal.evidence_brief.ado_delta_summary or proposal.current_text).strip()
    preview = " ".join(preview_source.split())
    if len(preview) <= 140:
        return preview
    return preview[:137].rstrip() + "..."


def _interactive_evidence_summary_lines(proposal: SectionRevisionProposal) -> tuple[str, ...]:
    evidence = proposal.evidence_brief
    lines = [f"Confidence: {evidence.confidence.value}"]
    if evidence.ado_delta_summary.strip():
        lines.append(f"ADO Delta: {_truncate_for_terminal(evidence.ado_delta_summary)}")
    if evidence.top_signals:
        lines.append(f"Top Signals: {', '.join(evidence.top_signals[:3])}")
    if evidence.kpi_summary:
        lines.append(f"KPI Summary: {_truncate_for_terminal(evidence.kpi_summary)}")
    if evidence.stale_claims:
        lines.append(f"Stale Claims: {_truncate_for_terminal(evidence.stale_claims[0])}")
    return tuple(lines)


def _truncate_for_terminal(value: str, *, limit: int = 160) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _parse_accept_modified_values(values: list[str]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for raw_value in values:
        section_id, separator, accepted_text = raw_value.partition("=")
        normalized_section_id = section_id.strip()
        normalized_text = accepted_text.strip()
        if not separator or not normalized_section_id or not normalized_text:
            raise typer.BadParameter(
                "--accept-modified entries must use the form <section_id>=<text>."
            )
        parsed.append((normalized_section_id, normalized_text))
    return tuple(parsed)


def _section_title(section_id: str) -> str:
    if section_id == "exec_summary":
        return "Executive Summary"
    normalized = section_id.removeprefix("ws:").replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in normalized.split()) or section_id


def _validate_apply_text_against_ban_list(*, section_id: str, text: str, editorial_rules) -> None:
    violations = find_ban_list_violations({section_id: text}, editorial_rules)
    if not violations:
        return
    phrases = ", ".join(sorted({violation.phrase for violation in violations}, key=str.lower))
    raise typer.BadParameter(
        f"Section {section_id} accepted text violates the editorial ban-list: {phrases}"
    )


def _summarize_proposal_confidence(proposals: tuple[SectionRevisionProposal, ...]) -> str:
    counts: dict[str, int] = {}
    for proposal in proposals:
        confidence = proposal.evidence_brief.confidence.value.strip().lower() or "unknown"
        counts[confidence] = counts.get(confidence, 0) + 1
    ordered_confidences = sorted(counts, key=lambda value: (_confidence_sort_key(value), value))
    return ", ".join(f"{confidence}={counts[confidence]}" for confidence in ordered_confidences) or "none"


def _confidence_sort_key(confidence: str) -> int:
    if confidence == "high":
        return 0
    if confidence == "medium":
        return 1
    if confidence == "low":
        return 2
    return 3
