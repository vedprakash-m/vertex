from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Callable, Literal

from src.core.ado_semantics import alias_from_identity, is_meaningful_owner_comment, is_vertex_generated_comment, item_owner_alias, latest_meaningful_ado_update
from src.core.leakage_detector import LeakageReport
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import VitalityAggregate, VitalityScore
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


_INITIAL_STATES = {"new", "proposed"}
_ACTION_VERB_RE = re.compile(r"\b(update|ship|resolve|mitigate|review|complete|confirm|prepare|follow up|sync|investigate)\b", re.IGNORECASE)
_DATE_TOKEN_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.IGNORECASE)
_OWNER_TOKEN_RE = re.compile(r"\b(owner|dri|team|@)\b", re.IGNORECASE)
_BLOCKER_TOKEN_RE = re.compile(r"\b(blocked|blocker|dependency|waiting|ask|need)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class VitalitySummary:
    total_items: int
    updated_this_week: int
    updated_this_week_percentage: int
    freshness_average_days: float
    stale_owner_aliases: tuple[str, ...]


def score_vitality(
    items: tuple[WorkItem, ...],
    *,
    as_of: datetime,
    workstream_resolver: Callable[[WorkItem], str | None] | None = None,
    leakage: LeakageReport | None = None,
    leakage_signal_threshold: int = 5,
) -> tuple[VitalityScore, ...]:
    leakage_counts_by_item = leakage.leakage_counts_by_item if leakage is not None else {}
    signal_counts_by_item = leakage.signal_counts_by_item if leakage is not None else {}
    leakage_available = _leakage_is_available(
        sum(signal_counts_by_item.values()),
        threshold=leakage_signal_threshold,
    )
    scores: list[VitalityScore] = []
    for item in items:
        if _should_exclude(item, as_of=as_of):
            continue
        owner_alias = item_owner_alias(item)
        workstream_id = workstream_resolver(item) if workstream_resolver is not None else None
        freshness_days = _freshness_days(item, as_of=as_of)
        freshness_grade = _freshness_grade(freshness_days)
        richness_score, richness_missing = _richness_score(item, as_of=as_of, owner_alias=owner_alias)
        freshness_pct = _freshness_percentage(freshness_grade)
        leakage_events = leakage_counts_by_item.get(item.id, 0)
        workiq_signal_count = signal_counts_by_item.get(item.id, 0)
        leakage_pct = _leakage_percentage(leakage_events, workiq_signal_count)
        composite_score = _composite_score(
            freshness_pct,
            richness_score,
            leakage_pct if leakage_available else None,
        )
        scores.append(
            VitalityScore(
                work_item_id=item.id,
                owner_alias=owner_alias,
                workstream_id=workstream_id,
                freshness_days=freshness_days,
                freshness_grade=freshness_grade,
                richness_score=richness_score,
                richness_missing=richness_missing,
                leakage_events=leakage_events,
                workiq_signal_count=workiq_signal_count,
                composite_score=composite_score,
                suggested_update=_suggested_update(richness_missing, freshness_grade),
            )
        )
    return tuple(sorted(scores, key=lambda score: score.work_item_id))


def aggregate_vitality(
    scores: tuple[VitalityScore, ...],
    *,
    scope_type: str,
    leakage_signal_threshold: int = 5,
) -> tuple[VitalityAggregate, ...]:
    leakage_available = _leakage_is_available(
        sum(score.workiq_signal_count for score in scores),
        threshold=leakage_signal_threshold,
    )
    grouped: dict[str, list[VitalityScore]] = {}
    for score in scores:
        scope_id = score.owner_alias if scope_type == "owner" else score.workstream_id
        if scope_id is None:
            continue
        grouped.setdefault(scope_id, []).append(score)

    aggregates: list[VitalityAggregate] = []
    for scope_id, entries in grouped.items():
        fresh_items = sum(1 for entry in entries if entry.freshness_grade == "green")
        avg_richness = round(sum(entry.richness_score for entry in entries) / len(entries), 1)
        freshness_pct = round((fresh_items / len(entries)) * 100)
        total_leakage = sum(entry.leakage_events for entry in entries)
        workiq_signal_count = sum(entry.workiq_signal_count for entry in entries)
        leakage_ratio = round((total_leakage / workiq_signal_count), 2) if workiq_signal_count else 0.0
        leakage_pct = _leakage_percentage(total_leakage, workiq_signal_count)
        composite_score = _composite_score(
            freshness_pct,
            avg_richness,
            leakage_pct if leakage_available else None,
        )
        aggregates.append(
            VitalityAggregate(
                scope_id=scope_id,
                scope_type=("owner" if scope_type == "owner" else "workstream"),
                total_items=len(entries),
                fresh_items=fresh_items,
                avg_richness=avg_richness,
                total_leakage=total_leakage,
                workiq_signal_count=workiq_signal_count,
                leakage_ratio=leakage_ratio,
                composite_score=composite_score,
                trend=None,
            )
        )
    return tuple(sorted(aggregates, key=lambda aggregate: (-aggregate.composite_score, aggregate.scope_id)))


def summarize_vitality(scores: tuple[VitalityScore, ...]) -> VitalitySummary:
    if not scores:
        return VitalitySummary(
            total_items=0,
            updated_this_week=0,
            updated_this_week_percentage=0,
            freshness_average_days=0.0,
            stale_owner_aliases=(),
        )
    updated_this_week = sum(1 for score in scores if score.freshness_grade == "green")
    stale_owner_aliases = tuple(sorted({score.owner_alias for score in scores if score.freshness_grade == "red" and score.owner_alias}))
    return VitalitySummary(
        total_items=len(scores),
        updated_this_week=updated_this_week,
        updated_this_week_percentage=round((updated_this_week / len(scores)) * 100),
        freshness_average_days=round(sum(score.freshness_days for score in scores) / len(scores), 1),
        stale_owner_aliases=stale_owner_aliases,
    )


def _should_exclude(item: WorkItem, *, as_of: datetime) -> bool:
    normalized_state = item.state.strip().lower()
    if normalized_state in TERMINAL_WORK_ITEM_STATES:
        return True
    if normalized_state in _INITIAL_STATES and _item_age_days(item, as_of=as_of) < 7:
        return True
    return False


def _item_age_days(item: WorkItem, *, as_of: datetime) -> int:
    timestamps = [revision.changed_date for revision in item.revisions]
    timestamps.extend(comment.created_date for comment in item.comments)
    changed_date = item.custom_fields.get("changed_date")
    if isinstance(changed_date, str):
        timestamps.append(datetime.fromisoformat(changed_date))
    if not timestamps:
        return 9999
    normalized = [timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc) for timestamp in timestamps]
    return max(0, (as_of.astimezone(timezone.utc) - min(normalized).astimezone(timezone.utc)).days)


