"""Pure-ish record parsing and identity helpers for ``vertex gather``."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import Any

from src.commands.gather_pipeline.projection_stage import trajectory_point_from_item as _trajectory_point_from_item
from src.core.ado_enrichment import infer_ado_risk_level
from src.core.ado_semantics import _vertex_service_identities as load_vertex_service_identities
from src.core.config_loader import load_editorial_rules
from src.core.m365_payload_support import optional_string
from src.core.models import Comment, Revision, RiskLevel, WorkItem
from src.core.models_v2 import Signal, Team, TrajectoryPoint, Workstream
from src.core.signal_review import signal_can_be_auto_approved
from src.core.store_factory import build_signal_store
from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest


log = logging.getLogger(__name__)
VERTEX_COMMENT_PREFIX = "📊 Vertex"


def read_recent_signals(
    program_id: str,
    *,
    start: datetime,
    end: datetime,
    programs_root: Path,
    signal_store: Any = None,
) -> tuple[Signal, ...]:
    store = signal_store or build_signal_store(programs_root=programs_root)
    return store.read(program_id, start=start, end=end)


def load_freshness_thresholds(program_id: str, programs_root: Path) -> tuple[int, int]:
    rules = load_editorial_rules(programs_root / program_id / "editorial_rules.yaml")
    return (rules.stale_warn_days, rules.stale_block_days)


def resolve_icm_workstream_id(
    *,
    owning_team: str | None,
    fallback_workstream_id: str | None,
    teams: tuple[Team, ...],
    workstreams: tuple[Workstream, ...],
) -> str | None:
    if owning_team is None:
        return fallback_workstream_id
    normalized_team = normalize_team_label(owning_team)
    candidate_workstreams: set[str] = set()
    for team in teams:
        if normalized_team not in {normalize_team_label(team.id), normalize_team_label(team.name)}:
            continue
        for area_path in team.area_paths:
            resolved = resolve_workstream_id_loose_longest(area_path, workstreams)
            if resolved is not None:
                candidate_workstreams.add(resolved)
    if len(candidate_workstreams) == 1:
        return next(iter(candidate_workstreams))
    if fallback_workstream_id is not None and fallback_workstream_id in candidate_workstreams:
        return fallback_workstream_id
    return fallback_workstream_id


def normalize_team_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def tracked_field_name(field_name: str) -> str | None:
    normalized = field_name.strip().lower()
    if normalized.endswith("targetdate"):
        return "TargetDate"
    if normalized == "system.state" or normalized.endswith(".state"):
        return "State"
    if normalized.endswith("assignedto"):
        return "AssignedTo"
    return None


def build_revision_signal_text(work_item_id: int, field_name: str, prior: str | None, current: str | None) -> str:
    before = display_value(field_name, prior)
    after = display_value(field_name, current)
    if field_name == "TargetDate":
        return f"ADO#{work_item_id} target date changed from {before} to {after}."
    if field_name == "AssignedTo":
        return f"ADO#{work_item_id} owner changed from {before} to {after}."
    return f"ADO#{work_item_id} state changed from {before} to {after}."


def display_value(field_name: str, value: str | None) -> str:
    if value is None or not str(value).strip():
        return "unset"
    return person_label(value) if field_name == "AssignedTo" else str(value).strip()


def person_label(value: str) -> str:
    text = str(value).strip()
    return text.split("@", 1)[0] if "@" in text else text


def vertex_service_identities() -> set[str]:
    return load_vertex_service_identities()


def is_auto_approved_signal(signal: Signal) -> bool:
    return signal_can_be_auto_approved(signal)


def trajectory_point_from_item(item: WorkItem, as_of: datetime) -> TrajectoryPoint:
    return _trajectory_point_from_item(item, as_of)


def is_echo_chamber_revision(revision: Revision, vertex_identities: set[str]) -> bool:
    changed_by = revision.changed_by.strip().lower()
    changed_by_email = revision.changed_by_email.strip().lower()
    if vertex_identities and (changed_by in vertex_identities or changed_by_email in vertex_identities):
        return True
    return any(
        field_name.strip().lower() == "system.history"
        and current_value is not None
        and current_value.strip().startswith(VERTEX_COMMENT_PREFIX)
        for field_name, (_, current_value) in revision.fields_changed.items()
    )


def is_echo_chamber_comment(comment: Comment, vertex_identities: set[str]) -> bool:
    created_by = comment.created_by.strip().lower()
    created_by_email = comment.created_by_email.strip().lower()
    return bool(
        vertex_identities and (created_by in vertex_identities or created_by_email in vertex_identities)
    ) or comment.text.strip().startswith(VERTEX_COMMENT_PREFIX)


def parse_identity(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        return (
            optional_string(value.get("displayName") or value.get("name")),
            optional_string(value.get("uniqueName") or value.get("mailAddress")),
        )
    return (value, None) if isinstance(value, str) else (None, None)


def field_value(value: Any) -> str | None:
    if isinstance(value, dict):
        display_name, email = parse_identity(value)
        return email or display_name
    return optional_string(value)


def parse_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(";") if tag.strip()]
    if isinstance(value, (list, tuple)):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    return [str(value)]


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_int(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(str(value))
    except (TypeError, ValueError):
        return None


def parse_float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(str(value))
    except (TypeError, ValueError):
        return None


def infer_risk_level(state: str, tags: list[str], risk_assessment: str | None = None) -> RiskLevel:
    return infer_ado_risk_level(state, tags, risk_assessment)


def rest_call_count(item_count: int, *, batch_size: int = 200) -> int:
    return 0 if item_count <= 0 else ((item_count - 1) // batch_size) + 1


def record_workiq_provenance(
    *,
    workiq_signals: tuple[Signal, ...],
    program_id: str,
    programs_root: Path,
    run_at: datetime,
) -> None:
    """Best-effort lane-level evidence provenance; never blocks gather."""
    from src.core.evidence_provenance import make_provenance_record, record_provenance

    lanes: dict[str, list[Signal]] = {}
    for signal in workiq_signals:
        if signal.workstream_id:
            lanes.setdefault(signal.workstream_id, []).append(signal)
    for lane_id, signals in lanes.items():
        source_counts: dict[str, int] = {}
        for signal in signals:
            source = (signal.metadata or {}).get("source_type", "workiq")
            source_counts[source] = source_counts.get(source, 0) + 1
        latest = max(signals, key=lambda signal: signal.timestamp)
        try:
            record_provenance(
                make_provenance_record(
                    lane_id=lane_id,
                    source_type=max(source_counts, key=source_counts.__getitem__),
                    source_id=(latest.metadata or {}).get("message_id", latest.raw_ref or ""),
                    source_date=latest.timestamp.date().isoformat(),
                    confidence=0.5,
                    fields_populated=("source_type", "confidence"),
                    operator="auto",
                    run_at=run_at,
                ),
                program_id=program_id,
                programs_root=programs_root,
            )
        except Exception as exc:  # noqa: BLE001 - non-critical observability must not gate gather
            log.warning("Failed to record provenance for lane %s: %s", lane_id, exc)
