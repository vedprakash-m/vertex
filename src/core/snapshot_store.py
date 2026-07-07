from __future__ import annotations

from contextlib import nullcontext
import json
import os
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
import shutil
from typing import Any

from src.core.baseline_lock import assert_issue_unlocked
from src.core.edition_resolver import resolve_edition_paths
from src.core.exceptions import StateError
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem


REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = REPO_ROOT / "archive"
LOCK_MAX_AGE = timedelta(minutes=30)


class ArchiveLock:
    def __init__(self, archive_root: Path) -> None:
        self._archive_root = archive_root
        self._lock_path = archive_root / ".lock"
        self._owned = False

    def __enter__(self) -> ArchiveLock:
        self._archive_root.mkdir(parents=True, exist_ok=True)
        if self._lock_path.exists():
            existing = _read_lock_metadata(self._lock_path)
            started_at = _parse_datetime(existing.get("started_at"))
            if started_at is not None and datetime.now(timezone.utc) - started_at < LOCK_MAX_AGE:
                raise StateError(
                    f"Archive is locked by PID {existing.get('pid', 'unknown')} at {self._lock_path}"
                )
            self._lock_path.unlink()

        metadata = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._lock_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self._owned = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._owned and self._lock_path.exists():
            self._lock_path.unlink()
        self._owned = False


def get_archive_root(edition: str, archive_root: Path = ARCHIVE_ROOT) -> Path:
    resolved_paths = resolve_edition_paths(
        edition,
        programs_root=archive_root.parent / "programs",
    )
    if resolved_paths is not None:
        return resolved_paths.archive_dir
    return archive_root / edition


def find_orphaned_staging(edition: str, archive_root: Path = ARCHIVE_ROOT) -> Path | None:
    staging_root = get_archive_root(edition, archive_root) / "staging"
    if not staging_root.exists():
        return None
    if any(staging_root.rglob("*")):
        return staging_root
    return None


def write_confirmed(
    edition: str,
    issue_number: int,
    snapshot: Snapshot,
    archive_root: Path = ARCHIVE_ROOT,
    promote: bool = True,
    acquire_lock: bool = True,
) -> Path:
    edition_root = get_archive_root(edition, archive_root)
    staging_root = edition_root / "staging"
    final_snapshot_path = edition_root / "snapshots" / _snapshot_filename(issue_number)
    staged_snapshot_path = staging_root / "snapshots" / _snapshot_filename(issue_number)
    # Hardlock: never overwrite the confirmed snapshot of a trusted/locked baseline issue.
    assert_issue_unlocked(issue_number, target_path=final_snapshot_path, artifact="confirmed snapshot")

    lock_context = ArchiveLock(edition_root) if acquire_lock else nullcontext()
    with lock_context:
        orphaned_staging = find_orphaned_staging(edition, archive_root)
        if orphaned_staging is not None:
            raise StateError(
                f"Incomplete confirm detected at {orphaned_staging}. Resolve staging before writing a new snapshot."
            )

        try:
            _write_atomic_json(staged_snapshot_path, _to_jsonable(snapshot))
            if not promote:
                return staged_snapshot_path
            final_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_snapshot_path, final_snapshot_path)
            shutil.rmtree(staging_root, ignore_errors=True)
            return final_snapshot_path
        except Exception:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise


def read_snapshot(path: Path) -> Snapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Snapshot(
        issue_number=int(payload["issue_number"]),
        generated_at=_require_datetime(payload["generated_at"], field_name="generated_at"),
        ado_data_as_of=_require_datetime(payload["ado_data_as_of"], field_name="ado_data_as_of"),
        edition_type=EditionType.from_string(payload["edition_type"]),
        items=tuple(
            SnapshotItem(
                id=int(item["id"]),
                type=str(item["type"]),
                title=str(item["title"]),
                state=str(item["state"]),
                assigned_to=item.get("assigned_to"),
                area_path=str(item["area_path"]),
                target_date=_parse_date(item.get("target_date")),
                risk_level=RiskLevel.from_string(item["risk_level"]),
                tags=list(item.get("tags", [])),
            )
            for item in payload.get("items", [])
        ),
        scorecards=tuple(
            ConfirmedDimension(
                scorecard_name=str(dimension["scorecard_name"]),
                name=str(dimension["name"]),
                risk=RiskLevel.from_string(dimension["risk"]),
                prior_risk=(
                    RiskLevel.from_string(dimension["prior_risk"])
                    if dimension.get("prior_risk") is not None
                    else None
                ),
                item_count=int(dimension["item_count"]),
                ado_query_url=str(dimension["ado_query_url"]),
            )
            for dimension in payload.get("scorecards", [])
        ),
        schema_version=str(payload.get("schema_version", "1.0")),
    )


def _snapshot_filename(issue_number: int) -> str:
    return f"issue_{issue_number:03d}.snapshot.json"


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _to_jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _require_aware_datetime(value, field_name="snapshot datetimes").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value


def _read_lock_metadata(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _require_datetime(value: Any, *, field_name: str) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        if isinstance(value, str):
            try:
                parsed_value = datetime.fromisoformat(value)
            except ValueError:
                parsed_value = None
            if parsed_value is not None and parsed_value.tzinfo is None:
                raise StateError(f"{field_name} must include timezone information")
        raise StateError(f"Invalid datetime value in snapshot: {value!r}")
    return parsed


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(timezone.utc)


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"Invalid date value in snapshot: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise StateError(f"Invalid date value in snapshot: {value!r}") from error
