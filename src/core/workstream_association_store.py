from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
from typing import Any

import portalocker


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"


@dataclass(frozen=True, slots=True)
class WorkstreamAssociationRecord:
    recorded_at: datetime
    edition: str
    issue_number: int
    workstream_id: str
    source_type: str
    source_slice_id: str | None = None
    section_id: str | None = None
    work_item_id: int | None = None
    note: str | None = None


def get_workstream_association_log_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "workstream_associations.jsonl"


def append_workstream_association_records(
    program_id: str,
    records: tuple[WorkstreamAssociationRecord, ...],
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    target = get_workstream_association_log_path(program_id, programs_root)
    for record in records:
        _append_jsonl(target, _record_to_dict(record))
    _mirror_records_to_fact_store(program_id, records, programs_root=programs_root)
    return target


def _mirror_records_to_fact_store(
    program_id: str,
    records: tuple[WorkstreamAssociationRecord, ...],
    *,
    programs_root: Path,
) -> None:
    """Append a ``workstream.association`` fact revision for each record (spec §22 Step 6).

    The JSONL append above remains the legacy source-of-truth; the fact
    revision is a parallel SoR projection.  ``append_workstream_association_fact``
    is idempotent on re-call with the same natural key, so this is a safe
    no-op for repeated confirms on the same issue and the legacy parity
    check still passes.

    The import is deferred to break the
    ``workstream_association_store <-> program_fact_store`` import cycle;
    ``program_fact_store`` imports ``WorkstreamAssociationRecord`` and
    ``read_workstream_association_records`` at module load for the
    legacy-read shim, so a top-level import here would deadlock.
    """
    from src.core.program_fact_store import append_workstream_association_fact  # noqa: PLC0415

    for record in records:
        append_workstream_association_fact(
            program_id,
            record,
            programs_root=programs_root,
        )


def read_workstream_association_records(
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[WorkstreamAssociationRecord, ...]:
    path = get_workstream_association_log_path(program_id, programs_root)
    if not path.exists():
        return ()
    return tuple(record_from_dict(entry) for entry in _read_jsonl(path))


def record_from_dict(record: dict[str, Any]) -> WorkstreamAssociationRecord:
    return WorkstreamAssociationRecord(
        recorded_at=datetime.fromisoformat(_required_string(record["recorded_at"], field_name="recorded_at")),
        edition=_required_string(record["edition"], field_name="edition"),
        issue_number=_required_int(record["issue_number"], field_name="issue_number"),
        workstream_id=_required_string(record["workstream_id"], field_name="workstream_id"),
        source_type=_required_string(record["source_type"], field_name="source_type"),
        source_slice_id=_optional_string(record.get("source_slice_id"), field_name="source_slice_id"),
        section_id=_optional_string(record.get("section_id"), field_name="section_id"),
        work_item_id=_optional_int(record.get("work_item_id"), field_name="work_item_id"),
        note=_optional_string(record.get("note"), field_name="note"),
    )


def _record_to_dict(record: WorkstreamAssociationRecord) -> dict[str, Any]:
    return {
        "recorded_at": record.recorded_at.isoformat(),
        "edition": record.edition,
        "issue_number": record.issue_number,
        "workstream_id": record.workstream_id,
        "source_type": record.source_type,
        "source_slice_id": record.source_slice_id,
        "section_id": record.section_id,
        "work_item_id": record.work_item_id,
        "note": record.note,
    }


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + os.linesep
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LockFlags.EXCLUSIVE)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        portalocker.unlock(handle)


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed = parse_jsonl_line(stripped)
        if not isinstance(parsed, dict):
            raise TypeError("workstream association rows must be JSON objects")
        entries.append(parsed)
    return tuple(entries)


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value