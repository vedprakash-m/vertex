"""Post-confirm artifact recorders for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). These helpers run
after the main archive/baseline transaction and only write optional tracking,
learning, and association artifacts or emit warnings when best-effort work
cannot be completed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.ai.draft_reviewer import build_suggestion_tracking_report, render_tracking_summary, review_artifact_from_payload
from src.ai.edit_learner import append_edit_patterns, build_edit_patterns
from src.ai.learning_distiller import LearningDistillerError, load_tracking_reports, render_learning_markdown, render_learning_summary
from src.commands.confirm_stages.learning_distiller import build_default_learning_distiller, build_learning_distillation_trace_context
from src.commands.report import _write_output_json, _write_output_text
from src.core.config_loader import EditorialRules
from src.core.edition_resolver import resolve_edition, get_program_output_dir
from src.core.models import ReportData
from src.core.ncfl_extractor import extract_proposals
from src.core.ncfl_proposal_store import get_proposals_path, stage_extracted_proposals
from src.core.view_models import WorkstreamData
from src.core.workstream_association_store import append_workstream_association_records, record_from_dict


def record_review_tracking(
    *,
    edition_name: str,
    issue_number: int,
    draft_state: dict[str, Any],
    report: ReportData,
    programs_root: Path,
) -> tuple[Path | None, str | None, str | None]:
    artifact_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.review.json"
    if not artifact_path.exists():
        return None, None, None

    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"AI review artifact at {artifact_path} is invalid.")
        current_rendered_kusto_query_ids = tuple(
            str(section.get("query_id"))
            for section in draft_state.get("kusto_sections", [])
            if isinstance(section, dict) and section.get("query_id")
        )
        workstream_blurbs = getattr(report, "workstream_blurbs", {})
        current_reviewed_section_ids = (
            "exec_summary",
            *(f"ws:{section_id}" for section_id in workstream_blurbs),
        )
        review_artifact = review_artifact_from_payload(
            payload,
            valid_reviewed_section_ids=current_reviewed_section_ids,
            valid_rendered_kusto_query_ids=current_rendered_kusto_query_ids,
        )
        if review_artifact.issue_number != issue_number:
            raise ValueError(
                f"AI review artifact at {artifact_path} is for issue {review_artifact.issue_number:03d}, expected {issue_number:03d}."
            )
        tracking_report = build_suggestion_tracking_report(
            review_artifact,
            confirmed_report=report,
            rendered_kusto_query_ids=current_rendered_kusto_query_ids,
        )
        tracking_path = _write_output_json(
            get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.review_tracking.json",
            tracking_report,
        )
        return tracking_path, render_tracking_summary(tracking_report), None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as error:
        return None, None, f"AI review tracking skipped: {error}"


def record_workstream_associations(
    *,
    edition_name: str,
    issue_number: int,
    program_id: str | None,
    programs_root: Path,
) -> tuple[Path | None, str | None]:
    if not program_id:
        return None, None
    artifact_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.workstream_associations.json"
    if not artifact_path.exists():
        return None, None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"Workstream association artifact at {artifact_path} is invalid.")
        if any(not isinstance(entry, dict) for entry in payload):
            raise ValueError(f"Workstream association artifact at {artifact_path} contains non-object entries.")
        records = tuple(record_from_dict(entry) for entry in payload)
        if any(record.edition != edition_name for record in records):
            raise ValueError(
                f"Workstream association artifact at {artifact_path} contains records for a different edition."
            )
        if any(record.issue_number != issue_number for record in records):
            raise ValueError(
                f"Workstream association artifact at {artifact_path} contains records for a different issue number."
            )
        if not records:
            return None, None
        return append_workstream_association_records(program_id, records, programs_root=programs_root), None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as error:
        return None, f"Workstream association ledger skipped: {error}"


def record_ncfl_proposals(
    *,
    edition_name: str,
    issue_number: int,
    program_id: str,
    reports_root: Path,
    programs_root: Path,
) -> tuple[Path | None, int]:
    proposals = extract_proposals(
        program_id,
        edition_name,
        issue_number,
        programs_root=programs_root,
        reports_root=reports_root,
    )
    if not proposals:
        return None, 0
    stage_extracted_proposals(
        program_id,
        issue_number,
        proposals,
        programs_root=programs_root,
    )
    return get_proposals_path(program_id, issue_number, programs_root=programs_root), len(proposals)


def record_learning_distillation(
    *,
    edition_name: str,
    issue_number: int,
    editorial_rules: EditorialRules,
    programs_root: Path,
) -> tuple[Path | None, Path | None, str | None, str | None]:
    current_tracking_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.review_tracking.json"
    if not current_tracking_path.exists():
        return None, None, None, None

    try:
        tracking_reports = load_tracking_reports(get_program_output_dir(edition_name, programs_root=programs_root))
        if not tracking_reports:
            return None, None, None, None
        distiller = build_default_learning_distiller(
            trace_context=build_learning_distillation_trace_context(
                edition_name=edition_name,
                issue_number=issue_number,
            )
        )
        distillation = distiller.distill(
            editorial_rules=editorial_rules,
            tracking_reports=tracking_reports,
        )
        learning_md_path = _write_output_text(
            get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.learning.md",
            render_learning_markdown(distillation),
        )
        learning_json_path = _write_output_json(
            get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.learning.json",
            distillation,
        )
        return learning_md_path, learning_json_path, render_learning_summary(distillation), None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, LearningDistillerError) as error:
        return None, None, None, f"AI learning distillation skipped: {error}"


def record_edit_patterns_for_v2(
    *,
    edition_name: str,
    issue_number: int,
    draft_state: dict[str, Any],
    report: ReportData,
    confirmed_at: datetime,
    reports_root: Path,
) -> str | None:
    repo_root = reports_root.parent
    programs_root = repo_root / "programs"
    resolved_v2 = resolve_edition(
        edition_name,
        programs_root=programs_root,
    )
    if resolved_v2 is None:
        return None

    draft_trace_run_id = draft_state.get("ai_trace_run_id")
    resolved_draft_trace_run_id = None
    if draft_trace_run_id is not None:
        draft_trace_run_id_text = str(draft_trace_run_id).strip()
        if draft_trace_run_id_text:
            resolved_draft_trace_run_id = draft_trace_run_id_text

    patterns = build_edit_patterns(
        program_id=resolved_v2.program.id,
        edition_id=edition_name,
        issue_number=issue_number,
        recorded_at=confirmed_at,
        draft_exec_summary_text=str(draft_state.get("exec_summary_text") or ""),
        confirmed_exec_summary_text=report.exec_summary_text,
        draft_workstream_blurbs={
            str(key): str(value)
            for key, value in (draft_state.get("workstream_blurbs") or {}).items()
        },
        confirmed_workstream_blurbs=report.workstream_blurbs,
        draft_prompt_versions={
            str(key): str(value)
            for key, value in (draft_state.get("ai_prompt_versions") or {}).items()
            if key is not None and value is not None
        },
        draft_ai_confidences={
            str(key): str(value)
            for key, value in (draft_state.get("ai_confidences") or {}).items()
            if key is not None and value is not None
        },
        draft_trace_run_id=resolved_draft_trace_run_id,
    )
    if not patterns:
        return None
    append_edit_patterns(resolved_v2.program.id, patterns, programs_root=programs_root)
    return None


def confirm_additional_failures(
    *,
    overrides_document: Any,
    report: ReportData,
    workstream_data: tuple[WorkstreamData, ...],
    manifest: Any,
    ack_forecast: bool,
    unresolved_scaffold_placeholders: dict[str, tuple[str, ...]],
    build_top_items_fn,
    count_new_high_dimensions_fn,
    decision_strip_ack_required_fn,
    validate_decision_strip_ack_fn,
    risk_level_high,
    risk_level_blocked,
) -> tuple[str, ...]:
    failures: list[str] = []
    top_items = build_top_items_fn(overrides_document)
    new_high_count = count_new_high_dimensions_fn(report.scorecard_deltas)
    ack_required = decision_strip_ack_required_fn(top_items, new_high_count, report.freshness)
    ack_errors = validate_decision_strip_ack_fn(overrides_document)
    if ack_required and (overrides_document.decision_strip_ack is None or ack_errors):
        failures.append(
            "BLOCKED: Decision Strip is empty but severe signals fired. Add top_3_now or decision_strip_ack.no_leadership_ask with a 12-40 word reason."
        )
        failures.extend(ack_errors)
    for workstream in workstream_data:
        if workstream.risk in {risk_level_blocked, risk_level_high} and workstream.narrative_empty:
            failures.append(
                f"BLOCKED: Narrative empty for High-risk section {workstream.section_id}. Edit narratives/issue_{report.issue_number:03d}/ws_{workstream.section_id}.md before confirming."
            )
    if manifest.metadata.get("forecast_summary") and not ack_forecast:
        failures.append(
            "BLOCKED: Forecast present in this issue. Re-run confirm with --ack-forecast after verifying the forecast sentence and confidence."
        )
    for file_name, placeholders in sorted(unresolved_scaffold_placeholders.items()):
        failures.append(
            f"BLOCKED: {file_name} contains unresolved scaffold placeholders: {', '.join(placeholders)}"
        )
    return tuple(failures)
