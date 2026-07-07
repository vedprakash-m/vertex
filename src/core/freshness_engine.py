from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import unescape
import re
from typing import Any, Mapping

from src.core.ado_semantics import is_vertex_generated_comment
from src.core.config_loader import NarrativeProgramContext, ProgramWorkstream
from src.core.models import DRISummary, FreshnessItem, FreshnessReport, NotifiedWorkItemState, PriorNotificationState, Snapshot, WorkItem
from src.core.response_tracker import has_response_since
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


ACTIVE_STATES = {"active", "proposed", "on track", "at risk", "off track", "blocked"}
PLACEHOLDER_MARKERS = ("tbd", "wip", "updating", "to be determined", "no update")
ACTION_VERBS = (
    "mitigate",
    "fix",
    "update",
    "complete",
    "ship",
    "close",
    "investigate",
    "resolve",
    "review",
    "deploy",
    "follow up",
    "work with",
)
DATE_TOKEN_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
NON_ALPHANUMERIC_PATTERN = re.compile(r"[^a-z0-9]+")
OWNER_ON_PTO_NO_ALTERNATE = "Owner on PTO \u2014 no alternate assigned."
_ACTION_LABELS = {
    "FR-20": "Data unavailable",
    "FR-21": "Overdue",
    "FR-22": "Stale",
    "FR-23": "Changed",
    "FR-24": "Changed",
    "FR-25": "New",
    "FR-26": "Hot",
    "FR-26a": "Ghost",
    "FR-42": "Bad Fresh",
    "FR-42a": "Bad Fresh",
    "FR-43": "Approaching",
    "FR-44": "Unmitigated",
    "FR-45": "Non-responder",
    "FR-46": "Unowned",
    "FR-47": "Escalate",
}


def build_freshness_report(
    current_items: tuple[WorkItem, ...] | list[WorkItem],
    issue_number: int,
    as_of: datetime,
    stale_warn_days: int,
    stale_block_days: int,
    previous_snapshot: Snapshot | None = None,
    previous_notification_state: PriorNotificationState | None = None,
    program_context: NarrativeProgramContext | None = None,
    workstream_narrative_history: Mapping[str, tuple[str, ...]] | None = None,
) -> FreshnessReport:
    previous_items = {item.id: item for item in previous_snapshot.items} if previous_snapshot is not None else {}
    previous_snapshot_as_of = previous_snapshot.ado_data_as_of if previous_snapshot is not None else None
    previous_notifications = (
        {item.work_item_id: item for item in previous_notification_state.items}
        if previous_notification_state is not None
        else {}
    )
    findings: list[FreshnessItem] = []

    for item in current_items:
        previous_item = previous_items.get(item.id)
        previous_notification = previous_notifications.get(item.id)
        findings.extend(
            _build_item_findings(
                item,
                previous_item,
                as_of,
                stale_warn_days,
                stale_block_days,
                previous_snapshot_as_of,
                previous_notification,
                program_context,
                workstream_narrative_history,
            )
        )

    sorted_findings = tuple(_attach_action_language(item) for item in sorted(findings, key=_freshness_sort_key))
    return FreshnessReport(
        issue_number=issue_number,
        items=sorted_findings,
        blocks=sum(1 for item in sorted_findings if item.severity == "block"),
        warns=sum(1 for item in sorted_findings if item.severity == "warn"),
        infos=sum(1 for item in sorted_findings if item.severity == "info"),
    )


def _attach_action_language(item: FreshnessItem) -> FreshnessItem:
    return replace(
        item,
        action_label=_ACTION_LABELS.get(item.rule_id, item.rule_id),
        action_message=item.suggested_fix or item.message,
    )


