from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from src.core.ado_pr_client import PullRequestSummary
from src.core.integration_types import ADOHydrationOutput, ExtractionResult
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Signal, TrajectoryPoint
from src.core.signal_ref_utils import extract_work_item_refs, merge_entity_refs


TRACKED_FIELDS = {
    "System.State": "State",
    "System.AssignedTo": "AssignedTo",
    "Microsoft.VSTS.Scheduling.TargetDate": "TargetDate",
    "System.Tags": "Tags",
}


class ADOSignalExtractor:
    @property
    def channel(self) -> str:
        return "ado"

    def extract(self, resources: ADOHydrationOutput, program_id: str) -> ExtractionResult:
        signals: list[Signal] = []
        trajectory_points: list[TrajectoryPoint] = []
        for item in resources.work_items:
            signals.extend(_revision_signals(item, program_id=program_id))
            signals.extend(_comment_signals(item, program_id=program_id))
            trajectory_points.append(
                TrajectoryPoint(
                    date=item.fetched_at.date(),
                    state=item.state,
                    assigned_to=item.assigned_to,
                    target_date=item.target_date,
                    risk_level=item.risk_level,
                    area_path=item.area_path,
                    tags=tuple(item.tags),
                    risk_assessment=item.risk_assessment,
                    risk_assessment_comment=item.risk_assessment_comment,
                )
            )
        for item in resources.freshness_items or ():
            signals.extend(_freshness_signals(item, program_id=program_id, as_of=item.fetched_at))
        for pr in getattr(resources, "pull_requests", ()):
            signals.extend(_pr_signals(pr, program_id=program_id))
        return ExtractionResult(
            channel="ado",
            signals=tuple(signals),
            trajectory_points=tuple(trajectory_points),
            side_artifacts={},
            errors=(),
        )


def _revision_signals(item: WorkItem, *, program_id: str) -> list[Signal]:
    signals: list[Signal] = []
    for revision in sorted(item.revisions, key=lambda entry: entry.changed_date):
        for field_name, values in revision.fields_changed.items():
            canonical_field = TRACKED_FIELDS.get(field_name)
            if canonical_field is None:
                continue
            prior, current = values
            for workstream_id, suffix in _workstream_suffixes(item):
                raw_ref = f"ado/revision/{item.id}/{revision.rev_number}/{canonical_field.lower()}/{suffix}"
                signals.append(
                    Signal(
                        id=raw_ref,
                        timestamp=revision.changed_date,
                        source="ado",
                        program_id=program_id,
                        workstream_id=workstream_id,
                        entity_refs=merge_entity_refs(
                            provider_refs=(f"ado:{item.id}", f"WI:{item.id}"),
                            workstream_id=workstream_id,
                        ),
                        text=_truncate(f"WI {item.id} {canonical_field}: {_display(prior)} -> {_display(current)}"),
                        raw_ref=raw_ref,
                        confidence=Confidence.HIGH,
                        metadata={
                            "work_item_id": item.id,
                            "revision_number": revision.rev_number,
                            "field": canonical_field,
                            "prior": prior,
                            "current": current,
                            "legacy_entity_ref": f"WI:{item.id}",
                        },
                    )
                )
    return signals


def _comment_signals(item: WorkItem, *, program_id: str) -> list[Signal]:
    signals: list[Signal] = []
    for comment in sorted(item.comments, key=lambda entry: entry.created_date):
        text = comment.text.strip()
        if not text:
            continue
        for workstream_id, suffix in _workstream_suffixes(item):
            raw_ref = f"ado/comment/{item.id}/{comment.comment_id}/{suffix}"
            signals.append(
                Signal(
                    id=raw_ref,
                    timestamp=comment.created_date,
                    source="ado",
                    program_id=program_id,
                    workstream_id=workstream_id,
                    entity_refs=merge_entity_refs(
                        provider_refs=(f"ado:{item.id}", f"WI:{item.id}"),
                        workstream_id=workstream_id,
                    ),
                    text=_truncate(f"Comment on WI {item.id}: {text}"),
                    raw_ref=raw_ref,
                    confidence=Confidence.HIGH,
                    metadata={
                        "work_item_id": item.id,
                        "comment_id": comment.comment_id,
                        "author": comment.created_by,
                        "legacy_entity_ref": f"WI:{item.id}",
                    },
                )
            )
    return signals


