from __future__ import annotations

from collections import deque
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any

import yaml

from src.core.archive_store import read_archive_index
from src.core.exceptions import ConfigError
from src.core.journal import PROGRAMS_ROOT
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Dependency, LegacyDependency, Milestone, MilestoneAssessment, MilestoneStatus, TrajectoryPoint
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES

_CLOSED_STATES = TERMINAL_WORK_ITEM_STATES - {"removed", "cut"}


def get_milestones_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "milestones.yaml"


def load_milestones(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[Milestone, ...]:
    """Load milestones from programs/<prog>/milestones.yaml."""

    path = get_milestones_path(program_id, programs_root)
    if not path.exists():
        return ()

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}.") from exc

    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str) or schema_version.split(".", 1)[0] != "1":
        raise ConfigError(f"Unsupported milestones schema_version {schema_version!r} in {path}.")

    raw_milestones = document.get("milestones") or ()
    if not isinstance(raw_milestones, list):
        raise ConfigError(f"Expected 'milestones' list in {path}.")

    milestones: list[Milestone] = []
    for index, raw_milestone in enumerate(raw_milestones, start=1):
        if not isinstance(raw_milestone, dict):
            raise ConfigError(f"Milestone entry #{index} in {path} must be a mapping.")
        try:
            milestones.append(_parse_milestone(program_id, raw_milestone))
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid milestone entry #{index} in {path}: {exc}") from exc
    return tuple(milestones)


def save_milestones(
    program_id: str,
    milestones: tuple[Milestone, ...],
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    path = get_milestones_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))

    document = {
        "schema_version": "1.0",
        "milestones": [_milestone_to_record(milestone) for milestone in milestones],
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    os.replace(temp_path, path)
    _sync_milestone_facts(program_id, milestones, programs_root=programs_root)


def _sync_milestone_facts(
    program_id: str,
    milestones: tuple[Milestone, ...],
    *,
    programs_root: Path,
) -> None:
    from src.core.program_fact_store import (
        FactLifecycleState,
        FactPrecedence,
        ProgramFactInput,
        ProgramFactStore,
        build_natural_key,
    )

    store = ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root))
    sync_time = datetime.now(timezone.utc)
    active_snapshot = store.snapshot(as_of=sync_time)
    active_milestones = {
        fact.natural_key: fact
        for fact in active_snapshot.facts
        if fact.fact_type == "milestone.entry" and fact.lifecycle_state == FactLifecycleState.ACTIVE
    }
    current_natural_keys: set[str] = set()

    for milestone in milestones:
        entity_refs = (f"MILESTONE:{milestone.id}",)
        natural_key = build_natural_key("milestone.entry", entity_refs=entity_refs, scope="program")
        current_natural_keys.add(natural_key)
        store.append_fact(
            ProgramFactInput(
                fact_type="milestone.entry",
                scope="program",
                entity_refs=entity_refs,
                payload=_milestone_fact_payload(milestone),
                precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
                natural_key=natural_key,
                created_by="vertex.milestone_engine",
            ),
            recorded_at=sync_time,
        )

    for natural_key, fact in active_milestones.items():
        if natural_key in current_natural_keys:
            continue
        store.append_fact(
            ProgramFactInput(
                fact_type="milestone.entry",
                scope=fact.scope,
                entity_refs=fact.entity_refs,
                payload=fact.payload,
                source_signal_ids=fact.source_signal_ids,
                confidence=fact.confidence,
                precedence=fact.precedence,
                review_state=fact.review_state,
                lifecycle_state=FactLifecycleState.CLOSED,
                valid_from=fact.valid_from,
                valid_until=sync_time,
                projection_history=fact.projection_history,
                natural_key=natural_key,
                created_by="vertex.milestone_engine",
                privacy_classification=fact.privacy_classification,
                accepted_by=fact.accepted_by,
            ),
            recorded_at=sync_time,
        )


def _resolve_fact_db_root(programs_root: Path) -> Path | None:
    if programs_root == PROGRAMS_ROOT:
        return None
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


