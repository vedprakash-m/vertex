from __future__ import annotations
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.core.archive_store import find_latest_confirmed_entry, load_previous_confirmed_snapshot, read_archive_index
from src.core.delta_engine import build_deltas
from src.core.exceptions import QueryError
from src.core.jinja_filters import risk_label
from src.core.models import Comment, DeltaKind, EditionType, EvidencePacket, ItemDelta, Revision, RiskLevel, Snapshot, SnapshotItem, WorkItem
from src.core.snapshot_store import read_snapshot


@dataclass(frozen=True, slots=True)
class _OfflineSnapshotCache:
    snapshot: Snapshot
    snapshot_path: Path
    source_label: str


def _load_previous_snapshot(
    edition_name: str,
    issue_number: int,
    archive_root: Path,
    trusted_issue_number: int | None = None,
) -> tuple[Snapshot | None, int | None]:
    if trusted_issue_number is not None:
        archive_index = read_archive_index(edition_name, archive_root=archive_root)
        for entry in archive_index.issues:
            if entry.kind != "confirmed" or entry.issue_number != trusted_issue_number or entry.snapshot_path is None:
                continue
            snapshot_path = Path(entry.snapshot_path)
            if not snapshot_path.exists():
                break
            return read_snapshot(snapshot_path), trusted_issue_number
    return load_previous_confirmed_snapshot(edition_name, issue_number, archive_root=archive_root)


def _load_previous_dry_run_state(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path | None = None,
) -> dict[str, Any] | None:
    path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.draft.json"  # type: ignore[arg-type]
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _find_offline_snapshot_cache(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path,
) -> _OfflineSnapshotCache | None:
    output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    output_candidates: list[tuple[int, Path]] = []
    if output_dir.exists():
        for path in output_dir.glob("issue_*/issue_*.snapshot.json"):
            issue_value = _snapshot_issue_number(path)
            if issue_value is None:
                continue
            output_candidates.append((issue_value, path))

    preferred_output_candidates = sorted(
        ((issue_value, path) for issue_value, path in output_candidates if issue_value <= issue_number),
        key=lambda entry: entry[0],
        reverse=True,
    )
    if not preferred_output_candidates:
        preferred_output_candidates = sorted(output_candidates, key=lambda entry: entry[0], reverse=True)

    for cached_issue_number, snapshot_path in preferred_output_candidates:
        if snapshot_path.exists():
            return _OfflineSnapshotCache(
                snapshot=read_snapshot(snapshot_path),
                snapshot_path=snapshot_path,
                source_label=f"cached draft Issue {cached_issue_number:03d}",
            )

    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    latest_confirmed_entry = find_latest_confirmed_entry(archive_index)
    if latest_confirmed_entry is None or latest_confirmed_entry.snapshot_path in (None, ""):
        return None

    assert latest_confirmed_entry.snapshot_path is not None
    snapshot_path = Path(latest_confirmed_entry.snapshot_path)
    if not snapshot_path.exists():
        return None
    return _OfflineSnapshotCache(
        snapshot=read_snapshot(snapshot_path),
        snapshot_path=snapshot_path,
        source_label=f"confirmed snapshot Issue {latest_confirmed_entry.issue_number:03d}",
    )


def _load_offline_snapshot_cache(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path,
) -> _OfflineSnapshotCache:
    cached_snapshot = _find_offline_snapshot_cache(
        edition_name=edition_name,
        issue_number=issue_number,
        programs_root=programs_root,
        archive_root=archive_root,
    )
    if cached_snapshot is None:
        raise QueryError(
            "Offline mode requires a cached snapshot. Run `vertex report --dry-run` online first or confirm at least one issue."
        )
    return cached_snapshot


def _snapshot_issue_number(path: Path) -> int | None:
    match = re.fullmatch(r"issue_(\d+)\.snapshot\.json", path.name)
    if match is None:
        return None
    return int(match.group(1))


