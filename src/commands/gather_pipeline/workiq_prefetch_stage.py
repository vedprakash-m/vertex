"""ADF-W1.5/ADF-W1.10 remainder (specs/arch-data-fix.md Section 10.6):
"gather consumes an unexpired committed snapshot before any live WorkIQ
attempt." This is the consumer-side half `prefetch_store.py`'s own
docstring flagged as a follow-up, plus the shared Signal<->JSON payload
shape the `vertex prefetch` writer (``src/commands/prefetch.py``) uses too.

``resolve_workiq_signals`` takes the live fetch as an **injected callable**
(``live_fetch_fn``) rather than importing ``gather.py::_build_workiq_signals``
directly -- gather.py is this module's own caller, so a direct import would
be circular. This also keeps the snapshot-vs-live decision fully unit
testable without constructing a real ``AgencyBridge``.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.core.journal import signal_from_record, signal_to_record
from src.core.models_v2 import Signal
from src.core.prefetch_store import (
    PrefetchSnapshotManifest,
    read_snapshot_payload,
    read_unexpired_committed_snapshot,
)

WORKIQ_PREFETCH_CHANNEL = "workiq"


def workiq_signals_to_payload(signals: tuple[Signal, ...]) -> dict[str, Any]:
    return {"signals": [signal_to_record(signal) for signal in signals]}


def workiq_signals_from_payload(payload: dict[str, Any]) -> tuple[Signal, ...]:
    records = payload.get("signals")
    if not isinstance(records, list):
        return ()
    return tuple(signal_from_record(record) for record in records)


def resolve_workiq_signals(
    *,
    program_id: str,
    programs_root: Path | None,
    live_fetch_fn: Callable[[], tuple[Signal, ...]],
    now: datetime | None = None,
) -> tuple[Signal, ...]:
    """Section 10.6's ordering, exactly: prefer an unexpired committed
    prefetch snapshot; only call ``live_fetch_fn`` (the live WorkIQ path)
    when no such snapshot exists. A missing/expired/corrupt snapshot always
    degrades to the live call -- this never silently returns stale or
    empty data in place of a real attempt. ``now`` overrides the expiry
    reference instant (test-only; production callers omit it)."""
    if programs_root is not None:
        manifest = read_unexpired_committed_snapshot(
            program_id, WORKIQ_PREFETCH_CHANNEL, programs_root=programs_root, now=now
        )
        if manifest is not None:
            signals = _try_read_snapshot_signals(program_id, manifest, programs_root=programs_root)
            if signals is not None:
                return signals
    return live_fetch_fn()


def _try_read_snapshot_signals(
    program_id: str, manifest: PrefetchSnapshotManifest, *, programs_root: Path
) -> tuple[Signal, ...] | None:
    try:
        payload = read_snapshot_payload(program_id, WORKIQ_PREFETCH_CHANNEL, manifest, programs_root=programs_root)
        return workiq_signals_from_payload(payload)
    except (OSError, ValueError, KeyError, TypeError):
        # A corrupt/malformed committed snapshot degrades to the live path
        # rather than propagating a parse error into the gather run.
        return None


__all__ = [
    "WORKIQ_PREFETCH_CHANNEL",
    "resolve_workiq_signals",
    "workiq_signals_from_payload",
    "workiq_signals_to_payload",
]
