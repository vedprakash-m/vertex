from __future__ import annotations

import csv
import json
from datetime import timedelta
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import typer

from src.commands.confirm import _deserialize_items, _load_draft_state
from src.commands.report import _build_continuity_deltas, _build_override_snapshot, _build_scorecard_data
from src.commands.report_narratives import _active_workstream_blurbs
from src.commands.report import _build_scorecard_packets, _humanize_anchor, _load_previous_dry_run_state, _load_report_signal_context, _parse_datetime
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.archive_store import ARCHIVE_ROOT, find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.jinja_filters import build_anchor, risk_label
from src.core.evidence_engine import build_evidence
from src.core.narrative_store import load_archived_narratives, load_narratives
from src.core.overrides_store import OverridesDocument, load_overrides
from src.core.publish_diff import build_publish_diff_report, render_publish_diff_report
from src.core.scorecard_trends import load_scorecard_trends
from src.core.snapshot_store import read_snapshot


def diff_command(
    edition: str = typer.Option("", "--edition", help="Edition name."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number. Defaults to the latest draft issue."),
    since: str = typer.Option("last-draft", "--since", help="Comparison point: last-draft, last-confirmed, or issue-N."),
    section: str | None = typer.Option(None, "--section", help="Optional section id or dimension name to diff."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    resolved_issue_number = issue or _latest_draft_issue_number(edition, programs_root=PROGRAMS_ROOT)
    if since.strip().lower() == "last-draft":
        summary = build_offline_diff_summary(
            edition_name=edition,
            issue_number=resolved_issue_number,
            section=section,
            reports_root=REPORTS_ROOT,
            programs_root=PROGRAMS_ROOT,
        )
        mode = "last-draft"
    else:
        if section is not None:
            raise typer.BadParameter("Section filtering is only supported with --since last-draft.")
        summary = build_publish_diff_summary(
            edition_name=edition,
            issue_number=resolved_issue_number,
            since=since,
            reports_root=REPORTS_ROOT,
            archive_root=ARCHIVE_ROOT,
            programs_root=PROGRAMS_ROOT,
        )
        mode = "publish"
    typer.echo(
        render_diff_output(
            {
                "edition": edition,
                "issue_number": resolved_issue_number,
                "since": since,
                "section": section or "-",
                "mode": mode,
                "summary": summary,
            },
            format=format,
        ),
        nl=False,
    )
    raise typer.Exit(code=0)


def render_diff_output(payload: dict[str, object], *, format: str) -> str:
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        columns = ("edition", "issue_number", "since", "section", "mode", "summary")
        writer.writerow(columns)
        writer.writerow([payload[column] for column in columns])
        return buffer.getvalue()
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    return str(payload["summary"])


def build_publish_diff_summary(
    *,
    edition_name: str,
    issue_number: int | None,
    since: str,
    reports_root: Path,
    archive_root: Path,
    programs_root: Path = PROGRAMS_ROOT,
) -> str:
    resolved_issue_number = issue_number or _latest_draft_issue_number(edition_name, programs_root=programs_root)
    current_draft_state = _load_draft_state(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        programs_root=programs_root,
    )

    reference_entry = _resolve_confirmed_reference(
        edition_name=edition_name,
        current_issue_number=resolved_issue_number,
        since=since,
        archive_root=archive_root,
    )
    if reference_entry is None or reference_entry.snapshot_path is None:
        return f"VERTEX DIFF - No confirmed baseline found for {since} before Issue {resolved_issue_number:03d}.\n"

    snapshot_path = Path(reference_entry.snapshot_path)
    if not snapshot_path.exists():
        return f"VERTEX DIFF - Confirmed snapshot for Issue {reference_entry.issue_number:03d} is missing.\n"
    reference_snapshot = read_snapshot(snapshot_path)

    bundle = load_bundle(edition_name, reports_root=reports_root)
    current_items = _deserialize_current_items(current_draft_state)
    current_data_as_of = _parse_datetime(current_draft_state.get("ado_data_as_of"))
    if current_data_as_of is None:
        raise typer.BadParameter(f"Draft state for Issue {resolved_issue_number:03d} is missing ado_data_as_of.")

    evidence_window_start = current_data_as_of - timedelta(days=bundle.config.ado.date_window_days)
    evidence_by_item = {
        item.id: build_evidence(item, evidence_window_start, current_data_as_of)
        for item in current_items
    }
    deltas = _build_continuity_deltas(
        current_items=current_items,
        previous_snapshot=reference_snapshot,
        issue_number=resolved_issue_number,
        previous_issue_number=reference_entry.issue_number,
        evidence_by_item=evidence_by_item,
    )

    overrides_document = load_overrides(
        edition_name,
        reports_root=reports_root,
        issue_number=resolved_issue_number,
    ) or OverridesDocument(issue_number=None, top_3_now=(), scorecards=())
    scorecard_packets = _build_scorecard_packets(
        bundle,
        current_items,
        reference_snapshot,
        edition_name=edition_name,
        archive_root=archive_root,
    )
    scorecards, _, _ = _build_scorecard_data(
        bundle,
        current_items,
        evidence_by_item,
        scorecard_packets,
        overrides_document,
        edition_name=edition_name,
        reports_root=reports_root,
    )
    current_dimension_risks = {
        (scorecard.scorecard_name, dimension.name): dimension.risk
        for scorecard in scorecards
        for dimension in scorecard.dimensions
    }
    scorecard_trends = load_scorecard_trends(
        edition_name,
        current_dimension_risks,
        archive_root=archive_root,
    )

    current_narratives = load_narratives(
        edition_name,
        resolved_issue_number,
        reports_root=reports_root,
    )
    previous_narratives = load_archived_narratives(
        edition_name,
        reference_entry.issue_number,
        archive_root=archive_root,
    )
    signal_context = _load_report_signal_context(
        edition_name=edition_name,
        bundle=bundle,
        items=current_items,
        as_of=current_data_as_of,
        previous_snapshot=reference_snapshot,
        reports_root=reports_root,
    )

    report = build_publish_diff_report(
        current_issue_number=resolved_issue_number,
        reference_issue_number=reference_entry.issue_number,
        reference_generated_at=reference_entry.generated_at,
        current_items=current_items,
        reference_items=reference_snapshot.items,
        deltas=deltas,
        current_dimension_risks=current_dimension_risks,
        reference_scorecards=reference_snapshot.scorecards,
        scorecard_trends=scorecard_trends,
        current_narratives=current_narratives,
        previous_narratives=previous_narratives,
        approved_signals=(signal_context.approved_signals if signal_context is not None else ()),
        drift_patterns=(signal_context.drift_patterns if signal_context is not None else ()),
    )
    return render_publish_diff_report(report)


def build_offline_diff_summary(
    *,
    edition_name: str,
    issue_number: int | None,
    section: str | None,
    reports_root: Path,
    programs_root: Path = PROGRAMS_ROOT,
) -> str:
    resolved_issue_number = issue_number or _latest_draft_issue_number(edition_name, programs_root=programs_root)
    _load_draft_state(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        programs_root=programs_root,
    )
    previous_dry_run_state = _load_previous_dry_run_state(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        programs_root=programs_root,
    )
    if previous_dry_run_state is None:
        return f"VERTEX DIFF - No previous dry-run found for Issue {resolved_issue_number:03d}.\n"

    bundle = load_bundle(edition_name, reports_root=reports_root)
    loaded_narratives = load_narratives(edition_name, resolved_issue_number, reports_root=reports_root)
    current_exec_summary_text = loaded_narratives.get("exec_summary.md", "").strip()
    current_workstream_blurbs = _active_workstream_blurbs(loaded_narratives)
    overrides_document = load_overrides(edition_name, reports_root=reports_root)
    current_override_snapshot = _build_override_snapshot(overrides_document) if overrides_document is not None else {}
    current_top_3_now = tuple(entry.text.strip() for entry in overrides_document.top_3_now if entry.text.strip()) if overrides_document is not None else ()

    section_labels = {
        build_anchor(f"{scorecard.name}-{dimension.name}"): dimension.name
        for scorecard in bundle.config.scorecards
        for dimension in scorecard.dimensions
    }
    available_section_ids = set(section_labels) | set(current_workstream_blurbs)
    previous_workstream_blurbs = previous_dry_run_state.get("workstream_blurbs")
    if isinstance(previous_workstream_blurbs, dict):
        available_section_ids.update(str(section_id) for section_id in previous_workstream_blurbs)
    resolved_section = _resolve_section(section, available_section_ids)

    previous_generated_at = _parse_previous_generated_at(previous_dry_run_state)
    header = f"VERTEX DIFF - Issue {resolved_issue_number:03d} vs last dry-run"
    if previous_generated_at is not None:
        header += f" ({previous_generated_at.strftime('%b %d %H:%M UTC')})"

    lines = [header, ""]
    changed_labels: list[str] = []
    unchanged_labels: list[str] = []

    narrative_lines, narrative_changed, narrative_unchanged = _build_narrative_diff_lines(
        previous_dry_run_state=previous_dry_run_state,
        current_exec_summary_text=current_exec_summary_text,
        current_workstream_blurbs=current_workstream_blurbs,
        resolved_section=resolved_section,
        section_labels=section_labels,
    )
    lines.extend(narrative_lines)
    changed_labels.extend(narrative_changed)
    unchanged_labels.extend(narrative_unchanged)

    override_lines, override_changed, override_unchanged = _build_override_diff_lines(
        previous_dry_run_state=previous_dry_run_state,
        current_override_snapshot=current_override_snapshot,
        resolved_section=resolved_section,
        section_labels=section_labels,
    )
    if narrative_lines and override_lines:
        lines.append("")
    lines.extend(override_lines)
    changed_labels.extend(override_changed)
    unchanged_labels.extend(override_unchanged)

    top3_lines, top3_changed, top3_unchanged = _build_top3_diff_lines(
        previous_dry_run_state=previous_dry_run_state,
        current_top_3_now=current_top_3_now,
        resolved_section=resolved_section,
    )
    if (narrative_lines or override_lines) and top3_lines:
        lines.append("")
    lines.extend(top3_lines)
    changed_labels.extend(top3_changed)
    unchanged_labels.extend(top3_unchanged)

    if not changed_labels:
        lines.append("No changes detected.")
    elif unchanged_labels and resolved_section is None:
        lines.extend(["", f"No changes to: {_summarize_labels(unchanged_labels)}."])

    return "\n".join(lines).rstrip() + "\n"


def _latest_draft_issue_number(edition_name: str, programs_root: Path = PROGRAMS_ROOT) -> int:
    output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    draft_paths = sorted(output_dir.glob("issue_*/issue_*.draft.json"))
    if not draft_paths:
        raise typer.BadParameter(f"No dry-run draft found for {edition_name}. Run `vertex report --dry-run --edition {edition_name}` first.")
    return max(int(path.stem.split("_")[1].split(".")[0]) for path in draft_paths)


def _resolve_confirmed_reference(
    *,
    edition_name: str,
    current_issue_number: int,
    since: str,
    archive_root: Path,
):
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    normalized = since.strip().lower()
    if normalized == "last-confirmed":
        return find_latest_confirmed_entry(archive_index, before_issue_number=current_issue_number)
    requested_issue = _parse_issue_reference(normalized)
    if requested_issue is None:
        raise typer.BadParameter("--since must be last-draft, last-confirmed, or issue-N.")
    for entry in archive_index.issues:
        if entry.kind == "confirmed" and entry.issue_number == requested_issue:
            return entry
    return None


def _parse_issue_reference(value: str) -> int | None:
    if value.startswith("issue-"):
        candidate = value.removeprefix("issue-")
        if candidate.isdigit():
            return int(candidate)
    if value.isdigit():
        return int(value)
    return None


def _deserialize_current_items(current_draft_state: dict[str, Any]) -> tuple[Any, ...]:
    payload = current_draft_state.get("items")
    if not isinstance(payload, list):
        raise typer.BadParameter("Draft state is missing serialized items.")
    return _deserialize_items(tuple(item for item in payload if isinstance(item, dict)))


def _resolve_section(section: str | None, available_section_ids: set[str]) -> str | None:
    if section is None:
        return None
    normalized = section.strip()
    if not normalized:
        return None
    if normalized == "exec_summary":
        return normalized
    if normalized in available_section_ids:
        return normalized
    anchored = build_anchor(normalized)
    candidates = sorted(
        section_id
        for section_id in available_section_ids
        if section_id == anchored or section_id.endswith(f"-{anchored}")
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        choices = ", ".join(["exec_summary", *sorted(available_section_ids)])
        raise typer.BadParameter(f"Unknown section '{section}'. Available sections: {choices}")
    raise typer.BadParameter(
        f"Section '{section}' is ambiguous. Use one of: {', '.join(candidates)}"
    )


def _build_narrative_diff_lines(
    *,
    previous_dry_run_state: dict[str, Any],
    current_exec_summary_text: str,
    current_workstream_blurbs: dict[str, str],
    resolved_section: str | None,
    section_labels: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    lines: list[str] = []
    changed_labels: list[str] = []
    unchanged_labels: list[str] = []

    if resolved_section in {None, "exec_summary"}:
        previous_exec_summary_text = str(previous_dry_run_state.get("exec_summary_text", "")).strip()
        if previous_exec_summary_text != current_exec_summary_text:
            lines.extend(
                [
                    "Executive Summary (narrative changed):",
                    f"  Before: {_inline_diff_value(previous_exec_summary_text)}",
                    f"  After:  {_inline_diff_value(current_exec_summary_text)}",
                ]
            )
            changed_labels.append("Executive Summary")
        elif resolved_section is None:
            unchanged_labels.append("Executive Summary")

    previous_workstream_blurbs = previous_dry_run_state.get("workstream_blurbs")
    if not isinstance(previous_workstream_blurbs, dict):
        return lines, changed_labels, unchanged_labels

    candidate_section_ids = sorted(set(previous_workstream_blurbs) | set(current_workstream_blurbs))
    if resolved_section not in {None, "exec_summary"}:
        candidate_section_ids = [resolved_section]

    for section_id in candidate_section_ids:
        previous_text = str(previous_workstream_blurbs.get(section_id, "")).strip()
        current_text = current_workstream_blurbs.get(section_id, "").strip()
        label = section_labels.get(section_id, _humanize_anchor(section_id))
        if previous_text == current_text:
            unchanged_labels.append(label)
            continue
        if lines:
            lines.append("")
        lines.extend(
            [
                f"{label} (narrative changed):",
                f"  Before: {_inline_diff_value(previous_text)}",
                f"  After:  {_inline_diff_value(current_text)}",
            ]
        )
        changed_labels.append(label)

    return lines, changed_labels, unchanged_labels


def _build_override_diff_lines(
    *,
    previous_dry_run_state: dict[str, Any],
    current_override_snapshot: dict[str, dict[str, dict[str, Any]]],
    resolved_section: str | None,
    section_labels: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    previous_override_snapshot = previous_dry_run_state.get("override_snapshot")
    if not isinstance(previous_override_snapshot, dict) or resolved_section == "exec_summary":
        return [], [], []

    lines: list[str] = []
    changed_labels: list[str] = []
    unchanged_labels: list[str] = []
    for scorecard_name in sorted(set(previous_override_snapshot) | set(current_override_snapshot)):
        previous_dimensions = previous_override_snapshot.get(scorecard_name, {})
        current_dimensions = current_override_snapshot.get(scorecard_name, {})
        if not isinstance(previous_dimensions, dict) or not isinstance(current_dimensions, dict):
            continue
        for dimension_name in sorted(set(previous_dimensions) | set(current_dimensions)):
            section_id = build_anchor(f"{scorecard_name}-{dimension_name}")
            if resolved_section is not None and resolved_section != section_id:
                continue
            previous_payload = previous_dimensions.get(dimension_name, {}) if isinstance(previous_dimensions, dict) else {}
            current_payload = current_dimensions.get(dimension_name, {}) if isinstance(current_dimensions, dict) else {}
            label = section_labels.get(section_id, dimension_name)
            changed = False

            previous_risk = previous_payload.get("risk") if isinstance(previous_payload, dict) else None
            current_risk = current_payload.get("risk") if isinstance(current_payload, dict) else None
            if previous_risk != current_risk:
                if lines:
                    lines.append("")
                lines.extend(
                    [
                        f"{label} (risk level changed via override):",
                        f"  Before: {_diff_risk_label(previous_risk)}",
                        f"  After:  {_diff_risk_label(current_risk)}",
                    ]
                )
                changed = True

            previous_eta = _normalize_optional(previous_payload.get("eta") if isinstance(previous_payload, dict) else None)
            current_eta = _normalize_optional(current_payload.get("eta") if isinstance(current_payload, dict) else None)
            if previous_eta != current_eta:
                if lines:
                    lines.append("")
                lines.extend(
                    [
                        f"{label} (ETA override changed):",
                        f"  Before: {_inline_diff_value(previous_eta)}",
                        f"  After:  {_inline_diff_value(current_eta)}",
                    ]
                )
                changed = True

            previous_summary = _normalize_optional(previous_payload.get("summary") if isinstance(previous_payload, dict) else None)
            current_summary = _normalize_optional(current_payload.get("summary") if isinstance(current_payload, dict) else None)
            if previous_summary != current_summary:
                if lines:
                    lines.append("")
                lines.extend(
                    [
                        f"{label} (summary override changed):",
                        f"  Before: {_inline_diff_value(previous_summary)}",
                        f"  After:  {_inline_diff_value(current_summary)}",
                    ]
                )
                changed = True

            if changed:
                changed_labels.append(label)
            else:
                unchanged_labels.append(label)

    return lines, changed_labels, unchanged_labels


def _build_top3_diff_lines(
    *,
    previous_dry_run_state: dict[str, Any],
    current_top_3_now: tuple[str, ...],
    resolved_section: str | None,
) -> tuple[list[str], list[str], list[str]]:
    if resolved_section is not None:
        return [], [], []
    previous_top_3_now = previous_dry_run_state.get("top_3_now")
    if not isinstance(previous_top_3_now, list):
        return [], [], []
    normalized_previous = tuple(str(item).strip() for item in previous_top_3_now if str(item).strip())
    if normalized_previous == current_top_3_now:
        return [], [], ["Top 3 Now"]
    return (
        [
            "Top 3 Now (selection changed):",
            f"  Before: {_inline_diff_value('; '.join(normalized_previous))}",
            f"  After:  {_inline_diff_value('; '.join(current_top_3_now))}",
        ],
        ["Top 3 Now"],
        [],
    )


def _inline_diff_value(value: str | None) -> str:
    normalized = _normalize_optional(value)
    return normalized if normalized is not None else "[empty]"


def _normalize_optional(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return " ".join(str(value).split())


def _diff_risk_label(value: Any) -> str:
    normalized = _normalize_optional(value)
    if normalized is None:
        return "none"
    try:
        return risk_label(normalized)
    except Exception:
        return normalized


def _parse_previous_generated_at(previous_dry_run_state: dict[str, Any]) -> datetime | None:
    raw_value = previous_dry_run_state.get("generated_at")
    if raw_value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _summarize_labels(labels: list[str]) -> str:
    unique_labels = list(dict.fromkeys(labels))
    if len(unique_labels) <= 3:
        return ", ".join(unique_labels)
    return ", ".join(unique_labels[:2]) + f", +{len(unique_labels) - 2} others"