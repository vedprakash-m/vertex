from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from io import StringIO
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

import typer

from src.core.ado_client import ADOClient
from src.core.ado_enrichment import ADO_RISK_ASSESSMENT_COMMENT_FIELD, ADO_RISK_ASSESSMENT_FIELD, infer_ado_risk_level, normalize_risk_assessment
from src.core.action_tracker import assess_action_staleness
from src.core.archive_store import load_previous_confirmed_snapshot, read_archive_index
from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerState
from src.core.config_loader import ReportBundle, load_bundle
from src.core.edition_resolver import get_program_output_dir, resolve_edition
from src.core.exceptions import QueryError, StateError
from src.core.freshness_engine import build_dri_summaries, build_freshness_report
from src.core.models_v2 import ActionStatus
from src.core.narrative_store import build_workstream_narrative_history, load_narratives
from src.core.notification_state_store import ConfirmedNotification, append_confirmed_notify_run, load_latest_notification_state
from src.core.models import Comment, DRISummary, EditionType, FreshnessReport, NotifyPreview, Revision, ReviewStatus, RiskLevel, Snapshot, WorkItem
from src.core.program_fact_store import ProgramFactSnapshot, load_program_facts, project_action_items, project_milestones
from src.core.quality_matrix_engine import SliceQualityRecord, build_quality_matrix
from src.core.review_status_store import load_review_status
from src.core.ncfl_proposal_store import conflicting_pending_proposals, load_proposals, stale_pending_proposals
from src.core.snapshot_store import ARCHIVE_ROOT as SNAPSHOT_ARCHIVE_ROOT, get_archive_root, read_snapshot
from src.core.trusted_baseline_store import load_trusted_baseline_issue


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
PROGRAMS_ROOT = REPO_ROOT / "programs"
ARCHIVE_ROOT = SNAPSHOT_ARCHIVE_ROOT
DEFAULT_ADO_TOP = 1000
_SINCE_PATTERN = re.compile(r"^(?P<value>\d+)d$", re.IGNORECASE)
_BATCH_FIELDS = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AssignedTo",
    "System.AreaPath",
    "System.IterationPath",
    "System.ChangedDate",
    "System.Description",
    "Microsoft.VSTS.Scheduling.TargetDate",
    "System.Tags",
    ADO_RISK_ASSESSMENT_FIELD,
    ADO_RISK_ASSESSMENT_COMMENT_FIELD,
)

FreshnessLoader = Callable[[ReportBundle, datetime, datetime], tuple[tuple[WorkItem, ...], int]]


@dataclass(frozen=True, slots=True)
class FreshnessArtifacts:
    issue_number: int
    exit_code: int
    report: FreshnessReport
    dri_summaries: tuple[DRISummary, ...]
    items: tuple[WorkItem, ...]
    item_urls: dict[int, str]
    slice_findings: tuple[SliceQualityRecord, ...]
    stale_banner: str | None
    proposal_summary: str | None
    plaintext_body: str
    markdown_body: str
    html_body: str
    md_path: Path
    html_path: Path
    notify_previews: tuple[NotifyPreview, ...]


