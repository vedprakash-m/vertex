from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, IO, Iterator, Sequence
import uuid

import portalocker
import yaml

from src.core.archive_store import ARCHIVE_ROOT, read_archive_index
from src.core.coverage_gap import CoverageGap
from src.core.models import Snapshot, SnapshotItem
from src.core.models import WorkItem
from src.core.models_v2 import VitalityScore
from src.core.snapshot_store import read_snapshot


REPO_ROOT = Path(__file__).resolve().parents[2]
from src.core.edition_resolver import get_program_output_dir, _output_subdir, _OUTPUT_SUBDIR_LEGACY, PROGRAMS_ROOT
PROGRAMS_ROOT = REPO_ROOT / "programs"


class ProposalManifestLockedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ADOUpdateEntry:
    work_item_id: int
    action: str
    field_or_tag: str
    current_value: str | None
    proposed_value: str
    reason: str
    revision_id: int | None = None
    entry_status: str = "pending"
    status_reason: str | None = None
    remote_rev: int | None = None
    # ADF-W1.2 (Appendix B.8): stable per-entry create identity, persisted
    # before dispatch. ``operation_intent_id`` becomes the ``vertex-intent-<id>``
    # System.Tags marker on create; ``attempted_at`` being non-None on a
    # pending/failed entry means a prior dispatch attempt was persisted (its
    # response may have been lost), so the next apply run must search before
    # creating again rather than assume the earlier attempt never reached ADO.
    # Both default None so pre-ADF-W1.2 manifests upcast cleanly.
    operation_intent_id: str | None = None
    attempted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ADOUpdateProposal:
    id: str
    program_id: str
    edition_id: str | None
    issue_number: int | None
    update_type: str
    created_at: datetime
    expires_at: datetime
    entries: tuple[ADOUpdateEntry, ...]


@dataclass(frozen=True, slots=True)
class ADOFieldMapping:
    vertex_field: str
    ado_field: str
    direction: str = "vertex_to_ado"
    auto_propose: bool = False


@dataclass(frozen=True, slots=True)
class ADOFieldMappingConfig:
    proposal_ttl_hours: int
    mappings: tuple[ADOFieldMapping, ...]


@dataclass(frozen=True, slots=True)
class ADOFieldProposalValue:
    value: str
    reason: str


def load_confirmed_issue_snapshot(
    edition_id: str,
    issue_number: int,
    *,
    archive_root: Path = ARCHIVE_ROOT,
) -> Snapshot:
    index = read_archive_index(edition_id, archive_root=archive_root)
    for entry in index.issues:
        if entry.issue_number != issue_number or entry.kind != "confirmed" or entry.snapshot_path is None:
            continue
        snapshot_path = Path(entry.snapshot_path)
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Confirmed snapshot is missing for {edition_id} issue #{issue_number:03d}: {snapshot_path}")
        return read_snapshot(snapshot_path)
    raise ValueError(f"Confirmed issue #{issue_number:03d} was not found for edition '{edition_id}'.")


def load_ado_field_mapping_config(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ADOFieldMappingConfig:
    path = programs_root / program_id / "ado_field_map.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Program '{program_id}' is missing ado_field_map.yaml. Add programs/{program_id}/ado_field_map.yaml to use --type field."
        )

    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, Mapping):
        raise ValueError("Expected a YAML object in ado_field_map.yaml.")

    schema_version = str(document.get("schema_version") or "1.0").strip()
    if schema_version.split(".", 1)[0] != "1":
        raise ValueError(f"Unsupported ado_field_map schema_version '{schema_version}'.")

    proposal_ttl_hours = _coerce_int(document.get("proposal_ttl_hours")) or 72
    if proposal_ttl_hours <= 0:
        raise ValueError("ado_field_map proposal_ttl_hours must be a positive integer.")

    raw_mappings = document.get("mappings") or ()
    if not isinstance(raw_mappings, list):
        raise ValueError("ado_field_map 'mappings' must be a list.")

    mappings: list[ADOFieldMapping] = []
    for raw_mapping in raw_mappings:
        if not isinstance(raw_mapping, Mapping):
            raise ValueError("Each ado_field_map mapping must be an object.")
        vertex_field = str(raw_mapping.get("vertex_field") or "").strip()
        ado_field = str(raw_mapping.get("ado_field") or "").strip()
        direction = str(raw_mapping.get("direction") or "vertex_to_ado").strip().lower()
        if not vertex_field or not ado_field:
            raise ValueError("Each ado_field_map mapping requires non-empty vertex_field and ado_field values.")
        if direction != "vertex_to_ado":
            raise ValueError(
                f"Unsupported ado_field_map direction '{direction}' for mapping '{vertex_field}'. Only vertex_to_ado is supported."
            )
        mappings.append(
            ADOFieldMapping(
                vertex_field=vertex_field,
                ado_field=ado_field,
                direction=direction,
                auto_propose=bool(raw_mapping.get("auto_propose", False)),
            )
        )

    if not mappings:
        raise ValueError("ado_field_map.yaml does not define any mappings.")

    return ADOFieldMappingConfig(
        proposal_ttl_hours=proposal_ttl_hours,
        mappings=tuple(mappings),
    )