def build_dri_summaries(
    report: FreshnessReport,
    current_items: tuple[WorkItem, ...] | list[WorkItem],
    program_context: NarrativeProgramContext | None = None,
) -> tuple[DRISummary, ...]:
    items_by_id = {item.id: item for item in current_items}
    findings_by_item_id: dict[int, list[FreshnessItem]] = defaultdict(list)
    for finding in report.items:
        findings_by_item_id[finding.work_item_id].append(finding)

    people_by_email = {
        person.email.lower(): person.display_name
        for person in (program_context.people if program_context is not None else ())
        if person.email
    }
    findings_by_dri: dict[str, list[FreshnessItem]] = defaultdict(list)

    for finding in report.items:
        item = items_by_id.get(finding.work_item_id)
        item_findings = tuple(findings_by_item_id.get(finding.work_item_id, ()))
        dri_email, routing_note = _routing_target(item, item_findings, program_context, people_by_email)
        findings_by_dri[dri_email].append(_apply_routing_note(finding, routing_note, item_findings))

    open_items_by_dri: dict[str, set[int]] = defaultdict(set)
    for item in current_items:
        if _is_terminal(item):
            continue
        item_findings = tuple(findings_by_item_id.get(item.id, ()))
        routed_email, _ = _routing_target(item, item_findings, program_context, people_by_email)
        open_items_by_dri[routed_email].add(item.id)

    summaries: list[DRISummary] = []
    for dri_email in sorted(findings_by_dri):
        findings = tuple(sorted(findings_by_dri[dri_email], key=_freshness_sort_key))
        related_items = {finding.work_item_id for finding in findings}
        overdue_ids = {
            finding.work_item_id
            for finding in findings
            if finding.rule_id in {"FR-21", "FR-43"}
        }
        stale_ids = {
            finding.work_item_id
            for finding in findings
            if finding.rule_id == "FR-22"
        }
        dri_name = _summary_owner_name(dri_email, related_items, items_by_id, people_by_email)

        summaries.append(
            DRISummary(
                dri_email=dri_email,
                dri_name=dri_name,
                open_count=len(open_items_by_dri.get(dri_email, set())),
                overdue_count=len(overdue_ids),
                stale_count=len(stale_ids),
                items=findings,
            )
        )

    return tuple(summaries)


