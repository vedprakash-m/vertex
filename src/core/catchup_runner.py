from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

import portalocker
import yaml

from src.core.catchup_scan import PROGRAMS_ROOT, WatchPollResult, WatchSource
from src.core.catchup_state_store import CatchupState, get_catchup_lock_path, load_catchup_state, write_catchup_state
from src.core.models_v2 import ReviewPolicy, Signal


DEFAULT_CATCHUP_INTERVAL_MINUTES = 30
DEFAULT_FIRST_RUN_LOOKBACK_HOURS = 24
MAX_CATCHUP_CHANGED_ITEMS = 500
DEFAULT_WORKIQ_TIMEOUT_SECONDS = 30
DEFAULT_WORKIQ_TOTAL_BUDGET_SECONDS = 90


@dataclass(frozen=True, slots=True)
class CatchupRunResult:
    program_id: str
    result: WatchPollResult
    duration_seconds: float
    state_path: Path


ScanFunc = Callable[..., WatchPollResult]


def should_run_catchup(
    program_id: str,
    *,
    interval_minutes: int | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
) -> bool:
    settings = _load_catchup_settings(program_id, programs_root=programs_root)
    if not settings.enabled:
        return False
    state = load_catchup_state(program_id, programs_root=programs_root)
    if state is None:
        return True
    current_time = _ensure_utc(as_of or _utc_now())
    resolved_interval = settings.interval_minutes if interval_minutes is None else interval_minutes
    return current_time - state.last_catchup_at >= timedelta(minutes=resolved_interval)


def maybe_catchup(
    program_id: str,
    *,
    interval_minutes: int | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    emit: Callable | None = None,
    scan_func: ScanFunc | None = None,
    as_of: datetime | None = None,
    summary_builder=None,
    event_builder=None,
) -> CatchupRunResult | None:
    if not should_run_catchup(
        program_id,
        interval_minutes=interval_minutes,
        programs_root=programs_root,
        as_of=as_of,
    ):
        return None
    try:
        result = run_catchup(
            program_id,
            programs_root=programs_root,
            scan_func=_require_scan_func(scan_func),
            as_of=as_of,
            summary_builder=summary_builder,
            event_builder=event_builder,
        )
    except portalocker.exceptions.LockException:
        return None
    except Exception as error:
        _append_catchup_usage_event(
            program_id,
            event="catchup_failed",
            reason=str(error),
            programs_root=programs_root,
            recorded_at=_ensure_utc(as_of or _utc_now()),
        )
        return None
    if emit is not None:
        emit(render_catchup_banner(result))
    return result


