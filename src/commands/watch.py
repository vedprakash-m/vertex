from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from typing import Callable

import typer

from src.commands import gather
from src.commands.watch_scan import validate_watch_program, watch_program_once
from src.core.catchup_scan import WatchCadence, WatchLoader, WatchPollResult, WatchSource
from src.core.kusto_client import build_live_kusto_query_executor
from src.core.exceptions import AuthError, ConfigError, QueryError
from src.core.models import WorkItem
from src.core.models_v2 import KustoQuery, Program, Workstream
from src.m365.agency_bridge import AgencyBridge, AgencyCapabilities


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"

NowProvider = Callable[[], datetime]
SleepFunc = Callable[[float], None]
EmitFunc = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class WatchRunSummary:
    program_id: str
    interval_seconds: int
    cycles: int
    total_new_signals: int
    total_auto_reviews_written: int
    total_trajectory_updates: int
    total_ado_calls: int
    last_polled_at: datetime | None


def watch_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    interval: int = typer.Option(60, "--interval", min=1, help="Polling interval in seconds."),
    cadence: WatchCadence = typer.Option(WatchCadence.INTRADAY, "--cadence", help="Polling cadence: intraday or daily."),
    source: list[str] | None = typer.Option(
        None,
        "--source",
        help="Signal source to poll. Repeat or use comma-separated values: ado, workiq, kusto, analytics, sprints, icm.",
    ),
) -> None:
    try:
        summary = watch_program(
            program,
            interval_seconds=interval,
            sources=parse_watch_sources(source or []),
            cadence=cadence,
            emit=typer.echo,
        )
        typer.echo(render_watch_summary(summary))
        raise typer.Exit(code=0)
    except (AuthError, ConfigError, QueryError, typer.BadParameter) as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)


def watch_program(
    program_id: str,
    *,
    interval_seconds: int,
    sources: tuple[WatchSource, ...] | None = None,
    cadence: WatchCadence = WatchCadence.INTRADAY,
    programs_root: Path = PROGRAMS_ROOT,
    loader: WatchLoader | None = None,
    full_loader: WatchLoader | None = None,
    now_provider: NowProvider | None = None,
    sleep_func: SleepFunc = time.sleep,
    emit: EmitFunc | None = None,
    max_cycles: int | None = None,
) -> WatchRunSummary:
    resolved_programs_root = programs_root
    current_time_provider = now_provider or _utc_now
    emit_line = emit or (lambda _: None)

    program, workstreams = gather._load_program_context(program_id, resolved_programs_root)
    validate_watch_program(program)
    selected_sources = _resolve_watch_sources(
        program_id=program_id,
        program=program,
        sources=sources,
        cadence=cadence,
        programs_root=resolved_programs_root,
    )
    _validate_watch_source_readiness(
        program_id=program_id,
        program=program,
        selected_sources=selected_sources,
        programs_root=resolved_programs_root,
    )

    emit_line(
        f"Watching {program_id} every {interval_seconds}s for {cadence.value} {_describe_watch_sources(selected_sources)}. Press Ctrl+C to stop."
    )

    cycle_count = 0
    total_new_signals = 0
    total_auto_reviews = 0
    total_trajectory_updates = 0
    total_ado_calls = 0
    last_polled_at: datetime | None = None
    since = _ensure_utc(current_time_provider()) - timedelta(seconds=interval_seconds)

    try:
        while True:
            polled_at = _ensure_utc(current_time_provider())
            result = watch_program_once(
                program_id,
                since=since,
                as_of=polled_at,
                sources=selected_sources,
                cadence=cadence,
                programs_root=resolved_programs_root,
                loader=loader,
                full_loader=full_loader,
                program_context=(program, workstreams),
                bridge_provider=AgencyBridge,
            )
            emit_line(render_watch_poll_result(result))

            cycle_count += 1
            total_new_signals += result.new_signals
            total_auto_reviews += result.auto_reviews_written
            total_trajectory_updates += result.trajectory_updates
            total_ado_calls += result.ado_calls
            last_polled_at = result.polled_at
            since = result.polled_at

            if max_cycles is not None and cycle_count >= max_cycles:
                break

            sleep_func(interval_seconds)
    except KeyboardInterrupt:
        pass

    return WatchRunSummary(
        program_id=program_id,
        interval_seconds=interval_seconds,
        cycles=cycle_count,
        total_new_signals=total_new_signals,
        total_auto_reviews_written=total_auto_reviews,
        total_trajectory_updates=total_trajectory_updates,
        total_ado_calls=total_ado_calls,
        last_polled_at=last_polled_at,
    )