def _milestone_fact_payload(milestone: Milestone) -> dict[str, object]:
    return {
        "id": milestone.id,
        "program_id": milestone.program_id,
        "name": milestone.name,
        "target_date": milestone.target_date.isoformat(),
        "owner_alias": milestone.owner_alias,
        "status": milestone.status.value,
        "exit_criteria": list(milestone.exit_criteria),
        "linked_workstream_ids": list(milestone.linked_workstream_ids),
        "linked_work_item_ids": list(milestone.linked_work_item_ids),
        "notes": milestone.notes,
        "last_reviewed_date": milestone.last_reviewed_date.isoformat() if milestone.last_reviewed_date is not None else None,
    }


def assess_milestone_health(
    milestone: Milestone,
    items: tuple[WorkItem, ...],
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    as_of: datetime,
) -> MilestoneAssessment:
    """Compare declared status against the linked item and trajectory state."""

    item_map = {item.id: item for item in items}
    linked_items = tuple(item_map[item_id] for item_id in milestone.linked_work_item_ids if item_id in item_map)
    missing_item_ids = tuple(item_id for item_id in milestone.linked_work_item_ids if item_id not in item_map)
    unresolved_items = tuple(item for item in linked_items if not _is_closed_state(item.state))
    latest_trajectories = {
        item_id: history[-1]
        for item_id, history in trajectories.items()
        if history
    }

    blocked_criteria: list[str] = []
    for item_id in missing_item_ids:
        blocked_criteria.append(f"Linked work item #{item_id} was not found in the current item set.")
    for item in unresolved_items:
        latest_point = latest_trajectories.get(item.id)
        latest_target_date = latest_point.target_date if latest_point is not None else item.target_date
        if item.target_date is not None and item.target_date > milestone.target_date:
            blocked_criteria.append(
                f"Linked work item #{item.id} extends beyond milestone target ({item.target_date.isoformat()})."
            )
            continue
        if latest_target_date is not None and latest_target_date > milestone.target_date:
            blocked_criteria.append(
                f"Linked work item #{item.id} trajectory now points past milestone target ({latest_target_date.isoformat()})."
            )
            continue
        if item.target_date is not None and item.target_date < as_of.date():
            blocked_criteria.append(f"Linked work item #{item.id} is overdue against its own target date.")
            continue
        if item.risk_level.value == "high":
            blocked_criteria.append(f"Linked work item #{item.id} is currently high risk.")

    slip_count = sum(_count_target_date_slips(trajectories.get(item.id, ())) for item in linked_items)
    days_to_target = (milestone.target_date - as_of.date()).days

    if milestone.status == MilestoneStatus.DEFERRED:
        computed_health = MilestoneStatus.DEFERRED
    elif milestone.linked_work_item_ids and not unresolved_items and not missing_item_ids:
        computed_health = MilestoneStatus.COMPLETED
    elif milestone.target_date < as_of.date() and (unresolved_items or missing_item_ids):
        computed_health = MilestoneStatus.MISSED
    elif blocked_criteria or (days_to_target <= 14 and unresolved_items) or slip_count >= 2:
        computed_health = MilestoneStatus.AT_RISK
    else:
        computed_health = MilestoneStatus.ON_TRACK

    if computed_health == MilestoneStatus.COMPLETED:
        slip_probability = 0.0
    elif computed_health == MilestoneStatus.MISSED:
        slip_probability = 1.0
    elif computed_health == MilestoneStatus.DEFERRED:
        slip_probability = 0.0
    else:
        slip_probability = 0.15
        if unresolved_items and days_to_target <= 14:
            slip_probability += 0.25
        if blocked_criteria:
            slip_probability += min(0.35, 0.15 * len(blocked_criteria))
        if slip_count:
            slip_probability += min(0.25, 0.1 * slip_count)
        slip_probability = min(0.95, round(slip_probability, 2))

    confidence = _resolve_confidence(linked_items, trajectories)
    completion_date = resolve_milestone_completion_date(milestone, items, trajectories, as_of)
    reasoning = _build_reasoning(
        milestone=milestone,
        linked_item_count=len(linked_items),
        unresolved_count=len(unresolved_items) + len(missing_item_ids),
        blocked_count=len(blocked_criteria),
        slip_count=slip_count,
        days_to_target=days_to_target,
        computed_health=computed_health,
    )

    return MilestoneAssessment(
        milestone_id=milestone.id,
        computed_health=computed_health,
        blocked_criteria=tuple(blocked_criteria),
        slip_probability=slip_probability,
        critical_path=False,
        confidence=confidence,
        reasoning=reasoning,
        completion_date=completion_date,
    )


