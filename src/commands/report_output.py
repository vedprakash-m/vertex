from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from src.core.config_loader import ReportBundle
from src.core.models import EditionType, ReportData, WorkItem
from src.core.pipeline import StageContext
from src.core.quality_matrix_engine import QualityMatrix
from src.core.workstream_registry import build_workstream_association_records, build_workstream_issue_snapshot, load_workstream_registry, render_workstream_issue_snapshot_markdown
from src.m365.adaptive_card_renderer import AdaptiveCardRenderer


def format_report_pipeline_progress_detail(
    stage_name: str, ctx: StageContext, *, safe_length: Callable[[object], int | None],
) -> str:
    details: list[str] = []
    if stage_name == "resolution":
        if ctx.resolved_issue_number is not None:
            details.append(f"issue={ctx.resolved_issue_number:03d}")
        if ctx.resolved_edition_type is not None:
            details.append(f"type={ctx.resolved_edition_type.value}")
    elif stage_name == "fetch":
        details.append(f"items={len(ctx.items)}")
        if ctx.ado_calls is not None:
            details.append(f"ado_calls={ctx.ado_calls}")
        if ctx.offline_source_label is not None:
            details.append(ctx.offline_source_label)
    elif stage_name == "compute":
        details.append(f"items={len(ctx.items)}")
        scorecard_count = safe_length(ctx.scorecards)
        if scorecard_count is not None:
            details.append(f"scorecards={scorecard_count}")
    elif stage_name == "milestone":
        milestone_count = safe_length(ctx.milestone_assessments)
        if milestone_count is not None:
            details.append(f"milestones={milestone_count}")
        details.append(f"warnings={len(ctx.milestone_warnings)}")
    elif stage_name == "risk":
        risk_count = safe_length(ctx.risks)
        if risk_count is not None:
            details.append(f"risks={risk_count}")
        details.append(f"stale={len(ctx.stale_risk_ids)}")
        details.append(f"warnings={len(ctx.risk_warnings)}")
    elif stage_name == "action":
        action_count = safe_length(ctx.actions)
        if action_count is not None:
            details.append(f"actions={action_count}")
        details.append(f"overdue={len(ctx.overdue_action_ids)}")
    elif stage_name == "narrative":
        section_count = safe_length(ctx.visible_section_ids)
        if section_count is not None:
            details.append(f"sections={section_count}")
        details.append(f"top_items={len(ctx.top_3_now)}")
    elif stage_name == "ai":
        blurb_count = safe_length(ctx.render_workstream_blurbs)
        if blurb_count is not None:
            details.append(f"blurbs={blurb_count}")
        if ctx.offline:
            details.append("offline")
    elif stage_name == "render":
        if ctx.render_exec_summary_text is not None:
            details.append("exec_summary=ready")
        blurb_count = safe_length(ctx.render_workstream_blurbs)
        if blurb_count is not None:
            details.append(f"blurbs={blurb_count}")
    elif stage_name == "validation":
        warning_count = safe_length(getattr(ctx.validation_state, "warnings", None))
        if warning_count is not None:
            details.append(f"warnings={warning_count}")
        exit_code = getattr(ctx.validation_state, "exit_code", None)
        if exit_code is not None:
            details.append(f"exit={exit_code}")
        persona_coverage = getattr(ctx.validation_state, "persona_coverage", None)
        if persona_coverage is not None:
            details.append(
                "persona="
                f"{len(persona_coverage.passed)}/{persona_coverage.total_evaluations}"
            )
    elif stage_name in {"output", "lookback"}:
        if ctx.artifacts is not None:
            details.append(f"issue={ctx.artifacts.issue_number:03d}")
            details.append(f"warnings={len(ctx.artifacts.warnings)}")
            details.append(f"exit={ctx.artifacts.exit_code}")
    return ", ".join(details)



def _build_item_urls(bundle: ReportBundle, items: tuple[WorkItem, ...]) -> dict[int, str]:
    base_url = _ado_item_base_url(bundle)
    return {item.id: f"{base_url}/{item.id}" for item in items}