def build_field_proposal(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int | None,
    mapping_config: ADOFieldMappingConfig,
    current_work_item_rows: Sequence[dict[str, Any]],
    field_values_by_item: Mapping[int, Mapping[str, ADOFieldProposalValue]],
    proposal_id: str | None = None,
    created_at: datetime | None = None,
) -> ADOUpdateProposal:
    resolved_created_at = _ensure_utc(created_at or datetime.now(timezone.utc))
    revision_ids = _revision_ids_from_rows(current_work_item_rows)
    rows_by_id = _rows_by_work_item_id(current_work_item_rows)
    entries: list[ADOUpdateEntry] = []

    for work_item_id in sorted(field_values_by_item):
        item_values = field_values_by_item[work_item_id]
        current_row = rows_by_id.get(work_item_id)
        for mapping in mapping_config.mappings:
            proposal_value = item_values.get(mapping.vertex_field)
            if proposal_value is None:
                continue
            current_value = _row_field_value(current_row, mapping.ado_field)
            if _normalize_value(current_value) == _normalize_value(proposal_value.value):
                continue
            entries.append(
                ADOUpdateEntry(
                    work_item_id=work_item_id,
                    action="set_field",
                    field_or_tag=mapping.ado_field,
                    current_value=current_value,
                    proposed_value=proposal_value.value,
                    reason=proposal_value.reason,
                    revision_id=revision_ids.get(work_item_id),
                )
            )

    return ADOUpdateProposal(
        id=proposal_id or f"prop-{uuid.uuid4().hex[:8]}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        update_type="field",
        created_at=resolved_created_at,
        expires_at=resolved_created_at + timedelta(hours=mapping_config.proposal_ttl_hours),
        entries=tuple(entries),
    )


def build_comment_proposal(
    *,
    program_id: str,
    edition_id: str,
    snapshot: Snapshot,
    current_work_item_rows: Sequence[dict[str, Any]],
    proposal_id: str | None = None,
    created_at: datetime | None = None,
    ttl_hours: int = 72,
    programs_root: Path = PROGRAMS_ROOT,
) -> ADOUpdateProposal:
    resolved_created_at = _ensure_utc(created_at or datetime.now(timezone.utc))
    revision_ids = _revision_ids_from_rows(current_work_item_rows)
    comment_template = _load_comment_template(program_id, programs_root=programs_root)
    entries = tuple(
        ADOUpdateEntry(
            work_item_id=item.id,
            action="add_comment",
            field_or_tag="comment",
            current_value=None,
            proposed_value=_build_comment_body(
                edition_id=edition_id,
                snapshot=snapshot,
                item=item,
                comment_template=comment_template,
            ),
            reason=f"Cited in confirmed issue #{snapshot.issue_number:03d}.",
            revision_id=revision_ids.get(item.id),
        )
        for item in sorted(snapshot.items, key=lambda snapshot_item: snapshot_item.id)
    )
    return ADOUpdateProposal(
        id=proposal_id or f"prop-{uuid.uuid4().hex[:8]}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=snapshot.issue_number,
        update_type="comment",
        created_at=resolved_created_at,
        expires_at=resolved_created_at + timedelta(hours=ttl_hours),
        entries=entries,
    )


