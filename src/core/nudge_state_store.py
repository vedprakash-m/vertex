from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import portalocker

from src.core.exceptions import ConfigError, StateError
from src.core.nudge_models import (
    NUDGE_STATE_LOCK_TIMEOUT_SECONDS,
    NUDGE_STATE_SCHEMA_VERSION,
    NUDGE_STATE_SCHEMA_VERSION_V12,
)


@dataclass(frozen=True, slots=True)
class NudgeStateEntry:
    work_item_id: int
    nudged_at: datetime
    # D-5: schema 1.2 provenance fields; None = legacy_unknown origin
    origin: str | None = None   # "generated" | "mark_sent" | "import_sent" | "legacy_unknown"
    run_id: str | None = None


def load_nudge_state(path: Path) -> tuple[NudgeStateEntry, ...]:
    """Read nudge state. Accepts legacy bare keys and canonical item: keys.

    D-5: Schema 1.2 dict values carry triggered_at/origin/run_id; bare string
    values are treated as legacy_unknown origin (no in-place rewrite).
    """
    payload = _load_payload(path)
    # Collect latest-timestamp entry per item_id
    best: dict[int, NudgeStateEntry] = {}
    for raw_key, raw_val in payload.items():
        if raw_key == "schema_version":
            continue
        item_id = _parse_item_key(raw_key)
        if item_id is None:
            continue

        # D-5: schema 1.2 dict shape: {triggered_at, origin, run_id}
        if isinstance(raw_val, dict):
            ts_str = str(raw_val.get("triggered_at") or "")
            ts = _parse_datetime(ts_str)
            if ts is None:
                raise ConfigError(
                    f"Invalid nudge triggered_at {ts_str!r} for work item {item_id} in {path}"
                )
            origin = str(raw_val.get("origin") or "legacy_unknown")
            run_id_val = raw_val.get("run_id")
            run_id = str(run_id_val) if run_id_val is not None else None
        else:
            # Schema 1.1 bare ISO string — treat as legacy_unknown
            ts = _parse_datetime(raw_val)
            if ts is None:
                raise ConfigError(
                    f"Invalid nudge timestamp {raw_val!r} for work item {item_id} in {path}"
                )
            origin = "legacy_unknown"
            run_id = None

        entry = NudgeStateEntry(work_item_id=item_id, nudged_at=ts, origin=origin, run_id=run_id)
        if item_id not in best or ts > best[item_id].nudged_at:
            best[item_id] = entry

    entries = sorted(best.values(), key=lambda e: e.work_item_id)
    return tuple(entries)


def record_nudge_state(
    path: Path,
    *,
    item_ids: Iterable[int],
    cooldown_keys: Iterable[str] = (),
    triggered_at: datetime,
    origin: str = "generated",
    run_id: str | None = None,
) -> Path:
    """Backward-compat wrapper; writes canonical item: keys. No pruning."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(
            str(lock_path),
            mode="a+",
            timeout=NUDGE_STATE_LOCK_TIMEOUT_SECONDS,
            encoding="utf-8",
        ):
            payload = _load_payload(path)
            payload["schema_version"] = NUDGE_STATE_SCHEMA_VERSION
            timestamp = _normalize_datetime(triggered_at).isoformat()
            record = {
                "triggered_at": timestamp,
                "origin": origin,
                "run_id": run_id,
            }
            for cooldown_key in cooldown_keys:
                normalized_key = str(cooldown_key).strip()
                if not normalized_key:
                    continue
                payload[normalized_key] = dict(record)
            for item_id in item_ids:
                canonical_key = f"item:{int(item_id)}"
                # Keep latest — preserve existing bare key if newer
                bare_key = str(int(item_id))
                existing_ts: datetime | None = None
                for k in (canonical_key, bare_key):
                    if k in payload:
                        t = _parse_datetime(payload[k])
                        if t is not None and (existing_ts is None or t > existing_ts):
                            existing_ts = t
                new_ts = _normalize_datetime(triggered_at)
                if existing_ts is None or new_ts >= existing_ts:
                    payload[canonical_key] = dict(record)
                    # Remove legacy bare key to avoid duplication
                    payload.pop(bare_key, None)
            _atomic_write(path, payload)
    except portalocker.exceptions.LockException as exc:
        raise StateError(
            f"Could not acquire nudge state lock {lock_path} within {NUDGE_STATE_LOCK_TIMEOUT_SECONDS}s. "
            "Another process may be writing nudge state. Retry after the other process completes."
        ) from exc
    return path


def update_nudge_state(
    path: Path,
    *,
    item_ids: Iterable[int],
    triggered_at: datetime,
    prune_before: datetime,
    origin: str = "generated",
    run_id: str | None = None,
) -> Path:
    """Atomic update with pruning. Creates canonical item: keys; preserves other namespaces."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with portalocker.Lock(
            str(lock_path),
            mode="a+",
            timeout=NUDGE_STATE_LOCK_TIMEOUT_SECONDS,
            encoding="utf-8",
        ):
            payload = _load_payload(path)
            payload["schema_version"] = NUDGE_STATE_SCHEMA_VERSION
            prune_before_utc = _normalize_datetime(prune_before)
            timestamp = _normalize_datetime(triggered_at).isoformat()
            record = {
                "triggered_at": timestamp,
                "origin": origin,
                "run_id": run_id,
            }

            # Prune old bare/item records (strict <)
            keys_to_delete: list[str] = []
            for key in list(payload.keys()):
                if key == "schema_version":
                    continue
                item_id = _parse_item_key(key)
                if item_id is None:
                    continue  # preserve non-item namespaced keys
                ts = _parse_datetime(payload[key])
                if ts is not None and ts < prune_before_utc:
                    keys_to_delete.append(key)
            for k in keys_to_delete:
                del payload[k]

            # Merge new item IDs (canonical item: keys)
            new_ts_utc = _normalize_datetime(triggered_at)
            for item_id in item_ids:
                canonical_key = f"item:{int(item_id)}"
                bare_key = str(int(item_id))
                # Determine existing latest timestamp
                existing_ts: datetime | None = None
                for k in (canonical_key, bare_key):
                    if k in payload:
                        t = _parse_datetime(payload[k])
                        if t is not None and (existing_ts is None or t > existing_ts):
                            existing_ts = t
                if existing_ts is None or new_ts_utc >= existing_ts:
                    payload[canonical_key] = dict(record)
                    payload.pop(bare_key, None)

            _atomic_write(path, payload)
    except portalocker.exceptions.LockException as exc:
        raise StateError(
            f"Could not acquire nudge state lock {lock_path} within {NUDGE_STATE_LOCK_TIMEOUT_SECONDS}s."
        ) from exc
    return path


