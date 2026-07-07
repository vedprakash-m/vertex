"""reality_checks.py — per-check health helpers for `vertex reality status`.

WI-5.1 checks:
  - entity_count_threshold: warns when entity count exceeds A-10 limit (150).
  - override_recertification_due: warns when a source-authority override's
    ``acknowledged_at`` is older than ``override_ttl_days`` (§6.2.2).

Each check returns a ``RealityCheckResult``.  The status field follows the
same convention as ``DoctorCheck``: ``"ok"`` | ``"warn"`` | ``"error"``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RealityCheckResult:
    check_id: str
    status: str          # "ok" | "warn" | "error"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Check: entity count threshold (A-10)
# ---------------------------------------------------------------------------

_ENTITY_COUNT_THRESHOLD = 150


def check_entity_count(
    program_id: str,
    *,
    programs_root: Path,
    threshold: int = _ENTITY_COUNT_THRESHOLD,
) -> RealityCheckResult:
    """Return a warning when the program's entity registry exceeds *threshold*.

    Uses the program-scoped ``knowledge/entities.yaml`` count when the
    EntityRegistry is not available (avoids loading the full registry just
    for a count).  Returns ``ok`` when the file is missing (no entities yet).
    """
    entities_path = programs_root / program_id / "knowledge" / "entities.yaml"
    if not entities_path.exists():
        return RealityCheckResult(
            check_id="entity_count_threshold",
            status="ok",
            message=f"No entities file at {entities_path.name}; count=0 < {threshold}.",
            details={"count": 0, "threshold": threshold},
        )
    try:
        raw = yaml.safe_load(entities_path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError:
        return RealityCheckResult(
            check_id="entity_count_threshold",
            status="warn",
            message="Could not parse entities.yaml — skipping entity count check.",
            details={"count": None, "threshold": threshold},
        )
    count = len(raw) if isinstance(raw, list) else len(raw) if isinstance(raw, dict) else 0
    if count > threshold:
        return RealityCheckResult(
            check_id="entity_count_threshold",
            status="warn",
            message=(
                f"Entity count {count} exceeds threshold {threshold} (assumption A-10). "
                "Consider pruning stale entities or raising the threshold in doctor policy."
            ),
            details={"count": count, "threshold": threshold},
        )
    return RealityCheckResult(
        check_id="entity_count_threshold",
        status="ok",
        message=f"Entity count {count} ≤ threshold {threshold}.",
        details={"count": count, "threshold": threshold},
    )


# ---------------------------------------------------------------------------
# Check: override recertification due (§6.2.2)
# ---------------------------------------------------------------------------

_DEFAULT_OVERRIDE_TTL_DAYS = 90


def check_override_recertification(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime | None = None,
    default_ttl_days: int = _DEFAULT_OVERRIDE_TTL_DAYS,
) -> RealityCheckResult:
    """Warn when a per-program source_authority override needs recertification.

    Looks for ``programs/<id>/source_authority.yaml``.  Each entry in the
    ``authority`` section that carries ``acknowledged_at`` is checked against
    ``override_ttl_days`` (defaults to ``default_ttl_days`` when absent from
    the override file).  An expired ``acknowledged_at`` means the operator
    must re-certify before the override is trusted again.
    """
    now = as_of or datetime.now(timezone.utc)
    override_path = programs_root / program_id / "source_authority.yaml"
    if not override_path.exists():
        return RealityCheckResult(
            check_id="override_recertification_due",
            status="ok",
            message="No per-program source_authority override file found.",
            details={"overrides_checked": 0},
        )

    try:
        raw = yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return RealityCheckResult(
            check_id="override_recertification_due",
            status="warn",
            message="Could not parse per-program source_authority.yaml.",
            details={"overrides_checked": 0},
        )

    ttl_days = int(raw.get("override_ttl_days", default_ttl_days))
    ttl_delta = timedelta(days=ttl_days)

    expired: list[str] = []
    authority_section = raw.get("authority") or {}
    for family_name, entry in authority_section.items():
        if not isinstance(entry, dict):
            continue
        ack_raw = entry.get("acknowledged_at")
        if ack_raw is None:
            continue
        # Parse acknowledged_at
        ack_str = str(ack_raw).strip().replace("Z", "+00:00")
        try:
            ack_dt = datetime.fromisoformat(ack_str)
        except ValueError:
            expired.append(f"{family_name} (unparseable acknowledged_at)")
            continue
        if ack_dt.tzinfo is None:
            ack_dt = ack_dt.replace(tzinfo=timezone.utc)
        else:
            ack_dt = ack_dt.astimezone(timezone.utc)
        if now - ack_dt > ttl_delta:
            days_overdue = int((now - ack_dt - ttl_delta).total_seconds() // 86400)
            expired.append(f"{family_name} (overdue {days_overdue}d)")

    if expired:
        return RealityCheckResult(
            check_id="override_recertification_due",
            status="warn",
            message=(
                f"{len(expired)} authority override(s) require recertification: "
                + "; ".join(expired)
                + ". Update 'acknowledged_at' in programs/"
                + program_id
                + "/source_authority.yaml."
            ),
            details={"expired_overrides": expired, "ttl_days": ttl_days},
        )

    overrides_count = sum(
        1
        for entry in authority_section.values()
        if isinstance(entry, dict) and entry.get("acknowledged_at") is not None
    )
    return RealityCheckResult(
        check_id="override_recertification_due",
        status="ok",
        message=f"All {overrides_count} active override(s) are within TTL ({ttl_days}d).",
        details={"overrides_checked": overrides_count, "ttl_days": ttl_days},
    )


# ---------------------------------------------------------------------------
# Convenience: run all checks
# ---------------------------------------------------------------------------


def run_reality_checks(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime | None = None,
) -> tuple[RealityCheckResult, ...]:
    """Run all WI-5.1 reality checks and return results."""
    return (
        check_entity_count(program_id, programs_root=programs_root),
        check_override_recertification(program_id, programs_root=programs_root, as_of=as_of),
    )
