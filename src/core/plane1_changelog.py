"""
Plane 1 change detection and persistence.

Implements §22 E1 of the program-context-maturity spec.

Change detection logic:
  - Milestone:   status, target_date, owner_alias
  - RiskEntry:   status, probability, impact, owner_alias, mitigation_due_date
  - Workstream:  current_blocker, dri_email
  - DecisionEntry: status, owner_alias
  - Assumption:   status, owner_alias

File layout:
  programs/<prog>/
    changelog/
      plane1_changes.jsonl        # append-only; Plane 2
    _state/
      plane1_last_seen.json       # mutable; Plane 3 (regenerated each gather)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
from typing import Any, Literal, cast

from src.core.models import Confidence
from src.core.models_v2 import (
    Assumption,
    DecisionEntry,
    DecisionStatus,
    Milestone,
    MilestoneStatus,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    ReviewPolicy,
    Signal,
    Workstream,
)
from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plane1ChangeRecord:
    """Records a single detected change in Plane 1 authored configuration."""

    ts: datetime
    program_id: str
    gather_run_id: str
    entity_type: str  # "milestone" | "risk" | "workstream" | "decision" | "assumption"
    entity_id: str
    entity_name: str
    field: str
    prior: str | None
    current: str | None
    kind: str  # "status_change" | "date_change" | "owner_change" | "field_change" | "entity_added" | "entity_removed"
    linked_workstream_ids: tuple[str, ...]
    record_type: Literal["plane1_change"] = "plane1_change"

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": _normalize_datetime(self.ts).isoformat().replace("+00:00", "Z"),
            "program_id": self.program_id,
            "gather_run_id": self.gather_run_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "field": self.field,
            "prior": self.prior,
            "current": self.current,
            "kind": self.kind,
            "linked_workstream_ids": list(self.linked_workstream_ids),
            "record_type": self.record_type,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> Plane1ChangeRecord:
        return Plane1ChangeRecord(
            ts=_parse_datetime(d["ts"]),
            program_id=str(d["program_id"]),
            gather_run_id=str(d["gather_run_id"]),
            entity_type=str(d["entity_type"]),
            entity_id=str(d["entity_id"]),
            entity_name=str(d["entity_name"]),
            field=str(d["field"]),
            prior=d.get("prior"),
            current=d.get("current"),
            kind=str(d["kind"]),
            linked_workstream_ids=tuple(d.get("linked_workstream_ids") or []),
            record_type=cast(Literal["plane1_change"], d.get("record_type", "plane1_change")),
        )


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_plane1_changes(
    program_id: str,
    milestones: list[Milestone],
    risks: list[RiskEntry],
    workstreams: list[Workstream],
    decisions: list[DecisionEntry],
    assumptions: list[Assumption],
    last_seen: dict[str, dict[str, Any]] | None,
    gather_run_id: str,
    gathered_at: datetime,
) -> list[Plane1ChangeRecord]:
    """
    Compute field-level changes between the current Plane 1 config and last-seen snapshot.

    last_seen: keyed "{entity_type}/{entity_id}" → {"field": serialized_value, ...}
    Returns a list of change records for all detected changes.
    """
    if last_seen is None:
        last_seen = {}

    current = build_plane1_snapshot(milestones, risks, workstreams, decisions, assumptions)
    changes: list[Plane1ChangeRecord] = []

    for key, current_fields in current.items():
        entity_type, entity_id = key.split("/", 1)
        prior_fields = last_seen.get(f"{entity_type}/{entity_id}", {})
        entity_name = _entity_name(entity_type, entity_id, current)
        linked_ws = _linked_workstream_ids(entity_type, entity_id, current)
        tracked_fields = _FIELDS_BY_ENTITY.get(entity_type, set())

        for field, current_value in current_fields.items():
            if field not in tracked_fields:
                continue
            prior_value = prior_fields.get(field)
            current_serialized = _serialize(current_value)
            prior_serialized = _serialize(prior_value) if prior_value is not None else None

            if current_serialized != prior_serialized:
                kind = _classify_change(entity_type, field, prior_value, current_value)
                changes.append(
                    Plane1ChangeRecord(
                        ts=gathered_at,
                        program_id=program_id,
                        gather_run_id=gathered_at.strftime("%Y%m%d%H%M%S"),
                        entity_type=entity_type,
                        entity_id=entity_id,
                        entity_name=entity_name,
                        field=field,
                        prior=prior_serialized,
                        current=current_serialized,
                        kind=kind,
                        linked_workstream_ids=linked_ws,
                    )
                )

    return changes


def build_plane1_snapshot(
    milestones: list[Milestone],
    risks: list[RiskEntry],
    workstreams: list[Workstream],
    decisions: list[DecisionEntry],
    assumptions: list[Assumption],
) -> dict[str, dict[str, Any]]:
    """
    Build a flat snapshot of all trackable Plane 1 fields.

    Returns dict keyed "{entity_type}/{entity_id}" → {"field": value, ...}
    """
    snap: dict[str, dict[str, Any]] = {}

    for m in milestones:
        snap[f"milestone/{m.id}"] = {
            "status": m.status.value if m.status else None,
            "target_date": m.target_date.isoformat() if m.target_date else None,
            "owner_alias": m.owner_alias,
            "_name": m.name,
            "_linked_workstream_ids": m.linked_workstream_ids,
        }

    for r in risks:
        snap[f"risk/{r.id}"] = {
            "status": r.status.value if r.status else None,
            "probability": r.probability.value if r.probability else None,
            "impact": r.impact.value if r.impact else None,
            "owner_alias": r.owner_alias,
            "mitigation_due_date": r.mitigation_due_date.isoformat() if r.mitigation_due_date else None,
            "_name": r.title,
            "_linked_workstream_ids": r.linked_workstream_ids,
        }

    for ws in workstreams:
        snap[f"workstream/{ws.id}"] = {
            "current_blocker": ws.current_blocker,
            "dri_email": ws.dri_email,
            "_name": ws.name,
            "_linked_workstream_ids": (ws.id,),
        }

    for d in decisions:
        snap[f"decision/{d.id}"] = {
            "status": d.status.value if d.status else None,
            "owner_alias": d.decided_by,
            "_name": d.title,
            "_linked_workstream_ids": (d.workstream_id,) if d.workstream_id else (),
        }

    for a in assumptions:
        snap[f"assumption/{a.id}"] = {
            "status": a.status.value if a.status else None,
            "owner_alias": a.owner_alias,
            "_name": getattr(a, "text", None) or getattr(a, "id", a.id),
            "_linked_workstream_ids": (),
        }

    return snap


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def append_plane1_changes(
    program_id: str,
    changes: list[Plane1ChangeRecord],
    *,
    programs_root: Path,
) -> None:
    """Append change records to the append-only Plane 1 changelog (atomic write)."""
    if not changes:
        return
    changelog_path = _changelog_path(program_id, programs_root)
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_append_jsonl(
        changelog_path,
        [rec.to_json() for rec in changes],
    )


def shadow_write_plane1_snapshot(
    program_id: str,
    snapshot: dict[str, dict[str, Any]],
    *,
    recorded_at: datetime,
    home_root: Path | None = None,
    db_root: Path | None = None,
) -> None:
    store = ProgramFactStore(program_id, home_root=home_root, db_root=db_root)
    for entity_key, fields in snapshot.items():
        if "/" not in entity_key:
            continue
        entity_type, entity_id = entity_key.split("/", 1)
        entity_ref = f"{entity_type}:{entity_id}"
        entity_name = fields.get("_name")
        linked_workstream_ids = [str(value) for value in (fields.get("_linked_workstream_ids") or ())]
        for field, value in fields.items():
            if field.startswith("_"):
                continue
            store.append_fact(
                ProgramFactInput(
                    fact_type=f"plane1.{entity_type}.{field}",
                    scope="program",
                    entity_refs=(entity_ref,),
                    payload={
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "entity_name": entity_name,
                        "field": field,
                        "value": value,
                        "linked_workstream_ids": linked_workstream_ids,
                    },
                    precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
                    created_by="vertex.gather",
                ),
                recorded_at=recorded_at,
            )


def load_plane1_changes(
    program_id: str,
    *,
    programs_root: Path,
    since: datetime | None = None,
) -> list[Plane1ChangeRecord]:
    """
    Load all Plane1ChangeRecord from the changelog, optionally filtered to
    records after `since` UTC datetime.
    """
    path = _changelog_path(program_id, programs_root)
    if not path.exists():
        return []

    records: list[Plane1ChangeRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = parse_jsonl_line(line)
            except json.JSONDecodeError:
                continue
            if d.get("record_type") != "plane1_change":
                continue
            rec = Plane1ChangeRecord.from_json(d)
            if since is not None and rec.ts <= since:
                continue
            records.append(rec)
    return records


def write_plane1_last_seen(
    program_id: str,
    snapshot: dict[str, dict[str, Any]],
    *,
    programs_root: Path,
) -> None:
    """Overwrite the mutable last-seen snapshot (Plane 3, regenerated each gather)."""
    path = _last_seen_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "program_id": program_id,
        "snapshot": snapshot,
        "written_at": _normalize_datetime(datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
    }
    _atomic_write_json(path, payload)


def load_plane1_last_seen(
    program_id: str,
    *,
    programs_root: Path,
) -> dict[str, dict[str, Any]] | None:
    """Load the last-seen snapshot, returning None if it doesn't exist."""
    path = _last_seen_path(program_id, programs_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload.get("snapshot")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _changelog_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "changelog" / "plane1_changes.jsonl"


def _last_seen_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "changelog" / "plane1_last_seen.json"


_TRACKED_MILESTONE_FIELDS = {"status", "target_date", "owner_alias"}
_TRACKED_RISK_FIELDS = {"status", "probability", "impact", "owner_alias", "mitigation_due_date"}
_TRACKED_WORKSTREAM_FIELDS = {"current_blocker", "dri_email"}
_TRACKED_DECISION_FIELDS = {"status", "owner_alias"}
_TRACKED_ASSUMPTION_FIELDS = {"status", "owner_alias"}

_FIELDS_BY_ENTITY = {
    "milestone": _TRACKED_MILESTONE_FIELDS,
    "risk": _TRACKED_RISK_FIELDS,
    "workstream": _TRACKED_WORKSTREAM_FIELDS,
    "decision": _TRACKED_DECISION_FIELDS,
    "assumption": _TRACKED_ASSUMPTION_FIELDS,
}


def _entity_name(entity_type: str, entity_id: str, snapshot: dict[str, dict[str, Any]]) -> str:
    key = f"{entity_type}/{entity_id}"
    return snapshot.get(key, {}).get("_name", entity_id)


def _linked_workstream_ids(entity_type: str, entity_id: str, snapshot: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    key = f"{entity_type}/{entity_id}"
    raw = snapshot.get(key, {}).get("_linked_workstream_ids", ())
    if isinstance(raw, (list, tuple)):
        return tuple(str(v) for v in raw)
    return ()


def _classify_change(
    entity_type: str,
    field: str,
    prior: Any,
    current: Any,
) -> str:
    if prior is None:
        return "entity_added"
    if current is None:
        return "entity_removed"
    if field in ("status", "probability", "impact"):
        return "status_change"
    if field in ("target_date", "mitigation_due_date"):
        return "date_change"
    if field in ("owner_alias", "dri_email"):
        return "owner_change"
    return "field_change"


def _serialize(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _normalize_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _atomic_append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Append a list of JSON objects to a JSONL file atomically."""
    temp_path = path.with_suffix(path.suffix + ".tmp")
    # Read existing content so we can append
    existing_lines: list[str] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    existing_lines.append(line)
    with temp_path.open("w", encoding="utf-8") as fh:
        for line in existing_lines:
            fh.write(line)
            fh.write("\n")
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True))
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp_path, path)


def plane1_change_to_signal(
    change: Plane1ChangeRecord,
    *,
    program_id: str,
) -> Signal:
    """
    Convert a Plane1ChangeRecord to a synthetic Signal for the evidence pipeline.

    Implements §22 E3 of the program-context-maturity spec.

    Routing:
      - Milestone signals → workstreams in change.linked_workstream_ids
      - Risk signals → workstreams in change.linked_workstream_ids
      - Workstream signals → that workstream directly
      - Decision/Assumption signals → None (cross-cutting)
    """
    src = f"plane1/{change.entity_type}_{change.field}"
    ws_id = change.linked_workstream_ids[0] if change.linked_workstream_ids else None

    # Build signal text from template
    text = _format_change_text(change)

    metadata: dict[str, Any] = {
        "prior": change.prior,
        "current": change.current,
        "entity_type": change.entity_type,
        "entity_id": change.entity_id,
        "change_kind": change.kind,
    }

    entity_refs: tuple[str, ...] = ()
    if change.entity_type == "milestone":
        entity_refs = (f"MILESTONE:{change.entity_id}",)
    elif change.entity_type == "risk":
        entity_refs = (f"RISK:{change.entity_id}",)
    elif change.entity_type == "workstream":
        entity_refs = (f"WS:{change.entity_id}",)
    elif change.entity_type == "decision":
        entity_refs = (f"DECISION:{change.entity_id}",)
    elif change.entity_type == "assumption":
        entity_refs = (f"ASSUMPTION:{change.entity_id}",)

    return Signal(
        id=f"plane1-change-{change.ts.strftime('%Y%m%dT%H%M%S')}-{change.entity_type}-{change.entity_id}-{change.field}",
        timestamp=change.ts,
        source=src,
        program_id=program_id,
        workstream_id=ws_id,
        entity_refs=entity_refs,
        text=text,
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata=metadata,
        thread_id=None,
        review_policy=ReviewPolicy.AUTO_APPROVED,  # §22 E3: bypass review queue
    )


def _format_change_text(change: Plane1ChangeRecord) -> str:
    """Format a human-readable text description of a Plane 1 change."""
    name = change.entity_name or change.entity_id
    kind = change.kind
    prior = change.prior or "(none)"
    current = change.current or "(none)"

    if change.entity_type == "milestone":
        if change.field == "status":
            return f'Milestone "{name}" ({change.entity_id}) status changed from {prior} to {current}.'
        elif change.field == "target_date":
            return f'Milestone "{name}" ({change.entity_id}) target date moved from {prior} to {current}.'
        elif change.field == "owner_alias":
            return f'Milestone "{name}" ({change.entity_id}) owner changed from {prior} to {current}.'

    elif change.entity_type == "risk":
        if change.field == "status":
            return f'Risk "{name}" ({change.entity_id}) status changed to {current}.'
        elif change.field == "probability":
            return f'Risk "{name}" ({change.entity_id}) probability changed from {prior} to {current}.'
        elif change.field == "impact":
            return f'Risk "{name}" ({change.entity_id}) impact changed from {prior} to {current}.'
        elif change.field == "owner_alias":
            return f'Risk "{name}" ({change.entity_id}) owner changed from {prior} to {current}.'

    elif change.entity_type == "workstream":
        if change.field == "current_blocker":
            if change.current:
                return f'Workstream "{name}" current blocker updated: "{current}"'
            else:
                return f'Workstream "{name}" blocker cleared (was: "{prior}").'
        elif change.field == "dri_email":
            return f'Workstream "{name}" DRI email changed from {prior} to {current}.'

    elif change.entity_type == "decision":
        if change.field == "status":
            return f'Decision "{name}" ({change.entity_id}) status changed to {current}.'
        elif change.field == "owner_alias":
            return f'Decision "{name}" ({change.entity_id}) owner changed from {prior} to {current}.'

    elif change.entity_type == "assumption":
        if change.field == "status":
            return f'Assumption "{name}" ({change.entity_id}) status changed to {current}.'
        elif change.field == "owner_alias":
            return f'Assumption "{name}" ({change.entity_id}) owner changed from {prior} to {current}.'

    # Generic fallback
    return f'{change.entity_type.title()} "{name}" ({change.entity_id}) {change.field} changed from {prior} to {current}.'


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp_path, path)