def _freshness_signals(item: WorkItem, *, program_id: str, as_of: datetime) -> list[Signal]:
    signals: list[Signal] = []
    changed_age_days = max((as_of - item.fetched_at).days, 0)
    if item.assigned_to:
        return signals
    for workstream_id, suffix in _workstream_suffixes(item):
        capture_date = as_of.date().isoformat()
        raw_ref = f"ado/freshness/{item.id}/unowned/{capture_date}/{suffix}"
        signals.append(
            Signal(
                id=raw_ref,
                timestamp=as_of,
                source="ado",
                program_id=program_id,
                workstream_id=workstream_id,
                entity_refs=merge_entity_refs(
                    provider_refs=(f"ado:{item.id}", f"WI:{item.id}"),
                    workstream_id=workstream_id,
                ),
                text=f"Unowned work item: WI {item.id} has no assigned owner.",
                raw_ref=raw_ref,
                confidence=Confidence.HIGH,
                metadata={
                    "work_item_id": item.id,
                    "finding_type": "unowned",
                    "date": capture_date,
                    "changed_age_days": changed_age_days,
                    "legacy_entity_ref": f"WI:{item.id}",
                },
            )
        )
    return signals


def _workstream_suffixes(item: WorkItem) -> tuple[tuple[str | None, str], ...]:
    raw_workstreams = item.custom_fields.get("workstream_ids")
    if isinstance(raw_workstreams, tuple):
        workstreams = tuple(str(value) for value in raw_workstreams if str(value).strip())
    elif isinstance(raw_workstreams, list):
        workstreams = tuple(str(value) for value in raw_workstreams if str(value).strip())
    else:
        workstreams = ()
    if not workstreams:
        return ((None, "_unassigned"),)
    return tuple((workstream_id, workstream_id) for workstream_id in dict.fromkeys(workstreams))


def _display(value: str | None) -> str:
    return "(empty)" if value is None or value == "" else value


def _truncate(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def legacy_signal_id(*, program_id: str, raw_ref: str, timestamp: datetime) -> str:
    return str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{timestamp.isoformat()}"))


def _pr_signals(pr: PullRequestSummary, program_id: str) -> list[Signal]:
    signals: list[Signal] = []
    status_lower = pr.status.lower()
    if status_lower == "completed":
        kind = "PR_MERGED"
    elif status_lower == "active":
        kind = "PR_ACTIVE"
    else:
        return []

    workstream_ids = pr.workstream_ids or (None,)
    for ws_id in workstream_ids:
        suffix = ws_id or "_unassigned"
        raw_ref = f"ado/pr/{pr.repository_id}/{pr.pr_id}/{status_lower}/{suffix}"
        
        if kind == "PR_MERGED":
            text = f"PR {pr.pr_id} Merged: {pr.title} merged to {pr.target_ref}"
        else:
            text = f"PR {pr.pr_id} Active: PR open: {pr.title} ({pr.created_by}, targeting {pr.target_ref})"

        signals.append(
            Signal(
                id=raw_ref,
                timestamp=pr.merged_at if (kind == "PR_MERGED" and pr.merged_at) else pr.created_at,
                source="ado",
                program_id=program_id,
                workstream_id=ws_id,
                entity_refs=merge_entity_refs(
                    provider_refs=(f"pr:{pr.pr_id}", f"ado/pr:{pr.pr_id}"),
                    workstream_id=ws_id,
                    additional_refs=extract_work_item_refs(pr.title),
                ),
                text=text,
                raw_ref=raw_ref,
                confidence=Confidence.HIGH,
                metadata={
                    "pr_id": pr.pr_id,
                    "title": pr.title,
                    "status": pr.status,
                    "created_by": pr.created_by,
                    "target_ref": pr.target_ref,
                    "source_ref": pr.source_ref,
                    "url": pr.url,
                    "repository_id": pr.repository_id,
                    "kind": kind,
                },
            )
        )
    return signals