def _freshness_days(item: WorkItem, *, as_of: datetime) -> int:
    latest = latest_meaningful_ado_update(item)
    if latest is None:
        return 9999
    return max(0, (as_of.astimezone(timezone.utc) - latest.astimezone(timezone.utc)).days)


def _freshness_grade(days: int) -> Literal["green", "amber", "red"]:
    if days < 7:
        return "green"
    if days <= 14:
        return "amber"
    return "red"


def _freshness_percentage(grade: Literal["green", "amber", "red"]) -> int:
    if grade == "green":
        return 100
    if grade == "amber":
        return 60
    return 20


def _leakage_percentage(leakage_events: int, workiq_signal_count: int) -> int:
    if workiq_signal_count <= 0:
        return 100
    leakage_ratio = min(max(leakage_events / workiq_signal_count, 0.0), 1.0)
    return round((1.0 - leakage_ratio) * 100)


def _composite_score(freshness_pct: int, richness_score: float, leakage_pct: int | None) -> int:
    if leakage_pct is None:
        return round((freshness_pct * 0.6) + (richness_score * 0.4))
    return round((freshness_pct * 0.4) + (richness_score * 0.3) + (leakage_pct * 0.3))


def _leakage_is_available(total_workiq_signals: int, *, threshold: int) -> bool:
    return total_workiq_signals >= max(1, threshold)


def _richness_score(item: WorkItem, *, as_of: datetime, owner_alias: str | None) -> tuple[int, tuple[str, ...]]:
    score = 0
    missing: list[str] = []
    if item.target_date is not None:
        score += 25
    else:
        missing.append("target_date")

    if _has_recent_owner_comment(item, as_of=as_of, owner_alias=owner_alias):
        score += 25
    else:
        missing.append("recent_comment")

    if item.risk_level != RiskLevel.UNKNOWN or bool(item.tags):
        score += 15
    else:
        missing.append("risk_assessment")

    description = _description_text(item)
    if description is not None and len(description) > 50 and description.casefold() != item.title.strip().casefold():
        score += 15
    else:
        missing.append("description")

    if _needs_blocker_clarity(item):
        if _has_blocker_clarity(item):
            score += 10
        else:
            missing.append("blocker_clarity")
    else:
        score += 10

    if _has_next_step(item):
        score += 10
    else:
        missing.append("next_step")

    return score, tuple(missing)


def _has_recent_owner_comment(item: WorkItem, *, as_of: datetime, owner_alias: str | None) -> bool:
    if owner_alias is None:
        return False
    cutoff = as_of - timedelta(days=14)
    for comment in item.comments:
        created_at = comment.created_date if comment.created_date.tzinfo is not None else comment.created_date.replace(tzinfo=timezone.utc)
        if created_at >= cutoff and is_meaningful_owner_comment(comment, owner_alias):
            return True
    return False


def _description_text(item: WorkItem) -> str | None:
    value = item.custom_fields.get("description") or item.custom_fields.get("System.Description")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _needs_blocker_clarity(item: WorkItem) -> bool:
    normalized_state = item.state.strip().lower()
    return item.risk_level == RiskLevel.HIGH or normalized_state in {"blocked", "at risk"}


def _has_blocker_clarity(item: WorkItem) -> bool:
    text = "\n".join(part for part in (_description_text(item), _latest_comment_text(item)) if part)
    return bool(_BLOCKER_TOKEN_RE.search(text)) and bool(_OWNER_TOKEN_RE.search(text) or _DATE_TOKEN_RE.search(text))


def _has_next_step(item: WorkItem) -> bool:
    text = _latest_comment_text(item) or _description_text(item) or ""
    return bool(_ACTION_VERB_RE.search(text)) and bool(_OWNER_TOKEN_RE.search(text) or _DATE_TOKEN_RE.search(text))


def _latest_comment_text(item: WorkItem) -> str | None:
    meaningful_comments = [comment for comment in item.comments if not is_vertex_generated_comment(comment)]
    if not meaningful_comments:
        return None
    latest = max(meaningful_comments, key=lambda comment: comment.created_date)
    return latest.text.strip() or None


def _owner_alias(item: WorkItem) -> str | None:
    return item_owner_alias(item)


def _alias_from_identity(value: str | None) -> str | None:
    return alias_from_identity(value)


def _suggested_update(richness_missing: tuple[str, ...], freshness_grade: Literal["green", "amber", "red"]) -> str | None:
    if "target_date" in richness_missing:
        return "Update target date"
    if "recent_comment" in richness_missing:
        return "Add an owner comment"
    if "description" in richness_missing:
        return "Expand the work item description"
    if freshness_grade == "red":
        return "Refresh ADO status"
    return None