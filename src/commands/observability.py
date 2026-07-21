"""WS-17: ``vertex observability`` subcommand.

Subcommands:
  - ``vertex observability diagnose --program <id>`` —
    explain the last gather failure (failure taxonomy, open alerts,
    per-channel failures).
  - ``vertex observability perf --program <id>`` —
    per-channel P50/P95 + SLO status.
  - ``vertex observability bundle --program <id> [--to <path>]`` —
    write a redacted support bundle.

Why a separate subcommand? ``vertex doctor`` is already 1449 LOC and
gated by ``run_doctor()``'s "choose one option" rule. New observability
flags (especially ``--diagnose`` and ``--perf``) need to take a
``--program`` argument, not an ``--edition``, which would force a
backwards-incompatible change to the existing doctor signature.

A dedicated subcommand is cleaner and is the established pattern for
program-scoped operations (see ``vertex privacy`` for the analog).
"""
from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path
from typing import Any

import typer

from src.commands.doctor_checks.observability_checks import (
    build_diagnose_report,
    build_perf_report,
)
from src.core.alerts import (
    AlertRecord,
    AlertSeverity,
    append_alert,
    read_alerts,
    resolve_alert,
    surface_alert_banner,
)
from src.core.config_loader import PROGRAMS_ROOT
from src.core.support_bundle import (
    SupportBundleResult,
    build_support_bundle,
)


observability_app = typer.Typer(
    help="SRE-grade observability: failure diagnosis, per-channel perf, support bundle.",
    no_args_is_help=True,
)


@observability_app.command("diagnose")
def observability_diagnose_command(
    program: str = typer.Option(..., "--program", help="Program id to diagnose."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
    window: int = typer.Option(1, "--window", help="How many recent run_telemetry rows to scan (default 1 = last run)."),
) -> None:
    """Explain the last gather failure for the program."""
    report = build_diagnose_report(
        program,
        programs_root=PROGRAMS_ROOT,
        window=window,
    )
    if format == "human":
        typer.echo(f"VERTEX DIAGNOSE — {program}")
        typer.echo("=" * (16 + len(program)))
        for finding in report.findings:
            icon = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "info": "INFO"}[finding.severity]
            typer.echo(f"{icon:4} {finding.label:<18} {finding.detail}")
            if finding.next_command:
                typer.echo(f"     -> {finding.next_command}")
        typer.echo("")
        typer.echo(f"Open alerts: {report.open_alert_count}")
        if report.last_failure_category is not None:
            typer.echo(
                f"Last failure category: {report.last_failure_category.value} "
                f"({'retryable' if report.last_failure_retryable else 'persistent'})"
            )
            typer.echo(f"Next command: {report.last_failure_next_command}")
    else:
        typer.echo(render_diagnose_output(report, format=format), nl=False)
    # Exit non-zero if any finding is "fail".
    has_fail = any(f.severity == "fail" for f in report.findings)
    raise typer.Exit(code=1 if has_fail else 0)