def build_vitality_nudge_proposal(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int | None,
    items: tuple[WorkItem, ...],
    scores: tuple[VitalityScore, ...],
    current_work_item_rows: Sequence[dict[str, Any]],
    proposal_id: str | None = None,
    created_at: datetime | None = None,
    ttl_hours: int = 72,
    composite_threshold: int = 40,
    stale_days: int = 14,
    recent_nudge_item_ids: set[int] | None = None,
) -> ADOUpdateProposal:
    resolved_created_at = _ensure_utc(created_at or datetime.now(timezone.utc))
    revision_ids = _revision_ids_from_rows(current_work_item_rows)
    item_by_id = {item.id: item for item in items}
    blocked_item_ids = recent_nudge_item_ids or set()
    entries = tuple(
        ADOUpdateEntry(
            work_item_id=item.id,
            action="add_comment",
            field_or_tag="comment",
            current_value=None,
            proposed_value=_build_vitality_nudge_body(item, score),
            reason=(
                f"Vitality composite {score.composite_score}% with {score.workiq_signal_count} non-ADO signals and {score.freshness_days} stale days."
            ),
            revision_id=revision_ids.get(item.id),
        )
        for score in sorted(scores, key=lambda entry: entry.work_item_id)
        for item in [item_by_id.get(score.work_item_id)]
        if item is not None
        if score.work_item_id not in blocked_item_ids
        if score.composite_score < composite_threshold
        if score.workiq_signal_count > 0
        if score.freshness_days > stale_days
    )
    return ADOUpdateProposal(
        id=proposal_id or f"prop-{uuid.uuid4().hex[:8]}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        update_type="vitality_nudge",
        created_at=resolved_created_at,
        expires_at=resolved_created_at + timedelta(hours=ttl_hours),
        entries=entries,
    )


def build_vitality_tag_proposal(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int | None,
    items: tuple[WorkItem, ...],
    scores: tuple[VitalityScore, ...],
    current_work_item_rows: Sequence[dict[str, Any]],
    coverage_gaps: tuple[CoverageGap, ...],
    proposal_id: str | None = None,
    created_at: datetime | None = None,
    ttl_hours: int = 72,
    tag_name: str = "Needs-PM-Review",
    consecutive_gap_threshold: int = 2,
    gap_window_days: int = 14,
) -> ADOUpdateProposal:
    resolved_created_at = _ensure_utc(created_at or datetime.now(timezone.utc))
    revision_ids = _revision_ids_from_rows(current_work_item_rows)
    score_by_id = {score.work_item_id: score for score in scores}
    coverage_gap_ids = {gap.work_item_id for gap in coverage_gaps}
    stale_gap_days = max(1, consecutive_gap_threshold * gap_window_days)
    entries: list[ADOUpdateEntry] = []
    for item in sorted(items, key=lambda work_item: work_item.id):
        score = score_by_id.get(item.id)
        if score is None:
            continue
        has_tag = any(tag.lower() == tag_name.lower() for tag in item.tags)
        if has_tag and score.freshness_grade == "green":
            entries.append(
                ADOUpdateEntry(
                    work_item_id=item.id,
                    action="remove_tag",
                    field_or_tag="System.Tags",
                    current_value="; ".join(item.tags) if item.tags else None,
                    proposed_value=tag_name,
                    reason="Item is fresh again; auto-resolve the vitality tag.",
                    revision_id=revision_ids.get(item.id),
                )
            )
            continue
        if item.id not in coverage_gap_ids or has_tag or score.freshness_days < stale_gap_days:
            continue
        entries.append(
            ADOUpdateEntry(
                work_item_id=item.id,
                action="add_tag",
                field_or_tag="System.Tags",
                current_value="; ".join(item.tags) if item.tags else None,
                proposed_value=tag_name,
                reason=(
                    f"Coverage gap persisted across roughly {consecutive_gap_threshold} stale windows ({score.freshness_days} days since the last update)."
                ),
                revision_id=revision_ids.get(item.id),
            )
        )
    return ADOUpdateProposal(
        id=proposal_id or f"prop-{uuid.uuid4().hex[:8]}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        update_type="vitality_tag",
        created_at=resolved_created_at,
        expires_at=resolved_created_at + timedelta(hours=ttl_hours),
        entries=tuple(entries),
    )


