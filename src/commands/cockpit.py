"""ADF-W0.8: ``vertex cockpit`` -- the read-only TPM/EM cockpit surface.

Composes existing readers and measurement stores into one ``CockpitSnapshot``
(Section 10.5: "cockpit composes and explains; it does not replace the
specialized commands"). This module owns only rendering and persistence of
the snapshot; all values come from :mod:`src.core.cockpit_builder`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import typer

from src.commands.cockpit_tui import run_cockpit_tui_loop
from src.core.adf_workflow_metrics import WorkflowMeasurementReport, compute_workflow_measurement_report
from src.core.adoption_telemetry import (
    GoldenWorkflow,
    NonAdoptionReason,
    compute_adoption_rate,
    record_adoption,
    record_non_adoption,
)
from src.core.proposal_autonomy_ladder import (
    MIN_SAMPLE_RATE,
    PROPOSAL_CLASSES,
    advance_proposal_class_autonomy,
    demote_proposal_class_explicit,
    promote_proposal_class_explicit,
)
from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.cockpit_html import render_cockpit_html
from src.core.cockpit_retention import rotate_cockpit_history
from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    ValueCockpitSummary,
    cockpit_history_filename,
    cockpit_snapshot_from_json_dict,
    cockpit_snapshot_to_json,
    cockpit_snapshot_to_json_dict,
)
from src.core.alerts import StateError, append_or_suppress_alert, read_alerts
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.lineage_regression_detector import (
    detect_lineage_regression,
    build_lineage_regression_alert_message,
)
from src.core.projection_lag_detector import (
    detect_projection_lag,
    build_projection_lag_alert_message,
)
from src.core.value_baseline_freshness_detector import (
    build_value_baseline_alert_message,
    detect_value_baseline_freshness,
)

app = typer.Typer(help="Program/platform/economics/value cockpit (read-only projection).")

_COCKPIT_DIR_NAME = "cockpit"
_HISTORY_DIR_NAME = "history"
_LATEST_FILENAME = "latest.json"
_MAX_NEXT_ACTIONS = 3


def _cockpit_dir(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "runtime" / _COCKPIT_DIR_NAME


def persist_cockpit_snapshot(snapshot: CockpitSnapshot, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, Path]:
    """Write ``runtime/cockpit/latest.json`` and a history snapshot. Returns both paths."""
    cockpit_dir = _cockpit_dir(snapshot.program_id, programs_root=programs_root)
    history_dir = cockpit_dir / _HISTORY_DIR_NAME
    history_dir.mkdir(parents=True, exist_ok=True)

    payload = cockpit_snapshot_to_json(snapshot)
    latest_path = cockpit_dir / _LATEST_FILENAME
    latest_path.write_text(payload, encoding="utf-8")

    history_path = history_dir / cockpit_history_filename(snapshot)
    history_path.write_text(payload, encoding="utf-8")

    # ADF-W5.9 (Section 9.7): "Last 30 builds + weekly keepers for 13
    # months. Delete oldest non-keeper." Runs on every write, matching
    # rev_cache_store.py's own "write then prune" precedent -- best-effort,
    # a rotation failure never breaks the write that just succeeded.
    rotate_cockpit_history(history_dir)

    return latest_path, history_path


def _emit_projection_lag_alert_best_effort(
    program_id: str, *, programs_root: Path
) -> None:
    """ADF-W5.8 (Section 8.2.5): ``projection_lag`` detection.

    Compares the *previously persisted* ``latest.json`` projection time
    against the freshest underlying data artifact (fact store / run telemetry).
    A projection that is older than the data it projects from -- by more than
    the lag budget -- means a scheduled build stopped running while the data
    kept changing; this is the failure mode Section 8.2.5 names.

    Reads the PRE-update snapshot (the persisted one an operator would see if
    they opened ``latest.json`` right now), so the lag it reports is the lag
    that existed before this command rebuilt it. Best-effort: a read/write or
    alert-store failure never breaks the cockpit render that called it.
    """
    latest_path = _cockpit_dir(program_id, programs_root=programs_root) / _LATEST_FILENAME
    try:
        if not latest_path.exists():
            return
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        generated_at_raw = payload.get("generated_at")
        if not isinstance(generated_at_raw, str):
            return
        projection_at = datetime.fromisoformat(generated_at_raw.replace("Z", "+00:00"))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return  # A corrupt/missing latest.json is not a lag condition.
    finding = detect_projection_lag(
        program_id, projection_at=projection_at, programs_root=programs_root,
    )
    if not finding.is_lagging:
        return
    message, next_command = build_projection_lag_alert_message(finding)
    append_or_suppress_alert(
        program_id=program_id,
        category="projection_lag",
        entity_type="cockpit_snapshot",
        entity_id=program_id,
        severity="warn",
        message=message,
        next_command=next_command.replace("{program}", program_id),
        programs_root=programs_root,
    )


def _emit_lineage_regression_alert_best_effort(
    program_id: str, snapshot: CockpitSnapshot, *, programs_root: Path
) -> None:
    """ADF-W5.8 (Section 8.2.5): ``lineage_regression`` detection.

    Compares ``snapshot``'s (the freshly-built, not-yet-persisted) lineage
    coverage against the nearest retained history snapshot at or before
    ``snapshot.generated_at`` -- the state an operator would have seen before
    this build. Called before ``persist_cockpit_snapshot`` so the lookup
    never finds ``snapshot`` itself. Best-effort: a history-read or
    alert-store failure never breaks the cockpit render that called it.
    """
    try:
        prior = find_nearest_history_snapshot(program_id, snapshot.generated_at, programs_root=programs_root)
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return  # A corrupt prior snapshot is not a regression condition.
    if prior is None:
        return  # First cockpit run for this program: nothing to regress from.
    finding = detect_lineage_regression(
        previous_coverage=prior.intelligence_summary.lineage_coverage,
        current_coverage=snapshot.intelligence_summary.lineage_coverage,
    )
    if not finding.is_regressing:
        return
    message, next_command = build_lineage_regression_alert_message(finding)
    append_or_suppress_alert(
        program_id=program_id,
        category="lineage_regression",
        entity_type="cockpit_snapshot",
        entity_id=program_id,
        severity="warn",
        message=message,
        next_command=next_command.replace("{program}", program_id),
        programs_root=programs_root,
    )


def _emit_value_baseline_freshness_alert_best_effort(
    program_id: str, snapshot: CockpitSnapshot, *, programs_root: Path
) -> None:
    """ADF-W5.8 (Section 8.2.5): ``value_baseline_expired_or_incomparable``
    detection -- the last open alert category for this item.

    Evaluates the freshly-built snapshot's ``value_summary`` measured metrics
    against the freshness (Section 15.6 "contemporaneously recorded") and
    evidence (Section 8.1.8 ">= 8 matched pairs") contracts. Only
    ``MEASUREDED``-confidence metrics are evaluated -- a ``CALIBRATED``/``PROXY``
    metric is already honestly labeled and cannot create the false confidence
    this category exists to catch (INV-ADF-11). Best-effort: a detection or
    alert-store failure never breaks the cockpit render that called it.
    """
    finding = detect_value_baseline_freshness(
        snapshot.value_summary, observed_at=snapshot.generated_at,
    )
    if not finding.is_degraded:
        return
    message, next_command = build_value_baseline_alert_message(finding)
    append_or_suppress_alert(
        program_id=program_id,
        category="value_baseline_expired_or_incomparable",
        entity_type="value_summary",
        entity_id=program_id,
        severity="warn",
        message=message,
        next_command=next_command.replace("{program}", program_id),
        programs_root=programs_root,
    )


def find_nearest_history_snapshot(
    program_id: str, at: datetime, *, programs_root: Path = PROGRAMS_ROOT
) -> CockpitSnapshot | None:
    """Section 10.9/9.7: ``--as-of`` means the nearest retained snapshot at
    or before the requested time -- never arbitrary time travel over
    mutable state, and never a snapshot AFTER ``at`` (that would silently
    answer a different question than the one asked). Returns ``None`` when
    no history predates ``at`` (a missing snapshot degrades to "unavailable
    for that time," never a nearest-after guess)."""
    history_dir = _cockpit_dir(program_id, programs_root=programs_root) / _HISTORY_DIR_NAME
    if not history_dir.is_dir():
        return None
    at_utc = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
    best_path: Path | None = None
    best_generated_at: datetime | None = None
    for candidate in history_dir.glob("*.json"):
        try:
            stamp = datetime.strptime(candidate.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp > at_utc:
            continue
        if best_generated_at is None or stamp > best_generated_at:
            best_generated_at = stamp
            best_path = candidate
    if best_path is None:
        return None
    try:
        payload = json.loads(best_path.read_text(encoding="utf-8"))
        return cockpit_snapshot_from_json_dict(payload)
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _render_value_line(value: ValueCockpitSummary) -> str:
    """ADF-W1.11: show the report wall-time before/after metric explicitly
    when available; fall back to a generic count for any other measured
    metric; QG-36 "not measured yet" when there is nothing measured."""
    if not value.metrics:
        return "not measured yet (QG-36)"
    wall_time = next((metric for metric in value.metrics if metric.metric_id == "report_wall_time_seconds"), None)
    if wall_time is not None and wall_time.baseline_value is not None and wall_time.delta_value is not None:
        direction = "faster" if wall_time.delta_value > 0 else ("slower" if wall_time.delta_value < 0 else "unchanged")
        return (
            f"report wall-time {wall_time.baseline_value:.0f}s -> {wall_time.value:.0f}s "
            f"({abs(wall_time.delta_value):.0f}s {direction})"
        )
    return f"{len(value.metrics)} metric(s) available"


def render_cockpit_terminal(snapshot: CockpitSnapshot) -> str:
    """Section 10.2 terminal contract: at most program-health, readiness, data-trust,
    value, up to three next actions, and a detail pointer."""
    program = snapshot.program_summary
    source = snapshot.source_summary
    value = snapshot.value_summary

    readiness = "not measured yet" if program.readiness_percent is None else f"{program.readiness_percent}%"
    lineage = (
        "not measured yet"
        if snapshot.intelligence_summary.lineage_coverage is None
        else f"{snapshot.intelligence_summary.lineage_coverage:.0%}"
    )
    value_line = _render_value_line(value)

    next_actions: list[str] = []
    for finding in snapshot.findings:
        if finding.next_command and finding.next_command not in next_actions:
            next_actions.append(finding.next_command)
        if len(next_actions) >= _MAX_NEXT_ACTIONS:
            break

    lines = [
        f"Program health:        {program.overall_risk.upper()} ({program.blocker_count} high-risk item(s))",
        f"Publication readiness: {readiness}",
        f"Data trust:            {source.required_healthy}/{source.required_total} required sources healthy; lineage {lineage}",
        f"Value:                 {value_line}",
    ]
    if next_actions:
        lines.append("Next actions:")
        lines.extend(f"  - {command}" for command in next_actions)
    lines.append(f"Detail: vertex cockpit show --program {snapshot.program_id} --format json")
    return "\n".join(lines)


_ONBOARDING_WALKTHROUGH = """\
Welcome to the Vertex cockpit -- your program's single entry point (Section 10.5, 10.7).

  Program vs. platform health
    "Program health" is your program's own risk/readiness. Platform health
    (see `vertex doctor`) is Vertex's own operating condition -- the two are
    never conflated on one line.

  Measured / calibrated / proxy value
    Every value metric this cockpit reports is labeled with its confidence:
    MEASURED (real before/after data), CALIBRATED (a forecast band with a
    real confidence interval), or PROXY (a heuristic estimate). A metric
    never claims to be measured when it is not.

  Source states
    "N/M required sources healthy" tells you whether the evidence this
    cockpit is built on is fresh and complete -- stale/degraded/manual
    sources are always named, never hidden inside an aggregate number.

  Confidence and lineage
    Lineage coverage tells you what fraction of published facts trace back
    to a real source. Low coverage means "trust this less," not an error.

  Next-command behavior
    Every finding that needs your attention names the exact command to run
    next -- `vertex cockpit explain --finding <id>` for the full story
    behind any of them.

This walkthrough only appears on your first cockpit run for this program.
"""


def _is_first_cockpit_run(program_id: str, *, programs_root: Path) -> bool:
    return not (_cockpit_dir(program_id, programs_root=programs_root) / _LATEST_FILENAME).exists()


@app.command("show")
def cockpit_show(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    edition: str | None = typer.Option(None, "--edition", help="Optional edition id to scope this snapshot to."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
    no_persist: bool = typer.Option(
        False, "--no-persist", help="Build and print the snapshot without writing latest.json/history (read-only preview)."
    ),
) -> None:
    # Section 10.7: onboarding walkthrough, checked BEFORE persisting so the
    # "first run" signal (no prior latest.json) is evaluated against
    # pre-this-run state, not this run's own about-to-be-written snapshot.
    first_run = not no_persist and _is_first_cockpit_run(program, programs_root=PROGRAMS_ROOT)

    # ADF-W5.8 (Section 8.2.5): projection_lag detection against the PRE-update
    # persisted snapshot. Best-effort; a corrupt latest.json or alert-store
    # failure never breaks the cockpit render. Skipped for --no-persist previews
    # (a preview is not a real cadence observation point).
    if not no_persist:
        try:
            _emit_projection_lag_alert_best_effort(program, programs_root=PROGRAMS_ROOT)
        except (OSError, StateError):
            pass

    snapshot = build_cockpit_snapshot(program, programs_root=PROGRAMS_ROOT, edition_id=edition)

    if not no_persist:
        # ADF-W5.8 (Section 8.2.5): lineage_regression detection. Runs against
        # the freshly-built snapshot but BEFORE persisting it, so the nearest-
        # history lookup can never find this run's own snapshot. Best-effort,
        # same rationale as projection_lag above.
        try:
            _emit_lineage_regression_alert_best_effort(program, snapshot, programs_root=PROGRAMS_ROOT)
        except (OSError, StateError):
            pass
        # ADF-W5.8 (Section 8.2.5): value_baseline_expired_or_incomparable
        # detection. Same freshly-built-snapshot / best-effort ordering.
        try:
            _emit_value_baseline_freshness_alert_best_effort(program, snapshot, programs_root=PROGRAMS_ROOT)
        except (OSError, StateError):
            pass
        persist_cockpit_snapshot(snapshot, programs_root=PROGRAMS_ROOT)
        # ADF-W5.14 (ADF-OM15): best-effort adoption telemetry -- never let a
        # telemetry write failure break the cockpit render that just succeeded.
        # A --no-persist preview run is not a real adoption event.
        try:
            record_adoption(program, GoldenWorkflow.COCKPIT_SHOW, programs_root=PROGRAMS_ROOT)
        except Exception:
            pass

    if format == "json":
        typer.echo(json.dumps(cockpit_snapshot_to_json_dict(snapshot), indent=2, sort_keys=True))
        return
    if format != "human":
        raise typer.BadParameter(f"Unsupported --format {format!r}; use human or json.")
    if first_run:
        typer.echo(_ONBOARDING_WALKTHROUGH)
    typer.echo(render_cockpit_terminal(snapshot))


@app.command("build")
def cockpit_build(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    edition: str | None = typer.Option(None, "--edition", help="Optional edition id to scope this snapshot to."),
    open_browser: bool = typer.Option(False, "--open", help="Open the rendered HTML in the default browser."),
    as_of: str | None = typer.Option(
        None, "--as-of", help="ISO timestamp: render the nearest retained history snapshot at or before this time, "
        "instead of building a fresh one."
    ),
) -> None:
    """Section 10.1: the local HTML dashboard. Never a live time-travel
    reconstruction -- ``--as-of`` reads a retained history snapshot."""
    if as_of is not None:
        try:
            at = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            raise typer.BadParameter(f"--as-of {as_of!r} is not a valid ISO timestamp.")
        snapshot = find_nearest_history_snapshot(program, at, programs_root=PROGRAMS_ROOT)
        if snapshot is None:
            typer.echo(f"No retained cockpit history for {program!r} at or before {as_of}.", err=True)
            raise typer.Exit(code=1)
    else:
        snapshot = build_cockpit_snapshot(program, programs_root=PROGRAMS_ROOT, edition_id=edition)
        # ADF-W5.8 (Section 8.2.5): lineage_regression, same best-effort
        # pre-persist ordering as cockpit_show above.
        try:
            _emit_lineage_regression_alert_best_effort(program, snapshot, programs_root=PROGRAMS_ROOT)
        except (OSError, StateError):
            pass
        # ADF-W5.8 (Section 8.2.5): value_baseline_expired_or_incomparable,
        # same freshly-built-snapshot / best-effort ordering.
        try:
            _emit_value_baseline_freshness_alert_best_effort(program, snapshot, programs_root=PROGRAMS_ROOT)
        except (OSError, StateError):
            pass
        persist_cockpit_snapshot(snapshot, programs_root=PROGRAMS_ROOT)

    html = render_cockpit_html(snapshot)
    html_path = _cockpit_dir(program, programs_root=PROGRAMS_ROOT) / "cockpit.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    typer.echo(f"Wrote {html_path}")
    if open_browser:
        typer.launch(str(html_path))


@app.command("explain")
def cockpit_explain(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    edition: str | None = typer.Option(None, "--edition", help="Optional edition id to scope this snapshot to."),
    finding: str = typer.Option(..., "--finding", help="finding_id to explain."),
) -> None:
    """Section 10.4: full explainability for one finding. Renders every
    field ``CockpitFinding`` structurally carries; explicitly labels the
    two Section 10.4 fields it has no dedicated data for yet (calculation/
    rule, and what-Vertex-did-not-do) rather than fabricating content."""
    snapshot = build_cockpit_snapshot(program, programs_root=PROGRAMS_ROOT, edition_id=edition)
    match = next((f for f in snapshot.findings if f.finding_id == finding), None)
    if match is None:
        typer.echo(f"No finding {finding!r} in the current cockpit snapshot for {program!r}.", err=True)
        raise typer.Exit(code=1)
    typer.echo(_render_finding_explanation(match))


def _render_finding_explanation(finding: CockpitFinding) -> str:
    lines = [
        f"Finding: {finding.finding_id} ({finding.area}, {finding.status})",
        f"Why this finding exists: {finding.summary}",
        f"Detail: {finding.detail}",
        f"Owner: {finding.owner or 'unassigned'}",
        f"Source age: observed at {finding.observed_at.isoformat()}",
        f"Evidence refs: {', '.join(finding.evidence_refs) if finding.evidence_refs else 'none'}",
        f"Next command: {finding.next_command or 'none'}",
        "Calculation/rule: not yet a structured field on CockpitFinding -- see 'Detail' above for the "
        "available free-text explanation.",
        "Confidence: not yet a structured field on CockpitFinding.",
        "What Vertex did / did not do: not yet structured fields on CockpitFinding.",
    ]
    return "\n".join(lines)


@app.command("tui")
def cockpit_tui(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    edition: str | None = typer.Option(None, "--edition", help="Optional edition id to scope this snapshot to."),
) -> None:
    """Section 10.3a: the optional interactive terminal cockpit. Read-only
    navigation this pass (findings list + explain detail + refresh) --
    never binds a port, never writes to a store, never bypasses any
    mutation path (there is none in this command)."""
    run_cockpit_tui_loop(
        program, edition_id=edition, input_fn=input, output_fn=typer.echo, programs_root=PROGRAMS_ROOT
    )


@app.command("compare")
def cockpit_compare(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    from_iso: str = typer.Option(..., "--from", help="ISO timestamp for the earlier snapshot."),
    to_iso: str = typer.Option(..., "--to", help="ISO timestamp for the later snapshot."),
) -> None:
    """Section 9.1/10.1: diffs two retained cockpit history snapshots.
    Operates ONLY on retained history (never recomputed from current
    mutable state) -- if a requested time has no retained snapshot at or
    before it, the comparison is unavailable, not silently substituted."""
    try:
        from_at = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
        to_at = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
    except ValueError:
        raise typer.BadParameter("--from/--to must be valid ISO timestamps.")

    earlier = find_nearest_history_snapshot(program, from_at, programs_root=PROGRAMS_ROOT)
    later = find_nearest_history_snapshot(program, to_at, programs_root=PROGRAMS_ROOT)
    if earlier is None or later is None:
        missing = "--from" if earlier is None else "--to"
        typer.echo(f"No retained cockpit history for {program!r} at or before the {missing} time.", err=True)
        raise typer.Exit(code=1)

    typer.echo(render_cockpit_comparison(earlier, later))


def render_cockpit_comparison(earlier: CockpitSnapshot, later: CockpitSnapshot) -> str:
    lines = [
        f"Cockpit comparison for {later.program_id}",
        f"  From: {earlier.generated_at.isoformat()} (actual retained snapshot time)",
        f"  To:   {later.generated_at.isoformat()} (actual retained snapshot time)",
        "",
        f"Overall risk:      {earlier.program_summary.overall_risk} -> {later.program_summary.overall_risk}",
        f"Readiness:         {earlier.program_summary.readiness_percent} -> {later.program_summary.readiness_percent}",
        f"Blocker count:     {earlier.program_summary.blocker_count} -> {later.program_summary.blocker_count}",
        f"Required sources:  {earlier.source_summary.required_healthy}/{earlier.source_summary.required_total} "
        f"-> {later.source_summary.required_healthy}/{later.source_summary.required_total}",
        f"Lineage coverage:  {earlier.intelligence_summary.lineage_coverage} -> {later.intelligence_summary.lineage_coverage}",
        f"Finding count:     {len(earlier.findings)} -> {len(later.findings)}",
    ]
    earlier_ids = {f.finding_id for f in earlier.findings}
    later_ids = {f.finding_id for f in later.findings}
    new_ids = sorted(later_ids - earlier_ids)
    resolved_ids = sorted(earlier_ids - later_ids)
    if new_ids:
        lines.append(f"New findings: {', '.join(new_ids)}")
    if resolved_ids:
        lines.append(f"Resolved/removed findings: {', '.join(resolved_ids)}")
    return "\n".join(lines)


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    minutes = value / 60.0
    if minutes < 1:
        return f"{value:.0f}s"
    return f"{minutes:.1f}min"


def render_workflow_measurement_terminal(report: WorkflowMeasurementReport) -> str:
    """ADF-W2.11/W3.8/W4.8: renders review-latency as an honest proxy for
    active time -- not a claim of true engaged time (ADR-0017 Decision 2).
    """
    lines = [
        f"Vertex Workflow Measurement — {report.program_id}",
        "(Review latency = decided_at - proposed_at; a proxy for review "
        "burden, not measured active engagement time.)",
        "",
    ]
    for summary in report.by_type:
        lines.append(
            f"{summary.proposal_type}: {summary.decided_count} decided "
            f"({summary.approved_count} approved / {summary.rejected_count} rejected) — "
            f"p50={_format_seconds(summary.p50_latency_seconds)} "
            f"p90={_format_seconds(summary.p90_latency_seconds)} "
            f"max={_format_seconds(summary.max_latency_seconds)}"
        )
    lines.append("")
    lines.append(f"Total recorded proposal-decision events: {report.total_proposal_events}")
    return "\n".join(lines)


@app.command("measure")
def cockpit_measure(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    since_days: int | None = typer.Option(
        None, "--since-days", help="Only include decisions from the last N days (default: all-time)."
    ),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """ADF-W2.11/W3.8/W4.8 (ADR-0017): review-latency/proposal-volume report
    computed from the proposal_audit.jsonl trail. Empty/near-empty until the
    approve_*/reject_* helpers are actually called with programs_root set by
    a real review flow -- this command reports what has accrued, it does
    not simulate or backfill data."""
    since = datetime.now(timezone.utc) - timedelta(days=since_days) if since_days is not None else None
    report = compute_workflow_measurement_report(program, since=since, programs_root=PROGRAMS_ROOT)

    if format == "json":
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return
    if format != "human":
        raise typer.BadParameter(f"Unsupported --format {format!r}; use human or json.")
    typer.echo(render_workflow_measurement_terminal(report))


@app.command("adoption-skip")
def cockpit_adoption_skip(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    workflow: str = typer.Option(
        ..., "--workflow",
        help=f"Golden workflow that was skipped this cadence: {', '.join(w.value for w in GoldenWorkflow)}.",
    ),
    reason: str = typer.Option(
        ..., "--reason",
        help=f"Non-adoption reason: {', '.join(r.value for r in NonAdoptionReason)}.",
    ),
) -> None:
    """ADF-W5.14 (ADF-OM15): explicit non-adoption reason capture. A CLI
    cannot observe a workflow that never ran, so this command is the
    deliberate log-the-skip entry point -- an operator (or the pilot TPM
    on their behalf) records why a golden workflow was not run this cadence
    period, rather than the platform inferring or fabricating a reason."""
    try:
        workflow_enum = GoldenWorkflow(workflow)
    except ValueError:
        raise typer.BadParameter(
            f"Unknown --workflow {workflow!r}; use one of: {', '.join(w.value for w in GoldenWorkflow)}."
        )
    try:
        reason_enum = NonAdoptionReason(reason)
    except ValueError:
        raise typer.BadParameter(
            f"Unknown --reason {reason!r}; use one of: {', '.join(r.value for r in NonAdoptionReason)}."
        )
    record_non_adoption(program, workflow_enum, reason_enum, programs_root=PROGRAMS_ROOT)
    typer.echo(f"Recorded non-adoption: program={program} workflow={workflow_enum.value} reason={reason_enum.value}")


@app.command("adoption")
def cockpit_adoption(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    workflow: str | None = typer.Option(
        None, "--workflow", help="Restrict to one golden workflow (default: all workflows combined)."
    ),
    since_weeks: int = typer.Option(13, "--since-weeks", help="Cadence window to summarize, in weeks."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """ADF-OM15 dashboard: adoption rate + non-adoption reason breakdown
    over a recent cadence window, from real recorded adoption/non-adoption
    events -- never simulated or backfilled."""
    workflow_enum: GoldenWorkflow | None = None
    if workflow is not None:
        try:
            workflow_enum = GoldenWorkflow(workflow)
        except ValueError:
            raise typer.BadParameter(
                f"Unknown --workflow {workflow!r}; use one of: {', '.join(w.value for w in GoldenWorkflow)}."
            )
    summary = compute_adoption_rate(
        program, workflow=workflow_enum, since_weeks=since_weeks, programs_root=PROGRAMS_ROOT
    )
    if format == "json":
        typer.echo(json.dumps(
            {
                "program_id": summary.program_id,
                "workflow": summary.workflow.value if summary.workflow else None,
                "cadence_periods_covered": summary.cadence_periods_covered,
                "adopted_count": summary.adopted_count,
                "non_adopted_count": summary.non_adopted_count,
                "adoption_rate": summary.adoption_rate,
                "reason_breakdown": summary.reason_breakdown,
            },
            indent=2, sort_keys=True,
        ))
        return
    if format != "human":
        raise typer.BadParameter(f"Unsupported --format {format!r}; use human or json.")
    rate_text = f"{summary.adoption_rate:.0%}" if summary.adoption_rate is not None else "no data yet"
    lines = [
        f"Adoption ({program}{'/' + summary.workflow.value if summary.workflow else ''}, last {since_weeks}w): {rate_text}",
        f"  adopted={summary.adopted_count} non_adopted={summary.non_adopted_count} periods_covered={summary.cadence_periods_covered}",
    ]
    if summary.reason_breakdown:
        lines.append("  non-adoption reasons:")
        for reason_key, count in sorted(summary.reason_breakdown.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {reason_key}: {count}")
    typer.echo("\n".join(lines))


@app.command("autonomy-evaluate")
def cockpit_autonomy_evaluate(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    proposal_class: str | None = typer.Option(
        None, "--class", help=f"Restrict to one proposal class: {', '.join(PROPOSAL_CLASSES)}. Default: all."
    ),
) -> None:
    """ADF-W5.12 (Section 8.15.1): runs the automatic evidence-based L0/L1/L2
    autonomy evaluator for one or all proposal classes and persists the
    result. L3/L4 require ``autonomy-promote`` (human-gated -- see that
    command's help)."""
    classes = (proposal_class,) if proposal_class else PROPOSAL_CLASSES
    for cls in classes:
        if cls not in PROPOSAL_CLASSES:
            raise typer.BadParameter(f"Unknown --class {cls!r}; use one of: {', '.join(PROPOSAL_CLASSES)}.")
    for cls in classes:
        evaluation = advance_proposal_class_autonomy(program, cls, programs_root=PROGRAMS_ROOT)
        typer.echo(f"{cls}: {evaluation.action} {evaluation.current_level}->{evaluation.proposed_level} ({evaluation.reason})")


@app.command("autonomy-promote")
def cockpit_autonomy_promote(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    proposal_class: str = typer.Option(..., "--class", help=f"One of: {', '.join(PROPOSAL_CLASSES)}."),
    to: str = typer.Option(..., "--to", help="Target level: l0..l4."),
    reason: str = typer.Option(..., "--reason", help="Evidence/justification for this promotion."),
    sample_rate: float | None = typer.Option(
        None, "--sample-rate",
        help="L3/L4 only (Section 8.15.2): fraction of a batch a human must still individually "
        "review via `ai-proposals review-batch`, e.g. 0.2 for 20%% reviewed/80%% auto-approved. "
        f"Must be between {MIN_SAMPLE_RATE} and 1.0. Omit to keep full review (1.0) at L3/L4.",
    ),
) -> None:
    """The explicit, human-gated path -- required for L3/L4 (Section 8.15.1's
    independent-review/outbox-proven evidence cannot be computed
    automatically) and always capped at the governance-configured ceiling."""
    if proposal_class not in PROPOSAL_CLASSES:
        raise typer.BadParameter(f"Unknown --class {proposal_class!r}; use one of: {', '.join(PROPOSAL_CLASSES)}.")
    try:
        entry = promote_proposal_class_explicit(
            program, proposal_class, to, reason, programs_root=PROGRAMS_ROOT, sample_rate=sample_rate,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error))
    typer.echo(f"{proposal_class}: promoted to {entry.level} ({reason}); sample_rate={entry.sample_rate:.2f}")


@app.command("autonomy-demote")
def cockpit_autonomy_demote(
    program: str = typer.Option(..., "--program", help="Program id, e.g. xpf."),
    proposal_class: str = typer.Option(..., "--class", help=f"One of: {', '.join(PROPOSAL_CLASSES)}."),
    reason: str = typer.Option(..., "--reason", help="Why this class is being demoted."),
) -> None:
    """Manual one-level demotion for a material contradiction, duplicate
    effect, or policy violation an operator observes but the automatic
    evaluator cannot detect (Section 8.15.1)."""
    if proposal_class not in PROPOSAL_CLASSES:
        raise typer.BadParameter(f"Unknown --class {proposal_class!r}; use one of: {', '.join(PROPOSAL_CLASSES)}.")
    entry = demote_proposal_class_explicit(program, proposal_class, reason, programs_root=PROGRAMS_ROOT)
    typer.echo(f"{proposal_class}: demoted to {entry.level} ({reason})")


__all__ = [
    "app",
    "cockpit_adoption",
    "cockpit_adoption_skip",
    "cockpit_autonomy_demote",
    "cockpit_autonomy_evaluate",
    "cockpit_autonomy_promote",
    "cockpit_build",
    "cockpit_compare",
    "cockpit_explain",
    "cockpit_measure",
    "cockpit_show",
    "find_nearest_history_snapshot",
    "persist_cockpit_snapshot",
    "render_cockpit_comparison",
    "render_cockpit_terminal",
    "render_workflow_measurement_terminal",
]
