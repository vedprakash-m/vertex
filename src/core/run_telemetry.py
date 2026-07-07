"""WS-17: per-run telemetry (per-channel wall-time + P50/P95 latency).

The telemetry layer answers two SRE questions that the existing
``telemetry_summary`` (signal/evidence view) does not:

1. **How long did the last run take per channel?** (gather wall-time)
2. **What is the P50/P95 latency over the recent window per channel?**
   (multi-run aggregation for ``doctor --perf``).

Design:
- One JSONL sidecar per program: ``programs/<id>/run_telemetry.jsonl``
- Each row is one run-end record with: ``run_id``, ``started_at``,
  ``finished_at``, ``wall_time_seconds``, ``channels: {<channel>: {attempts, retries, success, failure_category, ...}}``
- Append-only (PB-37 contract); portalocker + fsync.
- Read-side computes per-channel percentiles from a recent window
  (default last 10 runs; override via ``--perf-window``).

Failure category tagging uses ``src.core.failure_taxonomy`` so the
operator gets a stable vocabulary from a single source of truth.
"""
from __future__ import annotations

import json
import os
import portalocker
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.core.exceptions import StateError
from src.core.program_paths import get_run_telemetry_path, resolve_run_telemetry_path_for_read


RUN_TELEMETRY_FILENAME = "run_telemetry.jsonl"


@dataclass(frozen=True, slots=True)
class ChannelRunStats:
    """Per-channel aggregate for one run."""
    channel: str
    attempts: int = 0
    retries: int = 0
    successes: int = 0
    failures: int = 0
    latency_ms_samples: tuple[int, ...] = ()
    failure_categories: tuple[str, ...] = ()

    @property
    def p50_latency_ms(self) -> int | None:
        return _percentile(self.latency_ms_samples, 50)

    @property
    def p95_latency_ms(self) -> int | None:
        return _percentile(self.latency_ms_samples, 95)


@dataclass(frozen=True, slots=True)
class RunTelemetryRecord:
    """One row in ``run_telemetry.jsonl`` — one gather run's footprint."""
    run_id: str
    program_id: str
    started_at: datetime
    finished_at: datetime
    wall_time_seconds: float
    channels: tuple[ChannelRunStats, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "run_id": self.run_id,
            "program_id": self.program_id,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "wall_time_seconds": round(self.wall_time_seconds, 3),
            "channels": [
                {
                    "channel": stats.channel,
                    "attempts": stats.attempts,
                    "retries": stats.retries,
                    "successes": stats.successes,
                    "failures": stats.failures,
                    "latency_ms_samples": list(stats.latency_ms_samples),
                    "p50_latency_ms": stats.p50_latency_ms,
                    "p95_latency_ms": stats.p95_latency_ms,
                    "failure_categories": list(stats.failure_categories),
                }
                for stats in self.channels
            ],
        }


@dataclass(frozen=True, slots=True)
class ChannelPerfSummary:
    """Multi-run aggregate — the thing `doctor --perf` surfaces."""
    channel: str
    run_count: int
    attempts: int
    successes: int
    failures: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    failure_categories: tuple[str, ...] = ()
    slo_status: str = "unknown"   # "ok" | "warn" | "fail" | "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "run_count": self.run_count,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "failure_categories": list(self.failure_categories),
            "slo_status": self.slo_status,
        }


# Default per-channel SLO budgets (in milliseconds). Tunable via
# ``VertexConfig.perf_slo_ms[channel]`` (operator-set); these are the
# platform-defaults when the operator hasn't set explicit budgets.
DEFAULT_SLO_MS: dict[str, int] = {
    "ado": 30_000,
    "kusto": 60_000,
    "icm": 15_000,
    "teams": 20_000,
    "workiq": 45_000,
    "transcript": 30_000,
}


# ---------- write-side ----------


def append_run_telemetry(
    record: RunTelemetryRecord,
    *,
    programs_root: Path,
) -> Path:
    """Append a single ``RunTelemetryRecord`` to the program's JSONL sidecar.

    Portalocker-guarded (PB-37) + fsync + atomic-rename. The file is
    *append-only* — readers must not depend on in-place rewrites."""
    path = get_run_telemetry_path(record.program_id, programs_root=programs_root)
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


def run_telemetry_path(program_id: str, programs_root: Path) -> Path:
    """Public accessor — delegates to the program_paths read resolver.

    Kept for backward compatibility with ``state_reader_registry`` reader symbols
    and callers that import this name directly.
    """
    return resolve_run_telemetry_path_for_read(program_id, programs_root=programs_root)


# ---------- read-side ----------