def build_hygiene_field_proposal(
    *,
    program_id: str,
    edition_id: str,
    items: Sequence[WorkItem],
    proposal_id: str | None = None,
    created_at: datetime | None = None,
    ttl_hours: int = 72,
) -> ADOUpdateProposal:
    """Generate ADO field update proposals for items missing target_date or assigned_to (FR-SG-11)."""
    resolved_created_at = _ensure_utc(created_at or datetime.now(timezone.utc))
    entries: list[ADOUpdateEntry] = []
    for item in items:
        if item.state in {"Closed", "Done", "Resolved", "Completed", "Cancelled", "Removed"}:
            continue
        if not item.target_date:
            entries.append(ADOUpdateEntry(
                work_item_id=item.id,
                action="set_field",
                field_or_tag="Microsoft.VSTS.Scheduling.TargetDate",
                current_value=None,
                proposed_value="",  # placeholder — requires human acceptance
                reason=f"WI:{item.id} '{item.title[:60]}' is missing TargetDate; ADO hygiene gap (FR-SG-11).",
            ))
        if not item.assigned_to:
            entries.append(ADOUpdateEntry(
                work_item_id=item.id,
                action="set_field",
                field_or_tag="System.AssignedTo",
                current_value=None,
                proposed_value="",  # placeholder — requires human acceptance
                reason=f"WI:{item.id} '{item.title[:60]}' is unassigned; ADO hygiene gap (FR-SG-11).",
            ))
    return ADOUpdateProposal(
        id=proposal_id or f"hygiene-{uuid.uuid4().hex[:8]}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=None,
        update_type="hygiene_field",
        created_at=resolved_created_at,
        expires_at=resolved_created_at + timedelta(hours=ttl_hours),
        entries=tuple(entries),
    )


def build_action_item_proposal(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int | None,
    action_items: Sequence[Any],
    proposal_id: str | None = None,
    created_at: datetime | None = None,
    ttl_hours: int = 72,
) -> ADOUpdateProposal:
    resolved_created_at = _ensure_utc(created_at or datetime.now(timezone.utc))
    entries: list[ADOUpdateEntry] = []
    for action in action_items:
        task_data = {
            "title": action.text,
            "description": f"Vertex action item back-write from meeting source.\n\nText: {action.text}\nOwner: {action.owner_alias}\nSource Signal: {action.source_signal_id or 'teams'}",
            "assigned_to": action.owner_alias,
            "target_date": action.due_date.isoformat() if action.due_date is not None else None,
        }
        if action.linked_work_item_ids:
            task_data["description"] += f"\nLinked Work Items: {', '.join(str(wi) for wi in action.linked_work_item_ids)}"
        
        entries.append(
            ADOUpdateEntry(
                work_item_id=0,
                action="create_task",
                field_or_tag="Task",
                current_value=None,
                proposed_value=json.dumps(task_data),
                reason=f"Action item tracking back-write for {action.owner_alias}.",
                # ADF-W1.2: assigned at proposal build time so it is stable
                # across every re-application attempt of this manifest.
                operation_intent_id=uuid.uuid4().hex,
            )
        )
    return ADOUpdateProposal(
        id=proposal_id or f"prop-{uuid.uuid4().hex[:8]}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        update_type="action_item",
        created_at=resolved_created_at,
        expires_at=resolved_created_at + timedelta(hours=ttl_hours),
        entries=tuple(entries),
    )


def get_proposal_output_path(
    proposal: ADOUpdateProposal,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    scope_id = proposal.edition_id or proposal.program_id
    return get_program_output_dir(scope_id, programs_root=programs_root) / "ado_proposals" / f"{proposal.id}.json"


def proposal_to_document(
    proposal: ADOUpdateProposal,
    *,
    proposal_status: str = "pending",
) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "program_id": proposal.program_id,
        "edition_id": proposal.edition_id,
        "issue_number": proposal.issue_number,
        "update_type": proposal.update_type,
        "created_at": proposal.created_at.isoformat(),
        "expires_at": proposal.expires_at.isoformat(),
        "proposal_status": proposal_status,
        "entries": [
            {
                "work_item_id": entry.work_item_id,
                "action": entry.action,
                "field_or_tag": entry.field_or_tag,
                "current_value": entry.current_value,
                "proposed_value": entry.proposed_value,
                "reason": entry.reason,
                "revision_id": entry.revision_id,
                "entry_status": entry.entry_status,
                "status_reason": entry.status_reason,
                "remote_rev": entry.remote_rev,
                "operation_intent_id": entry.operation_intent_id,
                "attempted_at": entry.attempted_at.isoformat() if entry.attempted_at is not None else None,
            }
            for entry in proposal.entries
        ],
    }


