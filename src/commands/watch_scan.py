from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, cast

from src.commands import gather
from src.commands.gather import _DependencyQueryItems
from src.core.catchup_scan import CatchupEventBuilder, PROGRAMS_ROOT, SignalSummaryBuilder, SignalTransform, WatchCadence, WatchLoader, WatchPollResult, WatchSource
from src.core.exceptions import ConfigError
from src.core.kusto_client import build_live_kusto_query_executor
from src.core.models import WorkItem
from src.core.models_v2 import CatchupEvent, Program, Signal, SignalReviewDecision, Workstream
from src.core.signal_dedup import dedupe_signals
from src.core.signal_classification import classify_signal as _classify_signal
from src.core.store_factory import build_program_signal_store, build_program_trajectory_store
from src.m365.agency_bridge import AgencyBridge


@dataclass(frozen=True, slots=True)
class _UnavailableCapabilities:
    available: bool = False
    has_workiq: bool = False
    has_icm: bool = False
    server_tools: dict[str, tuple[str, ...]] | None = None


class _UnavailableBridge:
    def probe(self) -> _UnavailableCapabilities:
        return _UnavailableCapabilities(server_tools={})


def validate_watch_program(program: Program) -> None:
    if program.ado is None:
        raise ConfigError(f"Program '{program.id}' is missing ado configuration.")
    if program.maturity_level < 2:
        raise ConfigError(
            f"Program '{program.id}' is at maturity level {program.maturity_level}. "
            "'vertex watch' requires maturity_level >= 2."
        )