def _build_item_findings(
    item: WorkItem,
    previous_item: Any,
    as_of: datetime,
    stale_warn_days: int,
    stale_block_days: int,
    previous_snapshot_as_of: datetime | None,
    previous_notification: NotifiedWorkItemState | None,
    program_context: NarrativeProgramContext | None,
    workstream_narrative_history: Mapping[str, tuple[str, ...]] | None,
) -> list[FreshnessItem]:
    findings: list[FreshnessItem] = []
    today = as_of.date()

    if _is_active(item) and _owner_email(item) == "unassigned":
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-46",
                severity="block",
                message="Active item has no assigned owner.",
                suggested_fix="Assign a DRI before the next gather or publish run.",
            )
        )

    if _has_overdue_eta(item, today):
        days_overdue = (today - item.target_date).days if item.target_date is not None else 0
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-21",
                severity="block",
                message=f"ETA is in the past ({days_overdue} days overdue).",
                suggested_fix="Update the target date or close the item before authoring.",
            )
        )

    if _has_approaching_deadline(item, today):
        business_days = _business_days_until(today, item.target_date)
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-43",
                severity="block",
                message=f"Target date is within {business_days} business day(s).",
                suggested_fix="Confirm the date is still achievable and capture the latest mitigation in ADO.",
            )
        )

    last_relevant_activity = _last_relevant_activity(item)
    if _is_active(item):
        if last_relevant_activity is None:
            findings.append(
                FreshnessItem(
                    work_item_id=item.id,
                    rule_id="FR-20",
                    severity="warn",
                    message="Active item has no usable freshness activity timestamp.",
                    suggested_fix="Capture a valid changed date, state change, or non-Vertex discussion signal before the next publish run.",
                )
            )
        else:
            stale_age_days = (as_of - last_relevant_activity).days
            if stale_age_days >= stale_warn_days:
                threshold_note = (
                    f" Exceeds the stale-block threshold ({stale_block_days} days)."
                    if stale_age_days >= stale_block_days
                    else ""
                )
                findings.append(
                    FreshnessItem(
                        work_item_id=item.id,
                        rule_id="FR-22",
                        severity="warn",
                        message=f"No RiskComment, Discussion, or State activity in {stale_age_days} days.{threshold_note}",
                        suggested_fix="Ask the owner to refresh the item in ADO before the next published update.",
                    )
                )

    if previous_item is not None and previous_item.risk_level != item.risk_level:
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-23",
                severity="warn",
                message=f"Risk changed from {previous_item.risk_level.value} to {item.risk_level.value} since the last confirmed issue.",
                suggested_fix="Confirm the risk narrative and mitigation are still accurate.",
            )
        )

    if previous_item is not None and previous_item.state != item.state:
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-24",
                severity="warn",
                message=f"Status changed from {previous_item.state} to {item.state} since the last confirmed issue.",
                suggested_fix="Review whether the published summary still reflects the latest status.",
            )
        )

    if previous_item is None:
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-25",
                severity="info",
                message="New item appeared in scope since the last confirmed issue.",
                suggested_fix="Confirm it belongs in the upcoming issue and has an owner.",
            )
        )

    if _recent_activity_count(item, as_of) >= 3:
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-26",
                severity="warn",
                message="High recent activity detected (3+ comments or state changes in the last 7 days).",
                suggested_fix="Verify the item summary captures the latest situation before publishing.",
            )
        )

    if _has_ghost_change(item, previous_item, program_context, workstream_narrative_history):
        matched_workstream = _matching_workstream(item, program_context)
        workstream_name = matched_workstream.name if matched_workstream is not None else "the"
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-26a",
                severity="warn",
                message=(
                    f"State or target date changed since the last confirmed issue, but the {workstream_name} workstream blurb is unchanged across the last 3 issues."
                    if workstream_name != "the"
                    else "State or target date changed since the last confirmed issue, but the workstream blurb is unchanged across the last 3 issues."
                ),
                suggested_fix="Refresh the workstream blurb so the published update reflects the changed ADO state or ETA.",
            )
        )

    freshness_text = _freshness_text(item)
    recent_activity_days = (as_of - last_relevant_activity).days if last_relevant_activity is not None else None
    if recent_activity_days is not None and recent_activity_days < stale_warn_days:
        if _contains_placeholder_text(freshness_text):
            findings.append(
                FreshnessItem(
                    work_item_id=item.id,
                    rule_id="FR-42",
                    severity="warn",
                    message="Recent ADO update still uses placeholder language.",
                    suggested_fix="Replace placeholder text with a concrete update, ETA, and mitigation.",
                )
            )
        elif previous_item is not None and _is_at_risk(item) and previous_item.target_date == item.target_date:
            findings.append(
                FreshnessItem(
                    work_item_id=item.id,
                    rule_id="FR-42",
                    severity="warn",
                    message="Item is At Risk/Off Track but the ETA is unchanged from the last confirmed issue.",
                    suggested_fix="Confirm the unchanged ETA is still realistic or update it with supporting detail.",
                )
            )

        if previous_item is not None and previous_snapshot_as_of is not None and _is_copy_paste_update(item, previous_snapshot_as_of):
            findings.append(
                FreshnessItem(
                    work_item_id=item.id,
                    rule_id="FR-42a",
                    severity="warn",
                    message="RiskComment or Description is >90% similar to the previous confirmed issue.",
                    suggested_fix="Replace repeated status text with the specific change since the last issue.",
                )
            )

    if _is_at_risk(item) and not _has_actionable_next_steps(freshness_text):
        findings.append(
            FreshnessItem(
                work_item_id=item.id,
                rule_id="FR-44",
                severity="warn",
                message="At Risk/Off Track item lacks actionable mitigation language.",
                suggested_fix="Add explicit next steps with an owner, action, and date.",
            )
        )

    if previous_notification is not None and _owner_email(item).lower() == previous_notification.dri_email.lower():
        if not has_response_since(item, previous_notification.notified_at):
            findings.append(
                FreshnessItem(
                    work_item_id=item.id,
                    rule_id="FR-45",
                    severity="warn",
                    message=(
                        "Primary DRI has not responded since the previous notify run on "
                        f"{previous_notification.notified_at.date().isoformat()}."
                    ),
                    suggested_fix="Follow up before the publish deadline or reroute to the alternate owner.",
                )
            )
            alternate_owner_message = _alternate_owner_message(item, program_context)
            if alternate_owner_message is not None:
                findings.append(
                    FreshnessItem(
                        work_item_id=item.id,
                        rule_id="FR-47",
                        severity="warn",
                        message=alternate_owner_message,
                        suggested_fix=(
                            "Route the next follow-up to the alternate owner until the primary DRI responds."
                            if alternate_owner_message != OWNER_ON_PTO_NO_ALTERNATE
                            else "Assign a backup owner for this workstream or follow up directly."
                        ),
                    )
                )

    return findings


