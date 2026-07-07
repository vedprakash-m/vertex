"""WS-17 wire-in: capture per-channel latency from gather.py and emit a
``RunTelemetryRecord`` to the per-program ``run_telemetry.jsonl`` sidecar.

Design:
- A small accumulator closure (built by ``build_run_telemetry_accumulator()``)
  is called alongside the existing ``_complete_progress_step`` closure. The
  accumulator is fed by the same ``*_started_at`` markers the progress
  callback already uses — no edits to the stage bodies are required.
- At the end of ``gather_program`` (after ``run_state_write_stage`` returns),
  ``record_run_telemetry_for_gather()`` consumes the accumulator, builds a
  ``RunTelemetryRecord`` (mapping gather step names → channel buckets), and
  calls ``append_run_telemetry`` (PB-37 routed).

Mapping (gather step → channel bucket):
- ``prepare``       → not emitted (bootstrapping; not a channel fetch)
- ``ado``           → ``ado``
- ``signals``       → ``ado`` (consumes ADO fetch results)
- ``workiq``        → ``workiq`` (only emitted when ``include_workiq``)
- ``uil``           → ``ado`` (UIL piggy-backs on ADO; the bootstrap fetch
                     is still wall-time, not a separate channel)
- ``kusto``         → ``kusto`` (only when ``include_kusto``)
- ``charts``        → ``kusto`` (charts pull from the kusto cache)
- ``analytics``     → ``ado`` (ADO analytics view)
- ``sprints``       → ``ado``
- ``pipelines``     → ``ado``
- ``icm``           → ``icm`` (only when ``include_icm``)
- ``engms``         → ``ado`` (ADO engineering metrics view)
- ``persist``       → not emitted (stage-level, not a channel)
- ``trajectories``  → not emitted
- ``dependencies``  → not emitted
- ``synthesis``     → not emitted
- ``finalize``      → not emitted
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
import logging
import uuid

from src.core.run_telemetry import (
    ChannelRunStats,
    RunTelemetryRecord,
    append_run_telemetry,
)


_LOGGER = logging.getLogger(__name__)


# Gather step name → channel bucket. Steps not in this map are not emitted
# as channel samples (they are stage-level bookkeeping).
_STEP_TO_CHANNEL: dict[str, str] = {
    "ado": "ado",
    "signals": "ado",
    "workiq": "workiq",
    "uil": "ado",
    "kusto": "kusto",
    "charts": "kusto",
    "analytics": "ado",
    "sprints": "ado",
    "pipelines": "ado",
    "icm": "icm",
    "engms": "ado",
}


def build_run_telemetry_accumulator() -> dict[str, list[int]]:
    """Return a fresh accumulator: ``{channel: [latency_ms, ...]}``.

    The closure-free design is intentional — the same dict is mutated from
    the gather.py progress callback closure (no thread-safety guarantees
    needed; gather is single-threaded) and then handed to
    ``record_run_telemetry_for_gather``.
    """
    return {
        "ado": [],
        "workiq": [],
        "kusto": [],
        "icm": [],
    }


def observe_step(
    accumulator: dict[str, list[int]],
    *,
    step_name: str,
    started_at: float,
) -> int | None:
    """Record a single step's wall-time into the matching channel bucket.

    Returns the recorded latency_ms (or None if the step is not mapped
    to a channel). Safe to call for every progress step — unmapped
    steps are silently skipped.
    """
    channel = _STEP_TO_CHANNEL.get(step_name)
    if channel is None:
        return None
    elapsed_ms = int((perf_counter() - started_at) * 1000)
    accumulator.setdefault(channel, []).append(elapsed_ms)
    return elapsed_ms


def record_run_telemetry_for_gather(
    *,
    program_id: str,
    programs_root: Any,
    accumulator: dict[str, list[int]],
    started_at: datetime,
    run_id: str | None = None,
    include_workiq: bool = False,
    include_kusto: bool = False,
    include_icm: bool = False,
) -> Path | None:
    """Consume the accumulator and emit one ``RunTelemetryRecord``.

    Returns the path the record was written to, or None if the accumulator
    was empty (no channel steps observed — e.g. early-exit failure path).

    Failure category is left empty here — the gather pipeline records its
    own ``integration_error_details`` separately; telemetry is the
    wall-time view, not the error view. A future WS-17 follow-up can
    cross-link the two.
    """
    if not accumulator or not any(accumulator.values()):
        return None
    finished = datetime.now(timezone.utc)
    channels: list[ChannelRunStats] = []
    for channel in sorted(accumulator):
        samples = accumulator[channel]
        if not samples:
            continue
        # Drop the channel if it was requested-excluded (defensive: a
        # mapped step may have been recorded even when the flag was off,
        # but we don't want to invent per-channel stats the operator
        # didn't actually run).
        if channel == "workiq" and not include_workiq:
            continue
        if channel == "kusto" and not include_kusto:
            continue
        if channel == "icm" and not include_icm:
            continue
        channels.append(
            ChannelRunStats(
                channel=channel,
                attempts=1,            # one attempt per gather run
                retries=0,
                successes=1,
                failures=0,
                latency_ms_samples=tuple(samples),
                failure_categories=(),
            )
        )
    if not channels:
        return None
    record = RunTelemetryRecord(
        run_id=run_id or uuid.uuid4().hex,
        program_id=program_id,
        started_at=started_at,
        finished_at=finished,
        wall_time_seconds=(finished - started_at).total_seconds(),
        channels=tuple(channels),
    )
    try:
        path = append_run_telemetry(record, programs_root=programs_root)
    except Exception as exc:  # never block gather on telemetry failure
        _LOGGER.warning("run_telemetry emit failed for %s: %s", program_id, exc)
        return None
    return path