def _build_offline_work_items(snapshot: Snapshot) -> tuple[WorkItem, ...]:
    fetched_at = snapshot.ado_data_as_of
    return tuple(
        WorkItem(
            id=item.id,
            type=item.type,
            title=item.title,
            state=item.state,
            assigned_to=item.assigned_to,
            assigned_to_email=None,
            area_path=item.area_path,
            iteration_path="",
            target_date=item.target_date,
            risk_level=item.risk_level,
            tags=list(item.tags),
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=fetched_at,
        )
        for item in snapshot.items
    )


def _build_draft_ado_diff_lines(
    *,
    previous_dry_run_state: dict[str, Any],
    current_items: tuple[WorkItem, ...],
    current_evidence_by_item: dict[int, EvidencePacket],
    current_issue_number: int,
    current_data_as_of: datetime,
    current_edition_type: EditionType,
) -> tuple[str, ...]:
    previous_items_payload = previous_dry_run_state.get("items")
    if not isinstance(previous_items_payload, list):
        return ("No previous draft item snapshot is available.",)

    previous_items = tuple(_deserialize_work_item(item) for item in previous_items_payload)
    previous_snapshot = _build_draft_snapshot_from_items(
        items=previous_items,
        previous_dry_run_state=previous_dry_run_state,
        fallback_issue_number=current_issue_number,
        fallback_data_as_of=current_data_as_of,
        fallback_edition_type=current_edition_type,
    )
    deltas = build_deltas(
        current_items=current_items,
        previous_snapshot=previous_snapshot,
        issue_number=current_issue_number,
        previous_issue_number=previous_snapshot.issue_number,
        evidence_by_item=current_evidence_by_item,
    )
    current_item_lookup = {item.id: item for item in current_items}
    lines: list[str] = []
    for delta in _ordered_item_deltas(deltas):
        item = current_item_lookup.get(delta.work_item_id)
        title = item.title if item is not None else f"Work item {delta.work_item_id}"
        detail = _format_draft_item_delta(delta)
        lines.append(f'#{delta.work_item_id} "{title}" - {detail}')
    if not lines:
        lines.append("No ADO data changes detected.")
    return tuple(lines)


def _build_draft_snapshot_from_items(
    *,
    items: tuple[WorkItem, ...],
    previous_dry_run_state: dict[str, Any],
    fallback_issue_number: int,
    fallback_data_as_of: datetime,
    fallback_edition_type: EditionType,
) -> Snapshot:
    issue_number = int(previous_dry_run_state.get("issue_number", fallback_issue_number))
    generated_at = _parse_datetime(previous_dry_run_state.get("generated_at")) or fallback_data_as_of
    ado_data_as_of = _parse_datetime(previous_dry_run_state.get("ado_data_as_of")) or fallback_data_as_of
    edition_type_value = previous_dry_run_state.get("edition_type")
    edition_type = fallback_edition_type
    if edition_type_value not in (None, ""):
        try:
            edition_type = EditionType.from_string(str(edition_type_value))
        except ValueError:
            edition_type = fallback_edition_type

    return Snapshot(
        issue_number=issue_number,
        generated_at=generated_at,
        ado_data_as_of=ado_data_as_of,
        edition_type=edition_type,
        items=tuple(
            SnapshotItem(
                id=item.id,
                type=item.type,
                title=item.title,
                state=item.state,
                assigned_to=item.assigned_to,
                area_path=item.area_path,
                target_date=item.target_date,
                risk_level=item.risk_level,
                tags=list(item.tags),
            )
            for item in items
        ),
        scorecards=(),
    )


