from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import webbrowser

import typer

from src.commands.report import _ado_item_base_url
from src.commands.review_full import prepare_review_full_context
from src.core.ado_semantics import item_owner_alias
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.config_loader import REPORTS_ROOT, ReviewSettings, load_bundle
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.models import ReviewSection, ReviewState, ReviewStatus
from src.core.models_v2 import ActionItem, ActionStatus
from src.core.program_fact_store import load_program_facts, project_action_items
from src.core.reviewer_renderer import ReviewerRenderer, ReviewerTrackedEntryRow, ReviewerVitalityRow
from src.core.review_status_store import load_review_status, save_review_status
from src.core.snapshot_store import ARCHIVE_ROOT


app = typer.Typer(help="Manage per-section review status for the active issue.")


@dataclass(frozen=True, slots=True)
class ReviewSectionExportArtifacts:
    issue_number: int
    html_path: Path


@app.command("show")
def show_review_sections_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to inspect. Defaults to the active issue."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    resolved_issue, review_status, review_settings, resolved_reports_root = _load_review_context(edition, issue)
    assigned_reviewers = _assigned_reviewers_by_section(review_settings)
    current_manifest_id = _try_load_active_manifest_id(edition, resolved_issue)

    if format == "human":
        typer.echo(f"REVIEW STATUS — {edition} Issue {resolved_issue:03d}")
        typer.echo(f"Source: {resolved_reports_root / edition / 'review_status.yaml'}")
        for section in review_status.sections:
            assigned = assigned_reviewers.get(section.section_id)
            actual_reviewer = section.reviewer or assigned or "unassigned"
            updated_at = section.updated_at.isoformat() if section.updated_at is not None else "never"
            note = section.note or ""
            typer.echo(
                f"- {section.section_id}: {_display_state_label(section, current_manifest_id)} | reviewer={actual_reviewer} | updated_at={updated_at}"
            )
            if note:
                typer.echo(f"  note: {note}")
    else:
        typer.echo(
            render_review_sections_show_output(
                edition_name=edition,
                issue_number=resolved_issue,
                review_status=review_status,
                assigned_reviewers=assigned_reviewers,
                current_manifest_id=current_manifest_id,
                source_path=resolved_reports_root / edition / "review_status.yaml",
                format=format,
            ),
            nl=False,
        )


def render_review_sections_show_output(
    *,
    edition_name: str,
    issue_number: int,
    review_status: ReviewStatus,
    assigned_reviewers: dict[str, str],
    current_manifest_id: str | None,
    source_path: Path,
    format: str,
) -> str:
    payload = {
        "edition_name": edition_name,
        "issue_number": issue_number,
        "source_path": str(source_path),
        "sections": [
            {
                "section_id": section.section_id,
                "state": section.state.value,
                "display_state": _display_state_label(section, current_manifest_id),
                "reviewer": section.reviewer or assigned_reviewers.get(section.section_id) or "unassigned",
                "updated_at": section.updated_at.isoformat() if section.updated_at is not None else None,
                "note": section.note,
            }
            for section in review_status.sections
        ],
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("edition_name", "issue_number", "source_path", "section_id", "state", "display_state", "reviewer", "updated_at", "note"))
        for section in payload["sections"]:  # type: ignore[attr-defined]
            writer.writerow(
                (
                    payload["edition_name"],
                    payload["issue_number"],
                    payload["source_path"],
                    section["section_id"],
                    section["state"],
                    section["display_state"],
                    section["reviewer"],
                    section["updated_at"] or "",
                    section["note"] or "",
                )
            )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


