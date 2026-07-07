from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from html import unescape
import re
from typing import Any

from src.core.models import ChildWorkItem, RiskLevel, WorkItem
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory_analyzer import count_eta_slips


ADO_RISK_ASSESSMENT_FIELD = "Custom.RiskAssessment"
ADO_RISK_ASSESSMENT_COMMENT_FIELD = "Custom.RiskAssessmentComment"
ADO_ANALYTICS_RISK_ASSESSMENT_FIELD = "Custom_RiskAssessment"
ADO_ANALYTICS_HISTORY_FIELDS = (
    "DateValue",
    "WorkItemId",
    "State",
    "TargetDate",
    "TagNames",
    ADO_ANALYTICS_RISK_ASSESSMENT_FIELD,
)
ADO_CHILD_BATCH_FIELDS = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AssignedTo",
    "System.AreaPath",
    "System.IterationPath",
    "Microsoft.VSTS.Scheduling.TargetDate",
    "System.Tags",
    ADO_RISK_ASSESSMENT_FIELD,
    ADO_RISK_ASSESSMENT_COMMENT_FIELD,
)

_RISK_ASSESSMENT_LEVELS = {
    "on track": RiskLevel.LOW,
    "at risk": RiskLevel.MEDIUM,
    "off track": RiskLevel.HIGH,
}

_NEWSLETTER_SAFE_REWRITES = (
    (re.compile(r"\bdue to\b", flags=re.IGNORECASE), "after"),
    (re.compile(r"\bvarious\b", flags=re.IGNORECASE), "multiple"),
)
_RISK_ASSESSMENT_LABELS = {
    "on track": "On Track",
    "at risk": "At Risk",
    "off track": "Off Track",
}
_CHILD_ITEM_TYPES = frozenset({"product backlog item", "task"})
_HIERARCHY_FORWARD_REL = "system.linktypes.hierarchy-forward"
_WORK_ITEM_URL_ID_RE = re.compile(r"/workItems/(?P<id>\d+)$", re.IGNORECASE)


