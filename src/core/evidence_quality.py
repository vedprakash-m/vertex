"""Evidence quality tracking — per-gather-run metrics per lane (ME-05, BL-44)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from src.core.jsonl_utils import append_jsonl_line

_EVIDENCE_QUALITY_FILENAME = "evidence_quality.jsonl"
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


@dataclass(frozen=True, slots=True)
class EvidenceQualityRecord:
    """One record per lane per gather run."""
    run_at: datetime         # ISO-8601 UTC timestamp
    lane_id: str
    confidence: float        # 0.0 = placeholder, >0 = AI-extracted
    etas_found: int
    owners_found: int
    blocking_found: int
    body_text_chars: int     # chars of body_text available for extraction
    source_type: str         # "transcript" | "workiq_email" | "local_kb" | "placeholder"
    extractor: str           # "ContentExtractionAgent" | "placeholder" | "manual"


def _quality_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "journal" / _EVIDENCE_QUALITY_FILENAME


def record_evidence_quality(
    rec: EvidenceQualityRecord,
    *,
    program_id: str,
    programs_root: Path,
) -> None:
    """Append one EvidenceQualityRecord to evidence_quality.jsonl."""
    path = _quality_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(rec)
    data["run_at"] = rec.run_at.isoformat()
    append_jsonl_line(path, json.dumps(data, ensure_ascii=False) + "\n", max_bytes=_MAX_FILE_BYTES)


def load_evidence_quality(
    program_id: str,
    *,
    programs_root: Path,
    lane_id: str | None = None,
    since: datetime | None = None,
) -> list[EvidenceQualityRecord]:
    """Load EvidenceQualityRecord entries, optionally filtered."""
    path = _quality_path(program_id, programs_root)
    if not path.exists():
        return []
    results = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            rec = EvidenceQualityRecord(
                run_at=datetime.fromisoformat(d["run_at"]),
                lane_id=d["lane_id"],
                confidence=float(d["confidence"]),
                etas_found=int(d["etas_found"]),
                owners_found=int(d["owners_found"]),
                blocking_found=int(d["blocking_found"]),
                body_text_chars=int(d["body_text_chars"]),
                source_type=d["source_type"],
                extractor=d["extractor"],
            )
        except (KeyError, ValueError, TypeError):
            continue
        if lane_id is not None and rec.lane_id != lane_id:
            continue
        if since is not None and rec.run_at < since:
            continue
        results.append(rec)
    return results