def _ordered_item_deltas(deltas: Any) -> tuple[ItemDelta, ...]:
    owner_changes = tuple(getattr(deltas, "owner_changes", ()))
    return (
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_UP), key=lambda delta: delta.work_item_id),
        *sorted(deltas.new_items, key=lambda delta: delta.work_item_id),
        *sorted(deltas.eta_changes, key=lambda delta: delta.work_item_id),
        *sorted(owner_changes, key=lambda delta: delta.work_item_id),
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_DOWN), key=lambda delta: delta.work_item_id),
        *sorted(deltas.closed_items, key=lambda delta: delta.work_item_id),
    )


def _format_draft_item_delta(delta: ItemDelta) -> str:
    if delta.kind == DeltaKind.NEW:
        return "NEW"
    if delta.kind == DeltaKind.CLOSED:
        return "CLOSED"
    if delta.kind in {DeltaKind.RISK_UP, DeltaKind.RISK_DOWN}:
        return f"Risk changed: {_diff_risk_label(delta.old_risk.value if delta.old_risk is not None else None)} -> {_diff_risk_label(delta.new_risk.value if delta.new_risk is not None else None)}"
    if delta.kind == DeltaKind.ETA_CHANGED:
        return f"ETA changed: {_format_optional_date(delta.old_eta)} -> {_format_optional_date(delta.new_eta)}"
    if delta.kind == DeltaKind.OWNER_CHANGED:
        previous_owner, current_owner = delta.field_changes.get("assigned_to", (None, None))
        return f"Owner changed: {previous_owner or 'Unassigned'} -> {current_owner or 'Unassigned'}"
    return delta.kind.value.replace("_", " ")


def _format_optional_date(value: date | None) -> str:
    if value is None:
        return "None"
    return value.isoformat()


def _diff_risk_label(value: str | None) -> str:
    if value in (None, ""):
        return risk_label(RiskLevel.UNKNOWN)
    try:
        return risk_label(RiskLevel.from_string(value))
    except ValueError:
        return str(value)


def _normalize_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _deserialize_work_item(payload: dict[str, Any]) -> WorkItem:
    return WorkItem(
        id=int(payload["id"]),
        type=str(payload["type"]),
        title=str(payload["title"]),
        state=str(payload["state"]),
        assigned_to=_optional_string(payload.get("assigned_to")),
        assigned_to_email=_optional_string(payload.get("assigned_to_email")),
        area_path=str(payload["area_path"]),
        iteration_path=str(payload["iteration_path"]),
        target_date=_parse_date(payload.get("target_date")),
        risk_level=RiskLevel.from_string(str(payload["risk_level"])),
        tags=[str(tag) for tag in payload.get("tags", [])],
        custom_fields=dict(payload.get("custom_fields", {})),
        revisions=[_deserialize_revision(revision) for revision in payload.get("revisions", [])],
        comments=[_deserialize_comment(comment) for comment in payload.get("comments", [])],
        fetched_at=_parse_datetime(payload.get("fetched_at")) or datetime.now(timezone.utc),
        risk_assessment=_normalize_optional_string(payload.get("risk_assessment")),
        risk_assessment_comment=_normalize_optional_string(payload.get("risk_assessment_comment")),
    )


def _deserialize_revision(payload: dict[str, Any]) -> Revision:
    return Revision(
        work_item_id=int(payload["work_item_id"]),
        rev_number=int(payload["rev_number"]),
        changed_by=str(payload["changed_by"]),
        changed_by_email=str(payload["changed_by_email"]),
        changed_date=_parse_datetime(payload.get("changed_date")) or datetime.now(timezone.utc),
        fields_changed={
            str(field_name): (values[0], values[1])
            for field_name, values in payload.get("fields_changed", {}).items()
        },
    )


def _deserialize_comment(payload: dict[str, Any]) -> Comment:
    return Comment(
        work_item_id=int(payload["work_item_id"]),
        comment_id=int(payload["comment_id"]),
        created_by=str(payload["created_by"]),
        created_by_email=str(payload["created_by_email"]),
        created_date=_parse_datetime(payload.get("created_date")) or datetime.now(timezone.utc),
        text=str(payload["text"]),
    )


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)