def proposal_from_document(document: Mapping[str, Any]) -> ADOUpdateProposal:
    entries = tuple(
        ADOUpdateEntry(
            work_item_id=int(entry["work_item_id"]),
            action=str(entry["action"]),
            field_or_tag=str(entry["field_or_tag"]),
            current_value=_optional_string(entry.get("current_value")),
            proposed_value=str(entry["proposed_value"]),
            reason=str(entry["reason"]),
            revision_id=_coerce_int(entry.get("revision_id")),
            entry_status=str(entry.get("entry_status") or "pending"),
            status_reason=_optional_string(entry.get("status_reason")),
            remote_rev=_coerce_int(entry.get("remote_rev")),
            operation_intent_id=_optional_string(entry.get("operation_intent_id")),
            attempted_at=_parse_optional_timestamp(entry.get("attempted_at")),
        )
        for entry in document.get("entries") or ()
        if isinstance(entry, Mapping)
    )
    return ADOUpdateProposal(
        id=str(document["id"]),
        program_id=str(document["program_id"]),
        edition_id=_optional_string(document.get("edition_id")),
        issue_number=_coerce_int(document.get("issue_number")),
        update_type=str(document["update_type"]),
        created_at=_parse_timestamp(document["created_at"]),
        expires_at=_parse_timestamp(document["expires_at"]),
        entries=entries,
    )


def read_proposal_manifest(path: Path) -> tuple[ADOUpdateProposal, str]:
    with path.open("r", encoding="utf-8") as handle:
        return read_proposal_manifest_from_handle(handle)


def read_proposal_manifest_from_handle(handle: IO[str]) -> tuple[ADOUpdateProposal, str]:
    handle.seek(0)
    payload = json.loads(handle.read())
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object in proposal manifest.")
    return proposal_from_document(payload), str(payload.get("proposal_status") or "pending")