@app.command("set")
def set_review_section_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to update. Defaults to the active issue."),
    section: str = typer.Option(..., "--section", help="Section id, for example exec_summary or ws:deployment."),
    state: str = typer.Option(..., "--state", help="Review state: pending, sent, approved, changes_requested, rejected."),
    note: str | None = typer.Option(None, "--note", help="Optional reviewer note."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Optional reviewer name override."),
) -> None:
    try:
        resolved_issue, review_status, review_settings, resolved_reports_root = _load_review_context(edition, issue)
        manifest_id = None if ReviewState.from_string(state) == ReviewState.PENDING else _load_active_manifest_id(edition, resolved_issue)
        resolved_state = ReviewState.from_string(state)
        updated_status = _update_review_status(
            review_status=review_status,
            review_settings=review_settings,
            section_id=section,
            state=resolved_state,
            note=note,
            reviewer=reviewer,
            manifest_id=manifest_id,
        )
        save_review_status(edition, updated_status, reports_root=resolved_reports_root)
    except typer.BadParameter as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    typer.echo(f"Updated {section} to {resolved_state.value} for Issue {resolved_issue:03d}.")


@app.command("clear")
def clear_review_section_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to update. Defaults to the active issue."),
    section: str = typer.Option(..., "--section", help="Section id to reset to pending."),
) -> None:
    resolved_issue, review_status, review_settings, resolved_reports_root = _load_review_context(edition, issue)
    updated_status = _update_review_status(
        review_status=review_status,
        review_settings=review_settings,
        section_id=section,
        state=ReviewState.PENDING,
        note=None,
        reviewer=None,
        manifest_id=None,
        clear=True,
    )
    save_review_status(edition, updated_status, reports_root=resolved_reports_root)
    typer.echo(f"Cleared {section} review state for Issue {resolved_issue:03d}.")


@app.command("export")
def export_review_section_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to export. Defaults to the active issue."),
    section: str = typer.Option(..., "--section", help="Workstream review section id, for example ws:deployment_readiness."),
    open_browser: bool = typer.Option(False, "--open/--no-open", help="Open the exported section HTML in the browser after rendering."),
) -> None:
    try:
        artifacts = export_review_section(
            edition_name=edition,
            section_id=section,
            issue_number=issue,
            open_browser=open_browser,
        )
    except typer.BadParameter as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    typer.echo(f"Exported {section} review portal for Issue {artifacts.issue_number:03d}.")
    typer.echo(f"Section HTML: {artifacts.html_path}")


def export_review_section(
    *,
    edition_name: str,
    section_id: str,
    issue_number: int | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    output_root: Path | None = None,
    open_browser: bool = False,
) -> ReviewSectionExportArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    normalized_section_id = section_id.strip()
    if not normalized_section_id.startswith("ws:"):
        raise typer.BadParameter("review-sections export only supports workstream section ids, for example ws:deployment_readiness.")

    review_data = prepare_review_full_context(
        edition_name=edition_name,
        issue_number=issue_number,
        reports_root=resolved_reports_root,
        archive_root=resolved_archive_root,
    )
    selected_section = next(
        (section for section in review_data.reviewer_context.sections if section.section_id == normalized_section_id),
        None,
    )
    if selected_section is None:
        known = ", ".join(section.section_id for section in review_data.reviewer_context.sections if section.section_id.startswith("ws:"))
        raise typer.BadParameter(f"Unknown workstream review section '{normalized_section_id}'. Known workstream sections: {known}")

    vitality_rows = _filter_section_vitality_rows(
        section=selected_section,
        items=review_data.items,
        vitality_rows=review_data.reviewer_context.owner_vitality_rows,
    )
    remediation_rows = _build_remediation_rows(
        program_id=review_data.program_id,
        section=selected_section,
        programs_root=resolved_reports_root.parent / "programs",
        ado_item_base_url=_ado_item_base_url(review_data.bundle),
    )
    reviewer_html_path = get_program_output_dir(edition_name, programs_root=resolved_reports_root.parent / "programs") / "review" / f"issue_{review_data.issue_number:03d}.html"
    reviewer_html_uri = _artifact_uri(
        review_data.bundle.config.m365.artifact_base_url,
        reviewer_html_path,
    )
    render_command = f"vertex review-sections set --edition {edition_name} --section {selected_section.section_id}"
    html = ReviewerRenderer(edition_name, reports_root=resolved_reports_root).render_fragment(
        "base.review_section.j2",
        title=f"{selected_section.title} | DRI Review",
        subtitle=f"Issue {review_data.issue_number:03d} self-service review portal",
        edition_name=edition_name,
        issue_number=review_data.issue_number,
        section=selected_section,
        telemetry_rows=review_data.reviewer_context.telemetry_rows,
        vitality_rows=vitality_rows,
        remediation_rows=remediation_rows,
        published_html_uri=review_data.reviewer_context.published_html_uri,
        reviewer_html_uri=reviewer_html_uri,
        review_set_command=render_command,
        approve_command=f"{render_command} --state approved --reviewer <name>",
        changes_requested_command=f"{render_command} --state changes_requested --reviewer <name> --note \"<feedback>\"",
    ).strip() + "\n"
    target_path = _write_section_export_html(
        get_program_output_dir(edition_name, programs_root=resolved_reports_root.parent / "programs") / "review" / "sections" / f"issue_{review_data.issue_number:03d}.{_sanitize_section_filename_component(selected_section.section_id)}.html",
        html,
    )
    if open_browser:
        webbrowser.open(target_path.resolve().as_uri())
    return ReviewSectionExportArtifacts(issue_number=review_data.issue_number, html_path=target_path)


