from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from src.core.quality_matrix_engine import QualityMatrix, SliceQualityRecord


@dataclass(frozen=True, slots=True)
class RemediationItem:
    slice_id: str
    title: str
    quality_state: str
    status: str
    owner: str
    support_tpm: str | None
    newsletter_surface: str
    impact: str
    problem: str
    failing_condition: str
    item_ids: tuple[int, ...]
    query_refs: tuple[str, ...]
    missing_fields: dict[str, tuple[int, ...]]
    stale_item_ids: tuple[int, ...]
    required_action: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RemediationReport:
    schema_version: str
    edition: str
    issue_number: int
    generated_at: datetime
    summary: dict[str, int]
    items: tuple[RemediationItem, ...]


def build_remediation_report(matrix: QualityMatrix) -> RemediationReport:
    items = tuple(
        _build_remediation_item(slice_row)
        for slice_row in matrix.slices
        if slice_row.quality_state != "healthy"
    )
    summary_counter = Counter(item.impact for item in items)
    summary = {
        "total_items": len(items),
        "blocks_publication": summary_counter.get("blocks_publication", 0),
        "degrades_publication": summary_counter.get("degrades_publication", 0),
        "reduces_confidence": summary_counter.get("reduces_confidence", 0),
    }
    return RemediationReport(
        schema_version="1.0",
        edition=matrix.edition,
        issue_number=matrix.issue_number,
        generated_at=matrix.generated_at,
        summary=summary,
        items=items,
    )


def render_remediation_markdown(report: RemediationReport) -> str:
    lines = [
        f"# Remediation Report — {report.edition} Issue {report.issue_number:03d}",
        "",
        f"Generated: {report.generated_at.isoformat()}",
        "",
        "## Summary",
        f"- Total remediation asks: {report.summary['total_items']}",
        f"- Blocks publication: {report.summary['blocks_publication']}",
        f"- Degrades publication: {report.summary['degrades_publication']}",
        f"- Reduces confidence: {report.summary['reduces_confidence']}",
    ]
    if not report.items:
        lines.extend(["", "No remediation asks are currently open."])
        return "\n".join(lines) + "\n"

    for item in report.items:
        lines.extend(
            [
                "",
                f"## {item.slice_id} — {item.title}",
                f"Owner: {item.owner}",
                f"Support TPM: {item.support_tpm or 'n/a'}",
                f"Newsletter impact: {item.impact}",
                f"Problem: {item.problem}",
                f"Failing condition: {item.failing_condition}",
                f"Newsletter surface: {item.newsletter_surface}",
            ]
        )
        if item.item_ids:
            lines.append(f"ADO ids: {', '.join(str(item_id) for item_id in item.item_ids)}")
        if item.query_refs:
            lines.append(f"Query refs: {', '.join(item.query_refs)}")
        if item.missing_fields:
            lines.append(
                "Missing fields: "
                + "; ".join(
                    f"{field_name} on {', '.join(str(item_id) for item_id in item_ids)}"
                    for field_name, item_ids in item.missing_fields.items()
                )
            )
        lines.append("Required action:")
        for index, action in enumerate(item.required_action, start=1):
            lines.append(f"{index}. {action}")
    return "\n".join(lines) + "\n"


def _build_remediation_item(slice_row: SliceQualityRecord) -> RemediationItem:
    query_refs = tuple(slice_row.saved_queries) + ((slice_row.ado_query_url,) if slice_row.ado_query_url else ())
    failing_condition = slice_row.failing_conditions[0] if slice_row.failing_conditions else slice_row.quality_state
    return RemediationItem(
        slice_id=slice_row.slice_id,
        title=slice_row.title,
        quality_state=slice_row.quality_state,
        status=slice_row.status,
        owner=slice_row.primary_owner,
        support_tpm=slice_row.support_tpm,
        newsletter_surface=slice_row.newsletter_surface,
        impact=_impact_for(slice_row),
        problem=slice_row.issues[0] if slice_row.issues else f"Slice quality is {slice_row.quality_state}.",
        failing_condition=failing_condition,
        item_ids=slice_row.assigned_item_ids,
        query_refs=query_refs,
        missing_fields=slice_row.missing_fields,
        stale_item_ids=slice_row.stale_item_ids,
        required_action=_required_actions(slice_row),
    )


def _impact_for(slice_row: SliceQualityRecord) -> str:
    if slice_row.quality_state == "under_specified":
        return "blocks_publication"
    if slice_row.quality_state == "manual_only":
        return "reduces_confidence"
    return "degrades_publication"


def _required_actions(slice_row: SliceQualityRecord) -> tuple[str, ...]:
    actions: list[str] = []
    if "assignment_empty" in slice_row.failing_conditions:
        actions.append(
            "Confirm the slice query still maps to live work items, or record explicitly that this slice is clear for the current issue."
        )
    if slice_row.stale_item_ids:
        actions.append(
            "Add a current ADO update or owner comment to "
            + ", ".join(str(item_id) for item_id in slice_row.stale_item_ids)
            + "."
        )
    for field_name, item_ids in slice_row.missing_fields.items():
        actions.append(
            f"Populate {field_name} on ADO items {', '.join(str(item_id) for item_id in item_ids)}."
        )
    if slice_row.telemetry is not None and slice_row.telemetry.status in {"absent", "degraded"}:
        actions.append(slice_row.telemetry.fallback_behavior)
    if slice_row.telemetry is not None and slice_row.telemetry.contradiction is not None:
        actions.append("Reconcile the telemetry output against the linked ADO slice before repeating the claim in the newsletter.")
    if slice_row.remediation_template:
        actions.extend(_template_actions(slice_row.remediation_template))
    if not actions:
        actions.append("Review the slice and confirm the current author-owned data is still accurate.")
    return tuple(dict.fromkeys(actions))


def _template_actions(template: str) -> tuple[str, ...]:
    actions: list[str] = []
    for line in template.splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if stripped:
            actions.append(stripped)
    return tuple(actions)