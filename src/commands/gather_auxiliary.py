"""Bounded non-core helpers used by the Armada gather orchestrator.

Keeping these failure-isolated utilities outside ``gather.py`` lets the
orchestrator focus on lifecycle ordering and channel composition.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from src.commands.gather_workiq_helpers import _truncate_signal_text
from src.core.engms_signal_extractor import EngMsSignalExtractor, hashes_from_artifacts
from src.core.gather_channel_support import append_integration_error_once
from src.core.journal import archive_weekly_journal_files, archive_weekly_journal_files_by_retention, get_week_key
from src.core.journal_retention import load_signal_retention_policy
from src.core.models import Confidence
from src.core.models_v2 import IntegrationError, Signal


def build_integration_error_signal(
    *,
    program_id: str,
    source: str,
    error: str,
    as_of: datetime,
) -> Signal:
    normalized_error = error.strip() or "Unknown integration error"
    raw_ref = f"system:{source}:{as_of.isoformat()}"
    return Signal(
        id=str(uuid5(NAMESPACE_URL, f"{program_id}|{raw_ref}|{normalized_error}")),
        timestamp=as_of,
        source="system",
        program_id=program_id,
        workstream_id=None,
        entity_refs=(),
        text=_truncate_signal_text(f"{source} integration failed: {normalized_error}"),
        raw_ref=raw_ref,
        confidence=Confidence.NONE,
        metadata={"integration_source": source, "error": normalized_error},
    )


def compute_adaptive_window_days(
    program_id: str,
    *,
    signal_store: Any,
    as_of: datetime,
    default_days: int,
    lookback_days: int = 90,
) -> int:
    """Estimate a bounded evidence window from observed signal cadence."""
    del program_id  # retained in the public contract for future per-program policy.
    try:
        cutoff = as_of - timedelta(days=lookback_days)
        recent_signals = tuple(signal_store.signals_after(cutoff))
        if len(recent_signals) < 5:
            return default_days
        sorted_signals = sorted(recent_signals, key=lambda signal: signal.created_at or as_of)
        intervals_days = [
            (current.created_at - prior.created_at).total_seconds() / 86400.0
            for prior, current in zip(sorted_signals, sorted_signals[1:])
            if prior.created_at is not None and current.created_at is not None and current.created_at > prior.created_at
        ]
        if not intervals_days:
            return default_days
        median_interval = sorted(intervals_days)[len(intervals_days) // 2]
        return max(7, min(int(3 * median_interval), 45))
    except (OSError, AttributeError, TypeError, ValueError):
        return default_days


def archive_stale_weekly_journal_files(
    program_id: str,
    *,
    as_of: datetime,
    programs_root: Path,
    default_retention_weeks: int,
) -> tuple[Path, ...]:
    retention_policy = load_signal_retention_policy(program_id, programs_root=programs_root)
    if retention_policy is not None:
        return archive_weekly_journal_files_by_retention(
            program_id,
            as_of=as_of,
            retention_days_by_source=retention_policy.retention_days_by_source,
            default_retention_days=retention_policy.default_retention_days,
            programs_root=programs_root,
        )
    cutoff_week = get_week_key(as_of - timedelta(weeks=default_retention_weeks))
    return archive_weekly_journal_files(program_id, before_week=cutoff_week, programs_root=programs_root)


def build_engms_signals(
    *,
    items: tuple[Any, ...],
    program_id: str,
    previous_query_states: dict[str, dict[str, Any]],
    extractor: EngMsSignalExtractor | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
) -> tuple[tuple[Signal, ...], dict[str, Any]]:
    """Extract eng.ms signals without allowing an optional source to fail gather."""
    engms_extractor = extractor or EngMsSignalExtractor()
    previous_hashes = hashes_from_artifacts(previous_query_states.get("engms", {}))
    try:
        result = engms_extractor.extract(items, program_id, previous_hashes=previous_hashes)
    except Exception as exc:  # pragma: no cover - defensive optional integration boundary
        append_integration_error_once(integration_error_sink, source="engms", stage="gather", error=str(exc))
        return (), dict(previous_query_states.get("engms", {}))
    for error in result.errors:
        append_integration_error_once(
            integration_error_sink,
            source=error.source,
            stage=error.stage,
            error=error.message,
        )
    merged_state = dict(previous_query_states.get("engms", {}))
    merged_state.update(result.side_artifacts)
    return result.signals, merged_state


def run_sharepoint_ingest(
    *,
    program_id: str,
    programs_root: Path,
    existing_signals: tuple[Signal, ...],
    signal_store: Any,
    previous_gather_state: Any,
    include_lt_deck: bool,
    force_refresh: bool,
    as_of: datetime,
    pipeline_runner: Any | None,
    integration_error_sink: list[IntegrationError] | None,
) -> Any:
    """Run the optional SharePoint stage and isolate its failures."""
    from src.commands.gather_pipeline.sharepoint_ingest_stage import run_sharepoint_ingest_stage

    prior_sp_state = (
        previous_gather_state.m365_discovery.get("sharepoint", {})
        if previous_gather_state is not None
        else {}
    )
    try:
        return run_sharepoint_ingest_stage(
            program_id=program_id,
            programs_root=programs_root,
            existing_signals=existing_signals,
            signal_store=signal_store,
            prior_doc_states=prior_sp_state.get("doc_states", {}),
            batch_id=f"gather-{as_of.strftime('%Y%m%dT%H%M%S')}",
            include_lt_deck=include_lt_deck,
            force_refresh=force_refresh,
            as_of=as_of,
            pipeline_runner=pipeline_runner,
        )
    except Exception as exc:  # pragma: no cover - optional external integration boundary
        append_integration_error_once(integration_error_sink, source="sharepoint", stage="gather", error=str(exc))
        return None
