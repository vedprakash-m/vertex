"""
Context gap store — append-only log of feature-to-context gap signals.

Implements §21 of the program-context-maturity spec.

Schema (one JSON per line):
  {"ts": "...", "feature": "...", "program": "...", "lane": "...",
   "field": "...", "severity": "...", "message": "...", "impact_estimate": "..."}

File: programs/<prog>/_feedback/context_gaps.jsonl

Zone A only. No AI. No M365 calls. Append-only per ADR-001.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from src.core.jsonl_utils import parse_jsonl_line
import os
from pathlib import Path
from typing import Any

import portalocker

from src.core.edition_resolver import PROGRAMS_ROOT


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextGapRecord:
    """A single feature-to-context gap signal."""

    ts: datetime
    feature: str
    program: str
    lane: str | None  # None means "global" (no specific lane)
    field: str
    severity: str  # "feature_blocked" | "quality_degraded"
    message: str
    impact_estimate: str  # "low" | "medium" | "high"

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": _normalize_datetime(self.ts).isoformat().replace("+00:00", "Z"),
            "feature": self.feature,
            "program": self.program,
            "lane": self.lane,
            "field": self.field,
            "severity": self.severity,
            "message": self.message,
            "impact_estimate": self.impact_estimate,
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> ContextGapRecord:
        return ContextGapRecord(
            ts=_parse_datetime(d["ts"]),
            feature=str(d["feature"]),
            program=str(d["program"]),
            lane=d.get("lane") or None,
            field=str(d["field"]),
            severity=str(d["severity"]),
            message=str(d["message"]),
            impact_estimate=str(d.get("impact_estimate", "medium")),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_context_gap(
    *,
    feature: str,
    program: str,
    lane: str | None = None,
    field: str,
    severity: str = "quality_degraded",
    message: str,
    impact_estimate: str = "medium",
    programs_root: Path = PROGRAMS_ROOT,
    deduplicate: bool = True,
    dedup_window_days: int = 7,
) -> Path:
    """Append a context gap to _feedback/context_gaps.jsonl (ADR-001 atomic write).

    With deduplicate=True (default), records with the same fingerprint
    (program, feature, lane, field) are suppressed if an identical entry
    was written within dedup_window_days.  This prevents weekly re-run flood.

    Returns the path to the file.
    """
    path = _gap_path(program, programs_root=programs_root)
    now = datetime.now(timezone.utc)

    if deduplicate:
        window_start = now.timestamp() - dedup_window_days * 86400
        if _has_recent_gap(path, feature, lane, field, since_ts=window_start):
            return path

    record = ContextGapRecord(
        ts=now,
        feature=feature,
        program=program,
        lane=lane,
        field=field,
        severity=severity,
        message=message,
        impact_estimate=impact_estimate,
    )
    _append_jsonl(path, record.to_json())
    return path


def _has_recent_gap(path: Path, feature: str, lane: str | None, field: str, *, since_ts: float) -> bool:
    """Return True if a gap with matching fingerprint exists within the time window."""
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    import json as _json  # noqa: PLC0415
                    d = _json.loads(line)
                except Exception:
                    continue
                if (
                    d.get("feature") == feature
                    and d.get("lane") == lane
                    and d.get("field") == field
                ):
                    ts_str = d.get("ts", "")
                    try:
                        ts = _parse_datetime(ts_str)
                        if ts.timestamp() >= since_ts:
                            return True
                    except Exception:
                        pass
    except OSError:
        pass
    return False


def load_context_gaps(
    program: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    since: datetime | None = None,
    impact_filter: str | None = None,
) -> list[ContextGapRecord]:
    """
    Load all context gap records for a program, optionally filtered.

    - since: only records after this UTC datetime
    - impact_filter: only records with this impact_estimate ("high", "medium", "low")
    """
    path = _gap_path(program, programs_root=programs_root)
    if not path.exists():
        return []

    records: list[ContextGapRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = parse_jsonl_line(line)
            except json.JSONDecodeError:
                continue
            rec = ContextGapRecord.from_json(d)
            if since is not None and rec.ts <= since:
                continue
            if impact_filter is not None and rec.impact_estimate != impact_filter:
                continue
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankedGap:
    """A gap with aggregated frequency and ranking metadata."""

    feature: str
    program: str
    lane: str | None
    field: str
    severity: str
    impact_estimate: str
    count: int  # number of consecutive or total appearances
    first_seen: datetime
    last_seen: datetime
    message: str
    fix_hint: str


def rank_context_gaps(
    gaps: list[ContextGapRecord],
) -> list[RankedGap]:
    """
    Rank gaps by impact_estimate descending, then by count descending.

    Groups by (feature, lane, field) to aggregate frequency.
    """
    # Group by key
    from collections import defaultdict

    groups: dict[tuple[str, str | None, str], list[ContextGapRecord]] = defaultdict(list)
    for g in gaps:
        key = (g.feature, g.lane, g.field)
        groups[key].append(g)

    ranked: list[RankedGap] = []
    for (feature, lane, field), group in groups.items():
        group.sort(key=lambda r: r.ts)
        first = group[0]
        last = group[-1]
        impact = first.impact_estimate
        severity = first.severity
        message = first.message

        fix_hint = _make_fix_hint(feature, field, lane)

        ranked.append(RankedGap(
            feature=feature,
            program=first.program,
            lane=lane,
            field=field,
            severity=severity,
            impact_estimate=impact,
            count=len(group),
            first_seen=first.ts,
            last_seen=last.ts,
            message=message,
            fix_hint=fix_hint,
        ))

    # Sort: HIGH first, then by count descending
    _IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
    ranked.sort(key=lambda r: (_IMPACT_ORDER.get(r.impact_estimate, 2), -r.count))
    return ranked


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _gap_path(program: str, *, programs_root: Path) -> Path:
    return programs_root / program / "_feedback" / "context_gaps.jsonl"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _normalize_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ts must include timezone information")
    return parsed.astimezone(timezone.utc)


def _make_fix_hint(feature: str, field: str, lane: str | None) -> str:
    """Produce a actionable fix hint for a gap."""
    lane_part = f" in workstream {lane}" if lane else ""

    if field in ("deep_context", "why", "what", "how"):
        return f"add deep_context.why / .what / .how to workstream_registry.yaml{lane_part}"
    elif field == "workiq_latest":
        return f"run 'vertex gather --workiq' or manually update workiq_latest{lane_part}"
    elif field == "email" and lane:
        return f"add email: to primary_owner in workstream_registry.yaml{lane_part}"
    elif field == "validated" and "kpis" in feature:
        return "run a live Kusto query and set validated: true in kpis.yaml after confirmed result"
    elif "stale" in field:
        return f"refresh {field.split('_')[0]} data or update last_reviewed_date"
    else:
        return f"review {field} in program configuration"