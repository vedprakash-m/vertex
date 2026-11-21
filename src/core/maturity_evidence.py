"""Append-only evidence for proposal-class maturity transitions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records


_SCHEMA_VERSION = "1"
_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class MaturityTransitionEvidence:
    program_id: str
    proposal_class: str
    from_level: str
    to_level: str
    action: str
    reason: str
    evaluated_at: datetime
    counters: dict[str, int]
    ceiling: str
    approval_ref: str | None = None
    independent_review_ref: str | None = None
    rollback_ref: str | None = None
    schema_version: str = _SCHEMA_VERSION

    def payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.astimezone(timezone.utc).isoformat()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["content_sha256"] = hashlib.sha256(body).hexdigest()
        return payload


def maturity_transitions_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "journal" / "maturity_transitions.jsonl"


def append_maturity_transition(
    evidence: MaturityTransitionEvidence,
    *,
    programs_root: Path,
) -> Path:
    path = maturity_transitions_path(evidence.program_id, programs_root=programs_root)
    append_jsonl_line(
        path,
        json.dumps(evidence.payload(), sort_keys=True) + "\n",
        max_bytes=_MAX_BYTES,
    )
    return path


def read_maturity_transitions(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[dict[str, Any], ...]:
    rows = []
    for row in read_jsonl_records(
        maturity_transitions_path(program_id, programs_root=programs_root),
        repair_corruption=False,
    ):
        if row.get("program_id") != program_id:
            raise ValueError("Maturity transition evidence contains a foreign program.")
        stored = row.pop("content_sha256", None)
        canonical = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if stored != hashlib.sha256(canonical).hexdigest():
            raise ValueError("Maturity transition evidence has an invalid digest.")
        rows.append({**row, "content_sha256": stored})
    return tuple(rows)


__all__ = [
    "MaturityTransitionEvidence",
    "append_maturity_transition",
    "maturity_transitions_path",
    "read_maturity_transitions",
]