def reset_nudge_item_state(path: Path, *, confirmed: bool) -> int:
    """Preview (confirmed=False) or confirm removal of all bare/item records."""
    if not path.exists():
        return 0

    payload = _load_payload(path)
    item_keys: list[str] = [
        k for k in payload
        if k != "schema_version" and _parse_item_key(k) is not None
    ]
    # Count unique item IDs
    unique_ids: set[int] = set()
    for k in item_keys:
        item_id = _parse_item_key(k)
        if item_id is not None:
            unique_ids.add(item_id)
    count = len(unique_ids)

    if not confirmed:
        return count

    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with portalocker.Lock(
            str(lock_path),
            mode="a+",
            timeout=NUDGE_STATE_LOCK_TIMEOUT_SECONDS,
            encoding="utf-8",
        ):
            # Reload inside lock
            payload = _load_payload(path)
            before = dict(payload)
            to_remove = [
                k for k in payload
                if k != "schema_version" and _parse_item_key(k) is not None
            ]
            for k in to_remove:
                del payload[k]
            payload["schema_version"] = NUDGE_STATE_SCHEMA_VERSION
            if payload != before:
                _atomic_write(path, payload)
    except portalocker.exceptions.LockException as exc:
        raise StateError(
            f"Could not acquire nudge state lock {lock_path} within {NUDGE_STATE_LOCK_TIMEOUT_SECONDS}s."
        ) from exc
    return count


def compute_prune_before(*, generated_at: datetime, max_cooldown_days: int) -> datetime:
    """Compute the prune_before threshold per spec §9.4."""
    retention = max(7, 2 * max_cooldown_days)
    return _normalize_datetime(generated_at) - timedelta(days=retention)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_payload(path: Path) -> dict[str, Any]:
    """Load nudge state payload; accepts schema 1.1 (str values) and 1.2 (dict values).

    D-5 read-time reinterpretation: dict values carry triggered_at/origin/run_id;
    bare string values are treated as legacy_unknown origin on parse.  No in-place rewrite.
    """
    if not path.exists():
        return {}
    try:
        raw_text = path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid nudge state JSON in {path}") from error
    if not isinstance(payload, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if key == "schema_version":
            if isinstance(value, str):
                major = value.split(".", 1)[0]
                if major != "1":
                    raise ConfigError(
                        f"Unsupported nudge state schema_version {value!r} in {path}"
                    )
            normalized[key] = str(value) if isinstance(value, str) else "1.1"
            continue
        # Schema 1.2: dict values with triggered_at/origin/run_id
        if isinstance(value, dict):
            normalized[str(key)] = value
        elif isinstance(value, str):
            normalized[str(key)] = value
        else:
            raise ConfigError(
                f"Invalid nudge state timestamp {value!r} for key {key!r} in {path}"
            )
    return normalized


def _parse_item_key(key: str) -> int | None:
    """Parse bare numeric or item:<id> keys. Returns None for non-item keys."""
    if key == "schema_version":
        return None
    if key.startswith("item:"):
        suffix = key[5:]
        try:
            v = int(suffix)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    # Bare numeric key
    try:
        v = int(key)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    # D-5: schema 1.2 dict values carry triggered_at; extract it for comparison
    if isinstance(value, dict):
        value = value.get("triggered_at") or ""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    pid = os.getpid()
    unique = uuid.uuid4().hex[:8]
    temp_path = path.parent / f".nudge_state_{pid}_{unique}.tmp"
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
