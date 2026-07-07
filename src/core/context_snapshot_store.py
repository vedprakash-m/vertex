"""
Context snapshot store for confirmed issues.

Implements §22 E2 of the program-context-maturity spec.

File location:
  programs/<prog>/archive/<edition>/context_snapshots/issue_NNN.context.json

These are read-only forensic records. They do not support rollback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from src.core.models_v2 import (
    Assumption,
    DecisionEntry,
    Milestone,
    RiskEntry,
    Workstream,
)


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    schema_version: str
    issue_number: int
    edition: str
    program_id: str
    confirmed_at: datetime
    milestones: tuple[dict[str, Any], ...]
    risks: tuple[dict[str, Any], ...]
    workstreams: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    plane1_change_count_since_prior: int
    context_maturity_level: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "issue_number": self.issue_number,
            "edition": self.edition,
            "program_id": self.program_id,
            "confirmed_at": _normalize_datetime(self.confirmed_at).isoformat().replace("+00:00", "Z"),
            "milestones": list(self.milestones),
            "risks": list(self.risks),
            "workstreams": list(self.workstreams),
            "decisions": list(self.decisions),
            "plane1_change_count_since_prior": self.plane1_change_count_since_prior,
            "context_maturity_level": self.context_maturity_level,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> ContextSnapshot:
        return ContextSnapshot(
            schema_version=str(d.get("schema_version", "1.0")),
            issue_number=int(d["issue_number"]),
            edition=str(d["edition"]),
            program_id=str(d["program_id"]),
            confirmed_at=_parse_datetime(d["confirmed_at"]),
            milestones=tuple(d.get("milestones") or []),
            risks=tuple(d.get("risks") or []),
            workstreams=tuple(d.get("workstreams") or []),
            decisions=tuple(d.get("decisions") or []),
            plane1_change_count_since_prior=int(d.get("plane1_change_count_since_prior", 0)),
            context_maturity_level=int(d.get("context_maturity_level", 0)),
        )


def write_context_snapshot(
    program_id: str,
    edition_id: str,
    issue_number: int,
    milestones: list[Milestone],
    risks: list[RiskEntry],
    workstreams: list[Workstream],
    decisions: list[DecisionEntry],
    confirmed_at: datetime,
    plane1_change_count_since_prior: int,
    *,
    archive_root: Path,
    context_maturity_level: int = 0,
) -> Path:
    """Write a context snapshot for a confirmed issue."""
    snapshot_dir = archive_root / program_id / "archive" / edition_id / "context_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"issue_{issue_number:03d}.context.json"

    records_milestones = [
        {
            "id": m.id,
            "name": m.name,
            "status": m.status.value if m.status else None,
            "target_date": m.target_date.isoformat() if m.target_date else None,
            "owner_alias": m.owner_alias,
        }
        for m in milestones
    ]

    records_risks = [
        {
            "id": r.id,
            "title": r.title,
            "status": r.status.value if r.status else None,
            "probability": r.probability.value if r.probability else None,
            "impact": r.impact.value if r.impact else None,
            "owner_alias": r.owner_alias,
        }
        for r in risks
    ]

    records_workstreams = [
        {
            "id": ws.id,
            "name": ws.name,
            "current_blocker": ws.current_blocker,
            "dri_email": ws.dri_email,
        }
        for ws in workstreams
    ]

    records_decisions = [
        {
            "id": d.id,
            "title": d.title,
            "decided_by": d.decided_by,
            "decision_date": d.decision_date.isoformat() if d.decision_date is not None else None,
            "status": d.status.value if d.status else None,
        }
        for d in decisions
    ]

    snapshot = ContextSnapshot(
        schema_version="1.1",
        issue_number=issue_number,
        edition=edition_id,
        program_id=program_id,
        confirmed_at=confirmed_at,
        milestones=tuple(records_milestones),
        risks=tuple(records_risks),
        workstreams=tuple(records_workstreams),
        decisions=tuple(records_decisions),
        plane1_change_count_since_prior=plane1_change_count_since_prior,
        context_maturity_level=context_maturity_level,
    )

    _atomic_write_json(path, snapshot.to_json())
    return path


def load_context_snapshot(
    program_id: str,
    edition_id: str,
    issue_number: int,
    *,
    archive_root: Path,
) -> ContextSnapshot | None:
    """Load a context snapshot, returning None if it doesn't exist."""
    path = _snapshot_path(program_id, edition_id, issue_number, archive_root=archive_root)
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return ContextSnapshot.from_json(d)


def _snapshot_path(
    program_id: str,
    edition_id: str,
    issue_number: int,
    *,
    archive_root: Path,
) -> Path:
    return (
        archive_root
        / program_id
        / "archive"
        / edition_id
        / "context_snapshots"
        / f"issue_{issue_number:03d}.context.json"
    )


def _normalize_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    return _normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp_path, path)