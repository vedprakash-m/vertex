"""WS-5b: Per-program AI telemetry sidecar.

Records every AI call (including failures) as a JSONL line under the program
root.  The schema is intentionally flat so it can be queried without a DB.

Sidecar path:  ``programs/<id>/_state/ai_telemetry.jsonl``
Registry key:  ``"ai_telemetry"`` (state_reader_registry entry 26)

Public API
==========
- ``AiTelemetryStatus``  — typed status enum (ok/rate_limit/context_length/
  auth/timeout/other)
- ``AiTelemetryRecord``  — frozen, slots dataclass; every AI call → 1 record
- ``ai_telemetry_path``  — canonical path helper
- ``append_ai_telemetry``— portalocker-guarded JSONL append (PB-37 compliant)
- ``read_ai_telemetry``  — returns ``tuple[AiTelemetryRecord, ...]``
- ``build_feature_cost_summary`` — dict[feature → FeatureCostSummary] over
  the most recent *window_days* days; used by ``vertex calibration --cost``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import auto
from pathlib import Path
from typing import Any

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, parse_jsonl_line


# --------------------------------------------------------------------------- #
# Schema version bump when the set of required keys changes
# --------------------------------------------------------------------------- #
_SCHEMA_VERSION = "1.0"
_SIDECAR_RELATIVE = "_state/ai_telemetry.jsonl"


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
class AiTelemetryStatus(str):
    """Status string enum for an AI telemetry record.

    Using ``str`` sub-class (not ``enum.Enum``) so JSON serialisation stays
    transparent and older records without the field default to ``"ok"``.
    """
    OK = "ok"
    RATE_LIMIT = "rate_limit"
    CONTEXT_LENGTH = "context_length"
    AUTH = "auth"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    FALLBACK = "fallback"   # no deployment available / frontier_eligible=false
    OTHER = "other"

    @classmethod
    def from_exception(cls, exc: BaseException) -> str:
        """Classify a caught AI exception to one of the known status codes."""
        msg = str(exc).lower()
        exc_name = type(exc).__name__.lower()
        if "budget" in msg or "budgetexceeded" in exc_name:
            return cls.BUDGET_EXCEEDED
        if "rate" in msg or "429" in msg:
            return cls.RATE_LIMIT
        # Check auth before context_length: 401/403 are auth signals even if
        # the message also contains the word "token" (e.g. "invalid token").
        if "401" in msg or "403" in msg or "auth" in msg or "credential" in msg:
            return cls.AUTH
        if "context" in msg or "token" in msg:
            return cls.CONTEXT_LENGTH
        if "timeout" in msg or "timed out" in msg:
            return cls.TIMEOUT
        return cls.OTHER


@dataclass(frozen=True, slots=True)
class AiTelemetryRecord:
    """One AI call record in the program telemetry sidecar."""
    ts: datetime
    feature: str
    deployment_id: str
    status: str                   # AiTelemetryStatus constant
    program_id: str
    latency_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    error_detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "ts": self.ts.isoformat(),
            "feature": self.feature,
            "deployment_id": self.deployment_id,
            "status": self.status,
            "program_id": self.program_id,
            "latency_ms": self.latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "error_detail": self.error_detail,
        }


@dataclass(frozen=True, slots=True)
class FeatureCostSummary:
    """Aggregated cost statistics for one AI feature over a time window."""
    feature: str
    call_count: int
    ok_count: int
    error_count: int
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    avg_latency_ms: float | None


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #
def ai_telemetry_path(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    """Return the canonical sidecar path for *program_id*."""
    return programs_root / program_id / _SIDECAR_RELATIVE


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #
def append_ai_telemetry(
    record: AiTelemetryRecord,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append one telemetry record (portalocker-guarded, fsync'd, PB-37)."""
    path = ai_telemetry_path(record.program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_line(path, json.dumps(record.to_dict(), default=str) + os.linesep)


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def read_ai_telemetry(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AiTelemetryRecord, ...]:
    """Return all telemetry records for *program_id* (oldest first)."""
    path = ai_telemetry_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    records: list[AiTelemetryRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue  # skip blank lines (e.g. trailing newline)
        payload = parse_jsonl_line(line)
        if not isinstance(payload, dict):
            continue
        try:
            records.append(_record_from_payload(payload))
        except (KeyError, ValueError):
            continue  # malformed line — skip, do not break iteration
    return tuple(records)


def _record_from_payload(payload: dict[str, Any]) -> AiTelemetryRecord:
    raw_ts = payload.get("ts")
    if isinstance(raw_ts, str):
        ts = datetime.fromisoformat(raw_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)

    latency_raw = payload.get("latency_ms")
    cost_raw = payload.get("cost_usd")
    tokens_in_raw = payload.get("tokens_in")
    tokens_out_raw = payload.get("tokens_out")
    return AiTelemetryRecord(
        ts=ts,
        feature=str(payload.get("feature", "")),
        deployment_id=str(payload.get("deployment_id", "")),
        status=str(payload.get("status", AiTelemetryStatus.OK)),
        program_id=str(payload.get("program_id", "")),
        latency_ms=float(latency_raw) if latency_raw is not None else None,
        tokens_in=int(tokens_in_raw) if tokens_in_raw is not None else None,
        tokens_out=int(tokens_out_raw) if tokens_out_raw is not None else None,
        cost_usd=float(cost_raw) if cost_raw is not None else None,
        error_detail=payload.get("error_detail"),
    )


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def build_feature_cost_summary(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    window_days: int = 30,
) -> dict[str, FeatureCostSummary]:
    """Aggregate per-feature cost/error stats over the last *window_days* days.

    Returns a dict keyed by feature name.  Features with 0 records in the
    window are omitted.  Used by ``vertex calibration --cost`` and the
    reviewer pane cost section.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    records = [r for r in read_ai_telemetry(program_id, programs_root=programs_root) if r.ts >= cutoff]

    by_feature: dict[str, list[AiTelemetryRecord]] = {}
    for rec in records:
        by_feature.setdefault(rec.feature, []).append(rec)

    result: dict[str, FeatureCostSummary] = {}
    for feature, recs in by_feature.items():
        ok_count = sum(1 for r in recs if r.status == AiTelemetryStatus.OK)
        error_count = len(recs) - ok_count
        total_cost = sum(r.cost_usd for r in recs if r.cost_usd is not None)
        total_in = sum(r.tokens_in for r in recs if r.tokens_in is not None)
        total_out = sum(r.tokens_out for r in recs if r.tokens_out is not None)
        latencies = [r.latency_ms for r in recs if r.latency_ms is not None]
        avg_latency = sum(latencies) / len(latencies) if latencies else None
        result[feature] = FeatureCostSummary(
            feature=feature,
            call_count=len(recs),
            ok_count=ok_count,
            error_count=error_count,
            total_cost_usd=total_cost,
            total_tokens_in=total_in,
            total_tokens_out=total_out,
            avg_latency_ms=avg_latency,
        )
    return result
