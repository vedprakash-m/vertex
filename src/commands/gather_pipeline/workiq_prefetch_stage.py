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
import logging
from pathlib import Path
import sys
from typing import Any, Callable

from src.core.journal import signal_from_record, signal_to_record
from src.core.gather_channel_support import (
    WORKIQ_INVALID_RECORD_OPERATOR_ACTION,
    WORKIQ_PLAN_BUDGET_MESSAGE,
    WORKIQ_PLAN_BUDGET_OPERATOR_ACTION,
    append_integration_error_once,
)
from src.core.m365_payload_support import has_provisional_workiq_identity
from src.core.models_v2 import IntegrationError, Signal
from src.core.prefetch_store import (
    PrefetchSnapshotManifest,
    PrefetchSnapshotPayloadTooDeepError,
    read_snapshot_payload,
    read_unexpired_committed_snapshot,
)

WORKIQ_PREFETCH_CHANNEL = "workiq"
logger = logging.getLogger(__name__)


def workiq_signals_to_payload(signals: tuple[Signal, ...]) -> dict[str, Any]:
    if any(
        signal.source.startswith("workiq/") and has_provisional_workiq_identity(signal.metadata)
        for signal in signals
    ):
        logger.warning("WorkIQ snapshot contains a provisional identity; refusing to persist it.")
        raise ValueError("WorkIQ snapshot contains a provisional identity.")
    return {"signals": [signal_to_record(signal) for signal in signals]}


def workiq_signals_from_payload(payload: dict[str, Any]) -> tuple[Signal, ...]:
    records = payload.get("signals")
    if not isinstance(records, list):
        return ()
    signals = tuple(signal_from_record(record) for record in records)
    for signal in signals:
        metadata = signal.metadata or {}
        if signal.source.startswith("workiq/") and has_provisional_workiq_identity(metadata):
            logger.warning("WorkIQ snapshot contains a provisional identity; cached observations cannot be used.")
            raise ValueError("WorkIQ snapshot contains a provisional identity.")
        if signal.source == "system" and "workiq_plan_budget_exhausted" in metadata:
            marker = metadata["workiq_plan_budget_exhausted"]
            source = metadata.get("integration_source")
            if (marker is True and source != "workiq") or (source == "workiq" and marker is not True):
                logger.warning("WorkIQ query-plan budget diagnostic has an invalid marker or source; cached observations cannot be used.")
                raise ValueError("WorkIQ query-plan budget diagnostic has an invalid marker or source.")
        budget_diagnostic = signal.source == "system" and metadata.get("workiq_plan_budget_exhausted") is True
        if (
            metadata.get("integration_source") == "workiq"
            and (
                metadata.get("quarantined") is True
                or metadata.get("workiq_structured_response_unusable") is True
                or budget_diagnostic
            )
        ):
            error = metadata.get("error")
            if not isinstance(error, str) or not error.strip() or (
                budget_diagnostic and (
                    error != WORKIQ_PLAN_BUDGET_MESSAGE
                    or metadata.get("quarantined") is True
                    or metadata.get("workiq_structured_response_unusable") is True
                )
            ):
                kind = (
                    "query-plan budget" if budget_diagnostic
                    else "quarantine" if metadata.get("quarantined") is True
                    else "discovery"
                )
                logger.warning("WorkIQ %s diagnostic has an invalid or missing error; cached observations cannot be used.", kind)
                raise ValueError(f"WorkIQ {kind} diagnostic has an invalid or missing error.")
    return signals


def resolve_workiq_signals(
    *,
    program_id: str,
    programs_root: Path | None,
    live_fetch_fn: Callable[[], tuple[Signal, ...]],
    now: datetime | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
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
                for signal in signals:
                    metadata = signal.metadata or {}
                    if (
                        signal.source == "system" and metadata.get("quarantined") is True
                        and metadata.get("integration_source") == "workiq"
                    ):
                        append_integration_error_once(
                            integration_error_sink, source="workiq", stage="quarantine",
                            error=metadata["error"], retryable=False,
                            operator_action=(
                                WORKIQ_INVALID_RECORD_OPERATOR_ACTION
                                if metadata.get("workiq_structured_record_invalid") is True
                                else "Obtain native observation IDs and timezone-qualified source times before reviewing these records."
                            ),
                        )
                    elif (
                        signal.source == "system" and metadata.get("integration_source") == "workiq"
                        and metadata.get("workiq_structured_response_unusable") is True
                    ):
                        append_integration_error_once(
                            integration_error_sink, source="workiq", stage="discovery",
                            error=metadata["error"], retryable=True,
                            operator_action="Retry structured WorkIQ discovery and verify the returned JSON format.",
                        )
                    elif (
                        signal.source == "system" and metadata.get("integration_source") == "workiq"
                        and metadata.get("workiq_plan_budget_exhausted") is True
                    ):
                        append_integration_error_once(
                            integration_error_sink, source="workiq", stage="budget",
                            error=WORKIQ_PLAN_BUDGET_MESSAGE, retryable=True,
                            operator_action=WORKIQ_PLAN_BUDGET_OPERATOR_ACTION,
                        )
                return signals
    return live_fetch_fn()


def _try_read_snapshot_signals(
    program_id: str, manifest: PrefetchSnapshotManifest, *, programs_root: Path
) -> tuple[Signal, ...] | None:
    try:
        try:
            payload = read_snapshot_payload(program_id, WORKIQ_PREFETCH_CHANNEL, manifest, programs_root=programs_root)
        except (PrefetchSnapshotPayloadTooDeepError, RecursionError):
            logger.warning("WorkIQ cached snapshot is too deeply nested; falling back to live.")
            return None
        if _is_too_deep(payload):
            logger.warning("WorkIQ cached snapshot is too deeply nested; falling back to live.")
            return None
        records = payload.get("signals") if isinstance(payload, dict) else None
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            logger.warning("WorkIQ cached snapshot has an invalid signals payload; falling back to live.")
            return None
        return workiq_signals_from_payload(payload)
    except (OSError, ValueError, KeyError, TypeError):
        # A corrupt/malformed committed snapshot degrades to the live path
        # rather than propagating a parse error into the gather run.
        return None


def _is_too_deep(value: Any) -> bool:
    limit = sys.getrecursionlimit()
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > limit:
            return True
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
    return False


__all__ = [
    "WORKIQ_PREFETCH_CHANNEL",
    "resolve_workiq_signals",
    "workiq_signals_from_payload",
    "workiq_signals_to_payload",
]