def watch_program_once(
    program_id: str,
    *,
    since: datetime,
    as_of: datetime | None = None,
    sources: tuple[WatchSource, ...] = (WatchSource.ADO,),
    cadence: WatchCadence = WatchCadence.INTRADAY,
    programs_root: Path = PROGRAMS_ROOT,
    loader: WatchLoader | None = None,
    full_loader: WatchLoader | None = None,
    freshness_loader: Callable | None = None,
    dependency_loader: Callable | None = None,
    program_context: tuple[Program, tuple[Workstream, ...]] | None = None,
    bridge_provider: type[AgencyBridge] | AgencyBridge | Callable[[], AgencyBridge] | None = None,
    auto_approve_signals: bool = True,
    signal_transform: SignalTransform | None = None,
    max_changed_items: int | None = None,
    workiq_timeout_seconds: int | None = None,
    workiq_total_budget_seconds: int | None = None,
    summary_builder: SignalSummaryBuilder | None = None,
    event_builder: CatchupEventBuilder | None = None,
) -> WatchPollResult:
    resolved_programs_root = programs_root
    current_time = _ensure_utc(as_of or _utc_now())
    window_start = _ensure_utc(since)

    program, workstreams = program_context or gather._load_program_context(program_id, resolved_programs_root)
    validate_watch_program(program)
    ado_config = program.ado
    if ado_config is None:
        raise ConfigError("unreachable: validated by validate_watch_program")
    signal_store = build_program_signal_store(program, programs_root=resolved_programs_root)
    trajectory_store = build_program_trajectory_store(program, programs_root=resolved_programs_root)

    selected_sources = _normalize_watch_sources(sources)
    incremental_items: tuple[WorkItem, ...] = ()
    incremental_ado_calls = 0
    freshness_items: tuple[WorkItem, ...] = ()
    freshness_ado_calls = 0
    dependency_items: tuple[_DependencyQueryItems, ...] = ()
    dependency_ado_calls = 0
    total_changed_items: int | None = None
    if WatchSource.ADO in selected_sources:
        if cadence is WatchCadence.DAILY:
            if freshness_loader is not None:
                freshness_items, freshness_ado_calls = freshness_loader(program, workstreams, current_time)
            else:
                _, freshness_items, freshness_ado_calls = gather._load_ado_items_via_uil(
                    program, workstreams, current_time,
                    since=current_time - timedelta(days=ado_config.date_window_days),
                    programs_root=resolved_programs_root,
                )
            dependency_items, dependency_ado_calls = (dependency_loader or gather._load_dependency_program_items)(
                program,
                workstreams,
                current_time,
            )
        else:
            if loader is not None:
                incremental_items, incremental_ado_calls = loader(program, workstreams, current_time, window_start)
            else:
                incremental_items, _, incremental_ado_calls = gather._load_ado_items_via_uil(
                    program, workstreams, current_time,
                    since=window_start,
                    programs_root=resolved_programs_root,
                )
            if max_changed_items is not None and len(incremental_items) > max_changed_items:
                total_changed_items = len(incremental_items)
                incremental_items = incremental_items[:max_changed_items]

    context_items = freshness_items if cadence is WatchCadence.DAILY and WatchSource.ADO in selected_sources else incremental_items
    context_ado_calls = 0
    if _watch_sources_need_full_context_items(selected_sources):
        if full_loader is not None:
            context_items, context_ado_calls = full_loader(program, workstreams, current_time, None)
        else:
            context_items, _, context_ado_calls = gather._load_ado_items_via_uil(
                program, workstreams, current_time,
                since=current_time - timedelta(days=ado_config.date_window_days),
                programs_root=resolved_programs_root,
            )

    dedupe_start = current_time - timedelta(days=ado_config.date_window_days)
    existing_signals = gather._read_recent_signals(
        program_id,
        start=dedupe_start,
        end=current_time,
        programs_root=resolved_programs_root,
        signal_store=signal_store,
    )

    candidate_signals: tuple[Signal, ...] = ()
    if WatchSource.ADO in selected_sources:
        if cadence is WatchCadence.DAILY:
            stale_warn_days, stale_block_days = gather._load_freshness_thresholds(program_id, resolved_programs_root)
            candidate_signals = (
                *gather._build_freshness_signals(
                    freshness_items,
                    program_id=program_id,
                    workstreams=workstreams,
                    as_of=current_time,
                    stale_warn_days=stale_warn_days,
                    stale_block_days=stale_block_days,
                ),
                *gather._build_dependency_signals(
                    dependency_items,
                    program_id=program_id,
                    workstreams=workstreams,
                    as_of=current_time,
                    stale_warn_days=stale_warn_days,
                    stale_block_days=stale_block_days,
                ),
            )
        else:
            candidate_signals = gather._build_ado_revision_signals(
                incremental_items,
                program_id=program_id,
                workstreams=workstreams,
                since=window_start,
            )

    resolved_bridge_provider = cast(
        type[AgencyBridge] | AgencyBridge | Callable[[], AgencyBridge],
        bridge_provider if bridge_provider is not None else _UnavailableBridge,
    )

    if WatchSource.WORKIQ in selected_sources:
        candidate_signals = (
            *candidate_signals,
            *gather._build_workiq_signals(
                program=program,
                program_id=program_id,
                as_of=current_time,
                items=context_items,
                workstreams=workstreams,
                bridge=resolved_bridge_provider,
                timeout_seconds=workiq_timeout_seconds,
                total_budget_seconds=workiq_total_budget_seconds,
            ),
        )
    if WatchSource.KUSTO in selected_sources:
        candidate_signals = (
            *candidate_signals,
            *gather._build_kusto_signals(
                program=program,
                program_id=program_id,
                programs_root=resolved_programs_root,
                as_of=current_time,
                workstreams=workstreams,
                executor=build_live_kusto_query_executor(),
            ),
        )
    analytics_ado_calls = 0
    if WatchSource.ANALYTICS in selected_sources:
        analytics_signals, analytics_ado_calls = gather._load_analytics_signals(program, workstreams, current_time)
        candidate_signals = (*candidate_signals, *analytics_signals)
    sprint_ado_calls = 0
    if WatchSource.SPRINTS in selected_sources:
        sprint_signals, sprint_ado_calls = gather._load_sprint_signals(
            program,
            workstreams,
            context_items,
            current_time,
        )
        candidate_signals = (*candidate_signals, *sprint_signals)
    if WatchSource.ICM in selected_sources:
        candidate_signals = (
            *candidate_signals,
            *gather._build_icm_signals(
                program=program,
                program_id=program_id,
                programs_root=resolved_programs_root,
                as_of=current_time,
                workstreams=workstreams,
                executor=build_live_kusto_query_executor(),
                bridge=resolved_bridge_provider,
            ),
        )

    if signal_transform is not None:
        candidate_signals = tuple(signal_transform(signal) for signal in candidate_signals)

    new_signals = dedupe_signals(candidate_signals, existing_signals=existing_signals)

    for signal in new_signals:
        signal_store.append(_classify_signal(signal))

    auto_reviews_written = 0
    if auto_approve_signals:
        for signal in new_signals:
            if not gather._is_auto_approved_signal(signal):
                continue
            signal_store.append_review(
                program_id,
                SignalReviewDecision(
                    signal_id=signal.id,
                    decision="approved",
                    reviewed_at=current_time,
                    reviewed_by="system",
                    note=None,
                ),
            )
            auto_reviews_written += 1

    trajectory_updates = 0
    if WatchSource.ADO in selected_sources:
        trajectory_source_items = freshness_items if cadence is WatchCadence.DAILY else incremental_items
        for item in trajectory_source_items:
            if trajectory_store.append(program_id, item.id, gather._trajectory_point_from_item(item, current_time)):
                trajectory_updates += 1

    dependency_item_count = sum(len(group.items) for group in dependency_items)
    scanned_items = max(len(incremental_items), len(freshness_items), len(context_items), dependency_item_count)
    total_ado_calls = incremental_ado_calls + freshness_ado_calls + dependency_ado_calls + context_ado_calls + analytics_ado_calls + sprint_ado_calls
    catchup_events = event_builder(new_signals) if event_builder is not None else ()
    new_signal_summaries = _build_signal_summaries(
        new_signals,
        catchup_events=catchup_events,
        summary_builder=summary_builder,
    )

    return WatchPollResult(
        program_id=program_id,
        since=window_start,
        polled_at=current_time,
        scanned_items=scanned_items,
        discovered_signals=len(candidate_signals),
        new_signals=len(new_signals),
        auto_reviews_written=auto_reviews_written,
        trajectory_updates=trajectory_updates,
        ado_calls=total_ado_calls,
        new_signal_summaries=new_signal_summaries,
        total_changed_items=total_changed_items,
        catchup_events=catchup_events,
    )


def _build_signal_summaries(
    new_signals: tuple[Signal, ...],
    *,
    catchup_events: tuple[CatchupEvent, ...],
    summary_builder: SignalSummaryBuilder | None,
) -> tuple[str, ...]:
    if catchup_events:
        return tuple(event.summary for event in catchup_events[:3])
    if summary_builder is not None:
        return summary_builder(new_signals)
    return tuple(signal.text for signal in new_signals[:3])


def _watch_sources_need_full_context_items(sources: tuple[WatchSource, ...]) -> bool:
    return any(source in {WatchSource.WORKIQ, WatchSource.SPRINTS} for source in sources)


def _normalize_watch_sources(sources: tuple[WatchSource, ...]) -> tuple[WatchSource, ...]:
    if not sources:
        return (WatchSource.ADO,)
    resolved: list[WatchSource] = []
    seen: set[WatchSource] = set()
    for source in sources:
        if source in seen:
            continue
        resolved.append(source)
        seen.add(source)
    return tuple(resolved)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)