from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import re

import typer

from src.core.action_tracker import assess_action_staleness
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.capability_status import ProgramCapabilityStatus, latest_program_capability_reviewed_on, load_program_capability_status, summarize_program_capabilities, summarize_program_capability_reviews, summarize_program_capability_verification
from src.core.claim_tracker import load_open_decision_asks
from src.core.communication_plan import CommunicationPlanEntry, describe_communication_plan_entry, load_communication_plan_entries
from src.core.config_loader import load_report_bundle
from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerState
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, resolve_edition
from src.core.exceptions import ConfigError
from src.core.freshness_engine import build_freshness_report
from src.core.gather_state_store import build_gather_integration_summary, load_gather_state
from src.core.issue_projection import build_issue_projection
from src.core.policy_evaluator import check_cadence, get_cadence_window
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import ActionStatus, MilestoneStatus, RiskStatus
from src.core.program_fact_store import load_program_facts, project_action_items, project_risk_entries
from src.core.risk_register_engine import assess_risk_staleness
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.signal_ranking import signal_source_family
from src.core.snapshot_store import ARCHIVE_ROOT, read_snapshot
from src.core.store_factory import build_signal_store_for_program_id
from src.core.telemetry_summary import build_program_telemetry_summary


_MANIFEST_NAME_RE = re.compile(r"^issue_(\d{3})\.manifest\.json$")
_SNAPSHOT_NAME_RE = re.compile(r"^issue_(\d{3})\.snapshot\.json$")


@dataclass(frozen=True, slots=True)
class StatusReport:
    edition: str
    display_name: str
    issue_number: int
    current_phase: str | None
    scope_statement: str | None
    readiness_percent: int | None
    blocker_count: int | None
    risk_register_summary: str | None
    milestone_summary: str | None
    telemetry_summary: str | None
    telemetry_confidence: str | None
    last_gathered_at: datetime | None
    gather_integration_summary: str | None
    gather_integration_details: tuple[dict[str, object], ...]
    last_confirmed_at: datetime | None
    cadence: str
    cadence_status: str
    next_due_edition: str | None
    next_due_status: str | None
    next_due_context: str | None
    source_manifest_path: str | None
    ai_safety_summary: str | None
    ado_breaker_summary: str | None
    capability_summary: str | None
    capability_review_summary: str | None
    capability_verification_summary: str | None
    latest_capability_reviewed_on: date | None
    capabilities: tuple[ProgramCapabilityStatus, ...]

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["last_gathered_at"] = self.last_gathered_at.isoformat() if self.last_gathered_at is not None else None
        payload["gather_integration_details"] = list(self.gather_integration_details)
        payload["last_confirmed_at"] = self.last_confirmed_at.isoformat() if self.last_confirmed_at is not None else None
        payload["latest_capability_reviewed_on"] = self.latest_capability_reviewed_on.isoformat() if self.latest_capability_reviewed_on is not None else None
        payload["capabilities"] = [capability.to_payload() for capability in self.capabilities]
        return payload