def normalize_risk_assessment(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    normalized = " ".join(text.replace("_", " ").split())
    return _RISK_ASSESSMENT_LABELS.get(normalized.lower(), normalized)


def infer_ado_risk_level(
    state: str,
    tags: list[str] | tuple[str, ...],
    risk_assessment: str | None = None,
) -> RiskLevel:
    normalized_state = state.strip().lower()
    normalized_tags = {tag.strip().lower() for tag in tags}
    normalized_risk = normalize_risk_assessment(risk_assessment)
    if normalized_state in {"closed", "done", "resolved", "completed"}:
        return RiskLevel.DONE
    if "blocked" in normalized_state or "blocked" in normalized_tags:
        return RiskLevel.HIGH
    if normalized_risk is not None:
        mapped = _RISK_ASSESSMENT_LEVELS.get(normalized_risk.lower())
        if mapped is not None:
            return mapped
    if normalized_state in {"off track", "at risk"}:
        return RiskLevel.HIGH if normalized_state == "off track" else RiskLevel.MEDIUM
    return RiskLevel.LOW


def extract_child_ids_by_parent(rows: list[dict[str, Any]]) -> dict[int, tuple[int, ...]]:
    child_ids_by_parent: dict[int, tuple[int, ...]] = {}
    for row in rows:
        parent_id = int(row.get("id") or row.get("fields", {}).get("System.Id") or 0)
        if parent_id <= 0:
            continue
        ordered_child_ids: list[int] = []
        seen_child_ids: set[int] = set()
        for relation in row.get("relations", ()):
            if not isinstance(relation, dict):
                continue
            if str(relation.get("rel") or "").strip().lower() != _HIERARCHY_FORWARD_REL:
                continue
            child_id = _extract_relation_work_item_id(relation)
            if child_id <= 0 or child_id in seen_child_ids:
                continue
            seen_child_ids.add(child_id)
            ordered_child_ids.append(child_id)
        if ordered_child_ids:
            child_ids_by_parent[parent_id] = tuple(ordered_child_ids)
    return child_ids_by_parent


def build_child_work_items(batch_rows: list[dict[str, Any]]) -> tuple[ChildWorkItem, ...]:
    children: list[ChildWorkItem] = []
    for row in batch_rows:
        fields = row.get("fields", {}) if isinstance(row, dict) else {}
        work_item_type = str(fields.get("System.WorkItemType") or "").strip()
        if work_item_type.lower() not in _CHILD_ITEM_TYPES:
            continue
        state = str(fields.get("System.State") or "Active")
        tags = _parse_tags(fields.get("System.Tags") or row.get("Tags"))
        risk_assessment = normalize_risk_assessment(fields.get(ADO_RISK_ASSESSMENT_FIELD))
        assigned_to, assigned_to_email = _parse_identity(fields.get("System.AssignedTo"))
        children.append(
            ChildWorkItem(
                id=int(row.get("id") or fields.get("System.Id") or 0),
                type=work_item_type or "WorkItem",
                title=str(fields.get("System.Title") or row.get("Title") or ""),
                state=state,
                assigned_to=assigned_to,
                assigned_to_email=assigned_to_email,
                area_path=str(fields.get("System.AreaPath") or row.get("AreaPath") or ""),
                iteration_path=str(fields.get("System.IterationPath") or row.get("IterationPath") or ""),
                target_date=_parse_date(fields.get("Microsoft.VSTS.Scheduling.TargetDate") or row.get("TargetDate")),
                risk_level=infer_ado_risk_level(state, tags, risk_assessment),
                tags=tuple(tags),
                risk_assessment=risk_assessment,
                risk_assessment_comment=_optional_string(fields.get(ADO_RISK_ASSESSMENT_COMMENT_FIELD)),
            )
        )
    children.sort(key=lambda child: child.id)
    return tuple(children)


def build_analytics_history(
    snapshot_rows: list[dict[str, Any]],
    current_items: Mapping[int, WorkItem],
) -> dict[int, tuple[TrajectoryPoint, ...]]:
    history: dict[int, list[TrajectoryPoint]] = {}
    seen_keys: set[tuple[int, str, str | None, str | None, str | None]] = set()
    for row in snapshot_rows:
        work_item_id = int(row.get("WorkItemId") or 0)
        current_item = current_items.get(work_item_id)
        if work_item_id <= 0 or current_item is None:
            continue
        point_date = _parse_date(row.get("DateValue") or row.get("DateSK"))
        if point_date is None:
            continue
        risk_assessment = normalize_risk_assessment(
            row.get(ADO_ANALYTICS_RISK_ASSESSMENT_FIELD)
            or row.get(ADO_RISK_ASSESSMENT_FIELD)
            or current_item.risk_assessment
        )
        tags = tuple(_parse_tags(row.get("TagNames") or row.get("System.Tags") or row.get("Tags") or current_item.tags))
        point = TrajectoryPoint(
            date=point_date,
            state=str(row.get("State") or current_item.state or "Active"),
            assigned_to=_parse_identity(row.get("AssignedTo"))[1] or _parse_identity(row.get("AssignedTo"))[0] or current_item.assigned_to_email or current_item.assigned_to,
            target_date=_parse_date(row.get("TargetDate")) or current_item.target_date,
            risk_level=infer_ado_risk_level(
                str(row.get("State") or current_item.state or "Active"),
                list(tags),
                risk_assessment,
            ),
            area_path=str(row.get("AreaPath") or row.get("Area", {}).get("AreaPath") or current_item.area_path),
            tags=tags,
            risk_assessment=risk_assessment,
            risk_assessment_comment=current_item.risk_assessment_comment,
        )
        identity = (
            work_item_id,
            point.date.isoformat(),
            point.state,
            point.target_date.isoformat() if point.target_date is not None else None,
            point.risk_assessment,
        )
        if identity in seen_keys:
            continue
        seen_keys.add(identity)
        history.setdefault(work_item_id, []).append(point)
    return {
        work_item_id: tuple(sorted(points, key=lambda point: point.date))
        for work_item_id, points in history.items()
    }


def serialize_trajectory_points(points: tuple[TrajectoryPoint, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "date": point.date.isoformat(),
            "state": point.state,
            "assigned_to": point.assigned_to,
            "target_date": point.target_date.isoformat() if point.target_date is not None else None,
            "risk_level": point.risk_level.value if point.risk_level is not None else None,
            "area_path": point.area_path,
            "tags": list(point.tags),
            "risk_assessment": point.risk_assessment,
            "risk_assessment_comment": point.risk_assessment_comment,
        }
        for point in points
    )


def deserialize_trajectory_points(payload: Any) -> tuple[TrajectoryPoint, ...]:
    if not isinstance(payload, list):
        return ()
    points: list[TrajectoryPoint] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        point_date = _parse_date(entry.get("date"))
        if point_date is None:
            continue
        raw_risk_level = entry.get("risk_level")
        points.append(
            TrajectoryPoint(
                date=point_date,
                state=str(entry.get("state") or "Active"),
                assigned_to=_optional_string(entry.get("assigned_to")),
                target_date=_parse_date(entry.get("target_date")),
                risk_level=RiskLevel.from_string(str(raw_risk_level)) if raw_risk_level is not None else None,
                area_path=str(entry.get("area_path") or ""),
                tags=tuple(str(tag) for tag in entry.get("tags", [])),
                risk_assessment=normalize_risk_assessment(entry.get("risk_assessment")),
                risk_assessment_comment=_optional_string(entry.get("risk_assessment_comment")),
            )
        )
    return tuple(sorted(points, key=lambda point: point.date))


def merge_trajectory_points(*point_sets: tuple[TrajectoryPoint, ...]) -> tuple[TrajectoryPoint, ...]:
    merged: list[TrajectoryPoint] = []
    seen: set[tuple[object, ...]] = set()
    for point_set in point_sets:
        for point in sorted(point_set, key=lambda entry: entry.date):
            identity = (
                point.date.isoformat(),
                point.state,
                point.assigned_to,
                point.target_date.isoformat() if point.target_date is not None else None,
                point.risk_level.value if point.risk_level is not None else None,
                point.area_path,
                point.tags,
                point.risk_assessment,
                point.risk_assessment_comment,
            )
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(point)
    merged.sort(key=lambda point: point.date)
    return tuple(merged)


def build_significant_findings(
    item: WorkItem,
    trajectory_points: tuple[TrajectoryPoint, ...],
    *,
    as_of: date | None = None,
    window_days: int = 90,
) -> tuple[str, ...]:
    reference_date = as_of or datetime.now(timezone.utc).date()
    findings: list[str] = []

    if item.risk_assessment is not None:
        risk_comment = _sanitize_risk_assessment_comment(item.risk_assessment_comment)
        if risk_comment:
            findings.append(
                f"Risk assessment {item.risk_assessment}: {risk_comment}"
            )
        elif item.risk_assessment.lower() != "on track":
            findings.append(f"Risk assessment {item.risk_assessment}.")

    if item.child_items:
        child_summaries: list[str] = []
        for child in item.child_items[:3]:
            detail = f"{child.type} ADO#{child.id} - {_sanitize_newsletter_text(child.title)}"
            annotations: list[str] = []
            if child.target_date is not None and child.state.strip().lower() not in {"closed", "done", "resolved", "completed"}:
                annotations.append(f"ETA {child.target_date.isoformat()}")
            if child.risk_assessment and child.risk_assessment.lower() != "on track":
                annotations.append(child.risk_assessment)
            if annotations:
                detail = f"{detail} ({'; '.join(annotations)})"
            child_summaries.append(detail)
        if child_summaries:
            findings.append("Linked work: " + "; ".join(child_summaries) + ".")

    if trajectory_points:
        slip_count = count_eta_slips(trajectory_points, window_days=window_days, as_of=reference_date)
        total_slip_days = _total_slip_days(trajectory_points, window_days=window_days, as_of=reference_date)
        latest_target_date = trajectory_points[-1].target_date
        if slip_count > 0:
            findings.append(
                f"Target slipped {slip_count} time{'s' if slip_count != 1 else ''} in the last {window_days} days"
                + (f" (+{total_slip_days}d total)" if total_slip_days > 0 else "")
                + (f"; current target {latest_target_date.isoformat()}." if latest_target_date is not None else "; current target unset.")
            )
        elif _target_date_removed(trajectory_points, window_days=window_days, as_of=reference_date):
            findings.append(f"Target date was removed in the last {window_days} days.")

    return tuple(findings)


def _extract_relation_work_item_id(relation: dict[str, Any]) -> int:
    url = _optional_string(relation.get("url"))
    if url is None:
        return 0
    match = _WORK_ITEM_URL_ID_RE.search(url)
    return int(match.group("id")) if match is not None else 0


def _parse_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [tag.strip() for tag in re.split(r"[;,]", value) if tag.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _parse_identity(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        display_name = value.get("displayName") or value.get("name") or value.get("UserName")
        email = value.get("uniqueName") or value.get("mailAddress") or value.get("userPrincipalName")
        return (_optional_string(display_name), _optional_string(email))
    if isinstance(value, str):
        return (value.strip() or None, None)
    return (None, None)


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _collapse_whitespace(value: str, *, limit: int) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def _sanitize_risk_assessment_comment(value: str | None) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _collapse_whitespace(text, limit=180)
    return text or None


def _sanitize_newsletter_text(value: str) -> str:
    sanitized = value
    for pattern, replacement in _NEWSLETTER_SAFE_REWRITES:
        sanitized = pattern.sub(lambda match: _match_case(match.group(0), replacement), sanitized)
    return sanitized


def _match_case(source: str, replacement: str) -> str:
    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _total_slip_days(
    points: tuple[TrajectoryPoint, ...],
    *,
    window_days: int,
    as_of: date,
) -> int:
    window_start = as_of - timedelta(days=window_days)
    slip_days = 0
    ordered = tuple(sorted(points, key=lambda point: point.date))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.date < window_start:
            continue
        if previous.target_date is None or current.target_date is None:
            continue
        if current.target_date > previous.target_date:
            slip_days += (current.target_date - previous.target_date).days
    return slip_days


def _target_date_removed(
    points: tuple[TrajectoryPoint, ...],
    *,
    window_days: int,
    as_of: date,
) -> bool:
    window_start = as_of - timedelta(days=window_days)
    ordered = tuple(sorted(points, key=lambda point: point.date))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.date < window_start:
            continue
        if previous.target_date is not None and current.target_date is None:
            return True
    return False