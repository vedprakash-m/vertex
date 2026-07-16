"""ADF-W1.5/ADF-W1.10 remainder (specs/arch-data-fix.md Appendix A.7 /
Section 10.6): ``vertex prefetch`` -- the out-of-band, non-blocking writer
side of the prefetch snapshot mechanism.

Runs the slow live WorkIQ NL-search step (the historical p50 3,927.7s
latency source per Section 4's baseline) on an operator-controlled schedule
*outside* report/gather's own critical path, committing a content-addressed
snapshot (``src/core/prefetch_store.py``) that
``src/commands/gather_pipeline/workiq_prefetch_stage.py::resolve_workiq_signals``
then prefers over a live call. Never called from ``report``/``gather``
themselves -- this is a standalone command an operator (or Task Scheduler
per ADF-W5.10) invokes on its own cadence.

Wrapped in the same ``actuation_dispatch``-domain workspace lease other
write-side operations acquired in ADF-W1.10 -- a prefetch write competing
with a live ADO/actuation mutation for the same program now fails cleanly
(``LeaseHeldByAnotherOwner``) instead of racing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
import uuid

import typer

from src.commands.doctor_checks.milestone_health_checks import load_latest_confirmed_snapshot_items
from src.core.edition_resolver import PROGRAMS_ROOT, list_editions_for_program, load_program
from src.core.program_fact_store import load_current_workstreams
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root
from src.core.workspace_lease import ACTUATION_DISPATCH_DOMAIN, LeaseHeldByAnotherOwner, acquire_lease, release_lease
from src.m365.agency_bridge import AgencyBridge

_DEFAULT_TTL_SECONDS = 3600  # 1 hour -- an operator-tuned cadence overrides via --ttl-seconds


def prefetch_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    edition: str | None = typer.Option(
        None, "--edition", help="Edition id to resolve item context from (defaults to the program's first edition)."
    ),
    channel: str = typer.Option("workiq", "--channel", help="Prefetch channel (only 'workiq' is implemented)."),
    ttl_seconds: int = typer.Option(_DEFAULT_TTL_SECONDS, "--ttl-seconds", help="Snapshot freshness window."),
) -> None:
    if channel != "workiq":
        raise typer.BadParameter(f"Unsupported --channel {channel!r}; only 'workiq' is implemented.")

    program_obj = load_program(program, programs_root=PROGRAMS_ROOT)
    if program_obj is None:
        typer.echo(f"Program {program!r} not found under {PROGRAMS_ROOT}.", err=True)
        raise typer.Exit(code=1)

    resolved_edition = edition
    if resolved_edition is None:
        editions = list_editions_for_program(program, programs_root=PROGRAMS_ROOT)
        if not editions:
            typer.echo(f"No editions found for program {program!r}; pass --edition explicitly.", err=True)
            raise typer.Exit(code=1)
        resolved_edition = editions[0]

    workstreams = load_current_workstreams(program, programs_root=PROGRAMS_ROOT)
    archive_root: Path = get_archive_root(resolved_edition, ARCHIVE_ROOT)
    snapshot_items = load_latest_confirmed_snapshot_items(resolved_edition, archive_root=archive_root)
    items = snapshot_items[0] if snapshot_items is not None else ()
    if not items:
        typer.echo(
            f"No confirmed snapshot items found for edition {resolved_edition!r} -- "
            "WorkIQ query plans will have no item context.",
            err=True,
        )

    owner = f"vertex_prefetch:{uuid.uuid4().hex[:12]}"
    try:
        lease = acquire_lease(program, owner, mutation_domain=ACTUATION_DISPATCH_DOMAIN, programs_root=PROGRAMS_ROOT)
    except LeaseHeldByAnotherOwner:
        typer.echo(f"Program {program!r} actuation lease is busy; retry later.", err=True)
        raise typer.Exit(code=1)

    try:
        started_at = perf_counter()
        error: Exception | None = None
        signals: tuple = ()
        try:
            from src.commands.gather import _build_workiq_signals

            signals = _build_workiq_signals(
                program=program_obj,
                program_id=program,
                as_of=datetime.now(timezone.utc),
                items=items,
                workstreams=workstreams,
                bridge=AgencyBridge,
                programs_root=PROGRAMS_ROOT,
            )
        except Exception as exc:  # noqa: BLE001 -- any live-bridge failure degrades, never crashes the writer
            error = exc
        latency_ms = (perf_counter() - started_at) * 1000

        from src.commands.gather_pipeline.workiq_prefetch_stage import workiq_signals_to_payload
        from src.core.prefetch_store import write_prefetch_snapshot

        completeness = "complete" if error is None and signals else ("degraded" if error is not None else "partial")
        manifest = write_prefetch_snapshot(
            program_id=program,
            channel=channel,
            payload=workiq_signals_to_payload(signals),
            watermark=None,
            completeness=completeness,
            latency_ms=latency_ms,
            programs_root=PROGRAMS_ROOT,
            ttl_seconds=ttl_seconds,
        )
    finally:
        release_lease(lease, programs_root=PROGRAMS_ROOT)

    if error is not None:
        typer.echo(f"Prefetch for {program}/{channel} completed with an error (degraded snapshot): {error}", err=True)
    typer.echo(
        f"Prefetch committed: {program}/{channel} snapshot={manifest.snapshot_id[:12]} "
        f"signals={len(signals)} completeness={manifest.completeness} latency_ms={latency_ms:.0f} "
        f"expires_at={manifest.expires_at.isoformat()}"
    )


__all__ = ["prefetch_command"]