def status_command(
    edition: str = typer.Option(..., "--edition", help="Edition id, e.g. myprogram_weekly."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    report = build_status_report(edition)

    if format == "json":
        typer.echo(json.dumps(report.to_payload(), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if format == "csv":
        typer.echo(render_status_csv(report), nl=False)
        raise typer.Exit(code=0)
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")

    typer.echo(render_status_report(report))
    raise typer.Exit(code=0)


def build_status_report(
    edition_name: str,
    *,
    as_of: datetime | None = None,
    editions_root: Path | None = None,
    programs_root: Path | None = None,
    archive_root: Path | None = None,
) -> StatusReport:
    resolved_editions_root = editions_root or EDITIONS_ROOT
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT

    resolved = resolve_edition(
        edition_name,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
    )
    if resolved is None:
        raise ConfigError(f"Edition '{edition_name}' was not found.")

    now = as_of or datetime.now(timezone.utc)
    archive_index = read_archive_index(edition_name, archive_root=resolved_archive_root)
    latest_confirmed = find_latest_confirmed_entry(archive_index)
    latest_manifest = _load_latest_manifest(resolved.paths.publications_dir)
    latest_snapshot = _load_latest_snapshot(resolved.paths.publications_dir)

    readiness_percent: int | None = None
    blocker_count: int | None = None
    risk_register_summary: str | None = _build_status_risk_register_summary(
        resolved.program.id,
        programs_root=resolved_programs_root,
        as_of=now,
    )
    milestone_summary: str | None = None
    telemetry_summary: str | None = _build_status_telemetry_summary(
        resolved.program.id,
        programs_root=resolved_programs_root,
        as_of=now,
    )
    telemetry_confidence: str | None = _build_status_telemetry_confidence(
        resolved.program.id,
        programs_root=resolved_programs_root,
        as_of=now,
    )
    issue_number = 1
    source_manifest_path: str | None = None
    ai_safety_summary: str | None = None
    ado_breaker_summary: str | None = _build_status_ado_breaker_summary(
        resolved.paths.publications_dir,
    )

    if latest_manifest is not None:
        source_manifest_path = str(latest_manifest[0])
        issue_number = latest_manifest[1]
        qg_results = latest_manifest[2]
        if qg_results:
            readiness_percent = round((sum(1 for passed in qg_results.values() if passed) / len(qg_results)) * 100)
        milestone_summary = _build_status_milestone_summary(latest_manifest[0])
        ai_safety_summary = _build_status_ai_safety_summary(latest_manifest[0])
    elif latest_confirmed is not None:
        issue_number = latest_confirmed.issue_number + 1
    elif archive_index.issues:
        issue_number = max(entry.issue_number for entry in archive_index.issues) + 1

    if latest_snapshot is not None:
        blocker_count = _build_projected_blocker_count(
            edition_name=edition_name,
            program_id=resolved.program.id,
            snapshot_path=latest_snapshot[0],
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
    elif latest_manifest is not None:
        qg_results = latest_manifest[2]
        if qg_results:
            blocker_count = sum(1 for passed in qg_results.values() if not passed)

    gather_state = load_gather_state(resolved.program.id, programs_root=resolved_programs_root)
    last_gathered_at = (
        gather_state.gathered_at
        if gather_state is not None
        else latest_snapshot[1] if latest_snapshot is not None else _manifest_timestamp(latest_manifest)
    )
    gather_integration_summary = build_gather_integration_summary(gather_state)
    gather_integration_details: tuple[dict[str, object], ...] = tuple(
        {
            "source": detail.source,
            "stage": detail.stage,
            "retryable": detail.retryable,
            "message": detail.message,
            "operator_action": detail.operator_action,
        }
        for detail in (gather_state.integration_error_details if gather_state is not None else ())
    )
    last_confirmed_at = latest_confirmed.generated_at if latest_confirmed is not None else None
    cadence = resolved.edition.cadence
    cadence_status = _describe_cadence(cadence, last_confirmed_at, now)
    next_due_edition, next_due_status, next_due_context = _resolve_next_due_status(
        resolved.raw_program,
        fallback_edition=edition_name,
        fallback_status=cadence_status,
        as_of=now,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
        archive_root=resolved_archive_root,
    )
    capability_statuses = load_program_capability_status(
        resolved.program.id,
        programs_root=resolved_programs_root,
        program_document=resolved.raw_program,
    )

    return StatusReport(
        edition=edition_name,
        display_name=resolved.edition.brand_name or resolved.program.name,
        issue_number=issue_number,
        current_phase=resolved.program.current_phase,
        scope_statement=_charter_scope_statement(resolved.raw_program),
        readiness_percent=readiness_percent,
        blocker_count=blocker_count,
        risk_register_summary=risk_register_summary,
        milestone_summary=milestone_summary,
        telemetry_summary=telemetry_summary,
        telemetry_confidence=telemetry_confidence,
        last_gathered_at=last_gathered_at,
        gather_integration_summary=gather_integration_summary,
        gather_integration_details=gather_integration_details,
        last_confirmed_at=last_confirmed_at,
        cadence=cadence,
        cadence_status=cadence_status,
        next_due_edition=next_due_edition,
        next_due_status=next_due_status,
        next_due_context=next_due_context,
        source_manifest_path=source_manifest_path,
        ai_safety_summary=ai_safety_summary,
        ado_breaker_summary=ado_breaker_summary,
        capability_summary=summarize_program_capabilities(capability_statuses),
        capability_review_summary=summarize_program_capability_reviews(capability_statuses),
        capability_verification_summary=summarize_program_capability_verification(capability_statuses),
        latest_capability_reviewed_on=latest_program_capability_reviewed_on(capability_statuses),
        capabilities=capability_statuses,
    )


def render_status_report(report: StatusReport) -> str:
    readiness = (
        f"{report.readiness_percent}%"
        if report.readiness_percent is not None
        else "unknown (run vertex report --dry-run first)"
    )
    blockers = (
        f"{report.blocker_count} blocker{'s' if report.blocker_count != 1 else ''}"
        if report.blocker_count is not None
        else "blockers unknown"
    )
    gather = _format_relative_or_unknown(report.last_gathered_at)
    confirmed = _format_relative_or_unknown(report.last_confirmed_at)
    phase = report.current_phase or "unknown"
    lines = [
        f"{report.display_name} — Issue {report.issue_number:03d}",
        f"  Status:       {blockers}",
        f"  Readiness:    {readiness}",
        f"  Last gather:  {gather}",
        f"  Last confirm: {confirmed} ({report.cadence} — {report.cadence_status})",
        (
            f"  Next due:     {report.next_due_edition} ({report.next_due_status}; {report.next_due_context})"
            if report.next_due_context
            else f"  Next due:     {report.next_due_edition} ({report.next_due_status})"
        ),
        f"  Phase:        {phase}",
    ]
    if report.capability_summary:
        lines.append(f"  Capabilities: {report.capability_summary}")
    if report.capability_review_summary:
        lines.append(f"  Cap reviewed: {report.capability_review_summary}")
    if report.capability_verification_summary:
        lines.append(f"  Cap verify:   {report.capability_verification_summary}")
    if report.ai_safety_summary:
        lines.append(f"  AI Safety:    {report.ai_safety_summary}")
    if report.ado_breaker_summary:
        lines.append(f"  ADO Breaker:  {report.ado_breaker_summary}")
    if report.risk_register_summary:
        lines.append(f"  Risks:        {report.risk_register_summary}")
    if report.milestone_summary:
        lines.append(f"  Milestones:   {report.milestone_summary}")
    if report.telemetry_summary:
        telemetry_line = f"  Telemetry:    {report.telemetry_summary}"
        if report.telemetry_confidence:
            telemetry_line += f" ({report.telemetry_confidence} confidence)"
        lines.append(telemetry_line)
    if report.gather_integration_summary:
        lines.append(f"  Gather:       {report.gather_integration_summary}")
    if report.scope_statement:
        lines.append(f"  Scope:        {report.scope_statement}")
    return "\n".join(lines)


def render_status_csv(report: StatusReport) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "edition",
            "display_name",
            "issue_number",
            "current_phase",
            "scope_statement",
            "readiness_percent",
            "blocker_count",
            "risk_register_summary",
            "milestone_summary",
            "telemetry_summary",
            "telemetry_confidence",
            "last_gathered_at",
            "gather_integration_summary",
            "gather_integration_details_json",
            "last_confirmed_at",
            "cadence",
            "cadence_status",
            "next_due_edition",
            "next_due_status",
            "next_due_context",
            "source_manifest_path",
            "ai_safety_summary",
            "ado_breaker_summary",
            "capability_summary",
            "capability_review_summary",
            "capability_verification_summary",
            "latest_capability_reviewed_on",
            "capabilities_json",
        )
    )
    writer.writerow(
        (
            report.edition,
            report.display_name,
            report.issue_number,
            report.current_phase or "",
            report.scope_statement or "",
            report.readiness_percent if report.readiness_percent is not None else "",
            report.blocker_count if report.blocker_count is not None else "",
            report.risk_register_summary or "",
            report.milestone_summary or "",
            report.telemetry_summary or "",
            report.telemetry_confidence or "",
            report.last_gathered_at.isoformat() if report.last_gathered_at is not None else "",
            report.gather_integration_summary or "",
            json.dumps(list(report.gather_integration_details), sort_keys=True),
            report.last_confirmed_at.isoformat() if report.last_confirmed_at is not None else "",
            report.cadence,
            report.cadence_status,
            report.next_due_edition or "",
            report.next_due_status or "",
            report.next_due_context or "",
            report.source_manifest_path or "",
            report.ai_safety_summary or "",
            report.ado_breaker_summary or "",
            report.capability_summary or "",
            report.capability_review_summary or "",
            report.capability_verification_summary or "",
            report.latest_capability_reviewed_on.isoformat() if report.latest_capability_reviewed_on is not None else "",
            json.dumps([capability.to_payload() for capability in report.capabilities], sort_keys=True),
        )
    )
    return buffer.getvalue()


def _build_status_ado_breaker_summary(
    output_dir: Path,
) -> str | None:
    state_path = output_dir / ".ado_breaker.json"
    if not state_path.exists():
        return None

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"malformed state at {_display_status_path(state_path)}"

    if _status_breaker_payload_is_malformed(payload):
        return f"malformed state at {_display_status_path(state_path)}"

    snapshot = CircuitBreaker(state_path=state_path).get_state()
    if snapshot.state == CircuitBreakerState.CLOSED:
        return None

    detail = f"{snapshot.state.value}; failure_count={snapshot.failure_count}"
    if snapshot.last_opened_at is not None:
        detail += f"; last_opened_at={snapshot.last_opened_at.isoformat()}"
    if snapshot.state == CircuitBreakerState.OPEN:
        detail += "; live freshness ADO requests gated"
    elif snapshot.state == CircuitBreakerState.HALF_OPEN:
        detail += "; recovery probe in progress"
    return detail


def _status_breaker_payload_is_malformed(payload: object) -> bool:
    if not isinstance(payload, dict):
        return True
    try:
        CircuitBreakerState(str(payload.get("state", CircuitBreakerState.CLOSED.value)))
        int(payload.get("failure_count", 0))
    except (TypeError, ValueError):
        return True

    return any(
        not _status_breaker_timestamp_is_valid(payload.get(key))
        for key in ("last_failure_at", "last_opened_at", "last_success_at")
    )


def _status_breaker_timestamp_is_valid(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _display_status_path(path: Path) -> str:
    return str(path)


def _build_status_risk_register_summary(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime,
) -> str | None:
    try:
        risks = project_risk_entries(load_program_facts(program_id, db_root=programs_root.parent, programs_root=programs_root))
    except ConfigError:
        return None

    active_risks = tuple(
        risk
        for risk in risks
        if risk.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    )
    if not active_risks:
        return None

    stale_count = sum(
        1
        for risk in active_risks
        if assess_risk_staleness(risk, as_of.date())
    )
    summary = f"{len(active_risks)} active"
    if stale_count:
        summary += f", {stale_count} stale review{'s' if stale_count != 1 else ''}"
    else:
        summary += ", reviews current"
    return summary


def _build_status_milestone_summary(
    manifest_path: Path,
) -> str | None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None

    assessments = metadata.get("milestone_assessments")
    if not isinstance(assessments, list):
        return None

    counts: list[str] = []
    for status in (
        MilestoneStatus.MISSED,
        MilestoneStatus.AT_RISK,
        MilestoneStatus.ON_TRACK,
        MilestoneStatus.COMPLETED,
        MilestoneStatus.DEFERRED,
    ):
        count = sum(
            1
            for assessment in assessments
            if isinstance(assessment, dict) and assessment.get("computed_health") == status.value
        )
        if count:
            counts.append(f"{count} {status.value.replace('_', ' ')}")
    return ", ".join(counts) or None


def _build_status_ai_safety_summary(manifest_path: Path) -> str | None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"malformed manifest at {_display_status_path(manifest_path)}"
    if not isinstance(payload, dict):
        return f"malformed manifest at {_display_status_path(manifest_path)}"

    metadata = payload.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        return f"malformed manifest at {_display_status_path(manifest_path)}"

    ai_safety = metadata.get("ai_safety")
    if ai_safety is None:
        return None
    if not isinstance(ai_safety, dict):
        return f"malformed manifest at {_display_status_path(manifest_path)}"
    if not ai_safety:
        return None

    enabled = bool(ai_safety.get("enabled"))
    budget_usd = ai_safety.get("budget_usd")
    spent_usd = ai_safety.get("spent_usd")
    ai_calls = ai_safety.get("ai_calls")
    within_budget = ai_safety.get("within_budget")
    trace_run_id = ai_safety.get("trace_run_id")

    if not enabled and not ai_calls and not spent_usd:
        return "disabled"

    parts: list[str] = []
    if isinstance(ai_calls, int):
        parts.append(f"{ai_calls} AI call{'s' if ai_calls != 1 else ''}")
    if isinstance(spent_usd, (int, float)) and isinstance(budget_usd, (int, float)):
        posture = "within budget" if bool(within_budget) else "budget exceeded"
        parts.append(f"${float(spent_usd):.6f} / ${float(budget_usd):.6f} ({posture})")
    if isinstance(trace_run_id, str) and trace_run_id.strip():
        parts.append(f"trace {trace_run_id.strip()}")
    return "; ".join(parts) or None


def _build_status_telemetry_summary(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime,
) -> str | None:
    return build_program_telemetry_summary(
        program_id,
        programs_root=programs_root,
        as_of=as_of,
    )


def _build_status_telemetry_confidence(
    program_id: str,
    *,
    programs_root: Path,
    as_of: datetime,
) -> str | None:
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    signals = signal_store.read(program_id, end=as_of)
    review_states = signal_store.read_reviews(program_id)
    telemetry_signals = [
        signal
        for signal in signals
        if signal_is_approved_for_evidence(signal, review_states)
        and signal.source in {"ado/analytics", "ado/wiql", "ado/sprint", "ado/pipeline", "ado/pr"}
    ]
    if not telemetry_signals:
        return None
    confidence_order = {
        Confidence.HIGH: 3,
        Confidence.MEDIUM: 2,
        Confidence.LOW: 1,
        Confidence.NONE: 0,
    }
    return max(telemetry_signals, key=lambda signal: confidence_order[signal.confidence]).confidence.value.lower()


def _charter_scope_statement(raw_program: dict[str, object]) -> str | None:
    charter = raw_program.get("charter")
    if not isinstance(charter, dict):
        return None
    scope_statement = charter.get("scope_statement")
    if not isinstance(scope_statement, str):
        return None
    normalized = " ".join(scope_statement.strip().split())
    return normalized or None


def _load_latest_manifest(output_dir: Path) -> tuple[Path, int, dict[str, bool], datetime | None] | None:
    manifests: list[tuple[Path, int]] = []
    if output_dir.exists():
        for path in output_dir.glob("issue_*/issue_*.manifest.json"):
            match = _MANIFEST_NAME_RE.fullmatch(path.name)
            if match is not None:
                manifests.append((path, int(match.group(1))))
    if not manifests:
        return None
    path, issue_number = max(manifests, key=lambda item: item[1])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path, issue_number, {}, None
    raw_qg = payload.get("qg_results") if isinstance(payload, dict) else {}
    qg_results = {str(key): bool(value) for key, value in raw_qg.items()} if isinstance(raw_qg, dict) else {}
    ended_at = _parse_datetime(payload.get("ended_at")) if isinstance(payload, dict) else None
    return path, issue_number, qg_results, ended_at


def _load_latest_snapshot(output_dir: Path) -> tuple[Path, datetime] | None:
    snapshots: list[tuple[Path, int]] = []
    if output_dir.exists():
        for path in output_dir.glob("issue_*/issue_*.snapshot.json"):
            match = _SNAPSHOT_NAME_RE.fullmatch(path.name)
            if match is not None:
                snapshots.append((path, int(match.group(1))))
    if not snapshots:
        return None
    path, _issue_number = max(snapshots, key=lambda item: item[1])
    snapshot = read_snapshot(path)
    return path, snapshot.ado_data_as_of


def _build_projected_blocker_count(
    *,
    edition_name: str,
    program_id: str,
    snapshot_path: Path,
    editions_root: Path,
    programs_root: Path,
) -> int:
    bundle = load_report_bundle(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    snapshot = read_snapshot(snapshot_path)
    snapshot_items = _snapshot_to_work_items(snapshot)
    snapshot_as_of = snapshot.ado_data_as_of
    freshness_report = build_freshness_report(
        current_items=snapshot_items,
        issue_number=snapshot.issue_number,
        as_of=snapshot_as_of,
        stale_warn_days=bundle.editorial_rules.stale_warn_days,
        stale_block_days=bundle.editorial_rules.stale_block_days,
        previous_snapshot=None,
        previous_notification_state=None,
        program_context=None,
        workstream_narrative_history={},
    )
    overdue_actions = assess_action_staleness(
        tuple(
            action
            for action in project_action_items(
                load_program_facts(program_id, db_root=programs_root.parent, programs_root=programs_root)
            )
            if action.status in {ActionStatus.PROPOSED, ActionStatus.OPEN, ActionStatus.IN_PROGRESS}
        ),
        snapshot_as_of.date(),
    )
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    icm_signals = tuple(
        signal
        for signal in signal_store.read(
            program_id,
            start=snapshot_as_of - timedelta(days=bundle.config.ado.date_window_days),
            end=snapshot_as_of,
        )
        if signal_source_family(signal.source) == "icm"
    )
    projections = build_issue_projection(
        items=snapshot_items,
        freshness_report=freshness_report,
        icm_signals=icm_signals,
        open_asks=load_open_decision_asks(program_id, programs_root=programs_root),
        overdue_actions=overdue_actions,
    )
    return sum(1 for entry in projections if entry.severity == "block")


def _snapshot_to_work_items(snapshot) -> tuple[WorkItem, ...]:
    return tuple(
        WorkItem(
            id=item.id,
            type=item.type,
            title=item.title,
            state=item.state,
            assigned_to=item.assigned_to,
            assigned_to_email=None,
            area_path=item.area_path,
            iteration_path="",
            target_date=item.target_date,
            risk_level=item.risk_level,
            tags=list(item.tags),
            custom_fields={"changed_date": snapshot.ado_data_as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=snapshot.ado_data_as_of,
        )
        for item in snapshot.items
    )


def _manifest_timestamp(latest_manifest: tuple[Path, int, dict[str, bool], datetime | None] | None) -> datetime | None:
    if latest_manifest is None:
        return None
    return latest_manifest[3]


def _describe_cadence(cadence: str, last_confirmed_at: datetime | None, now: datetime) -> str:
    if last_confirmed_at is None:
        return "no confirmed issues yet"
    window = get_cadence_window(cadence)
    if window is None:
        return "cadence window unknown"
    if check_cadence(cadence, last_confirmed_at, as_of=now):
        return "on track"
    elapsed = now - last_confirmed_at
    overdue_days = max(1, int(elapsed.total_seconds() // timedelta(days=1).total_seconds()) - int(window.total_seconds() // timedelta(days=1).total_seconds()))
    return f"overdue by {overdue_days} day{'s' if overdue_days != 1 else ''}"


def _resolve_next_due_status(
    raw_program: dict[str, object],
    *,
    fallback_edition: str,
    fallback_status: str,
    as_of: datetime,
    editions_root: Path,
    programs_root: Path,
    archive_root: Path,
) -> tuple[str, str, str | None]:
    plan_entries = load_communication_plan_entries(raw_program)
    if not plan_entries:
        plan_entries = (CommunicationPlanEntry(edition=fallback_edition),)

    candidates: list[tuple[int, datetime, int, str, str, str | None]] = []
    for index, entry in enumerate(plan_entries):
        edition_name = entry.edition
        resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
        if resolved is None:
            continue
        latest_confirmed = find_latest_confirmed_entry(read_archive_index(edition_name, archive_root=archive_root))
        last_confirmed_at = latest_confirmed.generated_at if latest_confirmed is not None else None
        cadence = entry.cadence or resolved.edition.cadence
        status = _describe_cadence(cadence, last_confirmed_at, as_of)
        due_at = _cadence_due_at(cadence, last_confirmed_at)
        if due_at is not None and due_at <= as_of:
            priority = 0
            due_marker = due_at
        elif last_confirmed_at is None:
            priority = 1
            due_marker = datetime.min.replace(tzinfo=timezone.utc)
        elif due_at is None:
            priority = 3
            due_marker = datetime.max.replace(tzinfo=timezone.utc)
        else:
            priority = 2
            due_marker = due_at
        candidates.append((priority, due_marker, index, edition_name, status, describe_communication_plan_entry(entry)))

    if not candidates:
        return fallback_edition, fallback_status, None

    _priority, _due_marker, _index, edition_name, status, context = min(candidates)
    return edition_name, status, context


def _cadence_due_at(cadence: str, last_confirmed_at: datetime | None) -> datetime | None:
    if last_confirmed_at is None:
        return None
    window = get_cadence_window(cadence)
    if window is None:
        return None
    return last_confirmed_at + window


def _format_relative_or_unknown(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return _format_relative(value, datetime.now(timezone.utc))


def _format_relative(value: datetime, now: datetime) -> str:
    elapsed = max(now - value, timedelta())
    if elapsed < timedelta(minutes=1):
        return "just now"
    if elapsed < timedelta(hours=1):
        minutes = int(elapsed.total_seconds() // 60)
        return f"{minutes} min ago"
    if elapsed < timedelta(days=2):
        hours = int(elapsed.total_seconds() // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = elapsed.days
    return f"{days} day{'s' if days != 1 else ''} ago"


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)