def _artifact_url(bundle: ReportBundle, *, output_root: Path | None = None, artifact_path: Path) -> str:
    base_url = _artifact_base_url(bundle)
    if base_url is not None and output_root is not None:
        try:
            relative_path = artifact_path.relative_to(output_root).as_posix()
        except ValueError:
            return artifact_path.resolve().as_uri()
        return urljoin(base_url, relative_path)
    return artifact_path.resolve().as_uri()


def _artifact_base_url(bundle: ReportBundle) -> str | None:
    raw_value = bundle.config.m365.artifact_base_url
    if raw_value is None:
        return None
    normalized = raw_value.strip()
    if not normalized:
        return None
    return f"{normalized.rstrip('/')}/"


def _ado_item_base_url(bundle: ReportBundle) -> str:
    return f"https://dev.azure.com/{bundle.config.ado.organization}/{bundle.config.ado.project}/_workitems/edit"


def _ado_saved_query_base_url(bundle: ReportBundle) -> str:
    return f"https://dev.azure.com/{bundle.config.ado.organization}/{bundle.config.ado.project}/_queries/query"


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


def _write_output_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(_to_jsonable(payload), handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    return path


def _write_workstream_snapshot_artifacts(
    *,
    program_id: str | None,
    bundle: ReportBundle,
    issue_number: int,
    edition_name: str,
    generated_at: datetime,
    quality_matrix: QualityMatrix,
    markdown_body: str,
    items: tuple[WorkItem, ...],
    output_dir: Path,
    programs_root: Path,
) -> tuple[Path | None, Path | None, Path | None]:
    if not program_id:
        return None, None, None
    registry_entries = load_workstream_registry(
        program_id=program_id,
        slice_contracts=bundle.slice_contracts or (),
        programs_root=programs_root,
        program_context=bundle.program_context,
    )
    snapshot = build_workstream_issue_snapshot(
        program_id=program_id,
        issue_number=issue_number,
        edition=edition_name,
        generated_at=generated_at,
        registry_entries=registry_entries,
        quality_matrix=quality_matrix,
        markdown_body=markdown_body,
        items=items,
    )
    item_lookup = {item.id: item.title for item in items}
    snapshot_md_path = _write_output_text(
        output_dir / f"issue_{issue_number:03d}.workstream_snapshot.md",
        render_workstream_issue_snapshot_markdown(snapshot, item_lookup=item_lookup),
    )
    snapshot_json_path = _write_output_json(
        output_dir / f"issue_{issue_number:03d}.workstream_snapshot.json",
        snapshot,
    )
    association_records = build_workstream_association_records(
        snapshot=snapshot,
        slice_contracts=bundle.slice_contracts or (),
        items=items,
    )
    associations_json_path = _write_output_json(
        output_dir / f"issue_{issue_number:03d}.workstream_associations.json",
        association_records,
    )
    return snapshot_md_path, snapshot_json_path, associations_json_path


def _write_report_adaptive_cards(
    *,
    bundle: ReportBundle,
    edition_name: str,
    issue_number: int,
    edition_type: EditionType,
    report: ReportData,
    output_root: Path,
    item_urls: dict[int, str],
    report_html_url: str | None,
) -> tuple[Path, ...]:
    if edition_type in {EditionType.DECK, EditionType.LOOKBACK}:
        return ()
    if bundle.config.edition.cadence.lower() != "weekly":
        return ()

    renderer = AdaptiveCardRenderer()
    issue_dir = output_root / f"issue_{issue_number:03d}"
    issue_dir.mkdir(parents=True, exist_ok=True)
    card_path = issue_dir / f"issue_{issue_number:03d}.weekly_summary.json"
    payload = renderer.render_weekly_summary(
        edition_name=edition_name,
        issue_number=issue_number,
        report=report,
        item_urls=item_urls,
        report_html_url=report_html_url,
    )
    card_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return (card_path,)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value