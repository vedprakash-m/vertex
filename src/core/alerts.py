"""WS-17: between-runs alerts (NG-3-respecting, no-daemon).

Design:
- When gather/confirm/doctor encounters a notable failure, it
  **appends a row** to ``programs/<id>/_alerts/alerts.jsonl``. The
  file is append-only and portalocker-guarded (PB-37).
- The NEXT run surfaces those rows on stdout at the top of the command
  output: ``vertex gather --edition <name>`` starts with the alert
  banner ``[!] N unresolved alerts since last run``.
- Resolved alerts get a ``resolved_at`` row appended (still append-only).
- The **surfactant** is the operator's CLI session, not a daemon — so
  NG-3 is upheld. No background process is started.
"""
from __future__ import annotations

import json
import os
import portalocker
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.core.exceptions import StateError


ALERTS_DIR_NAME = "_alerts"
ALERTS_FILENAME = "alerts.jsonl"


class AlertSeverity(str, object):
    """Stable alert severity ladder."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class AlertRecord:
    """A single alert row."""
    alert_id: str
    program_id: str
    severity: str
    category: str
    message: str
    next_command: str
    created_at: datetime
    resolved_at: datetime | None = None
    context: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "alert_id": self.alert_id,
            "program_id": self.program_id,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "next_command": self.next_command,
            "created_at": _iso(self.created_at),
            "resolved_at": _iso(self.resolved_at) if self.resolved_at is not None else None,
            "context": self.context or {},
        }


def _alerts_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / ALERTS_DIR_NAME / ALERTS_FILENAME


def append_alert(record: AlertRecord, *, programs_root: Path) -> Path:
    """Append one alert row. Portalocker + fsync for PB-37."""
    path = _alerts_path(record.program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)
    return path


def read_alerts(
    program_id: str,
    *,
    programs_root: Path,
    include_resolved: bool = False,
) -> tuple[AlertRecord, ...]:
    """Return all alerts. By default, only unresolved (no `resolved_at`).

    The file is append-only: the same ``alert_id`` may appear in
    multiple rows (e.g. open + resolution). We return the *last* row
    per ``alert_id`` so the caller sees the current state. When
    ``include_resolved=True``, every distinct alert_id's last row is
    included regardless of resolution status.
    """
    path = _alerts_path(program_id, programs_root)
    if not path.exists():
        return ()
    by_id: dict[str, AlertRecord] = {}
    order: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise StateError(f"Invalid alerts row: {line[:80]!r}: {error}") from error
            record = _record_from_payload(payload)
            if not record.alert_id:
                continue
            if record.alert_id not in by_id:
                order.append(record.alert_id)
            by_id[record.alert_id] = record
    out: list[AlertRecord] = []
    for alert_id in order:
        record = by_id[alert_id]
        if not include_resolved and record.resolved_at is not None:
            continue
        out.append(record)
    return tuple(out)


def resolve_alert(
    alert_id: str,
    *,
    program_id: str,
    programs_root: Path,
    now: datetime | None = None,
) -> bool:
    """Mark an alert resolved by appending a *resolution* row.

    Returns True if a matching open alert was found. The file remains
    append-only; the reader uses the *last* row per ``alert_id`` to
    decide resolved/unresolved state."""
    open_alerts = read_alerts(program_id, programs_root=programs_root, include_resolved=True)
    target = next((a for a in open_alerts if a.alert_id == alert_id and a.resolved_at is None), None)
    if target is None:
        return False
    resolution = AlertRecord(
        alert_id=target.alert_id,
        program_id=target.program_id,
        severity=target.severity,
        category=target.category,
        message=target.message,
        next_command=target.next_command,
        created_at=target.created_at,
        resolved_at=now or datetime.now(timezone.utc),
        context=target.context,
    )
    append_alert(resolution, programs_root=programs_root)
    return True


def surface_alert_banner(
    program_id: str,
    *,
    programs_root: Path,
) -> str | None:
    """Return a one-line banner string if there are unresolved alerts, or None.

    Call this at the start of gather/confirm/doctor sessions to surface
    alerts before the main pipeline output — consistent with NG-3 (no daemon).
    Returns None when there are no open alerts."""
    open_alerts = read_alerts(program_id, programs_root=programs_root, include_resolved=False)
    if not open_alerts:
        return None
    critical = sum(1 for a in open_alerts if a.severity == AlertSeverity.CRITICAL)
    errors = sum(1 for a in open_alerts if a.severity == AlertSeverity.ERROR)
    warns = sum(1 for a in open_alerts if a.severity == AlertSeverity.WARN)
    counts: list[str] = []
    if critical:
        counts.append(f"{critical} critical")
    if errors:
        counts.append(f"{errors} error")
    if warns:
        counts.append(f"{warns} warn")
    plural = "s" if len(open_alerts) != 1 else ""
    return (
        f"[!] {len(open_alerts)} unresolved alert{plural} for {program_id} "
        f"({', '.join(counts) or 'all info'}). "
        f"Run `vertex doctor --diagnose {program_id}` to inspect."
    )


def _record_from_payload(payload: dict[str, Any]) -> AlertRecord:
    if not isinstance(payload, dict):
        raise StateError(f"Invalid alert payload (not dict): {payload!r}")
    try:
        created_at = _parse_dt(payload.get("created_at"))
        if created_at is None:
            raise StateError("alert row missing created_at")
        return AlertRecord(
            alert_id=str(payload.get("alert_id") or ""),
            program_id=str(payload.get("program_id") or ""),
            severity=str(payload.get("severity") or AlertSeverity.INFO),
            category=str(payload.get("category") or ""),
            message=str(payload.get("message") or ""),
            next_command=str(payload.get("next_command") or ""),
            created_at=created_at,
            resolved_at=_parse_dt(payload.get("resolved_at")),
            context=payload.get("context") if isinstance(payload.get("context"), dict) else None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StateError(f"Invalid alert payload: {error}") from error


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
