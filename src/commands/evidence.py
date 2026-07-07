from __future__ import annotations

import csv
from datetime import datetime, timedelta
from io import StringIO
import json
from pathlib import Path
from typing import Any

import typer

from src.commands.confirm import _deserialize_items, _load_draft_state
from src.commands.diff import _latest_draft_issue_number, _resolve_section
from src.commands.report import _build_item_urls, _build_scorecard_data, _build_scorecard_packets
from src.commands.report_narratives import _active_workstream_blurbs
from src.commands.report import _build_lookback_evidence, _build_top_items, _build_workstream_data, _is_continuity_layout, _load_previous_snapshot
from src.commands.report import _visible_continuity_chapters, _visible_detail_section_ids
from src.core.archive_store import read_archive_index
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.delta_engine import build_deltas
from src.core.evidence_engine import build_evidence
from src.core.forecast_engine import build_forecast_assessment
from src.core.jinja_filters import build_anchor
from src.core.lineage import build_ado_lineage_text, build_lineage_lookup, format_claim_text
from src.core.models import EditionType, ReviewSection, ReviewState, ReviewStatus, WorkItem
from src.core.narrative_store import load_archived_narratives, load_narratives
from src.core.overrides_store import load_archived_overrides, load_overrides
from src.core.snapshot_store import ARCHIVE_ROOT, read_snapshot
from src.core.trusted_baseline_store import load_trusted_baseline_issue


