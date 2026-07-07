from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import portalocker

import typer

from src.commands.reality import _build_delivery_date_snapshot_provider, _build_metric_definition_map, _load_expected_gather_cadence_hours
from src.commands import gather, watch as watch_command
from src.commands.watch_scan import watch_program_once
from src.core.feedback.catchup_classifier import build_catchup_events
from src.core.feedback.salience_modeler import SalienceEvent, append_salience_event, load_author_salience, predict_salience_event_weights, read_salience_events
from src.core.hypothesis_models import ChallengeKind, ChallengeState, DigestDelta
from src.core.catchup_runner import render_cached_catchup_banner, render_catchup_banner, run_catchup
from src.core.catchup_scan import CatchupEventBuilder, PROGRAMS_ROOT, SignalSummaryBuilder, WatchSource
from src.core.models_v2 import Signal
from src.core.reality_reconciler import reconcile_reality
from src.core.reality_store import RealityStore


_ACTIVE_CHALLENGE_STATES = {
    ChallengeState.OPEN,
    ChallengeState.ACKNOWLEDGED,
    ChallengeState.REOPENED,
    ChallengeState.SNOOZED,
}
_NATURAL_RESOLUTION_REASONS = {
    "delivery_date_recovered_on_reconcile",
    "staleness_recovered_on_reconcile",
    "data_loss_recovered_on_reconcile",
    "recovered_on_reconcile",
}


@dataclass(frozen=True, slots=True)
class RealityCatchupResult:
    program_id: str
    since: datetime
    as_of: datetime
    reason: str | None
    delta: DigestDelta
    resolved_naturally_count: int
    still_open_count: int
    resolved_staleness_challenge_ids: tuple[str, ...]


def catchup_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    since: str | None = typer.Option(None, "--since", help="Numeric hours for L0 catchup, or ISO date/time for L1 reality catch-up."),
    no_scan: bool = typer.Option(False, "--no-scan", help="Show the cached last catchup result without scanning ADO again."),
    source: list[str] = typer.Option(
        [WatchSource.ADO.value],
        "--source",
        help="Signal source to scan. Repeat or use comma-separated values: ado, workiq, kusto, analytics, sprints, icm.",
    ),
    interactive: bool = typer.Option(False, "--interactive", help="For ISO-date L1 catch-up, prompt once to acknowledge resolved staleness items."),
    notify: bool = typer.Option(False, "--notify", help="Emit a terminal bell after successful catchup output."),
    reason: str | None = typer.Option(None, "--reason", help="Optional note recorded with L1 catch-up audit events."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    since_text = since.strip() if isinstance(since, str) else None
    since_hours = _parse_since_hours(since_text)
    since_datetime = _parse_since_datetime(since_text)
    if since_datetime is not None or interactive or (isinstance(reason, str) and reason.strip()):
        if no_scan:
            raise typer.BadParameter("--no-scan is not supported with ISO-date L1 catch-up.")
        if since_datetime is None:
            raise typer.BadParameter("ISO date/time --since is required for L1 catch-up when --interactive or --reason is provided.")
        result = run_reality_catchup(
            program_id=program,
            since=since_datetime,
            reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
            db_root=db_root,
        )
        typer.echo(render_reality_catchup_summary(result))
        _emit_terminal_bell(enabled=notify)
        _append_catchup_audit_event(
            store=RealityStore(program.strip(), db_root=db_root),
            payload={
                "event_type": "catchup_summary",
                "recorded_at": result.as_of.isoformat(),
                "program_id": result.program_id,
                "since": result.since.isoformat(),
                "reason": result.reason,
                "delta": {
                    "challenges_opened": result.delta.challenges_opened,
                    "challenges_resolved": result.delta.challenges_resolved,
                    "challenges_dismissed": result.delta.challenges_dismissed,
                    "challenges_snoozed": result.delta.challenges_snoozed,
                    "hypotheses_proposed": result.delta.hypotheses_proposed,
                    "hypotheses_confirmed": result.delta.hypotheses_confirmed,
                    "hypotheses_recovered": result.delta.hypotheses_recovered,
                    "hypotheses_superseded": result.delta.hypotheses_superseded,
                },
                "resolved_naturally_count": result.resolved_naturally_count,
                "still_open_count": result.still_open_count,
            },
        )
        if interactive and result.resolved_staleness_challenge_ids:
            acknowledge = typer.confirm(
                f"Acknowledge {len(result.resolved_staleness_challenge_ids)} resolved staleness challenge(s)?",
                default=True,
            )
            if acknowledge:
                _append_catchup_audit_event(
                    store=RealityStore(program.strip(), db_root=db_root),
                    payload={
                        "event_type": "catchup_acknowledged",
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "program_id": result.program_id,
                        "since": result.since.isoformat(),
                        "challenge_ids": list(result.resolved_staleness_challenge_ids),
                        "reason": result.reason,
                    },
                )
                typer.echo(f"Acknowledged {len(result.resolved_staleness_challenge_ids)} resolved staleness challenge(s).")
        raise typer.Exit(code=0)
    if no_scan:
        typer.echo(render_cached_catchup_banner(program, programs_root=PROGRAMS_ROOT))
        _emit_terminal_bell(enabled=notify)
        raise typer.Exit(code=0)
    try:
        selected_sources = watch_command.parse_watch_sources(source)
        _validate_catchup_sources(program, selected_sources=selected_sources)
        catchup_result = run_catchup(
            program,
            since_hours=since_hours,
            sources=selected_sources,
            programs_root=PROGRAMS_ROOT,
            scan_func=watch_program_once,
            event_builder=build_catchup_event_builder(program, programs_root=PROGRAMS_ROOT),
        )
        typer.echo(render_catchup_banner(catchup_result))
        _emit_terminal_bell(enabled=notify)
        raise typer.Exit(code=0)
    except (typer.BadParameter, typer.Exit):
        raise
    except Exception as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)


