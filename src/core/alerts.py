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

ADF-W5.8 (specs/arch-data-fix.md Section 8.2.5 / Appendix A.3, 2026-07-13):
added entity scoping, cooldown/suppression, owner, and delivery to the
existing WS-17 mechanism rather than building a parallel one. Alert
identity is ``(program_id, category, entity_type, entity_id)`` per the
spec, not only ``(program_id, category)`` -- ``entity_scoped_alert_id``
computes it deterministically so repeated detections of the SAME
underlying condition correlate to the same ``alert_id`` (the existing
last-row-per-``alert_id`` read semantics already give "current state"
for free once the id is stable). New fields are additive with safe
defaults so every pre-existing caller (``platform_observability.py``,
``src/commands/observability.py``) is unaffected.
"""
from __future__ import annotations

import hashlib
import json
import os
import portalocker
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.adf_config import AlertsConfig
from src.core.exceptions import StateError


ALERTS_DIR_NAME = "_alerts"
ALERTS_FILENAME = "alerts.jsonl"


class AlertSeverity(str, object):
    """Stable alert severity ladder.

    Section 8.2.5 names ``info | warn | block``; this ladder predates that
    spec and already covers strictly more ground (``error``/``critical``
    both satisfy "block"-shaped severity -- a hard-stop condition). Not
    renamed: every existing caller constructs one of these four values and
    a rename would be a breaking change for zero behavioral gain.
    """

    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


def entity_scoped_alert_id(*, program_id: str, category: str, entity_type: str, entity_id: str) -> str:
    """Section 8.2.5: "Alert identity is (program_id, category, entity_type,
    entity_id), not only (program_id, category)." Deterministic so repeated
    detections of the same condition on the same entity always resolve to
    the same ``alert_id`` -- the existing last-row-per-id read semantics
    then give correct current-state aggregation for free."""
    joined = "|".join((program_id, category, entity_type, entity_id))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


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
    # ADF-W5.8 additive fields (Appendix A.3) -- all optional/defaulted so
    # every pre-existing construction call site is unaffected.
    entity_type: str | None = None
    entity_id: str | None = None
    owner: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    occurrence_count: int = 1
    suppressed_count: int = 0
    cooldown_minutes: int | None = None
    delivery: str | None = None

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
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "owner": self.owner,
            "first_seen": _iso(self.first_seen) if self.first_seen is not None else None,
            "last_seen": _iso(self.last_seen) if self.last_seen is not None else None,
            "occurrence_count": self.occurrence_count,
            "suppressed_count": self.suppressed_count,
            "cooldown_minutes": self.cooldown_minutes,
            "delivery": self.delivery,
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
        f"Run `vertex observability diagnose --program {program_id}` to inspect."
    )


def _record_from_payload(payload: dict[str, Any]) -> AlertRecord:
    if not isinstance(payload, dict):
        raise StateError(f"Invalid alert payload (not dict): {payload!r}")
    try:
        created_at = _parse_dt(payload.get("created_at"))
        if created_at is None:
            raise StateError("alert row missing created_at")
        occurrence_count = payload.get("occurrence_count")
        suppressed_count = payload.get("suppressed_count")
        cooldown_minutes = payload.get("cooldown_minutes")
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
            entity_type=payload.get("entity_type"),
            entity_id=payload.get("entity_id"),
            owner=payload.get("owner"),
            first_seen=_parse_dt(payload.get("first_seen")),
            last_seen=_parse_dt(payload.get("last_seen")),
            occurrence_count=int(occurrence_count) if isinstance(occurrence_count, (int, float)) else 1,
            suppressed_count=int(suppressed_count) if isinstance(suppressed_count, (int, float)) else 0,
            cooldown_minutes=int(cooldown_minutes) if isinstance(cooldown_minutes, (int, float)) else None,
            delivery=payload.get("delivery"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StateError(f"Invalid alert payload: {error}") from error


def append_or_suppress_alert(
    *,
    program_id: str,
    category: str,
    entity_type: str,
    entity_id: str,
    severity: str,
    message: str,
    next_command: str,
    programs_root: Path,
    context: dict[str, Any] | None = None,
    owner: str | None = None,
    delivery: str | None = None,
    cooldown_minutes: int | None = None,
    now: datetime | None = None,
    alerts_config: AlertsConfig | None = None,
) -> AlertRecord:
    """Section 8.2.5's entity-scoped detect-or-suppress path -- the single
    function a detector should call instead of ``append_alert`` directly
    when it wants cooldown/suppression/occurrence tracking for a specific
    ``(category, entity_type, entity_id)``. ``append_alert`` itself is left
    untouched for callers (e.g. ``platform_observability.py``) that manage
    their own dedup and do not need entity scoping.

    Behavior: looks up the existing open alert for this identity. If none
    exists, appends a fresh row (occurrence_count=1, suppressed_count=0).
    If one exists and we are still within its cooldown window, this
    occurrence is SUPPRESSED -- ``suppressed_count`` increments,
    ``last_seen`` advances, but the row's ``severity``/``message`` are not
    re-raised as a fresh notification. If one exists and the cooldown has
    elapsed, a fresh notification row is appended (``occurrence_count``
    increments; ``suppressed_count`` carries forward as a running total,
    it is a monitoring metric, not a per-window counter).
    """
    resolved_now = now or datetime.now(timezone.utc)
    alert_id = entity_scoped_alert_id(
        program_id=program_id, category=category, entity_type=entity_type, entity_id=entity_id
    )
    resolved_cooldown = cooldown_minutes
    if resolved_cooldown is None:
        resolved_cooldown = (alerts_config or AlertsConfig()).cooldown_minutes
    resolved_delivery = delivery
    if resolved_delivery is None:
        resolved_delivery = (alerts_config or AlertsConfig()).delivery.value

    existing = read_alerts(program_id, programs_root=programs_root, include_resolved=True)
    match = next((a for a in existing if a.alert_id == alert_id and a.resolved_at is None), None)

    if match is None:
        record = AlertRecord(
            alert_id=alert_id,
            program_id=program_id,
            severity=severity,
            category=category,
            message=message,
            next_command=next_command,
            created_at=resolved_now,
            context=context,
            entity_type=entity_type,
            entity_id=entity_id,
            owner=owner,
            first_seen=resolved_now,
            last_seen=resolved_now,
            occurrence_count=1,
            suppressed_count=0,
            cooldown_minutes=resolved_cooldown,
            delivery=resolved_delivery,
        )
        append_alert(record, programs_root=programs_root)
        return record

    last_seen = match.last_seen or match.created_at
    within_cooldown = resolved_cooldown is not None and resolved_now < last_seen + timedelta(minutes=resolved_cooldown)
    record = AlertRecord(
        alert_id=alert_id,
        program_id=program_id,
        severity=severity if not within_cooldown else match.severity,
        category=category,
        message=message if not within_cooldown else match.message,
        next_command=next_command if not within_cooldown else match.next_command,
        created_at=match.created_at,
        context=context if not within_cooldown else match.context,
        entity_type=entity_type,
        entity_id=entity_id,
        owner=owner or match.owner,
        first_seen=match.first_seen or match.created_at,
        last_seen=resolved_now,
        occurrence_count=match.occurrence_count + 1,
        suppressed_count=match.suppressed_count + (1 if within_cooldown else 0),
        cooldown_minutes=resolved_cooldown,
        delivery=resolved_delivery,
    )
    append_alert(record, programs_root=programs_root)
    return record


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
