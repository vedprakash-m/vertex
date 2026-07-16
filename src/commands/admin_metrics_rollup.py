"""ADF-W5.13 remainder (specs/arch-data-fix.md Section 9.7): the missing
scheduled/operator trigger for `src/core/weekly_metrics_store.py`'s rollup
engine. The engine itself was built and tested but had no caller -- this
is the CLI surface an operator (or a Task Scheduler/cron entry, per
`governance/runbooks/scheduled-tasks-runbook.md`) actually invokes weekly.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import typer

from src.core.ai_telemetry import ai_telemetry_path
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.measurement_store import tier_decision_store_path
from src.core.program_paths import get_run_telemetry_path
from src.core.weekly_metrics_store import rollup_jsonl_family_for_week

#: (source-path resolver, timestamp field, numeric fields to aggregate).
#: One entry per raw measurement family Section 9.7 names.
_FAMILY_SOURCES = {
    "tier_decisions": (tier_decision_store_path, "recorded_at", ("latency_ms", "input_tokens", "output_tokens", "cost_usd")),
    "ai_telemetry": (ai_telemetry_path, "ts", ("latency_ms", "tokens_in", "tokens_out", "cost_usd")),
    "run_telemetry": (get_run_telemetry_path, "started_at", ("wall_time_seconds",)),
}


def _current_iso_week(now: datetime) -> tuple[int, int]:
    iso_year, iso_week, _ = now.date().isocalendar()
    return iso_year, iso_week


def admin_metrics_rollup_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    family: str | None = typer.Option(
        None, "--family", help=f"Restrict to one family: {', '.join(_FAMILY_SOURCES)}. Default: all."
    ),
    iso_week: str | None = typer.Option(
        None, "--iso-week", help="ISO week to roll up, e.g. 2026-W28. Default: the current ISO week."
    ),
) -> None:
    """Computes and appends one ISO week's aggregate for one or all raw
    measurement families (Section 9.7's 13-month weekly rollup). Intended
    to run on a weekly schedule (see the scheduled-tasks runbook) alongside
    `vertex prefetch`/`vertex cockpit build` -- rolling up the prior
    complete week is the natural cadence, but any week may be named
    explicitly (e.g. to backfill a missed run)."""
    families = (family,) if family else tuple(_FAMILY_SOURCES)
    for name in families:
        if name not in _FAMILY_SOURCES:
            raise typer.BadParameter(f"Unknown --family {name!r}; use one of: {', '.join(_FAMILY_SOURCES)}.")

    now = datetime.now(timezone.utc)
    if iso_week is not None:
        try:
            year_str, week_str = iso_week.split("-W")
            target_year, target_week = int(year_str), int(week_str)
            date.fromisocalendar(target_year, target_week, 1)  # validates the week exists
        except (ValueError, IndexError):
            raise typer.BadParameter(f"--iso-week must look like 2026-W28, got {iso_week!r}.")
    else:
        target_year, target_week = _current_iso_week(now)

    for name in families:
        path_resolver, timestamp_field, numeric_fields = _FAMILY_SOURCES[name]
        source_path = path_resolver(program, programs_root=PROGRAMS_ROOT)
        record = rollup_jsonl_family_for_week(
            source_path,
            program_id=program,
            measurement_family=name,
            iso_year=target_year,
            iso_week=target_week,
            timestamp_field=timestamp_field,
            numeric_fields=numeric_fields,
            programs_root=PROGRAMS_ROOT,
            now=now,
        )
        if record is None:
            typer.echo(f"{name}: no rows in {target_year}-W{target_week:02d} -- nothing to roll up.")
        else:
            typer.echo(f"{name}: rolled up {record.record_count} row(s) for {target_year}-W{target_week:02d}.")


__all__ = ["admin_metrics_rollup_command"]
