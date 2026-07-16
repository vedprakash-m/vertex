from __future__ import annotations
from src.core.edition_resolver import get_program_output_dir

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from html import escape
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap
from typing import Any
import uuid

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.commands.ado import apply_ado_proposal
from src.core.action_extractor_basic import extract_actions_from_signals
from src.core.action_tracker import append_action, get_actions_path
from src.core.action_mapper import MeetingActionMapping, build_meeting_action_proposal, map_actions_to_work_items
from src.core.ado_client import ADOClient
from src.core.ado_proposal import write_proposal_manifest
from src.core.edition_resolver import load_program
from src.core.exceptions import QueryError
from src.core.models import Confidence
from src.core.models_v2 import ActionItem, ActionStatus, Program, Signal, Workstream
from src.core.program_fact_store import load_program_facts, project_action_items, project_workstreams
from src.core.teams_renderer import TeamsRenderer
from src.m365.agency_bridge import AgencyBridge
from src.m365.transcript_reader import TranscriptReader, TranscriptRecord


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"
_WI_REF_PATTERN = re.compile(r"\bWI:(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MeetingCloseArtifacts:
    program_id: str
    meeting_id: str
    transcript_title: str | None
    captured_at: str | None
    web_url: str | None
    transcript_content: str
    extractor: str
    mappings: tuple[MeetingActionMapping, ...]
    next_checkpoint_date: date | None
    follow_up_message: str
    packet_path: Path | None
    proposal_path: Path | None
    action_log_path: Path | None
    queued_action_count: int
    skipped_action_count: int
    html_path: Path | None
    teams_path: Path | None
    review_plan_applied: bool
    approved_action_count: int
    dismissed_action_count: int
    pending_action_count: int
    ado_apply_applied_count: int = 0
    ado_apply_skipped_count: int = 0
    ado_apply_conflict_count: int = 0
    ado_apply_failed_count: int = 0
    ado_apply_manifest_path: Path | None = None


@dataclass(frozen=True, slots=True)
class MeetingCloseReviewPlan:
    approved_action_indexes: tuple[int, ...] = ()
    dismissed_action_indexes: tuple[int, ...] = ()
    edited_action_index: int | None = None
    edited_text: str | None = None
    edited_owner_alias: str | None = None
    edited_due_date: date | None = None


@dataclass(frozen=True, slots=True)
class MeetingCloseReviewSummary:
    review_plan_applied: bool
    approved_action_count: int
    dismissed_action_count: int
    pending_action_count: int
    approved_action_ids: tuple[str, ...] = ()


def meeting_close_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    transcript: str = typer.Option(..., "--transcript", help="Meeting transcript id or meeting id."),
    title: str | None = typer.Option(None, "--title", help="Optional meeting title override."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
    html: bool = typer.Option(False, "--html", help="Also write an HTML review artifact and open it locally."),
    teams: bool = typer.Option(False, "--teams", help="Also write a Teams-markdown follow-up draft artifact."),
    promote_actions: bool = typer.Option(False, "--promote-actions", help="Queue extracted actions into the local action register for review."),
    apply_ado: bool = typer.Option(False, "--apply-ado", help="Apply the generated meeting-close ADO proposal immediately after review-plan filtering."),
    approve_action: list[int] | None = typer.Option(None, "--approve-action", help="1-based action index to approve. Repeat as needed."),
    dismiss_action: list[int] | None = typer.Option(None, "--dismiss-action", help="1-based action index to dismiss. Repeat as needed."),
    edit_action: int | None = typer.Option(None, "--edit-action", help="1-based action index to edit before writing artifacts."),
    edit_text: str | None = typer.Option(None, "--edit-text", help="Replacement action text for --edit-action."),
    edit_owner: str | None = typer.Option(None, "--edit-owner", help="Replacement owner alias for --edit-action."),
    edit_due: str | None = typer.Option(None, "--edit-due", help="Replacement due date (YYYY-MM-DD) for --edit-action."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render the closure packet but skip local packet/proposal writes."),
) -> None:
    normalized_format = _normalize_format(format)
    review_plan = _build_review_plan(
        approve_action=tuple(approve_action or ()),
        dismiss_action=tuple(dismiss_action or ()),
        edit_action=edit_action,
        edit_text=edit_text,
        edit_owner=edit_owner,
        edit_due=edit_due,
    )
    try:
        artifacts = generate_meeting_close_artifacts(
            program_id=program.strip(),
            meeting_id=transcript.strip(),
            title_override=title.strip() if title is not None and title.strip() else None,
            emit_html=html,
            emit_teams=teams,
            promote_actions=promote_actions,
            apply_ado=apply_ado,
            review_plan=review_plan,
            dry_run=dry_run,
            programs_root=PROGRAMS_ROOT,
        )
    except (FileNotFoundError, QueryError, ValueError) as error:
        typer.echo(str(error))
        raise typer.Exit(code=1) from error

    typer.echo(_render_artifacts(artifacts, output_format=normalized_format, programs_root=PROGRAMS_ROOT))
    raise typer.Exit(code=0)


def generate_meeting_close_artifacts(
    *,
    program_id: str,
    meeting_id: str,
    title_override: str | None,
    emit_html: bool,
    emit_teams: bool,
    promote_actions: bool = False,
    apply_ado: bool = False,
    review_plan: MeetingCloseReviewPlan | None = None,
    dry_run: bool,
    programs_root: Path,
) -> MeetingCloseArtifacts:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise FileNotFoundError(f"Program '{program_id}' is missing program.yaml.")
    if program.ado is None:
        raise ValueError(f"Program '{program_id}' is missing ado configuration.")

    # ADF-W2.12: one correlation id per meeting-close run -- this invocation
    # can queue several actions extracted from one meeting, so a real
    # multi-fact chain is worth tracing (unlike a single-item CLI mutation).
    correlation_id = uuid.uuid4().hex
    transcript_record = _build_transcript_reader().get_transcript(meeting_id=meeting_id)
    if transcript_record is None:
        raise ValueError(f"Transcript '{meeting_id}' was not found.")
    resolved_title = title_override or transcript_record.title

    signal = _build_transcript_signal(program_id, meeting_id=meeting_id, transcript_record=transcript_record)
    actions, extractor = _extract_actions_from_transcript(program, signal)

    workstreams = _load_workstreams(program_id, programs_root=programs_root)
    item_rows = _load_work_item_rows(program, actions)
    mappings = map_actions_to_work_items(actions, item_rows_by_id=item_rows, workstreams=workstreams)
    mappings, review_summary = _apply_review_plan(mappings, review_plan=review_plan)
    next_checkpoint_date = _suggest_next_checkpoint(mappings)
    follow_up_message = _build_follow_up_message(resolved_title or meeting_id, mappings)

    packet_path = None
    proposal_path = None
    action_log_path = None
    queued_action_count = 0
    skipped_action_count = 0
    html_path = None
    teams_path = None
    ado_apply_applied_count = 0
    ado_apply_skipped_count = 0
    ado_apply_conflict_count = 0
    ado_apply_failed_count = 0
    ado_apply_manifest_path = None
    if not dry_run:
        rendered_body = _render_artifacts_body(
            program_id=program_id,
            meeting_id=meeting_id,
            transcript_title=resolved_title,
            captured_at=transcript_record.captured_at,
            web_url=transcript_record.web_url,
            extractor=extractor,
            mappings=mappings,
            next_checkpoint_date=next_checkpoint_date,
            follow_up_message=follow_up_message,
        )
        packet_path = _write_closure_packet(program_id, meeting_id=meeting_id, rendered_packet=rendered_body, programs_root=programs_root)
        if emit_html:
            html_path = _write_html_artifact(
                program_id,
                meeting_id=meeting_id,
                html_body=_render_html_artifact(
                    program_id=program_id,
                    meeting_id=meeting_id,
                    transcript_title=resolved_title,
                    captured_at=transcript_record.captured_at,
                    web_url=transcript_record.web_url,
                    extractor=extractor,
                    mappings=mappings,
                    next_checkpoint_date=next_checkpoint_date,
                    follow_up_message=follow_up_message,
                ),
                programs_root=programs_root,
                )
            _open_html_artifact(html_path)
        if emit_teams:
            teams_path = _write_teams_artifact(
                program_id,
                meeting_id=meeting_id,
                teams_body=_render_teams_artifact(
                    program_id=program_id,
                    meeting_id=meeting_id,
                    transcript_title=resolved_title,
                    captured_at=transcript_record.captured_at,
                    web_url=transcript_record.web_url,
                    extractor=extractor,
                    mappings=mappings,
                    next_checkpoint_date=next_checkpoint_date,
                    follow_up_message=follow_up_message,
                ),
                programs_root=programs_root,
                )
        proposal = build_meeting_action_proposal(
            program_id=program_id,
            meeting_id=meeting_id,
            meeting_title=resolved_title,
            mappings=mappings,
        )
        if proposal.entries:
            proposal_path = write_proposal_manifest(proposal, programs_root=programs_root)
            if apply_ado:
                if not review_summary.review_plan_applied or review_summary.pending_action_count > 0:
                    raise ValueError(
                        "--apply-ado requires all extracted actions to be resolved in-command via --approve-action/--dismiss-action before applying the proposal."
                    )
                apply_artifacts = apply_ado_proposal(
                    str(proposal_path),
                    programs_root=programs_root,
                        )
                ado_apply_applied_count = apply_artifacts.applied_count
                ado_apply_skipped_count = apply_artifacts.skipped_count
                ado_apply_conflict_count = apply_artifacts.conflict_count
                ado_apply_failed_count = apply_artifacts.failed_count
                ado_apply_manifest_path = apply_artifacts.manifest_path
        if promote_actions:
            action_log_path, queued_action_count, skipped_action_count = _queue_actions_for_review(
                program_id,
                tuple(mapping.action for mapping in mappings),
                approved_action_ids=frozenset(review_summary.approved_action_ids),
                programs_root=programs_root,
                correlation_id=correlation_id,
            )

    return MeetingCloseArtifacts(
        program_id=program_id,
        meeting_id=meeting_id,
        transcript_title=resolved_title,
        captured_at=transcript_record.captured_at,
        web_url=transcript_record.web_url,
        transcript_content=transcript_record.content,
        extractor=extractor,
        mappings=mappings,
        next_checkpoint_date=next_checkpoint_date,
        follow_up_message=follow_up_message,
        packet_path=packet_path,
        proposal_path=proposal_path,
        action_log_path=action_log_path,
        queued_action_count=queued_action_count,
        skipped_action_count=skipped_action_count,
        html_path=html_path,
        teams_path=teams_path,
        review_plan_applied=review_summary.review_plan_applied,
        approved_action_count=review_summary.approved_action_count,
        dismissed_action_count=review_summary.dismissed_action_count,
        pending_action_count=review_summary.pending_action_count,
        ado_apply_applied_count=ado_apply_applied_count,
        ado_apply_skipped_count=ado_apply_skipped_count,
        ado_apply_conflict_count=ado_apply_conflict_count,
        ado_apply_failed_count=ado_apply_failed_count,
        ado_apply_manifest_path=ado_apply_manifest_path,
    )


def _build_transcript_reader() -> TranscriptReader:
    return TranscriptReader(AgencyBridge())


def _extract_actions_from_transcript(program: Program, signal: Signal) -> tuple[tuple[ActionItem, ...], str]:
    if program.ai is not None and program.ai.enabled:
        try:
            from src.ai.action_extractor import ActionExtractor, ActionExtractorError

            extractor = ActionExtractor.from_program(program)
            extractor_label = "deterministic" if get_ai_mode() == AIMode.DISABLED else "ai"
            return extractor.extract_actions(program_id=program.id, signals=(signal,)), extractor_label
        except (ActionExtractorError, RuntimeError):
            pass
    return extract_actions_from_signals((signal,), program.id), "basic"


def _build_transcript_signal(program_id: str, *, meeting_id: str, transcript_record: TranscriptRecord) -> Signal:
    timestamp = _parse_optional_datetime(transcript_record.captured_at) or datetime.now(timezone.utc)
    entity_refs = tuple(dict.fromkeys(f"WI:{match.group(1)}" for match in _WI_REF_PATTERN.finditer(transcript_record.content)))
    return Signal(
        id=f"meeting-close:{program_id}:{meeting_id}",
        timestamp=timestamp,
        source="m365/meeting_transcript",
        program_id=program_id,
        workstream_id=None,
        entity_refs=entity_refs,
        text=transcript_record.content,
        raw_ref=transcript_record.web_url or transcript_record.meeting_id,
        confidence=Confidence.MEDIUM,
        metadata={"meeting_id": transcript_record.meeting_id or meeting_id},
    )


def _load_work_item_rows(program: Program, actions: tuple[ActionItem, ...]) -> dict[int, dict[str, Any]]:
    work_item_ids = sorted({work_item_id for action in actions for work_item_id in action.linked_work_item_ids})
    if not work_item_ids:
        return {}
    client = _build_ado_client(program)
    rows = client.query_work_items_batch(
        work_item_ids,
        fields=("System.Id", "System.Title", "System.AreaPath", "System.AssignedTo", "System.Rev"),
    )
    return {
        int(row.get("id") or row.get("fields", {}).get("System.Id") or 0): row
        for row in rows
        if int(row.get("id") or row.get("fields", {}).get("System.Id") or 0) > 0
    }


def _build_ado_client(program: Program) -> ADOClient:
    assert program.ado is not None
    return ADOClient(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )


def _load_workstreams(program_id: str, *, programs_root: Path) -> tuple[Workstream, ...]:
    return project_workstreams(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("workstream.entry",),
        )
    )


def _queue_actions_for_review(
    program_id: str,
    actions: tuple[ActionItem, ...],
    *,
    approved_action_ids: frozenset[str] = frozenset(),
    programs_root: Path,
    correlation_id: str = "",
) -> tuple[Path | None, int, int]:
    if not actions:
        return (None, 0, 0)

    existing_action_ids = {
        action.id
        for action in project_action_items(
            load_program_facts(
                program_id,
                db_root=programs_root.parent,
                programs_root=programs_root,
                fact_types=("action.item",),
            )
        )
    }
    queued_action_count = 0
    skipped_action_count = 0
    for action in actions:
        queued_action = action
        target_status = ActionStatus.OPEN if queued_action.id in approved_action_ids else ActionStatus.PROPOSED
        if queued_action.status is not target_status:
            queued_action = replace(queued_action, status=target_status, resolved_at=None, resolution_note=None)
        if queued_action.id in existing_action_ids:
            skipped_action_count += 1
            continue
        append_action(program_id, queued_action, programs_root=programs_root, correlation_id=correlation_id)
        existing_action_ids.add(queued_action.id)
        queued_action_count += 1

    if queued_action_count == 0 and skipped_action_count == 0:
        return (None, 0, 0)
    return (get_actions_path(program_id, programs_root=programs_root), queued_action_count, skipped_action_count)


def _build_follow_up_message(meeting_title: str, mappings: tuple[MeetingActionMapping, ...]) -> str:
    lines = [f"{meeting_title} follow-up"]
    for mapping in mappings:
        if mapping.is_net_new:
            if mapping.needs_owner or mapping.needs_due_date:
                lines.append(f"- owner/date needed: {mapping.action.text}")
                continue
            owner_label = mapping.action.owner_alias
            due_label = mapping.action.due_date.isoformat() if mapping.action.due_date is not None else "date needed"
            lines.append(f"- {owner_label} by {due_label}: {mapping.action.text} (net-new)")
            continue
        item_refs = ", ".join(f"WI:{item.work_item_id}" for item in mapping.matched_items)
        owner_label = mapping.action.owner_alias if not mapping.needs_owner else "owner needed"
        due_label = mapping.action.due_date.isoformat() if mapping.action.due_date is not None else "date needed"
        lines.append(f"- {owner_label} by {due_label}: {mapping.action.text} ({item_refs})")
    return "\n".join(lines)


def _suggest_next_checkpoint(mappings: tuple[MeetingActionMapping, ...]) -> date | None:
    due_dates = sorted(
        mapping.action.due_date
        for mapping in mappings
        if mapping.action.due_date is not None
    )
    return due_dates[0] if due_dates else None


def _write_closure_packet(program_id: str, *, meeting_id: str, rendered_packet: str, programs_root: Path) -> Path:
    safe_id = _safe_identifier(meeting_id)
    target = get_program_output_dir(program_id, programs_root=programs_root) / "meeting_close" / f"{safe_id}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered_packet + "\n", encoding="utf-8")
    return target


def _write_html_artifact(program_id: str, *, meeting_id: str, html_body: str, programs_root: Path) -> Path:
    safe_id = _safe_identifier(meeting_id)
    target = get_program_output_dir(program_id, programs_root=programs_root) / "meeting_close" / f"{safe_id}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_body + "\n", encoding="utf-8")
    return target


def _write_teams_artifact(program_id: str, *, meeting_id: str, teams_body: str, programs_root: Path) -> Path:
    safe_id = _safe_identifier(meeting_id)
    target = get_program_output_dir(program_id, programs_root=programs_root) / "meeting_close" / f"{safe_id}.teams.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(teams_body, encoding="utf-8")
    return target


def _open_html_artifact(path: Path) -> None:
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
            return
        subprocess.run(["cmd", "/c", "start", "", str(path)], check=False)
    except Exception:
        return


def _render_artifacts(artifacts: MeetingCloseArtifacts, *, output_format: str, programs_root: Path) -> str:
    if output_format == "json":
        payload = {
            "program_id": artifacts.program_id,
            "meeting_id": artifacts.meeting_id,
            "transcript_title": artifacts.transcript_title,
            "captured_at": artifacts.captured_at,
            "web_url": artifacts.web_url,
            "extractor": artifacts.extractor,
            "next_checkpoint_date": artifacts.next_checkpoint_date.isoformat() if artifacts.next_checkpoint_date is not None else None,
            "mappings": [
                {
                    "action": {
                        "id": mapping.action.id,
                        "text": mapping.action.text,
                        "owner_alias": mapping.action.owner_alias,
                        "due_date": mapping.action.due_date.isoformat() if mapping.action.due_date is not None else None,
                    },
                    "resolved_workstream_id": mapping.resolved_workstream_id,
                    "matched_items": [asdict(item) for item in mapping.matched_items],
                    "missing_work_item_ids": list(mapping.missing_work_item_ids),
                    "is_net_new": mapping.is_net_new,
                    "needs_owner": mapping.needs_owner,
                    "needs_due_date": mapping.needs_due_date,
                }
                for mapping in artifacts.mappings
            ],
            "follow_up_message": artifacts.follow_up_message,
            "packet_path": _display_path(artifacts.packet_path, programs_root=programs_root),
            "proposal_path": _display_path(artifacts.proposal_path, programs_root=programs_root),
            "action_log_path": _display_path(artifacts.action_log_path, programs_root=programs_root),
            "queued_action_count": artifacts.queued_action_count,
            "skipped_action_count": artifacts.skipped_action_count,
            "review_plan_applied": artifacts.review_plan_applied,
            "approved_action_count": artifacts.approved_action_count,
            "dismissed_action_count": artifacts.dismissed_action_count,
            "pending_action_count": artifacts.pending_action_count,
            "review_command": (
                f"vertex actions review --program {artifacts.program_id}" if artifacts.action_log_path is not None else None
            ),
            "ado_apply_manifest_path": _display_path(artifacts.ado_apply_manifest_path, programs_root=programs_root),
            "ado_apply_applied_count": artifacts.ado_apply_applied_count,
            "ado_apply_skipped_count": artifacts.ado_apply_skipped_count,
            "ado_apply_conflict_count": artifacts.ado_apply_conflict_count,
            "ado_apply_failed_count": artifacts.ado_apply_failed_count,
            "html_path": _display_path(artifacts.html_path, programs_root=programs_root),
            "teams_path": _display_path(artifacts.teams_path, programs_root=programs_root),
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    return _render_review_layout(
        artifacts,
        programs_root=programs_root,
        terminal_width=_get_terminal_width(),
    )


def _render_review_layout(artifacts: MeetingCloseArtifacts, *, programs_root: Path, terminal_width: int) -> str:
    buffer = StringIO()
    console = Console(file=buffer, width=max(terminal_width, 80), no_color=True, force_terminal=False)
    mapped_count = sum(1 for mapping in artifacts.mappings if not mapping.is_net_new)
    net_new_count = sum(1 for mapping in artifacts.mappings if mapping.is_net_new)

    summary_lines = [
        f"Program: {artifacts.program_id}",
        f"Transcript: {artifacts.meeting_id}",
        f"Title: {artifacts.transcript_title or '-'}",
        f"Captured: {artifacts.captured_at or '-'}",
        f"Source: {artifacts.web_url or '-'}",
        f"Extractor: {artifacts.extractor}",
        f"Actions: {len(artifacts.mappings)} total | {mapped_count} mapped | {net_new_count} net-new",
        f"Suggested next checkpoint: {artifacts.next_checkpoint_date.isoformat() if artifacts.next_checkpoint_date is not None else '-'}",
    ]
    console.print(Panel("\n".join(summary_lines), title="Meeting close", box=box.ASCII, expand=True))

    action_panels = [
        Panel(_render_action_review_block(index=index, mapping=mapping), title=f"Action {index}", box=box.ASCII, expand=True)
        for index, mapping in enumerate(artifacts.mappings, start=1)
    ]
    evidence_panels = [
        Panel(
            _render_evidence_block(index=index, mapping=mapping, transcript_content=artifacts.transcript_content),
            title=f"Evidence {index}",
            box=box.ASCII,
            expand=True,
        )
        for index, mapping in enumerate(artifacts.mappings, start=1)
    ]

    action_group = Panel(Group(*action_panels), title="Action review", box=box.ASCII, expand=True)
    evidence_group = Panel(Group(*evidence_panels), title="Evidence excerpts", box=box.ASCII, expand=True)
    if terminal_width >= 120:
        console.print(Columns((action_group, evidence_group), equal=True, expand=True))
    else:
        console.print(action_group)
        console.print(evidence_group)

    footer_lines = [
        "Follow-up draft:",
        artifacts.follow_up_message,
        "",
        "Apply choices: approve | edit | dismiss",
        f"Closure packet: {_display_path(artifacts.packet_path, programs_root=programs_root) if artifacts.packet_path is not None else 'dry-run (not written)'}",
        f"ADO proposal: {_display_path(artifacts.proposal_path, programs_root=programs_root) if artifacts.proposal_path is not None else 'none'}",
    ]
    if artifacts.action_log_path is not None:
        footer_lines.append(f"Action register: {_display_path(artifacts.action_log_path, programs_root=programs_root)}")
        footer_lines.append(f"Queued proposed actions: {artifacts.queued_action_count}")
        if artifacts.skipped_action_count:
            footer_lines.append(f"Skipped existing actions: {artifacts.skipped_action_count}")
        footer_lines.append(f"Review queue: vertex actions review --program {artifacts.program_id}")
    if artifacts.review_plan_applied:
        footer_lines.append(
            f"Applied review decisions: {artifacts.approved_action_count} approved | {artifacts.dismissed_action_count} dismissed | {artifacts.pending_action_count} pending"
        )
    if artifacts.ado_apply_manifest_path is not None:
        footer_lines.append(
            "ADO apply: "
            f"{artifacts.ado_apply_applied_count} applied | "
            f"{artifacts.ado_apply_skipped_count} skipped | "
            f"{artifacts.ado_apply_conflict_count} conflict | "
            f"{artifacts.ado_apply_failed_count} failed"
        )
        footer_lines.append(
            f"Applied manifest: {_display_path(artifacts.ado_apply_manifest_path, programs_root=programs_root)}"
        )
    if artifacts.html_path is not None:
        footer_lines.append(f"HTML artifact: {_display_path(artifacts.html_path, programs_root=programs_root)}")
    if artifacts.teams_path is not None:
        footer_lines.append(f"Teams draft: {_display_path(artifacts.teams_path, programs_root=programs_root)}")
    console.print(Panel("\n".join(footer_lines), title="Next steps", box=box.ASCII, expand=True))
    return buffer.getvalue().rstrip("\n")


def _render_action_review_block(*, index: int, mapping: MeetingActionMapping) -> str:
    owner_label = mapping.action.owner_alias if not mapping.needs_owner else "needs_owner"
    due_label = mapping.action.due_date.isoformat() if mapping.action.due_date is not None else "needs_date"
    lines = [textwrap.fill(mapping.action.text, width=42)]
    if mapping.is_net_new:
        lines.extend(
            [
                f"status: net-new",
                f"owner: {owner_label}",
                f"due: {due_label}",
                "link: no matching work item",
                f"review: action {index} -> approve | edit | dismiss",
            ]
        )
        return "\n".join(lines)

    primary_item = mapping.matched_items[0]
    lines.extend(
        [
            f"status: mapped",
            f"work item: WI:{primary_item.work_item_id} | {primary_item.title}",
            f"workstream: {mapping.resolved_workstream_id or primary_item.workstream_id or '-'}",
            f"owner: {owner_label}",
            f"due: {due_label}",
            f"review: action {index} -> approve | edit | dismiss",
        ]
    )
    return "\n".join(lines)


def _render_evidence_block(*, index: int, mapping: MeetingActionMapping, transcript_content: str) -> str:
    excerpt, reason = _match_transcript_excerpt(mapping=mapping, transcript_content=transcript_content)
    lines = [
        f"trace: action {index}",
        f"reason: {reason}",
        "excerpt:",
        textwrap.fill(excerpt, width=42),
    ]
    if mapping.missing_work_item_ids:
        lines.append("missing refs: " + ", ".join(f"WI:{value}" for value in mapping.missing_work_item_ids))
    return "\n".join(lines)


def _match_transcript_excerpt(*, mapping: MeetingActionMapping, transcript_content: str) -> tuple[str, str]:
    sentences = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", transcript_content) if segment.strip()]
    if not sentences:
        return (transcript_content.strip() or "No transcript excerpt available.", "full transcript fallback")

    for work_item_id in mapping.action.linked_work_item_ids:
        token = f"WI:{work_item_id}".lower()
        for sentence in sentences:
            if token in sentence.lower():
                return (sentence, f"matched transcript ref WI:{work_item_id}")

    keywords = {token.lower() for token in re.findall(r"[A-Za-z0-9]{4,}", mapping.action.text)}
    best_sentence: str | None = None
    best_score = 0
    for sentence in sentences:
        score = sum(1 for token in keywords if token in sentence.lower())
        if score > best_score:
            best_score = score
            best_sentence = sentence
    if best_sentence is not None and best_score > 0:
        return (best_sentence, f"matched {best_score} action keywords")

    return (sentences[0], "first transcript sentence fallback")


def _build_review_plan(
    *,
    approve_action: tuple[int, ...],
    dismiss_action: tuple[int, ...],
    edit_action: int | None,
    edit_text: str | None,
    edit_owner: str | None,
    edit_due: str | None,
) -> MeetingCloseReviewPlan | None:
    if edit_action is None and any(value is not None for value in (edit_text, edit_owner, edit_due)):
        raise typer.BadParameter("--edit-text, --edit-owner, and --edit-due require --edit-action.")
    if edit_action is not None and all(value in (None, "") for value in (edit_text, edit_owner, edit_due)):
        raise typer.BadParameter("--edit-action requires at least one of --edit-text, --edit-owner, or --edit-due.")

    approved = tuple(sorted(dict.fromkeys(_validate_action_indexes(approve_action, option_name="--approve-action"))))
    dismissed = tuple(sorted(dict.fromkeys(_validate_action_indexes(dismiss_action, option_name="--dismiss-action"))))
    if edit_action is not None and edit_action <= 0:
        raise typer.BadParameter("--edit-action must be a positive integer.")
    if set(approved).intersection(dismissed):
        raise typer.BadParameter("The same action cannot be both approved and dismissed.")
    if edit_action is not None and edit_action in dismissed:
        raise typer.BadParameter("An edited action cannot also be dismissed.")
    if not approved and not dismissed and edit_action is None:
        return None

    edited_due_date = None
    if edit_due is not None and edit_due.strip():
        try:
            edited_due_date = date.fromisoformat(edit_due.strip())
        except ValueError as error:
            raise typer.BadParameter("--edit-due must be a valid ISO date (YYYY-MM-DD).") from error
    return MeetingCloseReviewPlan(
        approved_action_indexes=approved,
        dismissed_action_indexes=dismissed,
        edited_action_index=edit_action,
        edited_text=(edit_text.strip() if edit_text is not None and edit_text.strip() else None),
        edited_owner_alias=(edit_owner.strip() if edit_owner is not None and edit_owner.strip() else None),
        edited_due_date=edited_due_date,
    )


def _validate_action_indexes(values: tuple[int, ...], *, option_name: str) -> tuple[int, ...]:
    for value in values:
        if value <= 0:
            raise typer.BadParameter(f"{option_name} values must be positive integers.")
    return values


def _apply_review_plan(
    mappings: tuple[MeetingActionMapping, ...],
    *,
    review_plan: MeetingCloseReviewPlan | None,
) -> tuple[tuple[MeetingActionMapping, ...], MeetingCloseReviewSummary]:
    if review_plan is None:
        return (
            mappings,
            MeetingCloseReviewSummary(
                review_plan_applied=False,
                approved_action_count=0,
                dismissed_action_count=0,
                pending_action_count=len(mappings),
                approved_action_ids=(),
            ),
        )

    max_index = len(mappings)
    requested_indexes = set(review_plan.approved_action_indexes) | set(review_plan.dismissed_action_indexes)
    if review_plan.edited_action_index is not None:
        requested_indexes.add(review_plan.edited_action_index)
    invalid_indexes = sorted(index for index in requested_indexes if index < 1 or index > max_index)
    if invalid_indexes:
        preview = ", ".join(str(index) for index in invalid_indexes)
        raise ValueError(f"Meeting action review index out of range: {preview}.")

    kept: list[MeetingActionMapping] = []
    approved_action_ids: list[str] = []
    dismissed_count = 0
    approved_count = 0
    for index, mapping in enumerate(mappings, start=1):
        if index in review_plan.dismissed_action_indexes:
            dismissed_count += 1
            continue
        if review_plan.edited_action_index == index:
            mapping = _edit_mapping(
                mapping,
                edited_text=review_plan.edited_text,
                edited_owner_alias=review_plan.edited_owner_alias,
                edited_due_date=review_plan.edited_due_date,
            )
            approved_count += 1
            approved_action_ids.append(mapping.action.id)
            kept.append(mapping)
            continue
        if index in review_plan.approved_action_indexes:
            approved_count += 1
            approved_action_ids.append(mapping.action.id)
            kept.append(mapping)
            continue
        kept.append(mapping)

    pending_count = len(mappings) - approved_count - dismissed_count
    return (
        tuple(kept),
        MeetingCloseReviewSummary(
            review_plan_applied=True,
            approved_action_count=approved_count,
            dismissed_action_count=dismissed_count,
            pending_action_count=pending_count,
            approved_action_ids=tuple(approved_action_ids),
        ),
    )


def _edit_mapping(
    mapping: MeetingActionMapping,
    *,
    edited_text: str | None,
    edited_owner_alias: str | None,
    edited_due_date: date | None,
) -> MeetingActionMapping:
    edited_action = replace(
        mapping.action,
        text=edited_text or mapping.action.text,
        owner_alias=edited_owner_alias or mapping.action.owner_alias,
        due_date=edited_due_date if edited_due_date is not None else mapping.action.due_date,
    )
    normalized_owner = edited_action.owner_alias.strip().lower()
    return replace(
        mapping,
        action=edited_action,
        needs_owner=normalized_owner in {"", "unknown", "tbd", "unassigned"},
        needs_due_date=edited_action.due_date is None,
    )


def _get_terminal_width() -> int:
    return shutil.get_terminal_size(fallback=(100, 40)).columns


def _render_artifacts_body(
    *,
    program_id: str,
    meeting_id: str,
    transcript_title: str | None,
    captured_at: str | None,
    web_url: str | None,
    extractor: str,
    mappings: tuple[MeetingActionMapping, ...],
    next_checkpoint_date: date | None,
    follow_up_message: str,
) -> str:
    mapped_count = sum(1 for mapping in mappings if not mapping.is_net_new)
    net_new_count = sum(1 for mapping in mappings if mapping.is_net_new)
    lines = [
        f"MEETING CLOSE - {program_id}",
        f"Transcript: {meeting_id}",
        f"Title: {transcript_title or '-'}",
        f"Captured: {captured_at or '-'}",
        f"Source: {web_url or '-'}",
        f"Extractor: {extractor}",
        f"Actions: {len(mappings)} | mapped: {mapped_count} | net-new: {net_new_count}",
        f"Suggested next checkpoint: {next_checkpoint_date.isoformat() if next_checkpoint_date is not None else '-'}",
        "",
        "Mapped actions:",
    ]
    mapped_lines_added = False
    for mapping in mappings:
        if mapping.is_net_new:
            continue
        mapped_lines_added = True
        primary_item = mapping.matched_items[0]
        due_label = mapping.action.due_date.isoformat() if mapping.action.due_date is not None else "needs_date"
        owner_label = mapping.action.owner_alias if not mapping.needs_owner else "needs_owner"
        lines.append(
            f"- WI:{primary_item.work_item_id} | {primary_item.title} | ws {mapping.resolved_workstream_id or primary_item.workstream_id or '-'} | owner {owner_label} | due {due_label}"
        )
        lines.append(f"  {mapping.action.text}")
    if not mapped_lines_added:
        lines.append("- none")

    lines.extend(("", "Net-new or incomplete actions:"))
    incomplete_lines_added = False
    for mapping in mappings:
        if not mapping.is_net_new:
            continue
        incomplete_lines_added = True
        owner_label = mapping.action.owner_alias if not mapping.needs_owner else "needs_owner"
        due_label = mapping.action.due_date.isoformat() if mapping.action.due_date is not None else "needs_date"
        lines.append(f"- net-new | owner {owner_label} | due {due_label}")
        lines.append(f"  {mapping.action.text}")
    if not incomplete_lines_added:
        lines.append("- none")

    lines.extend(("", "Follow-up draft:", follow_up_message))
    return "\n".join(lines)


def _render_html_artifact(
    *,
    program_id: str,
    meeting_id: str,
    transcript_title: str | None,
    captured_at: str | None,
    web_url: str | None,
    extractor: str,
    mappings: tuple[MeetingActionMapping, ...],
    next_checkpoint_date: date | None,
    follow_up_message: str,
) -> str:
    mapped_lines: list[str] = []
    net_new_lines: list[str] = []
    for mapping in mappings:
        owner_label = mapping.action.owner_alias if not mapping.needs_owner else "needs_owner"
        due_label = mapping.action.due_date.isoformat() if mapping.action.due_date is not None else "needs_date"
        if mapping.is_net_new:
            net_new_lines.append(
                f"<li><strong>net-new</strong> | owner {escape(owner_label)} | due {escape(due_label)}<br/>{escape(mapping.action.text)}</li>"
            )
            continue
        primary_item = mapping.matched_items[0]
        mapped_lines.append(
            f"<li><strong>WI:{primary_item.work_item_id}</strong> | {escape(primary_item.title)} | ws {escape(mapping.resolved_workstream_id or primary_item.workstream_id or '-')} | owner {escape(owner_label)} | due {escape(due_label)}<br/>{escape(mapping.action.text)}</li>"
        )
    follow_up_html = "".join(f"<p>{escape(line)}</p>" for line in follow_up_message.splitlines())
    parts = [
        "<html><body style=\"font-family: Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif;\">",
        f"<h1>Meeting Close - {escape(program_id)}</h1>",
        f"<p><strong>Transcript:</strong> {escape(meeting_id)}<br/><strong>Title:</strong> {escape(transcript_title or '-')}<br/><strong>Captured:</strong> {escape(captured_at or '-')}<br/><strong>Source:</strong> {escape(web_url or '-')}<br/><strong>Extractor:</strong> {escape(extractor)}<br/><strong>Suggested next checkpoint:</strong> {escape(next_checkpoint_date.isoformat() if next_checkpoint_date is not None else '-')}</p>",
        "<h2>Mapped actions</h2>",
        "<ul>" + ("".join(mapped_lines) if mapped_lines else "<li>none</li>") + "</ul>",
        "<h2>Net-new or incomplete actions</h2>",
        "<ul>" + ("".join(net_new_lines) if net_new_lines else "<li>none</li>") + "</ul>",
        "<h2>Follow-up draft</h2>",
        follow_up_html,
        "</body></html>",
    ]
    return "".join(parts)


def _render_teams_artifact(
    *,
    program_id: str,
    meeting_id: str,
    transcript_title: str | None,
    captured_at: str | None,
    web_url: str | None,
    extractor: str,
    mappings: tuple[MeetingActionMapping, ...],
    next_checkpoint_date: date | None,
    follow_up_message: str,
) -> str:
    mapped_actions = tuple(mapping for mapping in mappings if not mapping.is_net_new)
    net_new_actions = tuple(mapping for mapping in mappings if mapping.is_net_new)
    return TeamsRenderer(program_id).render_template(
        "meeting_close.teams.j2",
        program_id=program_id,
        meeting_id=meeting_id,
        transcript_title=transcript_title or "-",
        captured_at=captured_at or "-",
        web_url=web_url,
        extractor=extractor,
        mapped_actions=mapped_actions,
        net_new_actions=net_new_actions,
        next_checkpoint_date=next_checkpoint_date.isoformat() if next_checkpoint_date is not None else "-",
        follow_up_lines=tuple(line for line in follow_up_message.splitlines() if line.strip()),
    )


def _normalize_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"human", "json"}:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    return normalized


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _display_path(path: Path | None, *, programs_root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.relative_to(programs_root.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_identifier(value: str) -> str:
    normalized = value.strip().lower()
    safe = "".join(character if character.isalnum() else "-" for character in normalized)
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-")
    return safe or "meeting"