def _freshness_sort_key(item: FreshnessItem) -> tuple[int, int, str]:
    severity_rank = {"block": 0, "warn": 1, "info": 2}
    return (severity_rank[item.severity], item.work_item_id, item.rule_id)


def _is_terminal(item: WorkItem) -> bool:
    return item.state.strip().lower() in TERMINAL_WORK_ITEM_STATES


def _is_active(item: WorkItem) -> bool:
    state = item.state.strip().lower()
    return state in ACTIVE_STATES or (state not in TERMINAL_WORK_ITEM_STATES)


def _is_at_risk(item: WorkItem) -> bool:
    return item.state.strip().lower() in {"at risk", "off track", "blocked"}


def _has_overdue_eta(item: WorkItem, today: date) -> bool:
    return item.target_date is not None and item.target_date < today and not _is_terminal(item)


def _has_approaching_deadline(item: WorkItem, today: date) -> bool:
    if item.target_date is None or _is_terminal(item):
        return False
    if item.target_date < today:
        return False
    business_days = _business_days_until(today, item.target_date)
    return 0 <= business_days <= 5


def _business_days_until(start_date: date, end_date: date | None) -> int:
    if end_date is None:
        return 9999
    current = start_date
    business_days = 0
    while current < end_date:
        current += timedelta(days=1)
        if current.weekday() < 5:
            business_days += 1
    return business_days


def _last_relevant_activity(item: WorkItem) -> datetime | None:
    candidates: list[datetime] = []
    for revision in item.revisions:
        if any(_is_relevant_change(field_name) for field_name in revision.fields_changed):
            candidates.append(_normalize_datetime(revision.changed_date))
    for comment in item.comments:
        if not is_vertex_generated_comment(comment):
            candidates.append(_normalize_datetime(comment.created_date))

    if candidates:
        return max(candidates)
    return _datetime_from_custom_fields(item.custom_fields, ("changed_date", "changed_at", "System.ChangedDate"))


def _recent_activity_count(item: WorkItem, as_of: datetime) -> int:
    window_start = as_of - timedelta(days=7)
    activity_count = sum(
        1
        for comment in item.comments
        if not is_vertex_generated_comment(comment) and _normalize_datetime(comment.created_date) >= window_start
    )
    activity_count += sum(
        1
        for revision in item.revisions
        if _normalize_datetime(revision.changed_date) >= window_start
        and any(_is_state_change(field_name) for field_name in revision.fields_changed)
    )
    return activity_count


def _is_relevant_change(field_name: str) -> bool:
    normalized = field_name.lower()
    return _is_state_change(field_name) or normalized.endswith("riskcomment") or normalized.endswith("discussion")


def _is_state_change(field_name: str) -> bool:
    return field_name.lower().endswith("state")


def _freshness_text(item: WorkItem) -> str:
    parts: list[str] = []
    for key in ("risk_comment", "riskComment", "description", "System.Description"):
        value = item.custom_fields.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    meaningful_comments = [comment for comment in item.comments if not is_vertex_generated_comment(comment)]
    if meaningful_comments:
        latest_comment = max(meaningful_comments, key=lambda comment: _normalize_datetime(comment.created_date))
        if latest_comment.text.strip():
            parts.append(latest_comment.text.strip())
    return "\n".join(parts)


def _contains_placeholder_text(text: str) -> bool:
    normalized = text.lower()
    return any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def _has_actionable_next_steps(text: str) -> bool:
    normalized = text.lower()
    has_action = any(verb in normalized for verb in ACTION_VERBS)
    has_date = bool(DATE_TOKEN_PATTERN.search(normalized))
    has_owner = bool(re.search(r"\b(owner|dri|@|pm|lead|team)\b", normalized))
    return has_action and (has_date or has_owner)