def render_watch_poll_result(result: WatchPollResult) -> str:
    return (
        f"[{result.polled_at.isoformat()}] scanned {result.scanned_items} item(s), "
        f"discovered {result.discovered_signals} signal(s), wrote {result.new_signals} new signal(s), "
        f"{result.auto_reviews_written} auto-review(s), {result.trajectory_updates} trajectory update(s), "
        f"{result.ado_calls} ADO call(s)."
    )


def render_watch_summary(summary: WatchRunSummary) -> str:
    if summary.cycles == 0:
        return f"Watch stopped for {summary.program_id} before any polls completed."
    return (
        f"Watch stopped for {summary.program_id} after {summary.cycles} poll(s): "
        f"{summary.total_new_signals} new signal(s), {summary.total_auto_reviews_written} auto-review(s), "
        f"{summary.total_trajectory_updates} trajectory update(s), {summary.total_ado_calls} ADO call(s)."
    )


def _validate_watch_source_readiness(
    *,
    program_id: str,
    program: Program,
    selected_sources: tuple[WatchSource, ...],
    programs_root: Path,
) -> None:
    issues = get_watch_source_readiness_issues(
        program_id=program_id,
        program=program,
        selected_sources=selected_sources,
        programs_root=programs_root,
    )

    if issues:
        raise typer.BadParameter(
            f"Watch source readiness failed for program '{program_id}': " + "; ".join(issues)
        )


def get_watch_source_readiness_issues(
    *,
    program_id: str,
    program: Program,
    selected_sources: tuple[WatchSource, ...],
    programs_root: Path,
) -> tuple[str, ...]:
    issues: list[str] = []

    agency_capabilities = AgencyCapabilities()
    if _watch_source_uses_agency(selected_sources, program=program):
        agency_capabilities = AgencyBridge().probe()

    kusto_queries: tuple[KustoQuery, ...] = ()
    kusto_query_error: str | None = None
    if _watch_source_uses_kusto(selected_sources):
        kusto_queries, kusto_query_error = _load_watch_kusto_queries(
            program_id=program_id,
            program=program,
            programs_root=programs_root,
        )

    if WatchSource.WORKIQ in selected_sources:
        issue = _workiq_watch_readiness_issue(program=program, agency_capabilities=agency_capabilities)
        if issue is not None:
            issues.append(issue)

    if WatchSource.KUSTO in selected_sources:
        issue = _kusto_watch_readiness_issue(
            program=program,
            kusto_queries=kusto_queries,
            kusto_query_error=kusto_query_error,
        )
        if issue is not None:
            issues.append(issue)

    if WatchSource.ICM in selected_sources:
        issue = _icm_watch_readiness_issue(
            program=program,
            agency_capabilities=agency_capabilities,
            kusto_queries=kusto_queries,
            kusto_query_error=kusto_query_error,
        )
        if issue is not None:
            issues.append(issue)

    return tuple(issues)


def _watch_source_uses_agency(sources: tuple[WatchSource, ...], *, program: Program) -> bool:
    return (
        (
            WatchSource.WORKIQ in sources
            and program.m365 is not None
            and program.m365.enabled
            and bool(program.m365.workiq_queries)
        )
        or (WatchSource.ICM in sources and gather._prefer_agency_icm(program))
    )


def _watch_source_uses_kusto(sources: tuple[WatchSource, ...]) -> bool:
    return WatchSource.KUSTO in sources or WatchSource.ICM in sources


def _load_watch_kusto_queries(
    *,
    program_id: str,
    program: Program,
    programs_root: Path,
) -> tuple[tuple[KustoQuery, ...], str | None]:
    if program.kusto is None or not program.kusto.enabled:
        return (), None
    try:
        return gather._load_kusto_queries(program_id, program=program, programs_root=programs_root), None
    except ConfigError as error:
        return (), str(error)


def _workiq_watch_readiness_issue(*, program: Program, agency_capabilities: AgencyCapabilities) -> str | None:
    if program.m365 is None or not program.m365.enabled or not program.m365.workiq_queries:
        return "source 'workiq' requires enabled m365.workiq_queries in program.yaml"
    if not agency_capabilities.available or not agency_capabilities.has_workiq:
        return "source 'workiq' requires Agency CLI WorkIQ access; run `vertex doctor --check-auth` or omit --source workiq"
    return None