def evidence_command(
    edition: str = typer.Option("", "--edition", help="Edition name."),
    issue: str = typer.Option("latest", "--issue", help="Issue number or 'latest'."),
    section: str | None = typer.Option(None, "--section", help="Section id or dimension name."),
    claim: str | None = typer.Option(None, "--claim", help="Claim key such as deployment-velocity.risk."),
    ado: int | None = typer.Option(None, "--ado", help="ADO work item id."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    summary = build_evidence_summary(
        edition_name=edition,
        issue_value=issue,
        section=section,
        claim=claim,
        ado_work_item_id=ado,
        reports_root=REPORTS_ROOT,
        archive_root=ARCHIVE_ROOT,
        programs_root=PROGRAMS_ROOT,
    )
    typer.echo(
        render_evidence_output(
            {
                "edition": edition,
                "issue": issue,
                "section": section or "-",
                "claim": claim or "-",
                "ado": ado if ado is not None else "-",
                "summary": summary,
            },
            format=format,
        ),
        nl=False,
    )
    raise typer.Exit(code=0)


def render_evidence_output(payload: dict[str, object], *, format: str) -> str:
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        columns = ("edition", "issue", "section", "claim", "ado", "summary")
        writer.writerow(columns)
        writer.writerow([payload[column] for column in columns])
        return buffer.getvalue()
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    return str(payload["summary"])


def build_evidence_summary(
    *,
    edition_name: str,
    issue_value: str,
    section: str | None,
    claim: str | None,
    ado_work_item_id: int | None,
    reports_root: Path,
    archive_root: Path,
    programs_root: Path = PROGRAMS_ROOT,
) -> str:
    if sum(value is not None for value in (section, claim, ado_work_item_id)) != 1:
        raise typer.BadParameter("Provide exactly one of --section, --claim, or --ado.")

    issue_number = _resolve_issue_number(edition_name, issue_value, programs_root=programs_root)
    context = _load_lineage_context(
        edition_name=edition_name,
        issue_number=issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
    )

    if ado_work_item_id is not None:
        item = context["item_lookup"].get(ado_work_item_id)
        if item is None:
            raise typer.BadParameter(f"ADO item {ado_work_item_id} is not part of Issue {issue_number:03d}.")
        return build_ado_lineage_text(
            work_item=item,
            evidence=context["evidence_by_item"].get(ado_work_item_id),
            item_url=context["item_urls"].get(ado_work_item_id),
            related_claims=context["lineage_lookup"].claims_for_work_item(ado_work_item_id),
            deltas=context["deltas"],
        )

    if claim is not None:
        claim_record = context["lineage_lookup"].find_claim(claim)
        if claim_record is None:
            raise typer.BadParameter(f"Claim '{claim}' is not traceable for Issue {issue_number:03d}.")
        return format_claim_text(claim_record)

    available_section_ids = {workstream.section_id for workstream in context["workstream_data"]}
    resolved_section = _resolve_evidence_section(
        section=section,
        bundle=context["bundle"],
        edition_type=context["edition_type"],
        available_section_ids=available_section_ids,
    )
    if resolved_section is None:
        raise typer.BadParameter("Section is required.")
    claim_records = context["lineage_lookup"].claims_for_section(resolved_section)
    if not claim_records:
        raise typer.BadParameter(f"Section '{resolved_section}' is not traceable for Issue {issue_number:03d}.")
    return "\n\n".join(format_claim_text(record).rstrip() for record in claim_records) + "\n"


def _resolve_issue_number(edition_name: str, issue_value: str, *, programs_root: Path = PROGRAMS_ROOT) -> int:
    normalized = issue_value.strip().lower()
    if normalized == "latest":
        return _latest_draft_issue_number(edition_name, programs_root=programs_root)
    try:
        return int(normalized)
    except ValueError as error:
        raise typer.BadParameter(f"Unsupported issue '{issue_value}'. Use an issue number or 'latest'.") from error


def _load_lineage_context(
    *,
    edition_name: str,
    issue_number: int,
    reports_root: Path,
    archive_root: Path,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any]:
    bundle = load_bundle(edition_name, reports_root=reports_root)
    try:
        draft_state = _load_draft_state(edition_name, issue_number, programs_root=programs_root)
        items = _deserialize_items(tuple(draft_state.get("items", [])))
        data_as_of = datetime.fromisoformat(str(draft_state["ado_data_as_of"]))
        overrides_document = load_overrides(edition_name, reports_root=reports_root)
        loaded_narratives = load_narratives(edition_name, issue_number, reports_root=reports_root)
        edition_type = EditionType.from_string(bundle.config.edition.type)
        evidence_window_start = data_as_of - timedelta(days=bundle.config.ado.date_window_days)
        evidence_by_item = {
            item.id: build_evidence(item, evidence_window_start, data_as_of)
            for item in items
        }
    except typer.BadParameter as error:
        if "Draft state not found at" not in str(error):
            raise
        archive_entry = _find_confirmed_archive_entry(edition_name, issue_number, archive_root)
        snapshot = read_snapshot(Path(archive_entry["snapshot_path"]))
        items = tuple(_snapshot_item_to_work_item(item, fetched_at=snapshot.ado_data_as_of) for item in snapshot.items)
        data_as_of = snapshot.ado_data_as_of
        overrides_document = load_archived_overrides(edition_name, issue_number, archive_root=archive_root)
        loaded_narratives = load_archived_narratives(edition_name, issue_number, archive_root=archive_root)
        edition_type = snapshot.edition_type
        evidence_by_item = {
            item.id: _build_lookback_evidence(
                work_item_id=item.id,
                summary=f"Archived snapshot for Issue {issue_number:03d}; no live evidence window captured.",
            )
            for item in items
        }

    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=issue_number,
        programs_root=reports_root.parent / "programs",
    )
    previous_snapshot, previous_issue_number = _load_previous_snapshot(
        edition_name=edition_name,
        issue_number=issue_number,
        archive_root=archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )
    deltas = build_deltas(
        current_items=items,
        previous_snapshot=previous_snapshot,
        issue_number=issue_number,
        previous_issue_number=previous_issue_number,
        evidence_by_item=evidence_by_item,
    )
    if overrides_document is None:
        raise typer.BadParameter(f"Overrides are not available for Issue {issue_number:03d}.")

    scorecard_packets = _build_scorecard_packets(bundle, items, previous_snapshot)
    scorecards, _, _ = _build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item=evidence_by_item,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=reports_root,
    )
    top_items = _build_top_items(overrides_document, scorecards)
    visible_section_ids = _visible_detail_section_ids(
        bundle,
        overrides_document,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        deltas=deltas,
        top_items=top_items,
    )
    workstream_blurbs = _active_workstream_blurbs(loaded_narratives, visible_section_ids)
    review_status = ReviewStatus(
        issue_number=issue_number,
        sections=tuple(
            ReviewSection(
                section_id=f"ws:{section_id}",
                state=ReviewState.PENDING,
                reviewer=None,
                note=None,
                updated_at=None,
            )
            for section_id in sorted(visible_section_ids)
        ),
    )
    item_urls = _build_item_urls(bundle, items)
    workstream_data = _build_workstream_data(
        issue_number=issue_number,
        bundle=bundle,
        edition_type=edition_type,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        workstream_blurbs=workstream_blurbs,
        dependency_cascades=(),
        review_status=review_status,
        evidence_by_item=evidence_by_item,
        item_urls=item_urls,
    )
    forecast = build_forecast_assessment(
        enabled=bundle.config.forecast_enabled,
        edition_name=edition_name,
        as_of=data_as_of,
        workstreams=workstream_data,
        deltas=deltas,
        archive_root=archive_root,
    )
    lineage_lookup = build_lineage_lookup(
        edition_name=edition_name,
        issue_number=issue_number,
        edition_type=edition_type,
        workstreams=workstream_data,
        items=items,
        deltas=deltas,
        evidence_by_item=evidence_by_item,
        overrides_document=overrides_document,
        previous_snapshot=previous_snapshot,
        archive_root=archive_root,
        forecast=forecast,
    )
    return {
        "bundle": bundle,
        "deltas": deltas,
        "edition_type": edition_type,
        "evidence_by_item": evidence_by_item,
        "item_lookup": {item.id: item for item in items},
        "item_urls": item_urls,
        "lineage_lookup": lineage_lookup,
        "workstream_data": workstream_data,
    }