def _has_ghost_change(
    item: WorkItem,
    previous_item: Any,
    program_context: NarrativeProgramContext | None,
    workstream_narrative_history: Mapping[str, tuple[str, ...]] | None,
) -> bool:
    if previous_item is None or program_context is None or not workstream_narrative_history:
        return False
    if previous_item.state == item.state and previous_item.target_date == item.target_date:
        return False

    workstream = _matching_workstream(item, program_context)
    if workstream is None:
        return False

    history = workstream_narrative_history.get(workstream.name)
    if history is None or len(history) < 3:
        return False

    normalized_history = tuple(_normalize_comparable_text(entry) for entry in history[:3])
    if any(not entry for entry in normalized_history):
        return False
    return len(set(normalized_history)) == 1


def _is_copy_paste_update(item: WorkItem, previous_snapshot_as_of: datetime) -> bool:
    current_text = _current_structured_update_text(item)
    previous_text = _previous_structured_update_text(item, previous_snapshot_as_of)
    if not current_text or not previous_text:
        return False
    return _text_similarity(current_text, previous_text) >= 0.9


def _current_structured_update_text(item: WorkItem) -> str:
    parts: list[str] = []
    for field_name in ("risk_comment", "riskComment", "description", "System.Description"):
        value = item.custom_fields.get(field_name)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _previous_structured_update_text(item: WorkItem, previous_snapshot_as_of: datetime) -> str | None:
    parts: list[str] = []
    for canonical_field in ("risk_comment", "description"):
        value = _previous_structured_field_value(item, canonical_field, previous_snapshot_as_of)
        if value:
            parts.append(value)
    if not parts:
        return None
    return "\n".join(parts)


def _previous_structured_field_value(item: WorkItem, canonical_field: str, previous_snapshot_as_of: datetime) -> str | None:
    current_value = _current_structured_field_value(item, canonical_field)
    latest_before: str | None = None
    earliest_after: str | None = None
    saw_change = False

    for revision in sorted(item.revisions, key=lambda revision: _normalize_datetime(revision.changed_date)):
        for field_name, (old_value, new_value) in revision.fields_changed.items():
            if _canonical_structured_field(field_name) != canonical_field:
                continue
            saw_change = True
            changed_at = _normalize_datetime(revision.changed_date)
            if changed_at <= previous_snapshot_as_of:
                latest_before = _coerce_optional_text(new_value)
            elif earliest_after is None:
                earliest_after = _coerce_optional_text(old_value)

    if latest_before is not None:
        return latest_before
    if earliest_after is not None:
        return earliest_after
    if not saw_change:
        return current_value
    return None


def _current_structured_field_value(item: WorkItem, canonical_field: str) -> str | None:
    aliases = ("risk_comment", "riskComment") if canonical_field == "risk_comment" else ("description", "System.Description")
    for alias in aliases:
        value = item.custom_fields.get(alias)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _canonical_structured_field(field_name: str) -> str | None:
    normalized = field_name.strip().lower()
    if normalized in {"risk_comment", "riskcomment"} or normalized.endswith("riskcomment"):
        return "risk_comment"
    if normalized == "description" or normalized.endswith("description"):
        return "description"
    return None


def _text_similarity(current_text: str, previous_text: str) -> float:
    normalized_current = _normalize_comparable_text(current_text)
    normalized_previous = _normalize_comparable_text(previous_text)
    if not normalized_current or not normalized_previous:
        return 0.0
    if normalized_current == normalized_previous:
        return 1.0
    return SequenceMatcher(a=normalized_current, b=normalized_previous).ratio()


def _normalize_comparable_text(text: str) -> str:
    without_html = HTML_TAG_PATTERN.sub(" ", unescape(text))
    alphanumeric_only = NON_ALPHANUMERIC_PATTERN.sub(" ", without_html.lower())
    return " ".join(alphanumeric_only.split())


def _coerce_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _datetime_from_custom_fields(custom_fields: Mapping[str, Any], keys: tuple[str, ...]) -> datetime | None:
    for key in keys:
        value = custom_fields.get(key)
        if value is None:
            continue
        parsed = _coerce_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, str):
        try:
            return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _routing_target(
    item: WorkItem | None,
    item_findings: tuple[FreshnessItem, ...],
    program_context: NarrativeProgramContext | None,
    people_by_email: Mapping[str, str | None],
) -> tuple[str, str | None]:
    owner_email = _owner_email(item)
    if item is None or not _has_non_responder(item_findings):
        return owner_email, None

    workstream = _matching_workstream(item, program_context)
    alternate_owner = workstream.alternate_owner.strip() if workstream is not None and workstream.alternate_owner else ""
    if not alternate_owner:
        return owner_email, OWNER_ON_PTO_NO_ALTERNATE

    primary_label = _owner_label(owner_email, item.assigned_to, people_by_email)
    alternate_label = _owner_label(alternate_owner, None, people_by_email)
    return alternate_owner, f"Alternate owner: {alternate_label} (backup for {primary_label})."