def _kusto_watch_readiness_issue(
    *,
    program: Program,
    kusto_queries: tuple[KustoQuery, ...],
    kusto_query_error: str | None,
) -> str | None:
    if program.kusto is None or not program.kusto.enabled:
        return "source 'kusto' requires program.kusto.enabled with at least one applicable non-IcM query; run `vertex doctor --kusto` or omit --source kusto"
    if kusto_query_error is not None:
        return f"source 'kusto' is not ready: {kusto_query_error}. Run `vertex doctor --kusto` or omit --source kusto"
    if not any(not gather._is_icm_query(query) for query in kusto_queries):
        return "source 'kusto' requires at least one applicable non-IcM query; run `vertex doctor --kusto` or omit --source kusto"
    return None


def _icm_watch_readiness_issue(
    *,
    program: Program,
    agency_capabilities: AgencyCapabilities,
    kusto_queries: tuple[KustoQuery, ...],
    kusto_query_error: str | None,
) -> str | None:
    if _watch_has_agency_icm_path(program=program, agency_capabilities=agency_capabilities):
        return None
    if program.kusto is None or not program.kusto.enabled:
        return "source 'icm' requires Agency IcM access or program.kusto.enabled with an applicable IcM query; run `vertex doctor --check-auth` and `vertex doctor --kusto`, or omit --source icm"
    if kusto_query_error is not None:
        return f"source 'icm' is not ready: {kusto_query_error}. Run `vertex doctor --check-auth` and `vertex doctor --kusto`, or omit --source icm"
    if not any(gather._is_icm_query(query) for query in kusto_queries):
        return "source 'icm' requires Agency IcM access or at least one applicable Kusto-backed IcM query; run `vertex doctor --check-auth` and `vertex doctor --kusto`, or omit --source icm"
    return None


def _watch_has_agency_icm_path(*, program: Program, agency_capabilities: AgencyCapabilities) -> bool:
    if not gather._prefer_agency_icm(program):
        return False
    if not agency_capabilities.available or not agency_capabilities.has_icm:
        return False
    return "list_incidents" in agency_capabilities.server_tools.get("icm", ())


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_watch_sources(values: list[str]) -> tuple[WatchSource, ...]:
    if not values:
        return ()

    resolved: list[WatchSource] = []
    seen: set[WatchSource] = set()
    for value in values:
        for part in value.split(","):
            normalized = part.strip().lower()
            if not normalized:
                continue
            try:
                source = WatchSource(normalized)
            except ValueError as error:
                allowed = ", ".join(source.value for source in WatchSource)
                raise typer.BadParameter(f"Unsupported watch source '{normalized}'. Choose from: {allowed}.") from error
            if source in seen:
                continue
            seen.add(source)
            resolved.append(source)
    return tuple(resolved)


def _resolve_watch_sources(
    *,
    program_id: str,
    program: Program,
    sources: tuple[WatchSource, ...] | None,
    cadence: WatchCadence,
    programs_root: Path,
) -> tuple[WatchSource, ...]:
    if sources:
        return tuple(sources)
    if cadence is WatchCadence.INTRADAY and _is_icm_watch_ready(
        program_id=program_id,
        program=program,
        programs_root=programs_root,
    ):
        return (WatchSource.ADO, WatchSource.ICM)
    return (WatchSource.ADO,)


def _is_icm_watch_ready(*, program_id: str, program: Program, programs_root: Path) -> bool:
    agency_capabilities = AgencyCapabilities()
    if _watch_source_uses_agency((WatchSource.ICM,), program=program):
        agency_capabilities = AgencyBridge().probe()
    kusto_queries: tuple[KustoQuery, ...] = ()
    kusto_query_error: str | None = None
    if _watch_source_uses_kusto((WatchSource.ICM,)):
        kusto_queries, kusto_query_error = _load_watch_kusto_queries(
            program_id=program_id,
            program=program,
            programs_root=programs_root,
        )
    return _icm_watch_readiness_issue(
        program=program,
        agency_capabilities=agency_capabilities,
        kusto_queries=kusto_queries,
        kusto_query_error=kusto_query_error,
    ) is None


def _describe_watch_sources(sources: tuple[WatchSource, ...]) -> str:
    if sources == (WatchSource.ADO,):
        return "ADO signals"
    return "signal sources " + ", ".join(source.value for source in sources)


def _watch_sources_need_full_context_items(sources: tuple[WatchSource, ...]) -> bool:
    return WatchSource.WORKIQ in sources or WatchSource.SPRINTS in sources