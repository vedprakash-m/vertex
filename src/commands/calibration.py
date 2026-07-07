from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any

import typer

from src.commands import gather as gather_helpers
from src.core.calibration_engine import CalibrationReport, build_calibration_report
from src.core.claim_tracker import load_claim_entries, load_latest_claim_statuses
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.feedback.calibration_router import refresh_forecast_calibration
from src.core.models_v2 import ForecastCalibrationModifier


app = typer.Typer(help="Inspect historical claim calibration.", invoke_without_command=True)
_SINCE_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")


@dataclass(frozen=True, slots=True)
class CalibrationCommandArtifacts:
    report: CalibrationReport
    modifier: ForecastCalibrationModifier
    feedback_path: Path | None


@app.callback(invoke_without_command=True)
def calibration_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    since: str | None = typer.Option(None, "--since", help="Inclusive ISO week filter, for example 2025-W01."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render the report but skip writing forecast_calibration.yaml."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _report_calibration(program.strip(), since=since, dry_run=dry_run)
    raise typer.Exit(code=0)


@app.command("report")
def report_calibration_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    since: str | None = typer.Option(None, "--since", help="Inclusive ISO week filter, for example 2025-W01."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render the report but skip writing forecast_calibration.yaml."),
) -> None:
    _report_calibration(program.strip(), since=since, dry_run=dry_run)
    raise typer.Exit(code=0)


def generate_calibration_report(
    program_id: str,
    *,
    since: str | None,
    dry_run: bool,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
) -> CalibrationCommandArtifacts:
    current_time = _ensure_utc(as_of or _utc_now())
    since_date = _parse_since_week(since)
    program, workstreams = gather_helpers._load_program_context(program_id, programs_root)
    items, _, _ = gather_helpers._load_ado_items_via_uil(
        program,
        workstreams,
        current_time,
        since=current_time - timedelta(days=program.ado.date_window_days if program.ado else 90),
        programs_root=programs_root,
    )
    report = build_calibration_report(
        program_id,
        claims=load_claim_entries(program_id, programs_root=programs_root),
        items=items,
        as_of=current_time,
        latest_statuses=load_latest_claim_statuses(program_id, programs_root=programs_root),
        since=since_date,
    )
    modifier, feedback_path = refresh_forecast_calibration(
        program_id,
        workstream_rows=report.workstream_rows,
        dri_rows=report.dri_rows,
        as_of=current_time,
        since=since_date,
        programs_root=programs_root,
        dry_run=dry_run,
    )
    return CalibrationCommandArtifacts(
        report=report,
        modifier=modifier,
        feedback_path=feedback_path,
    )


def render_calibration_report(report: CalibrationReport, modifier: ForecastCalibrationModifier) -> str:
    header = f"Claim Accuracy ({_window_label(report)}, {report.total_terminal_claims} terminal claims)"
    lines = [header, "-" * len(header)]
    if report.total_terminal_claims == 0 or report.overall_accuracy is None:
        lines.append("Overall:     no terminal claims in scope")
    else:
        lines.append(f"Overall:     {_format_percent(report.overall_accuracy)} met ({report.met}/{report.total_terminal_claims})")
    lines.append(f"Trajectory:  {_render_trajectory(report)}")
    lines.extend(["", "Worst workstreams:"])
    lines.extend(_render_workstream_lines(report))
    lines.extend(["", "Best DRIs:"])
    lines.extend(_render_dri_lines(report, reverse=True))
    lines.extend(["", "Worst DRIs:"])
    lines.extend(_render_dri_lines(report, reverse=False))
    lines.extend(["", "Forecast Bias Applied:"])
    lines.extend(_render_modifier_lines(modifier))
    return "\n".join(lines)


def _report_calibration(program_id: str, *, since: str | None, dry_run: bool) -> None:
    artifacts = generate_calibration_report(
        program_id,
        since=since,
        dry_run=dry_run,
        programs_root=PROGRAMS_ROOT,
    )
    typer.echo(render_calibration_report(artifacts.report, artifacts.modifier))
    if dry_run:
        typer.echo("Dry-run: skipped writing forecast_calibration.yaml.")
    elif artifacts.feedback_path is not None:
        typer.echo(f"Updated feedback: {artifacts.feedback_path}")


def _render_workstream_lines(report: CalibrationReport) -> list[str]:
    ordered = sorted(
        report.workstream_rows,
        key=lambda row: (_rollup_accuracy(row.met, row.sample_size), -row.sample_size, row.workstream_id),
    )
    if not ordered:
        return ["  None"]
    return [
        f"  {row.workstream_id}: {_format_percent(_rollup_accuracy(row.met, row.sample_size))} met ({row.met}/{row.sample_size}) - {row.contradicted} contradicted, {row.stale} stale"
        for row in ordered[:3]
    ]


def _render_dri_lines(report: CalibrationReport, *, reverse: bool) -> list[str]:
    ordered = sorted(
        report.dri_rows,
        key=lambda row: (_rollup_accuracy(row.met, row.sample_size), row.sample_size, row.subject_id),
        reverse=reverse,
    )
    if not ordered:
        return ["  None"]
    return [
        f"  {row.subject_id}: {_format_percent(_rollup_accuracy(row.met, row.sample_size))} met ({row.met}/{row.sample_size}) - {row.contradicted} contradicted, {row.stale} stale"
        for row in ordered[:3]
    ]


def _render_modifier_lines(modifier: ForecastCalibrationModifier) -> list[str]:
    lines: list[str] = []
    for workstream_id, slip_modifier in sorted(
        modifier.workstream_modifiers.items(),
        key=lambda item: (-item[1], item[0]),
    )[:3]:
        lines.append(f"  {workstream_id}: slip probability +{slip_modifier:.2f} (calibrated)")
    for dri_alias, slip_modifier in sorted(
        modifier.dri_modifiers.items(),
        key=lambda item: (-item[1], item[0]),
    )[:3]:
        lines.append(f"  {dri_alias} items: slip probability +{slip_modifier:.2f} (calibrated)")
    if not lines:
        lines.append("  None with >=5 samples")
    return lines


def _render_trajectory(report: CalibrationReport) -> str:
    if report.trajectory_delta_points is None:
        return "insufficient history for trend"
    if report.trajectory_delta_points > 0:
        return f"Improving (+{report.trajectory_delta_points} pp, last {report.trend_window_weeks} weeks)"
    if report.trajectory_delta_points < 0:
        return f"Declining ({report.trajectory_delta_points} pp, last {report.trend_window_weeks} weeks)"
    return f"Flat (0 pp, last {report.trend_window_weeks} weeks)"


def _window_label(report: CalibrationReport) -> str:
    if report.total_terminal_claims == 0:
        return "0 weeks"
    return f"{report.week_span} weeks"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{round(value * 100):d}%"


def _rollup_accuracy(met: int, sample_size: int) -> float:
    if sample_size <= 0:
        return 0.0
    return met / sample_size


def _parse_since_week(value: str | None) -> date | None:
    if value is None:
        return None
    match = _SINCE_PATTERN.match(value.strip())
    if match is None:
        raise typer.BadParameter("--since must use ISO week format YYYY-Www, for example 2025-W01.")
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError as error:
        raise typer.BadParameter("--since must reference a valid ISO week.") from error


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@app.command("edit-distance-trend")
def edit_distance_trend_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    window: int = typer.Option(10, "--window", min=2, help="Number of most-recent confirmed issues to consider."),
    min_issues: int = typer.Option(4, "--min-issues", min=2, help="Minimum issues required to compute a trend (default 4)."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """Show draft↔confirm edit distance trend per task type (WS-22 learning-loop efficacy)."""
    from src.ai.edit_learner import compute_edit_distance_trend  # noqa: PLC0415

    trends = compute_edit_distance_trend(
        program.strip(),
        window_issues=window,
        min_issues_for_trend=min_issues,
        programs_root=programs_root,
    )
    if not trends:
        typer.echo("No edit patterns recorded yet. Run at least one confirmed issue with AI draft to populate.")
        raise typer.Exit(code=0)

    typer.echo(f"Edit Distance Trend ({program}, last {window} issues)")
    typer.echo("-" * 50)
    for trend in trends:
        if trend.direction == "insufficient_data":
            typer.echo(
                f"  {trend.task_type}: insufficient data ({trend.issue_count} issue(s), need {min_issues})"
            )
        else:
            arrow = {"improving": "↓ improving", "declining": "↑ declining", "flat": "→ flat"}[trend.direction]
            typer.echo(
                f"  {trend.task_type}: {arrow} "
                f"(early={trend.mean_override_early:.3f}, late={trend.mean_override_late:.3f}, "
                f"delta={trend.delta:+.3f}, n={trend.issue_count})"
            )
    raise typer.Exit(code=0)


def render_edit_distance_trends(trends: tuple) -> str:
    """Render edit distance trends as a text block for embedding in reports."""
    if not trends:
        return "Edit Distance Trend: no data"
    lines = []
    for trend in trends:
        if trend.direction == "insufficient_data":
            lines.append(f"  {trend.task_type}: insufficient data ({trend.issue_count} issues)")
        else:
            arrow = {"improving": "↓", "declining": "↑", "flat": "→"}.get(trend.direction, "?")
            lines.append(
                f"  {trend.task_type}: {arrow} {trend.direction} "
                f"(Δ={trend.delta:+.3f}, early={trend.mean_override_early:.3f}, late={trend.mean_override_late:.3f})"
            )
    return "\n".join(lines)