def freshness_command(
    edition: str = typer.Option("", "--edition", help="Edition used for the freshness run."),
    since: str | None = typer.Option(None, "--since", help="Relative lookback window, for example 14d."),
    by: str = typer.Option("dri", "--by", help="Grouping mode for freshness findings."),
    teams_format: bool = typer.Option(False, "--teams-format", help="Print the Teams/Markdown version to stdout."),
    notify: bool = typer.Option(False, "--notify", help="Preview outbound DRI notifications without sending them."),
    allow_stale: bool = typer.Option(False, "--allow-stale", help="Allow stale-snapshot fallback when live ADO is unavailable."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview notification output without send confirmation."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if format != "human" and teams_format:
        raise typer.BadParameter("--teams-format is only supported with --format human.")
    if format != "human" and notify and not dry_run:
        raise typer.BadParameter("--format json/csv requires --dry-run when --notify is enabled.")

    artifacts = generate_freshness_report(
        edition_name=edition,
        since=since,
        by=by,
        notify=notify,
        allow_stale=allow_stale,
        dry_run=dry_run,
    )

    notification_log_path: Path | None = None
    if notify and artifacts.notify_previews:
        if format == "human":
            typer.echo("")
        if not dry_run:
            if not typer.confirm(
                f"Previewed {len(artifacts.notify_previews)} notification email(s). Record this notify run for FR-45 tracking?",
                default=True,
            ):
                raise typer.Exit(code=1)
            notification_log_path = _record_confirmed_notify_run(
                edition_name=edition,
                issue_number=artifacts.issue_number,
                dri_summaries=artifacts.dri_summaries,
                notify_previews=artifacts.notify_previews,
                programs_root=PROGRAMS_ROOT,
                confirmed_at=datetime.now(timezone.utc),
            )

    if format == "human":
        typer.echo(artifacts.markdown_body if teams_format else artifacts.plaintext_body)
        typer.echo(f"Markdown: {artifacts.md_path}")
        typer.echo(f"HTML: {artifacts.html_path}")
        if notify and artifacts.notify_previews:
            if notification_log_path is not None:
                typer.echo(f"Notification log: {notification_log_path}")
            typer.echo("Send disabled until Phase 2 (Graph permissions required).")
    else:
        typer.echo(
            render_freshness_output(
                _build_freshness_payload(
                    edition_name=edition,
                    artifacts=artifacts,
                    notification_log_path=notification_log_path,
                ),
                format=format,
            ),
            nl=False,
        )

    raise typer.Exit(code=artifacts.exit_code)


def _build_freshness_payload(
    *,
    edition_name: str,
    artifacts: FreshnessArtifacts,
    notification_log_path: Path | None,
) -> dict[str, Any]:
    items_by_id = {item.id: item for item in artifacts.items}
    findings: list[dict[str, Any]] = []
    dri_summaries: list[dict[str, Any]] = []

    for summary in artifacts.dri_summaries:
        dri_summaries.append(
            {
                "dri_email": summary.dri_email,
                "dri_name": summary.dri_name,
                "finding_count": len(summary.items),
                "open_count": summary.open_count,
                "overdue_count": summary.overdue_count,
                "stale_count": summary.stale_count,
            }
        )
        for finding in summary.items:
            item = items_by_id.get(finding.work_item_id)
            findings.append(
                {
                    "action_label": finding.action_label,
                    "action_message": finding.action_message,
                    "dri_email": summary.dri_email,
                    "dri_name": summary.dri_name,
                    "item_title": item.title if item is not None else None,
                    "item_url": artifacts.item_urls.get(finding.work_item_id),
                    "message": finding.message,
                    "rule_id": finding.rule_id,
                    "severity": finding.severity,
                    "suggested_fix": finding.suggested_fix,
                    "work_item_id": finding.work_item_id,
                }
            )

    return {
        "edition": edition_name,
        "exit_code": artifacts.exit_code,
        "issue_number": artifacts.issue_number,
        "notification_log_path": str(notification_log_path) if notification_log_path is not None else None,
        "notify_previews": [
            {
                "attachments": list(preview.attachments),
                "cc": list(preview.cc),
                "subject": preview.subject,
                "to": list(preview.to),
            }
            for preview in artifacts.notify_previews
        ],
        "outputs": {
            "html_path": str(artifacts.html_path),
            "markdown_path": str(artifacts.md_path),
        },
        "report": {
            "blocks": artifacts.report.blocks,
            "finding_count": len(artifacts.report.items),
            "infos": artifacts.report.infos,
            "is_clean": artifacts.report.is_clean,
            "warns": artifacts.report.warns,
        },
        "proposal_summary": artifacts.proposal_summary,
        "stale_banner": artifacts.stale_banner,
        "slice_findings": [
            {
                "issues": list(finding.issues),
                "slice_id": finding.slice_id,
                "stale_item_ids": list(finding.stale_item_ids),
            }
            for finding in artifacts.slice_findings
        ],
        "dri_summaries": dri_summaries,
        "findings": findings,
    }


def render_freshness_output(payload: dict[str, Any], *, format: str) -> str:
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        return _render_freshness_csv(payload)
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    raise typer.BadParameter("Human freshness output is rendered directly by the command.")


def _render_freshness_csv(payload: dict[str, Any]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    columns = (
        "edition",
        "issue_number",
        "dri_name",
        "dri_email",
        "work_item_id",
        "item_title",
        "severity",
        "rule_id",
        "action_label",
        "message",
        "action_message",
        "suggested_fix",
        "item_url",
        "blocks",
        "warns",
        "infos",
        "stale_banner",
        "markdown_path",
        "html_path",
    )
    writer.writerow(columns)

    report = payload["report"]
    outputs = payload["outputs"]
    findings = payload["findings"]
    if findings:
        for finding in findings:
            writer.writerow(
                [
                    payload["edition"],
                    payload["issue_number"],
                    finding["dri_name"],
                    finding["dri_email"],
                    finding["work_item_id"],
                    finding["item_title"],
                    finding["severity"],
                    finding["rule_id"],
                    finding["action_label"],
                    finding["message"],
                    finding["action_message"],
                    finding["suggested_fix"],
                    finding["item_url"],
                    report["blocks"],
                    report["warns"],
                    report["infos"],
                    payload["stale_banner"],
                    outputs["markdown_path"],
                    outputs["html_path"],
                ]
            )
    else:
        writer.writerow(
            [
                payload["edition"],
                payload["issue_number"],
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                report["blocks"],
                report["warns"],
                report["infos"],
                payload["stale_banner"],
                outputs["markdown_path"],
                outputs["html_path"],
            ]
        )
    return buffer.getvalue()


def generate_freshness_report(
    edition_name: str,
    since: str | None = None,
    by: str = "dri",
    notify: bool = False,
    allow_stale: bool = False,
    dry_run: bool = False,
    expected_issue_number: int | None = None,
    as_of: datetime | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    output_root: Path | None = None,
    programs_root: Path | None = None,
    work_item_loader: FreshnessLoader | None = None,
) -> FreshnessArtifacts:
    del dry_run
    if by != "dri":
        raise typer.BadParameter("Only '--by dri' is currently supported.")

    started_at = datetime.now(timezone.utc)
    data_as_of = as_of or started_at
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=resolved_reports_root.parent / "programs",
    )
    archive_index = read_archive_index(edition_name, archive_root=resolved_archive_root)
    issue_number = _next_issue_number(archive_index)
    if expected_issue_number is not None and issue_number != expected_issue_number:
        raise StateError(
            f"Notify previews are only available for pending issue {issue_number:03d}; requested issue {expected_issue_number:03d}."
        )
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=issue_number,
        programs_root=resolved_reports_root.parent / "programs",
    )
    previous_snapshot, previous_issue_number = _load_previous_snapshot(
        edition_name,
        issue_number,
        resolved_archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    since_datetime = _resolve_since(since, data_as_of, bundle.config.ado.date_window_days)
    loaded_narratives = load_narratives(edition_name, issue_number, reports_root=resolved_reports_root)
    workstream_blurbs = {
        section_id.removeprefix("ws_").removesuffix(".md"): content.strip()
        for section_id, content in loaded_narratives.items()
        if section_id.startswith("ws_") and section_id.endswith(".md")
    }

    loader = work_item_loader or _load_live_freshness_items
    _programs_root = programs_root or resolved_reports_root.parent / "programs"
    breaker = CircuitBreaker(state_path=_breaker_state_path(_programs_root, edition_name))
    items, _ado_calls, stale_banner = _load_items_with_fallback(
        bundle=bundle,
        as_of=data_as_of,
        since=since_datetime,
        previous_snapshot=previous_snapshot,
        loader=loader,
        breaker=breaker,
    )
    previous_notification_state = None if stale_banner is not None else load_latest_notification_state(
        edition=edition_name,
        programs_root=_programs_root,
    )
    workstream_narrative_history = build_workstream_narrative_history(
        edition=edition_name,
        issue_number=issue_number,
        workstream_names=tuple(workstream.name for workstream in bundle.program_context.workstreams) if bundle.program_context is not None else (),
        current_workstream_blurbs=workstream_blurbs,
        archive_root=resolved_archive_root,
    )

    freshness_report = build_freshness_report(
        current_items=items,
        issue_number=issue_number,
        as_of=data_as_of,
        stale_warn_days=bundle.editorial_rules.stale_warn_days,
        stale_block_days=bundle.editorial_rules.stale_block_days,
        previous_snapshot=previous_snapshot,
        previous_notification_state=previous_notification_state,
        program_context=bundle.program_context,
        workstream_narrative_history=workstream_narrative_history,
    )
    quality_matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=issue_number,
        generated_at=data_as_of,
        current_items=items,
        previous_issue_number=previous_issue_number,
    )
    slice_findings = tuple(slice_row for slice_row in quality_matrix.slices if slice_row.stale_item_ids)
    dri_summaries = build_dri_summaries(freshness_report, items, bundle.program_context)
    item_urls = _build_item_urls(bundle, items)
    review_status = load_review_status(edition_name, reports_root=resolved_reports_root)
    review_summary, review_readiness = _summarize_review_status(review_status, issue_number=issue_number)
    resolved = resolve_edition(
        edition_name,
        programs_root=resolved_reports_root.parent / "programs",
    )
    action_summary_lines = _build_action_summary_lines(
        program_id=(resolved.paths.program_id if resolved is not None else None),
        programs_root=resolved_reports_root.parent / "programs",
        as_of=data_as_of,
        program_facts=(
            load_program_facts(
                resolved.paths.program_id,
                db_root=(resolved_reports_root.parent / "programs").parent,
                programs_root=resolved_reports_root.parent / "programs",
            )
            if resolved is not None
            else None
        ),
    )
    milestone_summary_lines = _build_milestone_summary_lines(
        program_id=(resolved.paths.program_id if resolved is not None else None),
        programs_root=resolved_reports_root.parent / "programs",
        freshness_report=freshness_report,
    )
    summary_lines = (*action_summary_lines, *milestone_summary_lines)
    proposal_summary = _build_ncfl_summary_line(
        program_id=(resolved.paths.program_id if resolved is not None else None),
        programs_root=resolved_reports_root.parent / "programs",
    )
    if proposal_summary is not None:
        summary_lines = (*summary_lines, proposal_summary)

    plaintext_body = _render_plaintext(
        edition_name=edition_name,
        as_of=data_as_of,
        previous_issue_number=previous_issue_number,
        report=freshness_report,
        dri_summaries=dri_summaries,
        items=items,
        slice_findings=slice_findings,
        stale_banner=stale_banner,
        review_summary=review_summary,
        review_readiness=review_readiness,
        summary_lines=summary_lines,
    )
    markdown_body = _render_markdown(
        edition_name=edition_name,
        as_of=data_as_of,
        previous_issue_number=previous_issue_number,
        report=freshness_report,
        dri_summaries=dri_summaries,
        items=items,
        item_urls=item_urls,
        slice_findings=slice_findings,
        stale_banner=stale_banner,
        review_summary=review_summary,
        review_readiness=review_readiness,
        summary_lines=summary_lines,
    )
    notify_previews = _build_notify_previews(bundle, dri_summaries, items, item_urls) if notify else ()
    if notify_previews:
        markdown_body = f"{markdown_body}\n\n{_render_notify_preview_markdown(notify_previews)}"
        plaintext_body = f"{plaintext_body}\n\n{_render_notify_preview_plaintext(notify_previews)}"
    html_body = _render_html(
        edition_name=edition_name,
        as_of=data_as_of,
        previous_issue_number=previous_issue_number,
        report=freshness_report,
        dri_summaries=dri_summaries,
        items=items,
        item_urls=item_urls,
        notify_previews=notify_previews,
        slice_findings=slice_findings,
        stale_banner=stale_banner,
        review_summary=review_summary,
        review_readiness=review_readiness,
        summary_lines=summary_lines,
    )

    output_dir = get_program_output_dir(edition_name, programs_root=_programs_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = _write_output_text(output_dir / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.freshness.md", markdown_body)
    html_path = _write_output_text(output_dir / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.freshness.html", html_body)

    exit_code = 3 if stale_banner is not None and not allow_stale else 3 if freshness_report.blocks else 2 if freshness_report.warns or slice_findings else 0
    return FreshnessArtifacts(
        issue_number=issue_number,
        exit_code=exit_code,
        report=freshness_report,
        dri_summaries=dri_summaries,
        items=items,
        item_urls=item_urls,
        slice_findings=slice_findings,
        stale_banner=stale_banner,
        proposal_summary=proposal_summary,
        plaintext_body=plaintext_body,
        markdown_body=markdown_body,
        html_body=html_body,
        md_path=md_path,
        html_path=html_path,
        notify_previews=notify_previews,
    )


def _breaker_state_path(programs_root: Path, edition_name: str) -> Path:
    return get_program_output_dir(edition_name, programs_root=programs_root) / ".ado_breaker.json"


def _load_items_with_fallback(
    *,
    bundle: ReportBundle,
    as_of: datetime,
    since: datetime,
    previous_snapshot: Snapshot | None,
    loader: FreshnessLoader,
    breaker: CircuitBreaker,
) -> tuple[tuple[WorkItem, ...], int, str | None]:
    allow_request, is_probe = breaker.should_allow_request(now=as_of)
    if not allow_request:
        if previous_snapshot is None:
            raise QueryError("ADO circuit breaker is open and no confirmed snapshot is available for fallback.")
        return (_snapshot_to_work_items(previous_snapshot), 0, _build_stale_banner(previous_snapshot))

    try:
        items, ado_calls = loader(bundle, as_of, since)
    except QueryError as error:
        breaker.record_failure(error=str(error), is_probe=is_probe, now=as_of)
        if breaker.get_state().state == CircuitBreakerState.OPEN and previous_snapshot is not None:
            return (_snapshot_to_work_items(previous_snapshot), 0, _build_stale_banner(previous_snapshot))
        raise

    breaker.record_success(is_probe=is_probe, now=as_of)
    return (items, ado_calls, None)


def _snapshot_to_work_items(snapshot: Snapshot) -> tuple[WorkItem, ...]:
    return tuple(
        WorkItem(
            id=item.id,
            type=item.type,
            title=item.title,
            state=item.state,
            assigned_to=item.assigned_to,
            assigned_to_email=None,
            area_path=item.area_path,
            iteration_path="",
            target_date=item.target_date,
            risk_level=item.risk_level,
            tags=list(item.tags),
            custom_fields={"changed_date": snapshot.ado_data_as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=snapshot.ado_data_as_of,
        )
        for item in snapshot.items
    )


def _build_stale_banner(snapshot: Snapshot) -> str:
    return (
        "STALE DATA: Live ADO is unavailable; "
        f"using confirmed snapshot from issue {snapshot.issue_number:03d} captured {snapshot.ado_data_as_of.isoformat()}."
    )


def _next_issue_number(index: Any) -> int:
    if not index.issues:
        return 1
    return max(entry.issue_number for entry in index.issues) + 1


def _load_previous_snapshot(
    edition_name: str,
    issue_number: int,
    archive_root: Path,
    trusted_issue_number: int | None = None,
) -> tuple[Snapshot | None, int | None]:
    if trusted_issue_number is not None:
        archive_index = read_archive_index(edition_name, archive_root=archive_root)
        for entry in archive_index.issues:
            if entry.kind != "confirmed" or entry.issue_number != trusted_issue_number or entry.snapshot_path is None:
                continue
            snapshot_path = Path(entry.snapshot_path)
            if not snapshot_path.exists():
                break
            return read_snapshot(snapshot_path), trusted_issue_number
    return load_previous_confirmed_snapshot(edition_name, issue_number, archive_root=archive_root)


def _resolve_since(value: str | None, as_of: datetime, default_days: int) -> datetime:
    if value is None:
        return as_of - timedelta(days=default_days)
    match = _SINCE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise typer.BadParameter("since must use Nd format, for example 14d")
    return as_of - timedelta(days=int(match.group("value")))


def _load_live_freshness_items(
    bundle: ReportBundle,
    as_of: datetime,
    since: datetime,
) -> tuple[tuple[WorkItem, ...], int]:
    client = ADOClient(
        organization=bundle.config.ado.organization,
        project=bundle.config.ado.project,
        timeout=bundle.config.ado.api_timeout_seconds or 30,
    )
    rows = client.query_all(
        filter_expression=_build_odata_filter(bundle, since),
        select_fields=(
            "WorkItemId",
            "WorkItemType",
            "Title",
            "State",
            "ChangedDate",
        ),
        top=DEFAULT_ADO_TOP,
    )
    ids = [int(row.get("WorkItemId") or row.get("id") or 0) for row in rows if int(row.get("WorkItemId") or row.get("id") or 0) > 0]
    batch_rows = client.query_work_items_batch(ids, _BATCH_FIELDS)
    batch_by_id = {int(row.get("id") or row.get("fields", {}).get("System.Id") or 0): row for row in batch_rows}

    items: list[WorkItem] = []
    ado_calls = 1 + (1 if ids else 0)
    for row in rows:
        work_item_id = int(row.get("WorkItemId") or row.get("id") or 0)
        if work_item_id <= 0:
            continue
        comment_rows = client.list_work_item_comments(work_item_id)
        revision_rows = client.list_work_item_revisions(work_item_id)
        ado_calls += 2
        items.append(
            _work_item_from_sources(
                raw=row,
                batch_row=batch_by_id.get(work_item_id, {}),
                comment_rows=comment_rows,
                revision_rows=revision_rows,
                fetched_at=as_of,
            )
        )
    return tuple(items), ado_calls


def _build_odata_filter(bundle: ReportBundle, since: datetime) -> str:
    conditions = [f"startswith(Area/AreaPath, '{p}')" for p in [x.replace("'", "''") for x in bundle.config.ado.area_paths]]
    type_conditions = [f"WorkItemType eq '{t}'" for t in [x.replace("'", "''") for x in bundle.config.ado.work_item_types]]
    state_conditions = [f"State eq '{s}'" for s in [x.replace("'", "''") for x in bundle.config.ado.excluded_states]]
    since_value = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return " and ".join(
        [
            f"( {' or '.join(conditions)} )",
            f"( {' or '.join(type_conditions)} )",
            f"ChangedDate ge {since_value}",
            f"not ( {' or '.join(state_conditions)} )" if state_conditions else "true",
        ]
    )


def _work_item_from_sources(
    raw: dict[str, Any],
    batch_row: dict[str, Any],
    comment_rows: list[dict[str, Any]],
    revision_rows: list[dict[str, Any]],
    fetched_at: datetime,
) -> WorkItem:
    work_item_id = int(raw.get("WorkItemId") or raw.get("id") or 0)
    fields = batch_row.get("fields", {}) if isinstance(batch_row, dict) else {}
    assigned_to, assigned_to_email = _parse_identity(fields.get("System.AssignedTo"))
    tags = _parse_tags(fields.get("System.Tags") or raw.get("Tags"))
    state = str(fields.get("System.State") or raw.get("State") or "Active")
    changed_date = _parse_datetime(fields.get("System.ChangedDate") or raw.get("ChangedDate"))
    risk_assessment = normalize_risk_assessment(fields.get(ADO_RISK_ASSESSMENT_FIELD))

    custom_fields: dict[str, object] = {}
    if changed_date is not None:
        custom_fields["changed_date"] = changed_date.isoformat()
    description = fields.get("System.Description")
    if isinstance(description, str) and description.strip():
        custom_fields["description"] = description

    return WorkItem(
        id=work_item_id,
        type=str(fields.get("System.WorkItemType") or raw.get("WorkItemType") or "WorkItem"),
        title=str(fields.get("System.Title") or raw.get("Title") or f"Work Item {work_item_id}"),
        state=state,
        assigned_to=assigned_to,
        assigned_to_email=assigned_to_email,
        area_path=str(fields.get("System.AreaPath") or raw.get("AreaPath") or raw.get("Area", {}).get("AreaPath") or ""),
        iteration_path=str(fields.get("System.IterationPath") or raw.get("IterationPath") or ""),
        target_date=_parse_date(fields.get("Microsoft.VSTS.Scheduling.TargetDate") or raw.get("TargetDate")),
        risk_level=infer_ado_risk_level(state, tags, risk_assessment),
        tags=tags,
        custom_fields=custom_fields,
        revisions=_parse_revisions(work_item_id, revision_rows),
        comments=_parse_comments(work_item_id, comment_rows),
        fetched_at=fetched_at,
        risk_assessment=risk_assessment,
        risk_assessment_comment=_optional_string(fields.get(ADO_RISK_ASSESSMENT_COMMENT_FIELD)),
    )


def _parse_identity(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        display_name = value.get("displayName") or value.get("name")
        email = value.get("uniqueName") or value.get("mailAddress")
        return (_optional_string(display_name), _optional_string(email))
    if isinstance(value, str):
        return (value, None)
    return (None, None)


def _parse_comments(work_item_id: int, rows: list[dict[str, Any]]) -> list[Comment]:
    comments: list[Comment] = []
    for row in rows:
        created_by, created_by_email = _parse_identity(row.get("createdBy"))
        created_date = _parse_datetime(row.get("publishedDate") or row.get("createdDate"))
        if created_date is None:
            continue
        comments.append(
            Comment(
                work_item_id=work_item_id,
                comment_id=int(row.get("id") or row.get("commentId") or 0),
                created_by=created_by or "Unknown",
                created_by_email=created_by_email or "",
                created_date=created_date,
                text=str(row.get("text") or row.get("renderedText") or ""),
            )
        )
    return comments


def _parse_revisions(work_item_id: int, rows: list[dict[str, Any]]) -> list[Revision]:
    revisions: list[Revision] = []
    previous_fields: dict[str, Any] | None = None
    sorted_rows = sorted(rows, key=lambda row: int(row.get("rev") or 0))
    for row in sorted_rows:
        fields = row.get("fields", {}) if isinstance(row.get("fields"), dict) else {}
        changed_date = _parse_datetime(fields.get("System.ChangedDate"))
        if changed_date is None:
            continue
        changed_by, changed_by_email = _parse_identity(fields.get("System.ChangedBy"))
        field_changes: dict[str, tuple[str | None, str | None]] = {}
        if previous_fields is not None:
            all_keys = set(previous_fields) | set(fields)
            for key in all_keys:
                old_value = _field_value(previous_fields.get(key))
                new_value = _field_value(fields.get(key))
                if old_value != new_value:
                    field_changes[key] = (old_value, new_value)
        revisions.append(
            Revision(
                work_item_id=work_item_id,
                rev_number=int(row.get("rev") or 0),
                changed_by=changed_by or "Unknown",
                changed_by_email=changed_by_email or "",
                changed_date=changed_date,
                fields_changed=field_changes,
            )
        )
        previous_fields = fields
    return revisions


def _field_value(value: Any) -> str | None:
    if isinstance(value, dict):
        display_name, email = _parse_identity(value)
        return email or display_name
    return _optional_string(value)


def _build_item_urls(bundle: ReportBundle, items: tuple[WorkItem, ...]) -> dict[int, str]:
    base_url = f"https://dev.azure.com/{bundle.config.ado.organization}/{bundle.config.ado.project}/_workitems/edit"
    return {item.id: f"{base_url}/{item.id}" for item in items}


def _build_ncfl_summary_line(
    *,
    program_id: str | None,
    programs_root: Path,
) -> str | None:
    if not program_id:
        return None
    pending = load_proposals(
        program_id,
        status_filter={"pending"},
        programs_root=programs_root,
    )
    if not pending:
        return "NCFL: 0 pending context proposals"
    issue_numbers = sorted({proposal.issue_number for proposal in pending})
    issue_list = ", ".join(f"{issue:03d}" for issue in issue_numbers[:3])
    if len(issue_numbers) > 3:
        issue_list += f", +{len(issue_numbers) - 3} more"
    stale_count = len(stale_pending_proposals(program_id, programs_root=programs_root))
    conflict_count = len(conflicting_pending_proposals(program_id, programs_root=programs_root))
    parts = [
        f"NCFL: {len(pending)} pending context proposal{'s' if len(pending) != 1 else ''}",
        f"issues [{issue_list}]",
    ]
    if stale_count:
        parts.append(f"{stale_count} stale (>2 issues old)")
    if conflict_count:
        parts.append(f"{conflict_count} cross-issue conflict key{'s' if conflict_count != 1 else ''}")
    return " | ".join(parts)


def _render_plaintext(
    edition_name: str,
    as_of: datetime,
    previous_issue_number: int | None,
    report: FreshnessReport,
    dri_summaries: tuple[DRISummary, ...],
    items: tuple[WorkItem, ...],
    slice_findings: tuple[SliceQualityRecord, ...],
    stale_banner: str | None,
    review_summary: str | None,
    review_readiness: str | None,
    summary_lines: tuple[str, ...],
) -> str:
    items_by_id = {item.id: item for item in items}
    lines = [
        f"VERTEX FRESHNESS REPORT — {edition_name}",
        f"Generated: {as_of.isoformat()}" + (f" | vs. Issue {previous_issue_number:03d}" if previous_issue_number is not None else ""),
        stale_banner or "",
        f"SUMMARY: {len(report.items)} findings across {len(dri_summaries)} DRIs",
        f"  blocks={report.blocks} warns={report.warns} infos={report.infos}",
        f"REVIEW: {review_summary}" if review_summary is not None else "",
        f"  status={review_readiness}" if review_readiness is not None else "",
        *summary_lines,
        "",
    ]
    if slice_findings:
        lines.append("SLICE INPUT FINDINGS:")
        for finding in slice_findings:
            lines.append(f"- {finding.slice_id}: {finding.issues[0] if finding.issues else 'Slice freshness is stale.'}")
        lines.append("")
    for summary in dri_summaries:
        lines.append(
            f"{summary.dri_name} <{summary.dri_email}>: {len(summary.items)} findings ({summary.stale_count} stale, {summary.overdue_count} overdue, {summary.open_count} open)"
        )
        for fi in summary.items:
            item = items_by_id.get(fi.work_item_id)
            title = item.title if item is not None else f"Work item {fi.work_item_id}"
            lines.append(
                f"- ADO#{fi.work_item_id} {title}: {fi.severity.title()} | {fi.action_label or fi.rule_id}: {fi.message}"
            )
            if fi.action_message:
                lines.append(f"  Action: {fi.action_message}")
        lines.append("")
    return "\n".join(line for line in lines if line is not None).strip()


def _render_markdown(
    edition_name: str,
    as_of: datetime,
    previous_issue_number: int | None,
    report: FreshnessReport,
    dri_summaries: tuple[DRISummary, ...],
    items: tuple[WorkItem, ...],
    item_urls: dict[int, str],
    slice_findings: tuple[SliceQualityRecord, ...],
    stale_banner: str | None,
    review_summary: str | None,
    review_readiness: str | None,
    summary_lines: tuple[str, ...],
) -> str:
    items_by_id = {item.id: item for item in items}
    lines = [
        f"# Vertex Freshness Report — {edition_name}",
        f"Generated: {as_of.isoformat()}" + (f" | vs. Issue {previous_issue_number:03d}" if previous_issue_number is not None else ""),
        "",
        f"> {stale_banner}" if stale_banner is not None else "",
        "" if stale_banner is not None else "",
        f"Summary: {len(report.items)} findings across {len(dri_summaries)} DRIs.",
        f"Blocks: {report.blocks} | Warns: {report.warns} | Infos: {report.infos}",
        f"Review: {review_summary}" if review_summary is not None else "",
        f"Status: {review_readiness}" if review_readiness is not None else "",
        *summary_lines,
        "",
    ]
    if slice_findings:
        lines.extend(["## Slice Input Findings", ""])
        for finding in slice_findings:
            lines.append(f"- **{finding.slice_id}**: {finding.issues[0] if finding.issues else 'Slice freshness is stale.'}")
        lines.append("")
    for summary in dri_summaries:
        lines.append(f"## {summary.dri_name} ({summary.dri_email})")
        lines.append(
            f"{len(summary.items)} findings | {summary.stale_count} stale | {summary.overdue_count} overdue | {summary.open_count} open"
        )
        lines.append("")
        for fi in summary.items:
            item = items_by_id.get(fi.work_item_id)
            title = item.title if item is not None else f"Work item {fi.work_item_id}"
            lines.append(f"- **ADO#{fi.work_item_id}** [{title}]({item_urls.get(fi.work_item_id, '#')})")
            lines.append(f"  - {fi.severity.title()} | {fi.action_label or fi.rule_id}: {fi.message}")
            if fi.action_message:
                lines.append(f"  - Action: {fi.action_message}")
        lines.append("")
    return "\n".join(lines).strip()


def _render_html(
    edition_name: str,
    as_of: datetime,
    previous_issue_number: int | None,
    report: FreshnessReport,
    dri_summaries: tuple[DRISummary, ...],
    items: tuple[WorkItem, ...],
    item_urls: dict[int, str],
    notify_previews: tuple[NotifyPreview, ...],
    slice_findings: tuple[SliceQualityRecord, ...],
    stale_banner: str | None,
    review_summary: str | None,
    review_readiness: str | None,
    summary_lines: tuple[str, ...],
) -> str:
    items_by_id = {item.id: item for item in items}
    parts = [
        "<html><body style=\"font-family: Segoe UI, -apple-system, Roboto, Helvetica, Arial, sans-serif;\">",
        f"<h1>Vertex Freshness Report — {escape(edition_name)}</h1>",
        f"<p>Generated: {escape(as_of.isoformat())}" + (f" | vs. Issue {previous_issue_number:03d}" if previous_issue_number is not None else "") + "</p>",
    ]
    if stale_banner is not None:
        parts.append(
            "<p style=\"padding:12px;border:1px solid #b45309;background:#fff7ed;color:#9a3412;\"><strong>Stale snapshot fallback:</strong> "
            f"{escape(stale_banner)}</p>"
        )
    if slice_findings:
        parts.append("<h2>Slice Input Findings</h2><ul>")
        for finding in slice_findings:
            detail = finding.issues[0] if finding.issues else "Slice freshness is stale."
            parts.append(f"<li><strong>{escape(finding.slice_id)}</strong>: {escape(detail)}</li>")
        parts.append("</ul>")
    parts.extend(
        [
        f"<p><strong>Summary:</strong> {len(report.items)} findings across {len(dri_summaries)} DRIs. Blocks={report.blocks}, Warns={report.warns}, Infos={report.infos}</p>",
        ]
    )
    if review_summary is not None and review_readiness is not None:
        parts.append(f"<p><strong>Review:</strong> {escape(review_summary)}<br/><strong>Status:</strong> {escape(review_readiness)}</p>")
    for line in summary_lines:
        if ":" in line:
            label, detail = line.split(":", 1)
            parts.append(f"<p><strong>{escape(label)}:</strong>{escape(detail)}</p>")
        else:
            parts.append(f"<p><strong>{escape(line)}</strong></p>")
    for summary in dri_summaries:
        parts.append(f"<h2>{escape(summary.dri_name)} ({escape(summary.dri_email)})</h2>")
        parts.append(
            f"<p>{len(summary.items)} findings | {summary.stale_count} stale | {summary.overdue_count} overdue | {summary.open_count} open</p>"
        )
        parts.append("<ul>")
        for fi in summary.items:
            item = items_by_id.get(fi.work_item_id)
            title = item.title if item is not None else f"Work item {fi.work_item_id}"
            url = item_urls.get(fi.work_item_id, "#")
            action_html = f"<br/><span>Action: {escape(fi.action_message)}</span>" if fi.action_message else ""
            parts.append(
                "<li>"
                f"<strong>ADO#{fi.work_item_id}</strong> "
                f"<a href=\"{escape(url)}\">{escape(title)}</a>: {escape(fi.severity.title())} | {escape(fi.action_label or fi.rule_id)}: {escape(fi.message)}"
                f"{action_html}"
                "</li>"
            )
        parts.append("</ul>")
    if notify_previews:
        parts.append("<h2>Notify Preview</h2><ul>")
        for preview in notify_previews:
            parts.append(
                f"<li>{escape(', '.join(preview.to))}: {escape(preview.subject)}</li>"
            )
        parts.append("</ul><p>Send disabled until Phase 2 (Graph permissions required).</p>")
    parts.append("</body></html>")
    return "".join(parts)


def _build_action_summary_lines(
    *,
    program_id: str | None,
    programs_root: Path,
    as_of: datetime,
    program_facts: ProgramFactSnapshot | None = None,
) -> tuple[str, ...]:
    if program_id is None:
        return ()
    resolved_program_facts = program_facts or load_program_facts(
        program_id,
        db_root=programs_root.parent,
        programs_root=programs_root,
    )
    active_actions = tuple(
        action
        for action in project_action_items(resolved_program_facts)
        if action.status in {ActionStatus.PROPOSED, ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
    )
    if not active_actions:
        return ()
    overdue_actions = assess_action_staleness(active_actions, as_of.date())
    open_count = sum(1 for action in active_actions if action.status in {ActionStatus.OPEN, ActionStatus.IN_PROGRESS})
    proposed_count = sum(1 for action in active_actions if action.status is ActionStatus.PROPOSED)
    summary = f"ACTIONS: {open_count} open, {len(overdue_actions)} overdue"
    if proposed_count:
        summary = f"{summary}, {proposed_count} proposed"
    return (summary,)


def _build_milestone_summary_lines(
    *,
    program_id: str | None,
    programs_root: Path,
    freshness_report: FreshnessReport,
) -> tuple[str, ...]:
    if program_id is None:
        return ()

    stale_item_ids = {
        finding.work_item_id
        for finding in freshness_report.items
        if finding.rule_id == "FR-22"
    }
    if not stale_item_ids:
        return ()

    milestone_details: list[str] = []
    for milestone in project_milestones(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("milestone.entry",),
        )
    ):
        stale_linked_item_count = sum(1 for item_id in milestone.linked_work_item_ids if item_id in stale_item_ids)
        if stale_linked_item_count == 0:
            continue
        item_label = "item" if stale_linked_item_count == 1 else "items"
        milestone_details.append(f"{milestone.name}: {stale_linked_item_count} linked {item_label} stale")

    if not milestone_details:
        return ()

    milestone_label = "milestone" if len(milestone_details) == 1 else "milestones"
    return (
        f"MILESTONES: {len(milestone_details)} {milestone_label} with stale exit criteria ({'; '.join(milestone_details)})",
    )


def _build_notify_previews(
    bundle: ReportBundle,
    dri_summaries: tuple[DRISummary, ...],
    items: tuple[WorkItem, ...],
    item_urls: dict[int, str],
) -> tuple[NotifyPreview, ...]:
    items_by_id = {item.id: item for item in items}
    previews: list[NotifyPreview] = []
    for summary in dri_summaries:
        if summary.dri_email == "unassigned":
            continue
        lines = [
            f"Hi {summary.dri_name}, the following items need your update before the next published update:",
            "",
        ]
        for finding in summary.items:
            item = items_by_id.get(finding.work_item_id)
            if item is None:
                continue
            lines.append(f"- ADO#{item.id} {item.title}: {finding.message}")
            lines.append(f"  {item_urls.get(item.id, '#')}")
        md_body = "\n".join(lines)
        previews.append(
            NotifyPreview(
                to=(summary.dri_email,),
                cc=(bundle.config.author.email,),
                subject=f"{bundle.config.edition.name}: {len(summary.items)} items need your update",
                html_body=escape(md_body).replace("\n", "<br/>"),
                md_body=md_body,
                attachments=(),
            )
        )
    return tuple(previews)


def _record_confirmed_notify_run(
    *,
    edition_name: str,
    issue_number: int,
    dri_summaries: tuple[DRISummary, ...],
    notify_previews: tuple[NotifyPreview, ...],
    programs_root: Path,
    confirmed_at: datetime,
) -> Path:
    notifications: list[ConfirmedNotification] = []
    preview_index = 0
    for summary in dri_summaries:
        if summary.dri_email == "unassigned":
            continue
        if preview_index >= len(notify_previews):
            raise StateError("Notify preview count does not match the DRI summaries being recorded.")
        preview = notify_previews[preview_index]
        preview_index += 1
        notifications.append(
            ConfirmedNotification(
                dri_email=summary.dri_email,
                to=preview.to,
                cc=preview.cc,
                subject=preview.subject,
                work_item_ids=tuple(sorted({finding.work_item_id for finding in summary.items})),
            )
        )
    if preview_index != len(notify_previews):
        raise StateError("Notify previews contain entries that do not map to a DRI summary.")

    return append_confirmed_notify_run(
        edition=edition_name,
        issue_number=issue_number,
        confirmed_at=confirmed_at,
        notifications=notifications,
        programs_root=programs_root,
    )


def _summarize_review_status(
    review_status: ReviewStatus | None,
    *,
    issue_number: int,
) -> tuple[str | None, str | None]:
    if review_status is None or review_status.issue_number != issue_number:
        return None, None

    approved = sum(1 for section in review_status.sections if section.state.value == "approved")
    pending = sum(1 for section in review_status.sections if section.state.value == "sent")
    not_sent = sum(1 for section in review_status.sections if section.state.value == "pending")
    skipped = sum(1 for section in review_status.sections if section.state.value == "skipped_no_delta")
    needs_changes = sum(1 for section in review_status.sections if section.state.value == "changes_requested")
    rejected = sum(1 for section in review_status.sections if section.state.value == "rejected")

    summary_parts = [f"{approved} approved"]
    if pending:
        summary_parts.append(f"{pending} pending")
    if not_sent:
        summary_parts.append(f"{not_sent} not sent")
    if skipped:
        summary_parts.append(f"{skipped} skipped")
    if needs_changes:
        summary_parts.append(f"{needs_changes} needs changes")
    if rejected:
        summary_parts.append(f"{rejected} rejected")

    if needs_changes:
        readiness = f"NOT READY ({needs_changes} section{'s' if needs_changes != 1 else ''} need changes)"
    elif rejected:
        readiness = f"NOT READY ({rejected} section{'s' if rejected != 1 else ''} rejected)"
    elif pending:
        readiness = f"NOT READY ({pending} section{'s' if pending != 1 else ''} pending review)"
    elif not_sent:
        readiness = f"NOT READY ({not_sent} section{'s' if not_sent != 1 else ''} not sent)"
    else:
        readiness = "READY"

    return " · ".join(summary_parts), readiness


def _render_notify_preview_plaintext(previews: tuple[NotifyPreview, ...]) -> str:
    lines = ["NOTIFY PREVIEW"]
    for index, preview in enumerate(previews, start=1):
        lines.append(f"{index}. To: {', '.join(preview.to)}")
        lines.append(f"   Subject: {preview.subject}")
    return "\n".join(lines)


def _render_notify_preview_markdown(previews: tuple[NotifyPreview, ...]) -> str:
    lines = ["## Notify Preview", ""]
    for index, preview in enumerate(previews, start=1):
        lines.append(f"{index}. To: {', '.join(preview.to)}")
        lines.append(f"   Subject: {preview.subject}")
    return "\n".join(lines)


def _parse_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(";") if tag.strip()]
    if isinstance(value, (list, tuple)):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    return [str(value)]


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _infer_risk_level(state: str, tags: list[str]) -> RiskLevel:
    normalized_state = state.lower()
    normalized_tags = {tag.lower() for tag in tags}
    if normalized_state in {"closed", "done", "resolved", "completed"}:
        return RiskLevel.DONE
    if "blocked" in normalized_state or "blocked" in normalized_tags:
        return RiskLevel.HIGH
    if normalized_state in {"off track", "at risk"}:
        return RiskLevel.HIGH if normalized_state == "off track" else RiskLevel.MEDIUM
    return RiskLevel.LOW


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _write_output_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = content if content.endswith("\n") else f"{content}\n"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(normalized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    return path