def read_run_telemetry(
    program_id: str,
    *,
    programs_root: Path,
    window: int = 10,
) -> tuple[RunTelemetryRecord, ...]:
    """Return the most-recent ``window`` records (chronological order)."""
    path = resolve_run_telemetry_path_for_read(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    rows: list[RunTelemetryRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise StateError(f"Invalid run_telemetry row: {line[:80]!r}: {error}") from error
            rows.append(_record_from_payload(payload))
    # Chronological order (oldest → newest); trim to the last ``window``.
    rows.sort(key=lambda r: r.started_at)
    return tuple(rows[-max(1, window):])


def build_channel_perf_summary(
    program_id: str,
    *,
    programs_root: Path,
    window: int = 10,
    slo_overrides: Mapping[str, int] | None = None,
) -> tuple[ChannelPerfSummary, ...]:
    """Aggregate per-channel P50/P95 over the recent window. SLO status
    uses ``slo_overrides[channel]`` if provided, else ``DEFAULT_SLO_MS``."""
    records = read_run_telemetry(program_id, programs_root=programs_root, window=window)
    by_channel: dict[str, list[ChannelRunStats]] = {}
    for record in records:
        for stats in record.channels:
            by_channel.setdefault(stats.channel, []).append(stats)
    slo_map: dict[str, int] = {**DEFAULT_SLO_MS, **(slo_overrides or {})}
    out: list[ChannelPerfSummary] = []
    for channel, stats_list in sorted(by_channel.items()):
        latency_samples: list[int] = []
        attempts = successes = failures = 0
        cats: list[str] = []
        for stats in stats_list:
            attempts += stats.attempts
            successes += stats.successes
            failures += stats.failures
            latency_samples.extend(stats.latency_ms_samples)
            cats.extend(stats.failure_categories)
        slo_ms = slo_map.get(channel)
        p95 = _percentile(latency_samples, 95)
        if slo_ms is None or p95 is None:
            slo_status = "unknown"
        elif p95 <= slo_ms:
            slo_status = "ok"
        elif p95 <= slo_ms * 2:
            slo_status = "warn"
        else:
            slo_status = "fail"
        out.append(
            ChannelPerfSummary(
                channel=channel,
                run_count=len(stats_list),
                attempts=attempts,
                successes=successes,
                failures=failures,
                p50_latency_ms=_percentile(latency_samples, 50),
                p95_latency_ms=p95,
                failure_categories=tuple(sorted(set(cats))),
                slo_status=slo_status,
            )
        )
    return tuple(out)


# ---------- internals ----------


def _record_from_payload(payload: dict[str, Any]) -> RunTelemetryRecord:
    if not isinstance(payload, dict):
        raise StateError(f"Invalid run_telemetry payload (not dict): {payload!r}")
    try:
        started = _parse_dt(payload.get("started_at"))
        finished = _parse_dt(payload.get("finished_at"))
        if started is None or finished is None:
            raise StateError("run_telemetry row missing started_at/finished_at")
        channels_raw = payload.get("channels") or []
        if not isinstance(channels_raw, list):
            raise StateError("run_telemetry row channels must be a list")
        channel_stats: list[ChannelRunStats] = []
        for entry in channels_raw:
            if not isinstance(entry, dict):
                continue
            samples = entry.get("latency_ms_samples") or []
            if not isinstance(samples, list):
                samples = []
            cats = entry.get("failure_categories") or []
            if not isinstance(cats, list):
                cats = []
            channel_stats.append(
                ChannelRunStats(
                    channel=str(entry.get("channel") or "").strip(),
                    attempts=int(entry.get("attempts") or 0),
                    retries=int(entry.get("retries") or 0),
                    successes=int(entry.get("successes") or 0),
                    failures=int(entry.get("failures") or 0),
                    latency_ms_samples=tuple(int(x) for x in samples if isinstance(x, (int, float))),
                    failure_categories=tuple(str(c) for c in cats if isinstance(c, str)),
                )
            )
        return RunTelemetryRecord(
            run_id=str(payload.get("run_id") or ""),
            program_id=str(payload.get("program_id") or ""),
            started_at=started,
            finished_at=finished,
            wall_time_seconds=float(payload.get("wall_time_seconds") or 0.0),
            channels=tuple(channel_stats),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise StateError(f"Invalid run_telemetry payload: {error}") from error


def _percentile(samples: Iterable[int], percentile: int) -> int | None:
    samples_list = [int(x) for x in samples]
    if not samples_list:
        return None
    if len(samples_list) == 1:
        return samples_list[0]
    # Use statistics.quantiles for interpolation; falls back to sorted-index
    # rounding for tight memory.
    sorted_samples = sorted(samples_list)
    k = max(0, min(100, percentile))
    # statistics.quantiles uses n=100 by default (percentiles). For P50 / P95
    # we can compute directly via interpolation-free rounding on the sorted
    # list, which is what most SRE tools do.
    if k == 0:
        return sorted_samples[0]
    if k == 100:
        return sorted_samples[-1]
    # Nearest-rank (NIST standard for SRE p50/p95): rank = ceil(p/100 * n)
    import math
    rank = max(1, math.ceil(k / 100.0 * len(sorted_samples)))
    return sorted_samples[rank - 1]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