def run_catchup(
    program_id: str,
    *,
    since_hours: int | None = None,
    sources: tuple[WatchSource, ...] = (WatchSource.ADO,),
    programs_root: Path = PROGRAMS_ROOT,
    scan_func: ScanFunc | None = None,
    as_of: datetime | None = None,
    summary_builder=None,
    event_builder=None,
) -> CatchupRunResult:
    current_time = _ensure_utc(as_of or _utc_now())
    settings = _load_catchup_settings(program_id, programs_root=programs_root)
    prior_state = load_catchup_state(program_id, programs_root=programs_root)
    if since_hours is not None:
        window_start = current_time - timedelta(hours=since_hours)
    elif prior_state is not None:
        window_start = prior_state.last_scan_cursor_ado
    else:
        window_start = current_time - timedelta(hours=DEFAULT_FIRST_RUN_LOOKBACK_HOURS)

    lock_path = get_catchup_lock_path(program_id, programs_root=programs_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_scan_func = _require_scan_func(scan_func)

    started_at = time.perf_counter()
    with portalocker.Lock(lock_path, mode="a", timeout=1, encoding="utf-8"):
        poll_result = resolved_scan_func(
            program_id,
            since=window_start,
            as_of=current_time,
            sources=sources,
            programs_root=programs_root,
            auto_approve_signals=False,
            signal_transform=_build_catchup_signal,
            max_changed_items=MAX_CATCHUP_CHANGED_ITEMS,
            workiq_timeout_seconds=settings.workiq_timeout_seconds,
            workiq_total_budget_seconds=settings.workiq_total_budget_seconds,
            summary_builder=summary_builder,
            event_builder=event_builder,
        )
        if _is_catchup_truncated(poll_result):
            _append_catchup_usage_event(
                program_id,
                event="catchup_truncated",
                reason=f"processed first {poll_result.scanned_items} of {poll_result.total_changed_items} changes",
                programs_root=programs_root,
                recorded_at=current_time,
                processed_changes=poll_result.scanned_items,
                total_returned=poll_result.total_changed_items,
            )
        state = CatchupState(
            last_catchup_at=current_time,
            last_catchup_source=_describe_catchup_sources(sources),
            last_scan_cursor_ado=poll_result.polled_at,
            last_result=poll_result,
        )
        state_path = write_catchup_state(program_id, state, programs_root=programs_root)
    duration_seconds = time.perf_counter() - started_at
    return CatchupRunResult(
        program_id=program_id,
        result=poll_result,
        duration_seconds=duration_seconds,
        state_path=state_path,
    )


def render_catchup_banner(result: CatchupRunResult) -> str:
    lookback = _describe_lookback(result.result.since, result.result.polled_at)
    return _render_catchup_text(
        header=(
        f"[Catchup {result.duration_seconds:.1f}s | Since {lookback}] "
        f"scanned {result.result.scanned_items} item(s), "
        f"discovered {result.result.discovered_signals} signal(s), "
            f"wrote {result.result.new_signals} new signal(s)."
        ),
        note=_build_truncation_note(result.result),
        summaries=result.result.new_signal_summaries,
    )


def render_cached_catchup_banner(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> str:
    state = load_catchup_state(program_id, programs_root=programs_root)
    if state is None or state.last_result is None:
        return f"No prior catchup state for {program_id}."
    lookback = _describe_lookback(state.last_result.since, state.last_result.polled_at)
    return _render_catchup_text(
        header=(
            f"[Catchup cached | Since {lookback}] scanned {state.last_result.scanned_items} item(s), "
            f"discovered {state.last_result.discovered_signals} signal(s), wrote {state.last_result.new_signals} new signal(s)."
        ),
        note=_build_truncation_note(state.last_result),
        summaries=state.last_result.new_signal_summaries,
    )


def _render_catchup_text(*, header: str, summaries: tuple[str, ...], note: str | None = None) -> str:
    detail_lines: list[str] = []
    if note is not None:
        detail_lines.append(f"  {note}")
    detail_lines.extend(f"  - {summary}" for summary in summaries)
    if not detail_lines:
        return header
    return "\n".join((header, *detail_lines))


def _build_truncation_note(result: WatchPollResult) -> str | None:
    if not _is_catchup_truncated(result):
        return None
    return (
        f"[Truncated: {result.scanned_items} of {result.total_changed_items} changes - "
        "run 'vertex gather' for full refresh]"
    )


def _is_catchup_truncated(result: WatchPollResult) -> bool:
    return result.total_changed_items is not None and result.total_changed_items > result.scanned_items


def _describe_lookback(start: datetime, end: datetime) -> str:
    delta = end - start
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{start.strftime('%a %H:%M')}, {hours}h {minutes}m ago"
    return f"{start.strftime('%a %H:%M')}, {minutes}m ago"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class _CatchupSettings:
    enabled: bool = True
    interval_minutes: int = DEFAULT_CATCHUP_INTERVAL_MINUTES
    workiq_timeout_seconds: int = DEFAULT_WORKIQ_TIMEOUT_SECONDS
    workiq_total_budget_seconds: int = DEFAULT_WORKIQ_TOTAL_BUDGET_SECONDS


def _load_catchup_settings(program_id: str, *, programs_root: Path) -> _CatchupSettings:
    path = programs_root / program_id / "program.yaml"
    if not path.exists():
        return _CatchupSettings()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return _CatchupSettings()
    if not isinstance(document, dict):
        return _CatchupSettings()
    catchup = document.get("catchup")
    if not isinstance(catchup, dict):
        return _CatchupSettings()

    enabled = catchup.get("enabled")
    interval = catchup.get("catchup_interval_minutes")
    workiq_timeout = catchup.get("workiq_timeout_seconds")
    workiq_total_budget = catchup.get("workiq_total_budget_seconds")
    resolved_enabled = bool(enabled) if isinstance(enabled, bool) else True
    return _CatchupSettings(
        enabled=resolved_enabled,
        interval_minutes=_coerce_positive_int(interval, default=DEFAULT_CATCHUP_INTERVAL_MINUTES),
        workiq_timeout_seconds=_coerce_positive_int(workiq_timeout, default=DEFAULT_WORKIQ_TIMEOUT_SECONDS),
        workiq_total_budget_seconds=_coerce_positive_int(
            workiq_total_budget,
            default=DEFAULT_WORKIQ_TOTAL_BUDGET_SECONDS,
        ),
    )


def _coerce_positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        resolved = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            resolved = int(value.strip())
        except ValueError:
            return default
    else:
        return default
    if resolved < 1:
        return default
    return resolved


def _require_scan_func(scan_func: ScanFunc | None) -> ScanFunc:
    if scan_func is None:
        raise ValueError("run_catchup requires a scan_func implementation.")
    return scan_func


def _build_catchup_signal(signal: Signal) -> Signal:
    raw_ref = signal.raw_ref or signal.id
    return replace(
        signal,
        id=str(uuid5(NAMESPACE_URL, f"{signal.program_id}|vertex/catchup|{raw_ref}|{signal.timestamp.isoformat()}")),
        source="vertex/catchup",
        metadata={
            **(signal.metadata or {}),
            "catchup_origin": signal.source,
        },
        review_policy=ReviewPolicy.PENDING,
    )


def _describe_catchup_sources(sources: tuple[WatchSource, ...]) -> str:
    if not sources:
        return WatchSource.ADO.value
    return "+".join(source.value for source in sources)


def get_catchup_usage_log_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "usage_log.jsonl"


def _append_catchup_usage_event(
    program_id: str,
    *,
    event: str,
    reason: str,
    programs_root: Path,
    recorded_at: datetime,
    **metadata: object,
) -> Path:
    path = get_catchup_usage_log_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": event,
        "reason": reason,
        "recorded_at": _ensure_utc(recorded_at).isoformat(),
        **metadata,
    }
    with portalocker.Lock(path, mode="a", timeout=5, encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
        handle.flush()
    return path