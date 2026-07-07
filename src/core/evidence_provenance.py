"""Evidence provenance log: per-lane gather observability (BL-40).

Records where each piece of evidence came from after each gather run,
enabling debugging, auditing, and compliance tracing (INV-SG-1).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.jsonl_utils import append_jsonl_line


@dataclass(frozen=True, slots=True)
class EvidenceProvenanceRecord:
    """Audit record for one evidence-gather event on one lane."""
    lane_id: str
    run_at: str             # ISO datetime string
    source_type: str        # EvidenceSourceType value
    source_id: str | None   # email message ID, file path, transcript ID
    source_date: str | None # ISO date string of the source document
    confidence: float
    fields_populated: tuple[str, ...]
    operator: str           # "auto" for pipeline runs, alias for manual


def record_provenance(
    record: EvidenceProvenanceRecord,
    *,
    program_id: str,
    programs_root: Path,
) -> None:
    """Append one provenance record to the program's evidence_provenance.jsonl journal."""
    path = programs_root / program_id / "journal" / "evidence_provenance.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(record).items() if v is not None}
    # fields_populated is a tuple; convert to list for JSON
    if "fields_populated" in data:
        data["fields_populated"] = list(data["fields_populated"])
    append_jsonl_line(path, json.dumps(data) + "\n")


def make_provenance_record(
    *,
    lane_id: str,
    source_type: str,
    source_id: str | None,
    source_date: str | None,
    confidence: float,
    fields_populated: tuple[str, ...],
    operator: str = "auto",
    run_at: datetime | None = None,
) -> EvidenceProvenanceRecord:
    """Convenience constructor that fills run_at from current UTC time if not provided."""
    ts = (run_at or datetime.now(timezone.utc)).isoformat()
    return EvidenceProvenanceRecord(
        lane_id=lane_id,
        run_at=ts,
        source_type=source_type,
        source_id=source_id,
        source_date=source_date,
        confidence=confidence,
        fields_populated=fields_populated,
        operator=operator,
    )