def build_critical_path(
    milestones: tuple[Milestone, ...],
    dependencies: tuple[Dependency | LegacyDependency, ...],
) -> tuple[Milestone, ...]:
    """Return the longest milestone dependency chain using topological ordering."""

    if not milestones:
        return ()

    milestone_map = {milestone.id: milestone for milestone in milestones}
    adjacency: dict[str, set[str]] = {milestone.id: set() for milestone in milestones}
    indegree = {milestone.id: 0 for milestone in milestones}

    for dependency in dependencies:
        from_id, to_id = _milestone_dependency_ids(dependency)
        if from_id is None or to_id is None:
            continue
        if from_id not in milestone_map or to_id not in milestone_map:
            continue
        if to_id in adjacency[from_id]:
            continue
        adjacency[from_id].add(to_id)
        indegree[to_id] += 1

    queue = deque(sorted((node for node, degree in indegree.items() if degree == 0), key=lambda milestone_id: milestone_map[milestone_id].target_date))
    topo_order: list[str] = []
    while queue:
        current = queue.popleft()
        topo_order.append(current)
        for successor in sorted(adjacency[current], key=lambda milestone_id: milestone_map[milestone_id].target_date):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)

    if len(topo_order) != len(milestones):
        raise ConfigError("Milestone dependency cycle detected.")

    path_length = {milestone.id: 1 for milestone in milestones}
    predecessor: dict[str, str | None] = {milestone.id: None for milestone in milestones}
    for current in topo_order:
        for successor in adjacency[current]:
            candidate_length = path_length[current] + 1
            if candidate_length > path_length[successor]:
                path_length[successor] = candidate_length
                predecessor[successor] = current

    end_milestone_id = max(
        topo_order,
        key=lambda milestone_id: (path_length[milestone_id], milestone_map[milestone_id].target_date),
    )
    critical_path_ids: list[str] = []
    cursor: str | None = end_milestone_id
    while cursor is not None:
        critical_path_ids.append(cursor)
        cursor = predecessor[cursor]
    critical_path_ids.reverse()
    return tuple(milestone_map[milestone_id] for milestone_id in critical_path_ids)


def _milestone_dependency_ids(
    dependency: Dependency | LegacyDependency,
) -> tuple[str | None, str | None]:
    if isinstance(dependency, Dependency):
        return dependency.from_milestone_id, dependency.to_milestone_id
    return dependency.from_item, dependency.to_item


def detect_milestone_drift(
    milestone: Milestone,
    archive_history: tuple[dict[str, Any], ...],
) -> tuple[date, ...]:
    """Return the milestone target-date sequence captured in archive history."""

    target_dates: list[date] = []
    for entry in archive_history:
        raw_target_date = _extract_milestone_target_date(entry, milestone.id)
        if raw_target_date is None:
            continue
        target_date = date.fromisoformat(raw_target_date)
        if not target_dates or target_dates[-1] != target_date:
            target_dates.append(target_date)
    return tuple(target_dates)