def _find_confirmed_archive_entry(edition_name: str, issue_number: int, archive_root: Path) -> dict[str, Any]:
    index = read_archive_index(edition_name, archive_root=archive_root)
    for entry in index.issues:
        if entry.issue_number != issue_number or entry.kind != "confirmed":
            continue
        return {
            "snapshot_path": entry.snapshot_path,
            "manifest_path": entry.manifest_path,
            "html_path": entry.html_path,
            "md_path": entry.md_path,
        }

    raise typer.BadParameter(
        f"Issue {issue_number:03d} is not confirmed in archive for {edition_name}."
    )


def _resolve_evidence_section(
    *,
    section: str | None,
    bundle,
    edition_type: EditionType,
    available_section_ids: set[str],
) -> str | None:
    if section is None:
        return None
    if _is_continuity_layout(bundle) and bundle.chapter_contract is not None:
        alias_to_section: dict[str, str] = {}
        for chapter in _visible_continuity_chapters(bundle, edition_type):
            if chapter.id not in available_section_ids:
                continue
            aliases = {
                chapter.id,
                f"chapter_{chapter.id}",
                build_anchor(chapter.title),
            }
            for dimension_id in chapter.dimensions:
                binding = bundle.chapter_contract.resolve_dimension(dimension_id)
                if binding is None:
                    continue
                aliases.add(build_anchor(binding[1]))
                aliases.add(build_anchor(f"{binding[0]}-{binding[1]}"))
            for alias in aliases:
                alias_to_section.setdefault(alias, chapter.id)
        normalized = section.strip()
        anchored = build_anchor(normalized)
        if normalized in alias_to_section:
            return alias_to_section[normalized]
        if anchored in alias_to_section:
            return alias_to_section[anchored]
        matching_sections = {
            alias_to_section[alias]
            for alias in alias_to_section
            if alias == anchored or alias.endswith(f"-{anchored}")
        }
        if len(matching_sections) == 1:
            return next(iter(matching_sections))
        if matching_sections:
            choices = ", ".join(sorted(matching_sections))
            raise typer.BadParameter(f"Section '{section}' is ambiguous. Use one of: {choices}")
    return _resolve_section(section, available_section_ids | {"exec_summary"})


def _snapshot_item_to_work_item(snapshot_item: Any, *, fetched_at: datetime) -> WorkItem:
    return WorkItem(
        id=int(snapshot_item.id),
        type=str(snapshot_item.type),
        title=str(snapshot_item.title),
        state=str(snapshot_item.state),
        assigned_to=snapshot_item.assigned_to,
        assigned_to_email=None,
        area_path=str(snapshot_item.area_path),
        iteration_path="Archived",
        target_date=snapshot_item.target_date,
        risk_level=snapshot_item.risk_level,
        tags=list(snapshot_item.tags),
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=fetched_at,
    )