def run_reality_catchup(
    *,
    program_id: str,
    since: datetime,
    reason: str | None,
    db_root: Path | None = None,
    as_of: datetime | None = None,
) -> RealityCatchupResult:
    store = RealityStore(program_id.strip(), db_root=db_root)
    store.initialize()
    resolved_as_of = as_of or datetime.now(timezone.utc)
    reconcile_reality(
        store=store,
        as_of=resolved_as_of,
        l1_observations_written=0,
        delivery_date_snapshot_provider=_build_delivery_date_snapshot_provider(program_id.strip()),
        metric_definitions_by_id=_build_metric_definition_map(as_of=resolved_as_of),
        expected_gather_cadence_hours=_load_expected_gather_cadence_hours(program_id.strip()),
    )
    delta = store.build_digest_delta(since=since, to=resolved_as_of)
    challenges = store.list_challenges()
    still_open = tuple(
        challenge
        for challenge in challenges
        if since < challenge.detected_at <= resolved_as_of and challenge.current_state in _ACTIVE_CHALLENGE_STATES
    )
    resolved_naturally = tuple(
        challenge
        for challenge in challenges
        if challenge.current_state is ChallengeState.RESOLVED
        and challenge.state_changed_at is not None
        and since < challenge.state_changed_at <= resolved_as_of
        and challenge.state_reason in _NATURAL_RESOLUTION_REASONS
    )
    resolved_staleness = tuple(
        challenge
        for challenge in resolved_naturally
        if challenge.challenge_kind is ChallengeKind.STALENESS
    )
    return RealityCatchupResult(
        program_id=program_id.strip(),
        since=since,
        as_of=resolved_as_of,
        reason=reason,
        delta=delta,
        resolved_naturally_count=len(resolved_naturally),
        still_open_count=len(still_open),
        resolved_staleness_challenge_ids=tuple(challenge.id for challenge in resolved_staleness),
    )


def render_reality_catchup_summary(result: RealityCatchupResult) -> str:
    lines = [f"Reality catch-up - {result.program_id}"]
    lines.append(
        "During your absence: "
        + f"{result.delta.challenges_opened} challenges opened, "
        + f"{result.resolved_naturally_count} resolved naturally on fresh data, "
        + f"{result.still_open_count} still open."
    )
    lines.append(
        "Hypotheses: "
        + f"{result.delta.hypotheses_recovered} recovered, "
        + f"{result.delta.hypotheses_superseded} superseded, "
        + f"{result.delta.hypotheses_confirmed} confirmed, "
        + f"{result.delta.hypotheses_proposed} proposed."
    )
    lines.append(f"Window: {result.since.isoformat()} -> {result.as_of.isoformat()}")
    if result.reason:
        lines.append(f"Reason: {result.reason}")
    if result.resolved_staleness_challenge_ids:
        lines.append(f"Resolved staleness challenges ready to acknowledge: {len(result.resolved_staleness_challenge_ids)}")
    return "\n".join(lines)


def _emit_terminal_bell(*, enabled: bool) -> None:
    if enabled:
        typer.echo("\a", nl=False)


def _append_catchup_audit_event(*, store: RealityStore, payload: dict[str, object]) -> None:
    store.initialize()
    path = store.db_path.parent / "_confirmations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _parse_since_hours(value: str | None) -> int | None:
    if value is None or not value:
        return None
    if value.isdigit():
        return int(value)
    return None


