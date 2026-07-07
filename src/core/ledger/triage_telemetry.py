"""Triage telemetry — activation.md §6.10 / AG-13 / O-14.

The loop that lets maintainers improve prompts/schemas post-launch is only
useful if accept/reject/edit rates and **time-to-triage** are *measured*, not
inferred. ``append_triage_decision`` already records each decision immutably;
this module turns that stream into a small, bounded telemetry log
(``programs/<id>/_rev/triage_telemetry.jsonl``) carrying the per-decision
signal that drives prompt/schema iteration and the AG-13 benefit view.

What is emitted, per triage decision:
- ``time_to_triage_seconds`` — wall-clock from candidate ``staged_at`` to
  ``decided_at``. This is the AG-13 ``time-to-triage`` metric (the friction
  behind "judgment not discovery"). Computed only when ``staged_at`` is known.
- ``kind`` — approved / rejected / skipped / revoked (drives accept/reject/edit
  rates; ``edited=True`` is a sub-flag on the approve kind).
- ``decision_count`` / ``oldest_pending_age_seconds`` — context for batch ROI.

The file is append-only and best-effort: a telemetry write failure logs a
warning and never raises (telemetry must not break a triage decision).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.core.config_loader import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line

log = logging.getLogger(__name__)

_TELEMETRY_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB bound; rotated by append_jsonl_line.


def _telemetry_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "_rev" / "triage_telemetry.jsonl"


def _safe_seconds(delta: float) -> float | None:
    if delta < 0 or delta > 365 * 24 * 3600:
        # Negative (clock skew) or implausibly large — drop rather than mislead.
        return None
    return round(delta, 3)


def record_triage_decision_telemetry(
    *,
    program_id: str,
    candidate_id: str,
    kind: str,
    decided_at: datetime,
    triage_actor: str,
    staged_at: datetime | None,
    edited: bool | None = None,
    reason: str | None = None,
    batch_id: str | None = None,
    active_pending_count: int | None = None,
    oldest_pending_age_seconds: float | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Emit one triage-decision telemetry row (best-effort, never raises).

    ``active_pending_count`` / ``oldest_pending_age_seconds`` are optional
    context the caller (e.g. ``triage approve``) can supply from
    ``active_candidates`` so each decision row carries the backlog shape that
    drives the AG-20 time-motion ROI view.
    """
    time_to_triage: float | None = None
    if staged_at is not None:
        time_to_triage = _safe_seconds((decided_at - staged_at).total_seconds())

    record: dict[str, Any] = {
        "emitted_at": datetime.now(timezone.utc).isoformat(),
        "program_id": program_id,
        "candidate_id": candidate_id,
        "kind": kind,
        "triage_actor": triage_actor,
        "decided_at": decided_at.astimezone(timezone.utc).isoformat(),
        "time_to_triage_seconds": time_to_triage,
        "edited": bool(edited) if edited is not None else None,
        "reason": reason,
        "batch_id": batch_id,
        "active_pending_count": active_pending_count,
        "oldest_pending_age_seconds": (
            round(oldest_pending_age_seconds, 3)
            if oldest_pending_age_seconds is not None
            else None
        ),
    }
    try:
        append_jsonl_line(
            _telemetry_path(program_id, programs_root=programs_root),
            json.dumps(record, sort_keys=True) + "\n",
            max_bytes=_TELEMETRY_MAX_BYTES,
        )
    except OSError as exc:
        log.warning("triage telemetry: could not write row for %s: %s", candidate_id, exc)


def summarize_triage_telemetry(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any]:
    """Aggregate the telemetry log into the AG-13 benefit-view summary.

    Returns counts/rates per kind plus time-to-triage stats. Pure read; safe to
    call when the log is absent (returns an empty summary). This is the shape a
    future ``doctor --triage-health`` or benefit-telemetry view would render.
    """
    from src.core.jsonl_utils import read_jsonl_records

    path = _telemetry_path(program_id, programs_root=programs_root)
    counts: dict[str, int] = {}
    edit_count = 0
    times: list[float] = []
    latest: datetime | None = None
    for raw in read_jsonl_records(path):
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
        if raw.get("edited"):
            edit_count += 1
        tt = raw.get("time_to_triage_seconds")
        if isinstance(tt, (int, float)) and tt >= 0:
            times.append(float(tt))
        decided = raw.get("decided_at")
        if isinstance(decided, str):
            try:
                parsed = datetime.fromisoformat(decided)
                if latest is None or parsed > latest:
                    latest = parsed
            except ValueError:
                pass
    total = sum(counts.values())
    approved = counts.get("approved", 0)
    return {
        "program_id": program_id,
        "total_decisions": total,
        "counts": counts,
        "edit_count": edit_count,
        "approve_rate": round(approved / total, 4) if total else None,
        "time_to_triage_seconds": {
            "n": len(times),
            "mean": round(sum(times) / len(times), 3) if times else None,
            "max": round(max(times), 3) if times else None,
        },
        "latest_decision_at": latest.isoformat() if latest else None,
    }


__all__ = [
    "record_triage_decision_telemetry",
    "summarize_triage_telemetry",
]
