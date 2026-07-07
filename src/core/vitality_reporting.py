from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from src.core.models import WorkItem
from src.core.models_v2 import PersonDirectory, VitalityAggregate, VitalityArchiveEntry, VitalityArchiveWorkstream, VitalityProgramSnapshot, VitalityScore, Workstream
from src.core.vitality_scorer import aggregate_vitality
from src.core.view_models import AdoVitalityAccountabilityRow, AdoVitalitySectionData


@dataclass(frozen=True, slots=True)
class VitalitySettings:
    triage: bool
    newsletter_aggregate: bool
    newsletter_individual_praise: bool
    reviewer_pane: bool = False
    ado_nudge_comments: bool = False
    ado_tags: bool = False
    nudge_composite_threshold: int = 40
    nudge_stale_days: int = 14
    nudge_cooldown_days: int = 14
    tag_consecutive_gaps: int = 2
    sparse_workiq_threshold: int = 5
    vitality_tag_name: str = "Needs-PM-Review"
    vitality_archive_per_person: bool = False
    exempt_aliases: tuple[str, ...] = ()


def vitality_settings_from_program(raw_program: Mapping[str, Any]) -> VitalitySettings:
    vitality_block = raw_program.get("vitality") if isinstance(raw_program, Mapping) else None
    if not isinstance(vitality_block, Mapping):
        return VitalitySettings(
            triage=False,
            newsletter_aggregate=False,
            newsletter_individual_praise=False,
            reviewer_pane=False,
            ado_nudge_comments=False,
            ado_tags=False,
            nudge_composite_threshold=40,
            nudge_stale_days=14,
            nudge_cooldown_days=14,
            tag_consecutive_gaps=2,
            sparse_workiq_threshold=5,
            vitality_tag_name="Needs-PM-Review",
            vitality_archive_per_person=False,
            exempt_aliases=(),
        )
    surfaces = vitality_block.get("surfaces")
    if not isinstance(surfaces, Mapping):
        surfaces = {}
    exempt_aliases = vitality_block.get("exempt_aliases")
    return VitalitySettings(
        triage=bool(surfaces.get("triage", False)),
        newsletter_aggregate=bool(surfaces.get("newsletter_aggregate", False)),
        newsletter_individual_praise=bool(surfaces.get("newsletter_individual_praise", False)),
        reviewer_pane=bool(surfaces.get("reviewer_pane", False)),
        ado_nudge_comments=bool(surfaces.get("ado_nudge_comments", False)),
        ado_tags=bool(surfaces.get("ado_tags", False)),
        nudge_composite_threshold=int(vitality_block.get("nudge_composite_threshold", 40) or 40),
        nudge_stale_days=int(vitality_block.get("nudge_stale_days", 14) or 14),
        nudge_cooldown_days=int(vitality_block.get("nudge_cooldown_days", 14) or 14),
        tag_consecutive_gaps=int(vitality_block.get("tag_consecutive_gaps", 2) or 2),
        sparse_workiq_threshold=int(vitality_block.get("sparse_workiq_threshold", 5) or 5),
        vitality_tag_name=str(vitality_block.get("vitality_tag_name") or "Needs-PM-Review"),
        vitality_archive_per_person=bool(vitality_block.get("vitality_archive_per_person", False)),
        exempt_aliases=tuple(
            alias.strip().lower()
            for alias in exempt_aliases
            if isinstance(alias, str) and alias.strip()
        ) if isinstance(exempt_aliases, list) else (),
    )


def effective_vitality_exempt_aliases(
    settings: VitalitySettings,
    people_directory: tuple[PersonDirectory, ...] = (),
) -> tuple[str, ...]:
    aliases = set(settings.exempt_aliases)
    for person in people_directory:
        if not person.exempt_from_vitality:
            continue
        normalized = person.alias.strip().lower()
        if normalized:
            aliases.add(normalized)
    return tuple(sorted(aliases))


def build_vitality_snapshot(
    scores: tuple[VitalityScore, ...],
    workstream_aggregates: tuple[VitalityAggregate, ...],
    *,
    leakage_signal_threshold: int = 5,
) -> VitalityProgramSnapshot:
    if not scores:
        return VitalityProgramSnapshot(
            scores=(),
            workstream_aggregates=workstream_aggregates,
            total_items=0,
            items_fresh=0,
            updated_percentage=0,
            freshness_average_days=0.0,
            avg_richness=0,
            leakage_events=0,
            aggregate_score=0,
        )

    total_items = len(scores)
    items_fresh = sum(1 for score in scores if score.freshness_grade == "green")
    updated_percentage = round((items_fresh / total_items) * 100)
    freshness_average_days = round(sum(score.freshness_days for score in scores) / total_items, 1)
    avg_richness = round(sum(score.richness_score for score in scores) / total_items)
    leakage_events = sum(score.leakage_events for score in scores)
    workiq_signal_count = sum(score.workiq_signal_count for score in scores)
    leakage_available = workiq_signal_count >= max(1, leakage_signal_threshold)
    leakage_component = round((1.0 - min(max(leakage_events / workiq_signal_count, 0.0), 1.0)) * 100) if workiq_signal_count else 100
    if leakage_available:
        aggregate_score = round((updated_percentage * 0.4) + (avg_richness * 0.3) + (leakage_component * 0.3))
    else:
        aggregate_score = round((updated_percentage * 0.6) + (avg_richness * 0.4))
    return VitalityProgramSnapshot(
        scores=scores,
        workstream_aggregates=workstream_aggregates,
        total_items=total_items,
        items_fresh=items_fresh,
        updated_percentage=updated_percentage,
        freshness_average_days=freshness_average_days,
        avg_richness=avg_richness,
        leakage_events=leakage_events,
        aggregate_score=aggregate_score,
    )