def find_proposal_manifest(proposal_reference: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    candidate = Path(proposal_reference.strip())
    if candidate.exists():
        return candidate
        
    matches = sorted(programs_root.glob(f"*/{_output_subdir()}/*/ado_proposals/{proposal_reference.strip()}.json"))
    if not matches and _output_subdir() != _OUTPUT_SUBDIR_LEGACY:
        # Transition-window fallback: canonical subdir not yet renamed on disk
        matches = sorted(programs_root.glob(f"*/{_OUTPUT_SUBDIR_LEGACY}/*/ado_proposals/{proposal_reference.strip()}.json"))
    if not matches:
        raise FileNotFoundError(f"Proposal manifest '{proposal_reference}' was not found under {programs_root}.")
    if len(matches) > 1:
        raise ValueError(
            f"Proposal id '{proposal_reference}' is ambiguous across scopes: "
            + ", ".join(str(path) for path in matches)
        )
    return matches[0]


def write_proposal_manifest(
    proposal: ADOUpdateProposal,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    proposal_status: str = "pending",
) -> Path:
    path = get_proposal_output_path(proposal, programs_root=programs_root)
    return write_proposal_manifest_at_path(path, proposal, proposal_status=proposal_status)


def write_proposal_manifest_at_path(
    path: Path,
    proposal: ADOUpdateProposal,
    *,
    proposal_status: str = "pending",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        write_proposal_manifest_to_handle(handle, proposal, proposal_status=proposal_status)
    return path


def write_proposal_manifest_to_handle(
    handle: IO[str],
    proposal: ADOUpdateProposal,
    *,
    proposal_status: str = "pending",
) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(proposal_to_document(proposal, proposal_status=proposal_status), indent=2))
    handle.flush()
    os.fsync(handle.fileno())


@contextmanager
def open_locked_proposal_manifest(path: Path) -> Iterator[IO[str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException as error:
            raise ProposalManifestLockedError("Another apply is in progress.") from error
        try:
            yield handle
        finally:
            portalocker.unlock(handle)


def _build_comment_body(
    *,
    edition_id: str,
    snapshot: Snapshot,
    item: SnapshotItem,
    comment_template: str | None,
) -> str:
    if comment_template:
        rendered = comment_template.format_map(
            _SafeTemplateValues(
                {
                    "date": snapshot.generated_at.date().isoformat(),
                    "edition_id": edition_id,
                    "issue_number": snapshot.issue_number,
                    "issue_number_padded": f"{snapshot.issue_number:03d}",
                    "one_line_summary": item.title,
                    "risk_level": item.risk_level.value,
                    "state": item.state,
                    "target_date": item.target_date.isoformat() if item.target_date is not None else "",
                    "target_date_or_unknown": item.target_date.isoformat() if item.target_date is not None else "n/a",
                    "title": item.title,
                    "trajectory_insight": "",
                    "link_to_newsletter": "",
                    "work_item_id": item.id,
                }
            )
        )
        normalized = _normalize_comment(rendered)
        if normalized:
            return normalized

    parts = [
        f"Discussed in Vertex {edition_id} issue #{snapshot.issue_number:03d} ({snapshot.generated_at.date().isoformat()}).",
        f"Risk: {item.risk_level.value}.",
        f"State: {item.state}.",
    ]
    if item.target_date is not None:
        parts.append(f"Target date: {item.target_date.isoformat()}.")
    return " ".join(parts)


def _rows_by_work_item_id(rows: Sequence[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for row in rows:
        work_item_id = _coerce_int(row.get("id"))
        if work_item_id is None:
            work_item_id = _coerce_int(_row_field_value(row, "System.Id"))
        if work_item_id is not None:
            indexed[work_item_id] = row
    return indexed


def _row_field_value(row: dict[str, Any] | None, field_name: str) -> str | None:
    if not isinstance(row, Mapping):
        return None
    raw_fields = row.get("fields")
    fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    value = fields.get(field_name)
    if value is None:
        return None
    return str(value)


def _normalize_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _build_vitality_nudge_body(item: WorkItem, score: VitalityScore) -> str:
    lines = [
        f"Vertex Vitality Check - WI:{item.id}",
        "",
        f"This item has not been updated in ADO for {score.freshness_days} days.",
        "Recent non-ADO activity was detected for this item.",
        "",
        "A quick status update here would help keep the weekly review accurate.",
        "",
        "Suggested fields to update:",
    ]
    for entry in _vitality_nudge_checklist(item, score):
        lines.append(f"- {entry}")
    lines.extend(("", "Vertex (automated vitality check)"))
    return "\n".join(lines)


def _vitality_nudge_checklist(item: WorkItem, score: VitalityScore) -> tuple[str, ...]:
    checklist = ["Current status / blockers"]
    if item.target_date is not None:
        checklist.append(f"Target date (currently: {item.target_date.isoformat()} - still accurate?)")
    else:
        checklist.append("Target date")
    if "recent_comment" in score.richness_missing:
        checklist.append("Owner comment / next step")
    elif score.suggested_update is not None:
        checklist.append(score.suggested_update)
    return tuple(checklist)


def _revision_ids_from_rows(rows: Sequence[dict[str, Any]]) -> dict[int, int | None]:
    revision_ids: dict[int, int | None] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_fields = row.get("fields")
        fields = raw_fields if isinstance(raw_fields, dict) else {}
        work_item_id = _coerce_int(row.get("id"))
        if work_item_id is None:
            work_item_id = _coerce_int(fields.get("System.Id"))
        if work_item_id is None:
            continue
        revision_ids[work_item_id] = _coerce_int(row.get("rev"))
        if revision_ids[work_item_id] is None:
            revision_ids[work_item_id] = _coerce_int(fields.get("System.Rev"))
    return revision_ids


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO timestamp string, found {type(value).__name__}.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Proposal timestamps must be timezone-aware.")
    return parsed.astimezone(timezone.utc)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_optional_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value)


def _load_comment_template(program_id: str, *, programs_root: Path) -> str | None:
    path = programs_root / program_id / "ado_comment_template.md"
    if not path.exists():
        return None
    template = path.read_text(encoding="utf-8")
    return template if template.strip() else None


def _normalize_comment(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    normalized: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized.append(line)
        previous_blank = is_blank
    return "\n".join(normalized)


class _SafeTemplateValues(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""