@observability_app.command("perf")
def observability_perf_command(
    program: str = typer.Option(..., "--program", help="Program id to inspect."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
    window: int = typer.Option(10, "--window", help="How many recent run_telemetry rows to aggregate (default 10)."),
) -> None:
    """Per-channel P50/P95 latency + SLO status."""
    report = build_perf_report(
        program,
        programs_root=PROGRAMS_ROOT,
        window=window,
    )
    if format == "human":
        typer.echo(f"VERTEX PERF — {program} (last {report.run_count} run(s), {report.channel_count} channel(s))")
        typer.echo("=" * (16 + len(program)))
        typer.echo(f"{'channel':<12} {'runs':<5} {'p50':<10} {'p95':<10} {'SLO':<6} {'failures'}")
        for summary in report.summaries:
            p50 = f"{summary.p50_latency_ms}ms" if summary.p50_latency_ms is not None else "—"
            p95 = f"{summary.p95_latency_ms}ms" if summary.p95_latency_ms is not None else "—"
            typer.echo(
                f"{summary.channel:<12} {summary.run_count:<5} {p50:<10} {p95:<10} "
                f"{summary.slo_status:<6} {summary.failures}"
            )
        typer.echo("")
        typer.echo(f"Overall SLO: {report.slo_status_overall}")
    else:
        typer.echo(render_perf_output(report, format=format), nl=False)
    raise typer.Exit(code=0)


@observability_app.command("bundle")
def observability_bundle_command(
    program: str = typer.Option(..., "--program", help="Program id to bundle."),
    to: Path | None = typer.Option(None, "--to", help="Output path. Defaults to programs/<id>/_alerts/support_bundle_<ts>.tar.gz"),
    archive_root: Path | None = typer.Option(None, "--archive-root", help="Optional archive root (defaults to repo archive/)."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    """Build a redacted support bundle (.tar.gz) for SRE triage."""
    if archive_root is None:
        from src.core.snapshot_store import ARCHIVE_ROOT
        archive_root = ARCHIVE_ROOT
    result = build_support_bundle(
        program,
        programs_root=PROGRAMS_ROOT,
        archive_root=archive_root,
        output_path=to,
    )
    if format == "human":
        typer.echo(f"VERTEX SUPPORT BUNDLE — {program}")
        typer.echo("=" * (24 + len(program)))
        typer.echo(f"  Path:           {result.bundle_path}")
        typer.echo(f"  Files included: {result.file_count}")
        typer.echo(f"  Bundle size:    {result.size_bytes} B")
        typer.echo(f"  Redactions:     {result.redaction_count}")
    else:
        typer.echo(render_bundle_output(result, format=format), nl=False)
    raise typer.Exit(code=0)


# --- alert subcommands (WS-17 between-runs alerts) ---


alerts_app = typer.Typer(
    help="Between-runs alert management (WS-17).",
    no_args_is_help=True,
)


@alerts_app.command("show")
def alerts_show_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    include_resolved: bool = typer.Option(False, "--include-resolved", help="Include resolved alerts."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    """List alerts for a program (open by default)."""
    alerts = read_alerts(program, programs_root=PROGRAMS_ROOT, include_resolved=include_resolved)
    if format == "human":
        if not alerts:
            typer.echo(f"No {'alerts' if include_resolved else 'open alerts'} for {program}.")
        else:
            typer.echo(f"VERTEX ALERTS — {program} ({len(alerts)} {'incl. resolved' if include_resolved else 'open'})")
            typer.echo("=" * (16 + len(program)))
            for a in alerts:
                icon = {"info": "I", "warn": "W", "error": "E", "critical": "C"}.get(a.severity, "?")
                resolved_marker = " [resolved]" if a.resolved_at is not None else ""
                typer.echo(f"  {icon}/{a.alert_id:<12} {a.severity:<10} {a.category:<20}{resolved_marker}")
                typer.echo(f"    {a.message}")
                if a.next_command:
                    typer.echo(f"    -> {a.next_command}")
    else:
        typer.echo(render_alerts_output(alerts, format=format, include_resolved=include_resolved), nl=False)
    raise typer.Exit(code=0)


@alerts_app.command("append")
def alerts_append_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    severity: str = typer.Option("warn", "--severity", help="info | warn | error | critical."),
    category: str = typer.Option("unknown", "--category", help="Failure category (failure taxonomy)."),
    message: str = typer.Option(..., "--message", help="Alert message."),
    next_command: str = typer.Option("", "--next-command", help="Operator next command."),
    format: str = typer.Option("human", "--format", help="Output format: human, json."),
) -> None:
    """Append a new alert (operator- or tool-curated)."""
    if severity not in (AlertSeverity.INFO, AlertSeverity.WARN, AlertSeverity.ERROR, AlertSeverity.CRITICAL):
        raise typer.BadParameter("--severity must be info|warn|error|critical.")
    from datetime import datetime, timezone
    import uuid
    record = AlertRecord(
        alert_id=f"alt-{uuid.uuid4().hex[:8]}",
        program_id=program,
        severity=severity,
        category=category,
        message=message,
        next_command=next_command,
        created_at=datetime.now(timezone.utc),
    )
    append_alert(record, programs_root=PROGRAMS_ROOT)
    if format == "human":
        typer.echo(f"Appended alert {record.alert_id} ({severity}/{category}).")
    else:
        typer.echo(json.dumps({"alert_id": record.alert_id, "severity": severity}, sort_keys=True) + "\n", nl=False)
    raise typer.Exit(code=0)


@alerts_app.command("resolve")
def alerts_resolve_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    alert_id: str = typer.Option(..., "--alert-id", help="Alert id to resolve."),
    format: str = typer.Option("human", "--format", help="Output format: human, json."),
) -> None:
    """Mark an alert resolved (append-only; no in-place rewrite)."""
    resolved = resolve_alert(alert_id, program_id=program, programs_root=PROGRAMS_ROOT)
    if format == "human":
        if resolved:
            typer.echo(f"Resolved {alert_id}.")
        else:
            typer.echo(f"No open alert with id {alert_id}.", err=True)
    else:
        typer.echo(json.dumps({"alert_id": alert_id, "resolved": resolved}, sort_keys=True) + "\n", nl=False)
    raise typer.Exit(code=0 if resolved else 2)


@alerts_app.command("banner")
def alerts_banner_command(
    program: str = typer.Option(..., "--program", help="Program id."),
) -> None:
    """Print the next-run banner (or nothing if all clear)."""
    banner = surface_alert_banner(program, programs_root=PROGRAMS_ROOT)
    if banner is None:
        raise typer.Exit(code=0)
    typer.echo(banner)
    raise typer.Exit(code=1)


# ----- renderers (json/csv) -----


def render_diagnose_output(report: Any, *, format: str) -> str:
    payload = report.to_dict()
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(("program_id", "severity", "label", "detail", "next_command"))
        for finding in payload["findings"]:
            writer.writerow((payload["program_id"], finding["severity"], finding["label"], finding["detail"], finding.get("next_command") or ""))
        return buf.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_perf_output(report: Any, *, format: str) -> str:
    payload = report.to_dict()
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(("program_id", "channel", "run_count", "p50_ms", "p95_ms", "slo_status", "failures"))
        for summary in payload["channels"]:
            writer.writerow((
                payload["program_id"], summary["channel"], summary["run_count"],
                summary["p50_latency_ms"] or "", summary["p95_latency_ms"] or "",
                summary["slo_status"], summary["failures"],
            ))
        return buf.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_bundle_output(result: SupportBundleResult, *, format: str) -> str:
    payload = {
        "bundle_path": str(result.bundle_path),
        "size_bytes": result.size_bytes,
        "file_count": result.file_count,
        "redaction_count": result.redaction_count,
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(("bundle_path", "size_bytes", "file_count", "redaction_count"))
        writer.writerow((payload["bundle_path"], payload["size_bytes"], payload["file_count"], payload["redaction_count"]))
        return buf.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_alerts_output(alerts: tuple[AlertRecord, ...], *, format: str, include_resolved: bool) -> str:
    if format == "json":
        return json.dumps(
            {
                "include_resolved": include_resolved,
                "alerts": [a.to_dict() for a in alerts],
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
    if format == "csv":
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(("alert_id", "severity", "category", "created_at", "resolved_at", "message", "next_command"))
        for a in alerts:
            writer.writerow((
                a.alert_id, a.severity, a.category,
                a.created_at.isoformat() if a.created_at else "",
                a.resolved_at.isoformat() if a.resolved_at else "",
                a.message, a.next_command,
            ))
        return buf.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