def _parse_since_datetime(value: str | None) -> datetime | None:
    if value is None or not value or value.isdigit():
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        parsed_date = None
    if parsed_date is not None:
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid --since value: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_catchup_sources(program_id: str, *, selected_sources: tuple[WatchSource, ...]) -> None:
    program, _workstreams = gather._load_program_context(program_id, PROGRAMS_ROOT)
    watch_command.validate_watch_program(program)
    issues = watch_command.get_watch_source_readiness_issues(
        program_id=program_id,
        program=program,
        selected_sources=selected_sources,
        programs_root=PROGRAMS_ROOT,
    )
    if issues:
        raise typer.BadParameter(
            f"Catchup source readiness failed for program '{program_id}': " + "; ".join(issues)
        )


def build_catchup_summary_builder(
    program_id: str,
    *,
    programs_root=PROGRAMS_ROOT,
) -> SignalSummaryBuilder:
    salience_weights = _load_salience_weights(program_id, programs_root=programs_root)
    return lambda signals: tuple(
        event.summary
        for event in _build_events_with_salience(
            program_id,
            signals,
            programs_root=programs_root,
            salience_weights=salience_weights,
            persist_confirmed_slips=False,
        )[:3]
    )


def build_catchup_event_builder(
    program_id: str,
    *,
    programs_root=PROGRAMS_ROOT,
) -> CatchupEventBuilder:
    salience_weights = _load_salience_weights(program_id, programs_root=programs_root)
    return lambda signals: _build_events_with_salience(
        program_id,
        signals,
        programs_root=programs_root,
        salience_weights=salience_weights,
        persist_confirmed_slips=True,
    )


def _load_salience_weights(program_id: str, *, programs_root=PROGRAMS_ROOT) -> dict[str, float]:
    salience = load_author_salience(program_id, programs_root=programs_root)
    return {
        entry.workstream_id: entry.attention_weight
        for entry in (salience.workstreams if salience is not None else ())
    }


def _build_events_with_salience(
    program_id: str,
    signals: tuple[Signal, ...],
    *,
    programs_root=PROGRAMS_ROOT,
    salience_weights: dict[str, float],
    persist_confirmed_slips: bool,
):
    events = build_catchup_events(signals, salience_weights=salience_weights)
    if persist_confirmed_slips:
        _append_confirmed_slip_events(
            program_id,
            catchup_signals=signals,
            catchup_events=events,
            programs_root=programs_root,
        )
    return events


def _append_confirmed_slip_events(
    program_id: str,
    *,
    catchup_signals: tuple[Signal, ...],
    catchup_events,
    programs_root=PROGRAMS_ROOT,
) -> None:
    salience_events = read_salience_events(program_id, programs_root=programs_root)
    confirmed_anomaly_ids = {
        event.anomaly_id
        for event in salience_events
        if event.action == "confirmed_slip"
    }
    current_signals_by_id = {signal.id: signal for signal in catchup_signals}

    for catchup_event in catchup_events:
        if catchup_event.kind != "eta_slip" or catchup_event.work_item_id is None or not catchup_event.workstream_id:
            continue
        matching_dismissal = _find_matching_dismissed_anomaly(
            salience_events,
            work_item_id=catchup_event.work_item_id,
            workstream_id=catchup_event.workstream_id,
            detected_at=catchup_event.detected_at,
            confirmed_anomaly_ids=confirmed_anomaly_ids,
        )
        if matching_dismissal is None:
            continue
        current_signal = current_signals_by_id.get(catchup_event.signal_id or "")
        if current_signal is None:
            continue
        weight_before, weight_after = predict_salience_event_weights(
            program_id,
            workstream_id=catchup_event.workstream_id,
            action="confirmed_slip",
            programs_root=programs_root,
        )
        decision_latency_ms = int((catchup_event.detected_at - matching_dismissal.recorded_at).total_seconds() * 1000)
        append_salience_event(
            program_id,
            SalienceEvent(
                event_id=f"{matching_dismissal.event_id}:confirmed:{catchup_event.event_id}",
                recorded_at=catchup_event.detected_at,
                anomaly_id=matching_dismissal.anomaly_id,
                workstream_id=catchup_event.workstream_id,
                action="confirmed_slip",
                work_item_id=catchup_event.work_item_id,
                decision_latency_ms=max(decision_latency_ms, 0),
                weight_before=weight_before,
                weight_after=weight_after,
                confirmed_within_30d=True,
            ),
            programs_root=programs_root,
        )
        confirmed_anomaly_ids.add(matching_dismissal.anomaly_id)


def _find_matching_dismissed_anomaly(
    salience_events: tuple[SalienceEvent, ...],
    *,
    work_item_id: int,
    workstream_id: str,
    detected_at,
    confirmed_anomaly_ids: set[str],
) -> SalienceEvent | None:
    cutoff = detected_at - timedelta(days=30)
    candidates = [
        event
        for event in salience_events
        if event.action == "dismissed"
        and event.work_item_id == work_item_id
        and event.workstream_id == workstream_id
        and cutoff <= event.recorded_at <= detected_at
        and event.anomaly_id not in confirmed_anomaly_ids
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda event: event.recorded_at, reverse=True)
    return candidates[0]