def load_milestone_target_date_history_map(
    program_id: str,
    milestones: tuple[Milestone, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, tuple[str, ...]]:
    archive_root = programs_root / program_id / "archive"
    if not archive_root.exists():
        return {
            milestone.id: (milestone.target_date.isoformat(),)
            for milestone in milestones
        }

    archive_history = _load_confirmed_milestone_archive_history(archive_root)
    if not archive_history:
        return {
            milestone.id: (milestone.target_date.isoformat(),)
            for milestone in milestones
        }

    history_by_milestone: dict[str, tuple[str, ...]] = {}
    for milestone in milestones:
        history = list(detect_milestone_drift(milestone, archive_history))
        if not history or history[-1] != milestone.target_date:
            history.append(milestone.target_date)
        history_by_milestone[milestone.id] = tuple(entry.isoformat() for entry in history)
    return history_by_milestone


def summarize_milestone_target_date_history(history: tuple[str, ...], *, prefix: str = "Target history") -> str | None:
    if len(history) < 2:
        return None
    return f"{prefix} {' -> '.join(history)}"


def detect_milestone_completion_history(
    milestone: Milestone,
    archive_history: tuple[dict[str, Any], ...],
) -> tuple[date, ...]:
    """Return the milestone completion-date sequence captured in archive history."""

    completion_dates: list[date] = []
    for entry in archive_history:
        raw_completion_date = _extract_milestone_completion_date(entry, milestone.id)
        if raw_completion_date is None:
            continue
        completion_date = date.fromisoformat(raw_completion_date)
        if not completion_dates or completion_dates[-1] != completion_date:
            completion_dates.append(completion_date)
    return tuple(completion_dates)


def load_milestone_completion_date_history_map(
    program_id: str,
    milestones: tuple[Milestone, ...],
    *,
    current_completion_dates: dict[str, date | None] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, tuple[str, ...]]:
    archive_root = programs_root / program_id / "archive"
    if not archive_root.exists():
        return {}

    archive_history = _load_confirmed_milestone_archive_history(archive_root)
    if not archive_history:
        return {}

    resolved_current_dates = current_completion_dates or {}
    history_by_milestone: dict[str, tuple[str, ...]] = {}
    for milestone in milestones:
        history = list(detect_milestone_completion_history(milestone, archive_history))
        current_completion_date = resolved_current_dates.get(milestone.id)
        if current_completion_date is not None and (not history or history[-1] != current_completion_date):
            history.append(current_completion_date)
        if history:
            history_by_milestone[milestone.id] = tuple(entry.isoformat() for entry in history)
    return history_by_milestone


def summarize_milestone_completion_date_history(history: tuple[str, ...], *, prefix: str = "Completion history") -> str | None:
    if len(history) < 2:
        return None
    return f"{prefix} {' -> '.join(history)}"


def describe_milestone_schedule_variance(
    milestone: Milestone,
    items: tuple[WorkItem, ...],
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    as_of: datetime,
) -> str | None:
    """Summarize milestone schedule variance from linked-item progress."""

    item_map = {item.id: item for item in items}
    linked_items = tuple(item_map[item_id] for item_id in milestone.linked_work_item_ids if item_id in item_map)
    if not linked_items:
        return None

    completion_date, used_snapshot_fallback = _resolve_milestone_completion_outcome(
        milestone,
        items,
        trajectories,
        as_of,
    )
    if completion_date is not None:
        completion_prefix = "Complete by" if used_snapshot_fallback else "Completed"
        return _format_schedule_variance_summary(milestone.target_date, completion_date, prefix=completion_prefix)

    tracking_dates: list[date] = []
    for item in linked_items:
        history = trajectories.get(item.id, ())
        tracking_date = _resolve_latest_target_date(item, history)
        if tracking_date is not None and tracking_date > milestone.target_date:
            tracking_dates.append(tracking_date)

    if tracking_dates:
        return _format_schedule_variance_summary(milestone.target_date, max(tracking_dates), prefix="Tracking")
    return None


def resolve_milestone_completion_date(
    milestone: Milestone,
    items: tuple[WorkItem, ...],
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    as_of: datetime,
) -> date | None:
    completion_date, _ = _resolve_milestone_completion_outcome(milestone, items, trajectories, as_of)
    return completion_date


def _parse_milestone(program_id: str, raw_milestone: dict[str, Any]) -> Milestone:
    raw_program_id = _optional_string(raw_milestone.get("program_id"), field_name="program_id") or program_id
    exit_criteria = _parse_string_tuple(raw_milestone.get("exit_criteria"), field_name="exit_criteria")
    linked_workstream_ids = _parse_string_tuple(raw_milestone.get("linked_workstream_ids"), field_name="linked_workstream_ids")
    linked_work_item_ids = _parse_int_tuple(raw_milestone.get("linked_work_item_ids"), field_name="linked_work_item_ids")
    return Milestone(
        id=_required_string(raw_milestone.get("id"), field_name="id").strip(),
        program_id=raw_program_id,
        name=_required_string(raw_milestone.get("name"), field_name="name").strip(),
        target_date=_parse_required_date(raw_milestone.get("target_date"), field_name="target_date"),
        owner_alias=_required_string(raw_milestone.get("owner_alias"), field_name="owner_alias").strip(),
        status=MilestoneStatus.from_string(_required_string(raw_milestone.get("status"), field_name="status")),
        exit_criteria=exit_criteria,
        linked_workstream_ids=linked_workstream_ids,
        linked_work_item_ids=linked_work_item_ids,
        notes=_optional_string(raw_milestone.get("notes"), field_name="notes"),
        last_reviewed_date=_parse_optional_date(raw_milestone.get("last_reviewed_date"), field_name="last_reviewed_date"),
    )


def _milestone_to_record(milestone: Milestone) -> dict[str, Any]:
    return {
        "id": milestone.id,
        "program_id": milestone.program_id,
        "name": milestone.name,
        "target_date": milestone.target_date.isoformat(),
        "owner_alias": milestone.owner_alias,
        "status": milestone.status.value,
        "exit_criteria": list(milestone.exit_criteria),
        "linked_workstream_ids": list(milestone.linked_workstream_ids),
        "linked_work_item_ids": list(milestone.linked_work_item_ids),
        "notes": milestone.notes,
        "last_reviewed_date": milestone.last_reviewed_date.isoformat() if milestone.last_reviewed_date is not None else None,
    }


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_required_date(value: object, *, field_name: str) -> date:
    parsed = _parse_optional_date(value, field_name=field_name)
    if parsed is None:
        raise ValueError(f"missing {field_name}")
    return parsed


def _parse_optional_date(value: object, *, field_name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    raw_value = value.strip()
    if not raw_value:
        return None
    return date.fromisoformat(raw_value)


def _parse_string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    raw_values = value or ()
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of strings")
    parsed: list[str] = []
    for entry in raw_values:
        if not isinstance(entry, str):
            raise TypeError(f"{field_name} must contain strings only")
        text = entry.strip()
        if text:
            parsed.append(text)
    return tuple(parsed)


def _parse_int_tuple(value: object, *, field_name: str) -> tuple[int, ...]:
    raw_values = value or ()
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of integers")
    parsed: list[int] = []
    for entry in raw_values:
        if not isinstance(entry, int) or isinstance(entry, bool):
            raise TypeError(f"{field_name} must contain integers only")
        parsed.append(entry)
    return tuple(parsed)


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def _is_closed_state(state: str) -> bool:
    return state.strip().lower() in _CLOSED_STATES


def _count_target_date_slips(history: tuple[TrajectoryPoint, ...]) -> int:
    slips = 0
    previous_target_date: date | None = None
    for point in sorted(history, key=lambda entry: entry.date):
        if previous_target_date is not None and point.target_date is not None and point.target_date > previous_target_date:
            slips += 1
        if point.target_date is not None:
            previous_target_date = point.target_date
    return slips


def _resolve_completion_date(history: tuple[TrajectoryPoint, ...]) -> date | None:
    for point in sorted(history, key=lambda entry: entry.date):
        if _is_closed_state(point.state):
            return point.date
    return None


def _resolve_milestone_completion_outcome(
    milestone: Milestone,
    items: tuple[WorkItem, ...],
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    as_of: datetime,
) -> tuple[date | None, bool]:
    item_map = {item.id: item for item in items}
    linked_items = tuple(item_map[item_id] for item_id in milestone.linked_work_item_ids if item_id in item_map)
    if not linked_items:
        return None, False

    completion_dates: list[date] = []
    used_snapshot_fallback = False
    for item in linked_items:
        history = trajectories.get(item.id, ())
        completion_date = _resolve_completion_date(history)
        if completion_date is not None:
            completion_dates.append(completion_date)
            continue
        if _is_closed_state(item.state):
            completion_dates.append(as_of.date())
            used_snapshot_fallback = True
            continue
        return None, False
    return max(completion_dates), used_snapshot_fallback


def _resolve_latest_target_date(item: WorkItem, history: tuple[TrajectoryPoint, ...]) -> date | None:
    for point in sorted(history, key=lambda entry: entry.date, reverse=True):
        if point.target_date is not None:
            return point.target_date
    return item.target_date


def _format_schedule_variance_summary(planned_date: date, observed_date: date, *, prefix: str) -> str:
    variance_days = (observed_date - planned_date).days
    if variance_days == 0:
        variance_label = "on target"
    elif variance_days > 0:
        day_label = "day" if variance_days == 1 else "days"
        variance_label = f"{variance_days} {day_label} late vs target"
    else:
        early_days = abs(variance_days)
        day_label = "day" if early_days == 1 else "days"
        variance_label = f"{early_days} {day_label} early vs target"
    return f"{prefix} {observed_date.isoformat()} ({variance_label})"


def _resolve_confidence(
    linked_items: tuple[WorkItem, ...],
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
) -> Confidence:
    if linked_items and all(trajectories.get(item.id) for item in linked_items):
        return Confidence.HIGH
    if linked_items:
        return Confidence.MEDIUM
    return Confidence.LOW


def _build_reasoning(
    *,
    milestone: Milestone,
    linked_item_count: int,
    unresolved_count: int,
    blocked_count: int,
    slip_count: int,
    days_to_target: int,
    computed_health: MilestoneStatus,
) -> str:
    if computed_health == MilestoneStatus.COMPLETED:
        return f"All {linked_item_count} linked work items are complete."
    if computed_health == MilestoneStatus.DEFERRED:
        return "Milestone is author-deferred."
    days_label = f"{days_to_target} days to target" if days_to_target >= 0 else f"{-days_to_target} days past target"
    return (
        f"{linked_item_count} linked items, {unresolved_count} unresolved, {blocked_count} blocked signals, "
        f"{slip_count} target-date slips observed, {days_label}."
    )


def _extract_milestone_target_date(entry: dict[str, Any], milestone_id: str) -> str | None:
    if entry.get("id") == milestone_id or entry.get("milestone_id") == milestone_id:
        raw_target_date = entry.get("target_date")
        return str(raw_target_date) if raw_target_date is not None else None
    raw_milestones = entry.get("milestones")
    if not isinstance(raw_milestones, list):
        return None
    for raw_milestone in raw_milestones:
        if not isinstance(raw_milestone, dict):
            continue
        if raw_milestone.get("id") != milestone_id and raw_milestone.get("milestone_id") != milestone_id:
            continue
        raw_target_date = raw_milestone.get("target_date")
        return str(raw_target_date) if raw_target_date is not None else None
    return None


def _extract_milestone_completion_date(entry: dict[str, Any], milestone_id: str) -> str | None:
    if entry.get("id") == milestone_id or entry.get("milestone_id") == milestone_id:
        raw_completion_date = entry.get("completion_date")
        return str(raw_completion_date) if raw_completion_date is not None else None
    raw_milestones = entry.get("milestones")
    if not isinstance(raw_milestones, list):
        return None
    for raw_milestone in raw_milestones:
        if not isinstance(raw_milestone, dict):
            continue
        if raw_milestone.get("id") != milestone_id and raw_milestone.get("milestone_id") != milestone_id:
            continue
        raw_completion_date = raw_milestone.get("completion_date")
        return str(raw_completion_date) if raw_completion_date is not None else None
    return None


def _load_confirmed_milestone_archive_history(archive_root: Path) -> tuple[dict[str, Any], ...]:
    manifest_history: list[tuple[datetime, dict[str, Any]]] = []
    for edition_dir in sorted(archive_root.iterdir()):
        if not edition_dir.is_dir():
            continue
        index_path = edition_dir / "index.json"
        if not index_path.exists():
            continue
        try:
            index_payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for entry_raw in index_payload.get("issues", []):
            if entry_raw.get("kind") != "confirmed":
                continue
            manifest_path_str = entry_raw.get("manifest_path")
            generated_at_str = entry_raw.get("generated_at")
            if not manifest_path_str or not generated_at_str:
                continue
            try:
                generated_at = datetime.fromisoformat(generated_at_str)
            except ValueError:
                continue
            manifest_path = Path(manifest_path_str)
            manifest_payload = _read_manifest_payload(manifest_path)
            if manifest_payload is None:
                continue
            metadata = manifest_payload.get("metadata")
            if not isinstance(metadata, dict):
                continue
            milestone_assessments = metadata.get("milestone_assessments")
            if not isinstance(milestone_assessments, list):
                continue
            manifest_history.append((generated_at, {"milestones": milestone_assessments}))
    manifest_history.sort(key=lambda entry: entry[0])
    return tuple(payload for _, payload in manifest_history)


def _read_manifest_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    return payload