def _load_review_context(
    edition_name: str,
    issue_number: int | None,
    *,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
) -> tuple[int, ReviewStatus, ReviewSettings, Path]:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=resolved_reports_root.parent / "programs",
    )
    archive_index = read_archive_index(edition_name, archive_root=resolved_archive_root)
    resolved_issue_number = issue_number if issue_number is not None else _next_issue_number(archive_index)
    review_status = load_review_status(edition_name, reports_root=resolved_reports_root)
    if review_status is None or review_status.issue_number != resolved_issue_number:
        raise typer.BadParameter(
            f"review_status.yaml is not initialized for Issue {resolved_issue_number:03d}. Run `vertex report --dry-run --edition {edition_name}` first."
        )
    return resolved_issue_number, review_status, bundle.review, resolved_reports_root


def _update_review_status(
    *,
    review_status: ReviewStatus,
    review_settings: ReviewSettings,
    section_id: str,
    state: ReviewState,
    note: str | None,
    reviewer: str | None,
    manifest_id: str | None,
    clear: bool = False,
) -> ReviewStatus:
    known_sections = {section.section_id: section for section in review_status.sections}
    if section_id not in known_sections:
        known = ", ".join(section.section_id for section in review_status.sections)
        raise typer.BadParameter(f"Unknown review section '{section_id}'. Known sections: {known}")

    assigned_reviewer = _assigned_reviewers_by_section(review_settings).get(section_id)
    updated_sections = []
    for section in review_status.sections:
        if section.section_id != section_id:
            updated_sections.append(section)
            continue
        updated_sections.append(
            ReviewSection(
                section_id=section.section_id,
                state=state,
                reviewer=(None if clear else (reviewer or section.reviewer or assigned_reviewer)),
                note=(None if clear else (note if note is not None else section.note)),
                updated_at=datetime.now(timezone.utc),
                manifest_id=(None if clear or state == ReviewState.PENDING else manifest_id),
            )
        )

    return ReviewStatus(issue_number=review_status.issue_number, sections=tuple(updated_sections))


def _assigned_reviewers_by_section(review_settings: ReviewSettings) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for reviewer in review_settings.reviewers:
        for section_id in reviewer.sections:
            assignments[section_id] = reviewer.name
    return assignments


def _next_issue_number(archive_index) -> int:
    latest = find_latest_confirmed_entry(archive_index)
    if latest is None:
        return 1
    return latest.issue_number + 1