def build_vitality_archive_entry(
    snapshot: VitalityProgramSnapshot,
    *,
    issue_number: int,
    confirmed_at: datetime,
    include_per_owner: bool = False,
    leakage_signal_threshold: int = 5,
) -> VitalityArchiveEntry:
    owner_aggregates = (
        aggregate_vitality(
            snapshot.scores,
            scope_type="owner",
            leakage_signal_threshold=leakage_signal_threshold,
        )
        if include_per_owner
        else ()
    )
    return VitalityArchiveEntry(
        issue_number=issue_number,
        confirmed_at=_ensure_utc(confirmed_at),
        aggregate_score=snapshot.aggregate_score,
        items_total=snapshot.total_items,
        items_fresh=snapshot.items_fresh,
        avg_richness=snapshot.avg_richness,
        leakage_events=snapshot.leakage_events,
        per_workstream={
            aggregate.scope_id: VitalityArchiveWorkstream(
                score=aggregate.composite_score,
                items=aggregate.total_items,
                fresh=aggregate.fresh_items,
            )
            for aggregate in snapshot.workstream_aggregates
        },
        per_owner={
            aggregate.scope_id: VitalityArchiveWorkstream(
                score=aggregate.composite_score,
                items=aggregate.total_items,
                fresh=aggregate.fresh_items,
            )
            for aggregate in owner_aggregates
        },
    )


def parse_vitality_archive_entry(payload: Mapping[str, Any]) -> VitalityArchiveEntry | None:
    try:
        confirmed_at = datetime.fromisoformat(str(payload["confirmed_at"]))
        per_workstream_payload = payload.get("per_workstream", {})
        if not isinstance(per_workstream_payload, Mapping):
            per_workstream_payload = {}
        per_owner_payload = payload.get("per_owner", {})
        if not isinstance(per_owner_payload, Mapping):
            per_owner_payload = {}
        return VitalityArchiveEntry(
            issue_number=int(payload["issue_number"]),
            confirmed_at=_ensure_utc(confirmed_at),
            aggregate_score=int(payload["aggregate_score"]),
            items_total=int(payload["items_total"]),
            items_fresh=int(payload["items_fresh"]),
            avg_richness=int(payload["avg_richness"]),
            leakage_events=int(payload["leakage_events"]),
            per_workstream={
                str(workstream_id): VitalityArchiveWorkstream(
                    score=int(entry.get("score", 0)),
                    items=int(entry.get("items", 0)),
                    fresh=int(entry.get("fresh", 0)),
                )
                for workstream_id, entry in per_workstream_payload.items()
                if isinstance(entry, Mapping)
            },
            per_owner={
                str(owner_alias): VitalityArchiveWorkstream(
                    score=int(entry.get("score", 0)),
                    items=int(entry.get("items", 0)),
                    fresh=int(entry.get("fresh", 0)),
                )
                for owner_alias, entry in per_owner_payload.items()
                if isinstance(entry, Mapping)
            },
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_vitality_section(
    snapshot: VitalityProgramSnapshot,
    *,
    current_issue_number: int,
    history_entries: tuple[VitalityArchiveEntry, ...],
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...] = (),
    include_individual_praise: bool,
) -> AdoVitalitySectionData:
    best_documented_score = _best_documented_score(snapshot.scores)
    best_documented_item = next((item for item in items if best_documented_score is not None and item.id == best_documented_score.work_item_id), None)
    best_documented_label, best_documented_detail = _build_best_documented_copy(
        best_documented_score,
        best_documented_item,
        include_individual_praise=include_individual_praise,
    )
    trend_summary = _build_trend_summary(
        current_issue_number=current_issue_number,
        current_score=snapshot.aggregate_score,
        history_entries=history_entries,
    )
    accountability_rows = _build_accountability_rows(snapshot.scores, items, workstreams)
    return AdoVitalitySectionData(
        section_id="ado-vitality",
        title="ADO Vitality This Week",
        items_updated=snapshot.items_fresh,
        items_total=snapshot.total_items,
        updated_percentage=snapshot.updated_percentage,
        freshness_average_days=snapshot.freshness_average_days,
        leakage_events=snapshot.leakage_events,
        best_documented_label=best_documented_label,
        best_documented_detail=best_documented_detail,
        trend_summary=trend_summary,
        accountability_rows=accountability_rows,
    )


