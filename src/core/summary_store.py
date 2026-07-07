from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from src.core.journal import PROGRAMS_ROOT


_SUMMARY_HEADER_PREFIX = "<!-- vertex-summary "
_SUMMARY_HEADER_SUFFIX = " -->"


@dataclass(frozen=True, slots=True)
class RollingSummary:
    workstream_id: str
    generated_at: datetime
    prompt_version: str | None
    source_mode: str
    signal_count: int
    text: str


def get_summaries_dir(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "summaries"


def get_summary_path(program_id: str, workstream_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_summaries_dir(program_id, programs_root) / f"ws_{workstream_id}.md"


def load_summary(program_id: str, workstream_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> RollingSummary | None:
    path = get_summary_path(program_id, workstream_id, programs_root)
    if not path.exists():
        return None
    return load_summary_from_path(path)


def load_summary_from_path(path: Path) -> RollingSummary:
    raw_text = path.read_text(encoding="utf-8")
    first_line, separator, remainder = raw_text.partition("\n")
    if first_line.startswith(_SUMMARY_HEADER_PREFIX) and first_line.endswith(_SUMMARY_HEADER_SUFFIX):
        metadata = json.loads(first_line[len(_SUMMARY_HEADER_PREFIX) : -len(_SUMMARY_HEADER_SUFFIX)])
        if not isinstance(metadata, dict):
            raise ValueError(f"Summary metadata in {path} must be a JSON object.")
        body = remainder.lstrip("\n")
        return RollingSummary(
            workstream_id=str(metadata.get("workstream_id") or _workstream_id_from_path(path)),
            generated_at=_parse_datetime(metadata.get("generated_at")),
            prompt_version=_optional_string(metadata.get("prompt_version")),
            source_mode=str(metadata.get("source_mode") or "incremental"),
            signal_count=int(metadata.get("signal_count") or 0),
            text=body.strip(),
        )

    return RollingSummary(
        workstream_id=_workstream_id_from_path(path),
        generated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
        prompt_version=None,
        source_mode="legacy",
        signal_count=0,
        text=raw_text.strip(),
    )


def save_summary(program_id: str, summary: RollingSummary, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    path = get_summary_path(program_id, summary.workstream_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "generated_at": _require_utc(summary.generated_at).isoformat(),
        "prompt_version": summary.prompt_version,
        "signal_count": summary.signal_count,
        "source_mode": summary.source_mode,
        "workstream_id": summary.workstream_id,
    }
    payload = _SUMMARY_HEADER_PREFIX + json.dumps(metadata, ensure_ascii=True, sort_keys=True) + _SUMMARY_HEADER_SUFFIX
    body = summary.text.strip()
    if body:
        payload = f"{payload}\n\n{body}\n"
    else:
        payload = f"{payload}\n"
    path.write_text(payload, encoding="utf-8")
    return path


def summary_word_count(text: str) -> int:
    return len(tuple(part for part in text.split() if part.strip()))


def _workstream_id_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("ws_"):
        return stem[3:]
    return stem


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Summary metadata generated_at must be an ISO timestamp string.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Summary metadata generated_at must include timezone information.")
    return parsed.astimezone(timezone.utc)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Summary timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)