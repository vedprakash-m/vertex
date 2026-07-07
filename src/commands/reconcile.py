from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from src.commands import gather as gather_helpers
from src.core.analytics_store import load_contradiction_state, replace_contradiction_state
from src.core.claim_tracker import load_open_claims
from src.core.contradiction_engine import build_contradiction_packets
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.feedback.calibration_router import load_forecast_calibration_modifier
from src.core.models import WorkItem
from src.core.models_v2 import ClaimEntry, ContradictionPacket, ForecastCalibrationModifier, Signal, Workstream
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.store_factory import build_signal_store_for_program_id


ProgramLoader = Callable[[str, Path], tuple[object, tuple[Workstream, ...]]]
ItemLoader = Callable[[object, tuple[Workstream, ...], datetime], tuple[tuple[WorkItem, ...], int]]
SignalLoader = Callable[[str, datetime, int, Path], tuple[Signal, ...]]
ClaimLoader = Callable[[str, Path], tuple[ClaimEntry, ...]]
CalibrationLoader = Callable[[str, Path], ForecastCalibrationModifier | None]


@dataclass(frozen=True, slots=True)
class ReconcileCommandArtifacts:
    program_id: str
    packets: tuple[ContradictionPacket, ...]
    cached: bool
    generated_at: datetime


def reconcile_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    refresh: bool = typer.Option(False, "--refresh", help="Recompute contradiction state instead of reading the cached analytics state."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute contradictions but skip updating the cached analytics state."),
) -> None:
    artifacts = generate_reconcile_report(
        program.strip(),
        refresh=refresh,
        dry_run=dry_run,
        programs_root=PROGRAMS_ROOT,
    )
    typer.echo(render_reconcile_report(artifacts))
    if dry_run and not artifacts.cached:
        typer.echo("Dry-run: skipped updating contradiction_state cache.")
    raise typer.Exit(code=0)


def generate_reconcile_report(
    program_id: str,
    *,
    refresh: bool,
    dry_run: bool,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
    program_loader: ProgramLoader | None = None,
    item_loader: ItemLoader | None = None,
    signal_loader: SignalLoader | None = None,
    claim_loader: ClaimLoader | None = None,
    calibration_loader: CalibrationLoader | None = None,
) -> ReconcileCommandArtifacts:
    current_time = _ensure_utc(as_of or datetime.now(timezone.utc))
    cached_packets = () if refresh else load_contradiction_state(program_id, programs_root=programs_root)
    if cached_packets:
        cached_generated_at = max(packet.generated_at for packet in cached_packets)
        return ReconcileCommandArtifacts(
            program_id=program_id,
            packets=cached_packets,
            cached=True,
            generated_at=cached_generated_at,
        )

    program, workstreams = (program_loader or gather_helpers._load_program_context)(program_id, programs_root)
    items, _ = (item_loader or _load_live_program_items)(program, workstreams, current_time)
    approved_signals = (signal_loader or _load_approved_signals)(
        program_id,
        current_time,
        30 if getattr(program, "ado", None) is None else getattr(program, "ado").date_window_days,
        programs_root,
    )
    open_claims = (claim_loader or load_open_claims)(program_id, programs_root)
    calibration_modifier = (calibration_loader or _load_calibration_modifier)(program_id, programs_root)
    packets = build_contradiction_packets(
        items=items,
        claims=open_claims,
        signals=approved_signals,
        workstreams=workstreams,
        as_of=current_time,
        calibration_modifier=calibration_modifier,
    )
    if not dry_run:
        replace_contradiction_state(program_id, packets, programs_root=programs_root)
    return ReconcileCommandArtifacts(
        program_id=program_id,
        packets=packets,
        cached=False,
        generated_at=current_time,
    )


def render_reconcile_report(artifacts: ReconcileCommandArtifacts) -> str:
    header = f"Active Contradictions - {artifacts.program_id}"
    lines = [header, "-" * len(header)]
    lines.append(f"Source: {'cached analytics state' if artifacts.cached else 'live recompute'}")
    lines.append(f"Generated: {artifacts.generated_at.strftime('%Y-%m-%d %H:%MZ')}")
    if not artifacts.packets:
        lines.append("No active contradictions.")
        return "\n".join(lines)

    grouped: dict[str, list[ContradictionPacket]] = defaultdict(list)
    for packet in artifacts.packets:
        grouped[packet.workstream_id or "unmapped"].append(packet)

    for workstream_id in sorted(grouped):
        lines.extend(["", f"{workstream_id}:"])
        for packet in sorted(grouped[workstream_id], key=lambda entry: entry.work_item_id):
            lines.append(f"  WI:{packet.work_item_id} [{packet.confidence.value}]")
            for contradiction in packet.contradictions:
                lines.append(
                    f"    - {contradiction.source_a} vs {contradiction.source_b}: {contradiction.summary}"
                )
            if packet.recommended_resolution is not None:
                recommendation = packet.recommended_resolution
                lines.append(
                    f"    recommendation: prefer {recommendation.winning_source.value} ({recommendation.confidence.value}) - {recommendation.rationale}"
                )
    return "\n".join(lines)


def _load_live_program_items(program: object, workstreams: tuple[Workstream, ...], as_of: datetime) -> tuple[tuple[WorkItem, ...], int]:
    items, _, ado_calls = gather_helpers._load_ado_items_via_uil(
        program, workstreams, as_of,  # type: ignore[arg-type]
        since=as_of - timedelta(days=getattr(getattr(program, "ado", None), "date_window_days", 90)),
        programs_root=PROGRAMS_ROOT,
    )
    return items, ado_calls


def _load_approved_signals(program_id: str, as_of: datetime, window_days: int, programs_root: Path) -> tuple[Signal, ...]:
    window_start = as_of - __import__("datetime", fromlist=["timedelta"]).timedelta(days=window_days)
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = signal_store.read_reviews(program_id)
    return tuple(
        signal
        for signal in signal_store.read(program_id, start=window_start, end=as_of)
        if signal_is_approved_for_evidence(signal, review_states)
    )


def _load_calibration_modifier(program_id: str, programs_root: Path) -> ForecastCalibrationModifier | None:
    return load_forecast_calibration_modifier(program_id, programs_root=programs_root)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)