def _alternate_owner_message(item: WorkItem, program_context: NarrativeProgramContext | None) -> str | None:
    workstream = _matching_workstream(item, program_context)
    if workstream is None:
        return None

    alternate_owner = workstream.alternate_owner.strip() if workstream.alternate_owner else ""
    if not alternate_owner:
        return OWNER_ON_PTO_NO_ALTERNATE

    people_by_email = {
        person.email.lower(): person.display_name
        for person in (program_context.people if program_context is not None else ())
        if person.email
    }
    return f"Route follow-up to alternate owner {_owner_label(alternate_owner, None, people_by_email)}."


def _has_non_responder(findings: tuple[FreshnessItem, ...]) -> bool:
    return any(finding.rule_id == "FR-45" for finding in findings)


def _matching_workstream(item: WorkItem, program_context: NarrativeProgramContext | None) -> ProgramWorkstream | None:
    if program_context is None:
        return None

    normalized_area_path = _normalize_area_path(item.area_path)
    matched_workstream: ProgramWorkstream | None = None
    matched_prefix_length = -1
    for workstream in program_context.workstreams:
        for area_path in workstream.area_paths:
            normalized_prefix = _normalize_area_path(area_path)
            if not normalized_prefix:
                continue
            if normalized_area_path == normalized_prefix or normalized_area_path.startswith(f"{normalized_prefix}\\"):
                if len(normalized_prefix) > matched_prefix_length:
                    matched_workstream = workstream
                    matched_prefix_length = len(normalized_prefix)
    return matched_workstream


def _normalize_area_path(value: str) -> str:
    return value.strip().replace("/", "\\").rstrip("\\").lower()


def _summary_owner_name(
    dri_email: str,
    related_items: set[int],
    items_by_id: Mapping[int, WorkItem],
    people_by_email: Mapping[str, str | None],
) -> str:
    if dri_email == "unassigned":
        return "Unassigned"

    known_name = people_by_email.get(dri_email.lower())
    if known_name:
        return known_name

    if any(_owner_email(items_by_id[item_id]) != dri_email for item_id in related_items if item_id in items_by_id):
        return dri_email

    for item_id in related_items:
        item = items_by_id.get(item_id)
        if item is not None and item.assigned_to:
            return item.assigned_to
    return dri_email


def _owner_label(
    owner_email: str,
    fallback_name: str | None,
    people_by_email: Mapping[str, str | None],
) -> str:
    known_name = people_by_email.get(owner_email.lower()) if owner_email != "unassigned" else None
    if known_name:
        return f"{known_name} <{owner_email}>"
    if fallback_name is not None and fallback_name.strip() and fallback_name.strip().lower() != owner_email.lower():
        return f"{fallback_name.strip()} <{owner_email}>"
    return owner_email


def _apply_routing_note(
    finding: FreshnessItem,
    routing_note: str | None,
    item_findings: tuple[FreshnessItem, ...],
) -> FreshnessItem:
    if (
        routing_note is None
        or finding.rule_id != "FR-45"
        or any(item_finding.rule_id == "FR-47" for item_finding in item_findings)
    ):
        return finding

    message = finding.message.rstrip()
    if not message.endswith((".", "!", "?")):
        message = f"{message}."
    return FreshnessItem(
        work_item_id=finding.work_item_id,
        rule_id=finding.rule_id,
        severity=finding.severity,
        message=f"{message} {routing_note}",
        suggested_fix=finding.suggested_fix,
        action_label=finding.action_label,
        action_message=finding.action_message,
    )


def _owner_email(item: WorkItem | None) -> str:
    if item is None:
        return "unassigned"
    return (item.assigned_to_email or item.assigned_to or "unassigned").strip() or "unassigned"
