from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from src.core.models import AttributionTier, Comment, Confidence, Enrichment, EvidencePacket, Revision, WorkItem


def build_evidence(
    item: WorkItem,
    window_start: datetime,
    window_end: datetime,
    enrichments_by_item: Mapping[int, tuple[Enrichment, ...]] | None = None,
) -> EvidencePacket:
    revisions = tuple(
        revision
        for revision in item.revisions
        if window_start <= revision.changed_date <= window_end
    )
    comments = tuple(
        comment
        for comment in item.comments
        if window_start <= comment.created_date <= window_end
    )
    enrichments = tuple((enrichments_by_item or {}).get(item.id, ()))
    changed_date = _changed_date_from_custom_fields(item)
    has_changed_date_evidence = (
        changed_date is not None and window_start <= changed_date <= window_end
    )

    if revisions or comments or enrichments:
        if revisions and comments:
            confidence = Confidence.HIGH
        elif revisions or comments:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW
    elif has_changed_date_evidence:
        confidence = Confidence.LOW
    else:
        confidence = Confidence.NONE

    tier = (
        AttributionTier.TIER1
        if confidence in (Confidence.HIGH, Confidence.MEDIUM)
        else AttributionTier.TIER3
    )
    return EvidencePacket(
        work_item_id=item.id,
        revisions=revisions,
        comments=comments,
        enrichments=enrichments,
        confidence=confidence,
        tier=tier,
        summary_for_reviewer=render_reviewer_summary(
            item,
            revisions,
            comments,
            enrichments,
            changed_date=changed_date if has_changed_date_evidence else None,
        ),
    )


def render_reviewer_summary(
    item: WorkItem,
    revisions: tuple[Revision, ...],
    comments: tuple[Comment, ...],
    enrichments: tuple[Enrichment, ...],
    *,
    changed_date: datetime | None = None,
) -> str:
    lines: list[str] = []
    if revisions:
        lines.append(f"Revisions ({len(revisions)}):")
        for revision in revisions:
            changed_fields = ", ".join(sorted(revision.fields_changed)) or "no tracked fields"
            lines.append(
                f"- rev {revision.rev_number} by {revision.changed_by} on {revision.changed_date.isoformat()} ({changed_fields})"
            )
    if comments:
        lines.append(f"Comments ({len(comments)}):")
        for comment in comments:
            preview = comment.text.strip().replace("\n", " ")[:120]
            lines.append(
                f"- {comment.created_by} on {comment.created_date.isoformat()}: {preview}"
            )
    if enrichments:
        lines.append(f"Enrichments ({len(enrichments)}):")
        for enrichment in enrichments:
            preview = enrichment.excerpt.strip().replace("\n", " ")[:120]
            lines.append(
                f"- {enrichment.source} by {enrichment.author} on {enrichment.timestamp.isoformat()}: {preview}"
            )
    if changed_date is not None:
        lines.append(f"ADO item changed on {changed_date.isoformat()}.")
    if item.risk_assessment is not None:
        if item.risk_assessment_comment:
            lines.append(f"Risk assessment: {item.risk_assessment} | {' '.join(item.risk_assessment_comment.split())[:180]}")
        else:
            lines.append(f"Risk assessment: {item.risk_assessment}")
    if item.child_items:
        blocked_children = sum(1 for child in item.child_items if child.risk_level.value == "high")
        lines.append(
            f"Child work: {len(item.child_items)} item(s), {blocked_children} blocked/high-risk."
        )
    findings = item.custom_fields.get("significant_findings")
    if isinstance(findings, list):
        for finding in findings[:3]:
            lines.append(f"Finding: {str(finding)}")
    if not lines:
        return "No evidence in selected window."
    return "\n".join(lines)


def _changed_date_from_custom_fields(item: WorkItem) -> datetime | None:
    raw_value = item.custom_fields.get("changed_date")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    normalized = raw_value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