def _build_accountability_rows(
    scores: tuple[VitalityScore, ...],
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
) -> tuple[AdoVitalityAccountabilityRow, ...]:
    items_by_id = {item.id: item for item in items}
    workstreams_by_id = {workstream.id: workstream for workstream in workstreams}
    rows: list[AdoVitalityAccountabilityRow] = []
    for score in sorted(scores, key=_accountability_sort_key):
        fields_to_update = _fields_to_update(score)
        if not fields_to_update:
            continue
        item = items_by_id.get(score.work_item_id)
        if item is None:
            continue
        workstream = workstreams_by_id.get(score.workstream_id or "")
        if workstream is None:
            continue
        rows.append(
            AdoVitalityAccountabilityRow(
                workstream=workstream.name,
                owners_and_assignee=_owners_and_assignee_label(workstream, item),
                ado_label=f"ADO#{item.id}",
                ado_title=item.title,
                fields_to_update=fields_to_update,
            )
        )
    return tuple(rows)


def _accountability_sort_key(score: VitalityScore) -> tuple[int, int, int, int]:
    stale_rank = 0 if score.freshness_grade == "red" else 1
    return (stale_rank, -score.freshness_days, -len(score.richness_missing), score.work_item_id)


def _fields_to_update(score: VitalityScore) -> tuple[str, ...]:
    fields: list[str] = []
    if score.freshness_grade == "red":
        fields.append(f"Meaningful ADO update ({score.freshness_days}d stale)")
    for field_name in score.richness_missing:
        label = _VITALITY_FIELD_LABELS.get(field_name)
        if label is not None and label not in fields:
            fields.append(label)
    if not fields and score.suggested_update:
        fields.append(score.suggested_update)
    return tuple(fields)


def _owners_and_assignee_label(workstream: Workstream | None, item: WorkItem) -> str:
    workstream_owner = _workstream_owner_label(workstream)
    assignee = item.assigned_to or item.assigned_to_email or "Unassigned"
    if workstream_owner:
        return f"WS owner: {workstream_owner} | ADO assignee: {assignee}"
    return f"ADO assignee: {assignee}"


def _workstream_owner_label(workstream: Workstream | None) -> str | None:
    if workstream is None:
        return None
    for value in (workstream.pm_owner, workstream.eng_owner, workstream.alternate_owner, workstream.dri_email):
        if value is not None and value.strip():
            return value.strip()
    return None


_VITALITY_FIELD_LABELS = {
    "target_date": "Target date",
    "recent_comment": "Owner update or comment",
    "risk_assessment": "Risk assessment or tags",
    "description": "Description",
    "blocker_clarity": "Blocker clarity",
    "next_step": "Next step",
}


def _build_best_documented_copy(
    score: VitalityScore | None,
    item: WorkItem | None,
    *,
    include_individual_praise: bool,
) -> tuple[str | None, str | None]:
    if score is None or item is None:
        return None, None
    touch_count = _recent_touch_count(item)
    detail = f"{touch_count} ADO {'touch' if touch_count == 1 else 'touches'} in the last 7 days"
    if include_individual_praise and item.assigned_to:
        detail = f"{detail} by {item.assigned_to}"
    return f"WI:{item.id}", detail


def _best_documented_score(scores: tuple[VitalityScore, ...]) -> VitalityScore | None:
    if not scores:
        return None
    return max(
        scores,
        key=lambda score: (
            score.richness_score,
            score.freshness_grade == "green",
            -score.freshness_days,
            -score.workiq_signal_count,
            -score.work_item_id,
        ),
    )


def _recent_touch_count(item: WorkItem, *, days: int = 7) -> int:
    timestamps: list[datetime] = []
    timestamps.extend(revision.changed_date for revision in item.revisions)
    timestamps.extend(comment.created_date for comment in item.comments)
    if not timestamps:
        return 0
    latest = max(_ensure_utc(timestamp) for timestamp in timestamps)
    cutoff = latest - timedelta(days=days)
    return sum(1 for timestamp in timestamps if _ensure_utc(timestamp) >= cutoff)


def _build_trend_summary(
    *,
    current_issue_number: int,
    current_score: int,
    history_entries: tuple[VitalityArchiveEntry, ...],
) -> str:
    recent_entries = tuple(
        sorted(
            (entry for entry in history_entries if entry.issue_number < current_issue_number),
            key=lambda entry: entry.issue_number,
        )
    )
    scores = [entry.aggregate_score for entry in recent_entries[-3:]]
    scores.append(current_score)
    trend_line = " -> ".join(f"{score}%" for score in scores)
    if len(scores) <= 1:
        return f"{trend_line} (baseline issue)"
    first_score = scores[0]
    last_score = scores[-1]
    if last_score > first_score:
        direction = "improving"
    elif last_score < first_score:
        direction = "worsening"
    else:
        direction = "stable"
    return f"{trend_line} ({direction} over {len(scores)} issues)"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)