def _load_active_manifest_id(edition_name: str, issue_number: int) -> str:
    path = get_program_output_dir(edition_name, programs_root=PROGRAMS_ROOT) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
    if not path.exists():
        raise typer.BadParameter(
            f"Manifest not found at {path}. Run `vertex report --dry-run --edition {edition_name} --issue {issue_number}` first."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise typer.BadParameter(f"Manifest at {path} is invalid.") from error
    manifest_id = payload.get("manifest_id") if isinstance(payload, dict) else None
    if not manifest_id:
        raise typer.BadParameter(f"Manifest at {path} is missing manifest_id.")
    return str(manifest_id)


def _try_load_active_manifest_id(edition_name: str, issue_number: int) -> str | None:
    try:
        return _load_active_manifest_id(edition_name, issue_number)
    except typer.BadParameter:
        return None


def _display_state_label(section: ReviewSection, current_manifest_id: str | None) -> str:
    if section.state == ReviewState.SKIPPED_NO_DELTA:
        return "skipped_no_delta (no delta - skipped)"
    if (
        current_manifest_id is not None
        and section.state == ReviewState.APPROVED
        and section.manifest_id is not None
        and section.manifest_id != current_manifest_id
    ):
        return f"{section.state.value} STALE"
    return section.state.value


def _filter_section_vitality_rows(
    *,
    section,
    items,
    vitality_rows: tuple[ReviewerVitalityRow, ...],
) -> tuple[ReviewerVitalityRow, ...]:
    relevant_item_ids = set(section.item_ids)
    if not relevant_item_ids:
        return ()
    owner_aliases = {
        alias
        for item in items
        if item.id in relevant_item_ids
        for alias in (item_owner_alias(item),)
        if alias is not None
    }
    if not owner_aliases:
        return ()
    return tuple(
        row
        for row in vitality_rows
        if row.owner_alias.strip().lower() in owner_aliases
    )


def _build_remediation_rows(
    *,
    program_id: str | None,
    section,
    programs_root: Path,
    ado_item_base_url: str | None,
) -> tuple[ReviewerTrackedEntryRow, ...]:
    if program_id is None:
        return ()
    relevant_item_ids = set(section.item_ids)
    workstream_id = section.section_id.removeprefix("ws:")
    program_facts = load_program_facts(
        program_id,
        db_root=programs_root.parent,
        programs_root=programs_root,
    )
    active_actions = tuple(
        action
        for action in project_action_items(program_facts)
        if action.status in {ActionStatus.PROPOSED, ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
    )
    scoped_actions = tuple(
        action
        for action in active_actions
        if action.workstream_id == workstream_id or relevant_item_ids.intersection(action.linked_work_item_ids)
    )
    return tuple(
        ReviewerTrackedEntryRow(
            title=_format_remediation_action_title(action),
            detail=_format_remediation_action_detail(action),
            summary=action.text,
            href=_resolve_remediation_action_href(action, ado_item_base_url),
        )
        for action in sorted(
            scoped_actions,
            key=lambda entry: (
                entry.owner_alias.lower(),
                entry.due_date.isoformat() if entry.due_date is not None else "9999-12-31",
                entry.text.lower(),
            ),
        )
    )


def _format_remediation_action_title(action: ActionItem) -> str:
    return " · ".join((action.owner_alias, action.id, action.status.value))


def _format_remediation_action_detail(action: ActionItem) -> str:
    parts = [f"Due {action.due_date.isoformat()}" if action.due_date is not None else "Due -"]
    if action.workstream_id:
        parts.append(f"Workstream {action.workstream_id}")
    if action.linked_work_item_ids:
        parts.append("Linked " + ", ".join(f"WI:{work_item_id}" for work_item_id in action.linked_work_item_ids))
    if action.linked_risk_id:
        parts.append(f"Risk {action.linked_risk_id}")
    return " | ".join(parts)


def _resolve_remediation_action_href(action: ActionItem, ado_item_base_url: str | None) -> str | None:
    if not ado_item_base_url:
        return None
    for work_item_id in action.linked_work_item_ids:
        if work_item_id > 0:
            return f"{ado_item_base_url}/{work_item_id}"
    return None


def _sanitize_section_filename_component(value: str) -> str:
    cleaned = [character.lower() if character.isalnum() else "_" for character in value.strip()]
    collapsed = "".join(cleaned).strip("_")
    return collapsed or "section"


def _write_section_export_html(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _artifact_uri(artifact_base_url: str | None, path: Path, *, output_root: Path | None = None) -> str:
    if artifact_base_url and output_root is not None:
        try:
            relative_path = path.resolve().relative_to(output_root.resolve())
        except ValueError:
            return path.resolve().as_uri()
        return artifact_base_url.rstrip("/") + "/" + "/".join(relative_path.parts)
    return path.resolve().as_uri()
