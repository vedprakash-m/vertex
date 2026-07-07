"""Editorial/advisory gate helpers extracted from ``src/core/quality_gates``."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, cast

from src.core.journal import PROGRAMS_ROOT
from src.core.ledger.candidate_store import active_candidates, active_count
from src.core.ledger.event_log import read_events
from src.core.ledger.program_views import canonical_projection_dump, get_current_projection_path
from src.core.models import WorkItem
from src.core.program_fact_store import load_program_facts
from src.core.quality_gates.models import GateEvaluation


def evaluate_claim_freshness_gate(
    stale_claim_ids: tuple[str, ...] | list[str],
) -> GateEvaluation:
    resolved_claim_ids = tuple(dict.fromkeys(claim_id for claim_id in stale_claim_ids if isinstance(claim_id, str) and claim_id.strip()))
    if not resolved_claim_ids:
        return GateEvaluation(
            gate_id="QG-DM-13",
            passed=True,
            message="Claim freshness gate passed. No stale cited claims detected in persisted section evidence.",
            exit_code=1,
            forceable=True,
        )
    preview = ", ".join(resolved_claim_ids[:5])
    suffix = "" if len(resolved_claim_ids) <= 5 else f" (+{len(resolved_claim_ids) - 5} more)"
    return GateEvaluation(
        gate_id="QG-DM-13",
        passed=False,
        message=f"Claim freshness advisory: latest persisted section evidence still cites expired/stale claims {preview}{suffix}.",
        exit_code=1,
        forceable=True,
    )


def evaluate_candidate_triage_latency_gate(
    *,
    program_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation(
            gate_id="QG-DM-6",
            passed=True,
            message="Candidate triage latency gate passed (skipped: program id not provided).",
            exit_code=1,
            forceable=True,
        )

    current_active_count = active_count(program_id, programs_root=programs_root)
    current_active = active_candidates(program_id, programs_root=programs_root)
    if current_active_count <= 0:
        return GateEvaluation(
            gate_id="QG-DM-6",
            passed=True,
            message="Candidate triage latency gate passed. No active ledger candidates pending triage.",
            exit_code=1,
            forceable=True,
        )

    oldest_staged_at = min(
        (candidate.staged_at for candidate in current_active if candidate.staged_at is not None),
        default=None,
    )
    if oldest_staged_at is None:
        return GateEvaluation(
            gate_id="QG-DM-6",
            passed=False,
            message=f"Candidate triage latency advisory: {current_active_count} active ledger candidate(s) pending triage; oldest staging time is unavailable.",
            exit_code=1,
            forceable=True,
        )

    age_days = ((now or datetime.now(timezone.utc)) - oldest_staged_at).days
    if current_active_count > 100 or age_days > 14:
        return GateEvaluation(
            gate_id="QG-DM-6",
            passed=False,
            message=f"Candidate triage latency advisory: {current_active_count} active ledger candidate(s) pending triage; oldest staged {age_days} day(s) ago.",
            exit_code=1,
            forceable=True,
        )

    return GateEvaluation(
        gate_id="QG-DM-6",
        passed=True,
        message=f"Candidate triage latency gate passed. {current_active_count} active ledger candidate(s); oldest staged {age_days} day(s) ago.",
        exit_code=1,
        forceable=True,
    )


def evaluate_gap_detection_sla_gate(
    *,
    program_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation(
            gate_id="QG-DM-5",
            passed=True,
            message="Gap-detection SLA gate passed (skipped: program id not provided).",
            exit_code=1,
            forceable=True,
        )

    try:
        from src.core.edition_resolver import load_program
        from src.core.gather_state_store import load_gather_state

        program = load_program(program_id, programs_root=programs_root)
        gather_state = load_gather_state(program_id, programs_root=programs_root)
    except Exception:
        return GateEvaluation(
            gate_id="QG-DM-5",
            passed=True,
            message="Gap-detection SLA gate passed (skipped: program metadata or gather state unavailable).",
            exit_code=1,
            forceable=True,
        )

    cadence_hours = getattr(program, "expected_gather_cadence_hours", None) if program is not None else None
    if cadence_hours is None:
        return GateEvaluation(
            gate_id="QG-DM-5",
            passed=True,
            message="Gap-detection SLA gate passed (skipped: expected gather cadence is not configured).",
            exit_code=1,
            forceable=True,
        )
    if gather_state is None:
        return GateEvaluation(
            gate_id="QG-DM-5",
            passed=True,
            message="Gap-detection SLA gate passed (skipped: no gather state recorded yet).",
            exit_code=1,
            forceable=True,
        )

    active_channels = tuple(
        channel_name
        for channel_name, channel_state in sorted((gather_state.channels or {}).items())
        if isinstance(channel_state, dict) and bool(channel_state.get("active"))
    )
    if not active_channels:
        return GateEvaluation(
            gate_id="QG-DM-5",
            passed=True,
            message="Gap-detection SLA gate passed (skipped: no active gathered channels recorded).",
            exit_code=1,
            forceable=True,
        )

    effective_now = now or datetime.now(timezone.utc)
    stale_after = timedelta(hours=float(cadence_hours) * 2.0)
    gather_age = effective_now - gather_state.gathered_at
    if gather_age <= stale_after:
        return GateEvaluation(
            gate_id="QG-DM-5",
            passed=True,
            message=(
                f"Gap-detection SLA gate passed. Latest gather heartbeat is {int(gather_age.total_seconds() // 3600)} hour(s) old "
                f"across active channels {', '.join(active_channels)}."
            ),
            exit_code=1,
            forceable=True,
        )

    recent_gap_pipelines = {
        str(event.payload.get("pipeline") or "").strip().lower()
        for event in read_events(program_id, programs_root=programs_root)
        if event.event_type == "pipeline.gap_detected.v1" and event.recorded_at >= (effective_now - stale_after)
    }
    missing_gap_channels = tuple(
        channel_name
        for channel_name in active_channels
        if channel_name.strip().lower() not in recent_gap_pipelines
    )
    if missing_gap_channels:
        return GateEvaluation(
            gate_id="QG-DM-5",
            passed=False,
            message=(
                f"Gap-detection SLA advisory: latest gather heartbeat is {int(gather_age.total_seconds() // 3600)} hour(s) old "
                f"(threshold {int(stale_after.total_seconds() // 3600)}h) and active channel(s) {', '.join(missing_gap_channels)} "
                "have no recent `pipeline.gap_detected.v1` record."
            ),
            exit_code=1,
            forceable=True,
        )

    return GateEvaluation(
        gate_id="QG-DM-5",
        passed=True,
        message=(
            f"Gap-detection SLA gate passed. Latest gather heartbeat is stale at {int(gather_age.total_seconds() // 3600)} hour(s), "
            f"but all active channels are covered by recent pipeline gap records: {', '.join(active_channels)}."
        ),
        exit_code=1,
        forceable=True,
    )


def evaluate_unresolved_conflict_budget_gate(
    *,
    program_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation(
            gate_id="QG-DM-7",
            passed=True,
            message="Unresolved conflict budget gate passed (skipped: program id not provided).",
            exit_code=1,
            forceable=True,
        )

    try:
        snapshot = load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("fact.conflict",),
        )
    except Exception:
        return GateEvaluation(
            gate_id="QG-DM-7",
            passed=True,
            message="Unresolved conflict budget gate passed (skipped: program fact snapshot unavailable).",
            exit_code=1,
            forceable=True,
        )

    open_material_conflicts = load_open_material_conflicts(
        program_id,
        programs_root=programs_root,
    )
    if not open_material_conflicts:
        return GateEvaluation(
            gate_id="QG-DM-7",
            passed=True,
            message="Unresolved conflict budget gate passed. No unresolved material fact conflicts are present.",
            exit_code=1,
            forceable=True,
        )

    conflict_summary = summarize_open_material_conflicts(
        program_id,
        programs_root=programs_root,
    )
    preview = ", ".join(cast(list[str], conflict_summary["previews"]))
    suffix = conflict_summary["additional_count_suffix"]
    return GateEvaluation(
        gate_id="QG-DM-7",
        passed=False,
        message=(
            f"Unresolved conflict budget advisory: {len(open_material_conflicts)} unresolved material fact conflict(s) remain at confirm time: "
            f"{preview}{suffix}."
        ),
        exit_code=1,
        forceable=True,
    )


def load_open_material_conflicts(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Any, ...]:
    snapshot = load_program_facts(
        program_id,
        programs_root=programs_root,
        fact_types=("fact.conflict",),
    )
    return tuple(
        fact
        for fact in snapshot.facts
        if fact.fact_type == "fact.conflict"
        and not bool(fact.payload.get("resolved", False))
        and bool(fact.payload.get("is_material", False))
    )


def summarize_open_material_conflicts(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    preview_limit: int = 5,
) -> dict[str, object]:
    conflicts = load_open_material_conflicts(program_id, programs_root=programs_root)
    previews = tuple(_format_material_conflict_preview(fact) for fact in conflicts[:preview_limit])
    additional_count = max(0, len(conflicts) - len(previews))
    suffix = "" if additional_count == 0 else f" (+{additional_count} more)"
    return {
        "count": len(conflicts),
        "previews": previews,
        "additional_count": additional_count,
        "additional_count_suffix": suffix,
    }


def _format_material_conflict_preview(fact: Any) -> str:
    description = str(
        fact.payload.get("description")
        or fact.payload.get("conflict_description")
        or fact.natural_key
    ).strip()
    if len(description) > 80:
        description = description[:77] + "..."
    family = str(fact.payload.get("family") or "unknown")
    return f"{family}: {description}"


def evaluate_projection_freshness_gate(
    *,
    program_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation(
            gate_id="QG-DM-10",
            passed=True,
            message="Projection freshness gate passed (skipped: program id not provided).",
            exit_code=1,
            forceable=True,
        )

    events = read_events(program_id, programs_root=programs_root)
    if not events:
        return GateEvaluation(
            gate_id="QG-DM-10",
            passed=True,
            message="Projection freshness gate passed. No ledger events recorded yet.",
            exit_code=1,
            forceable=True,
        )

    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    if not projection_path.exists():
        return GateEvaluation(
            gate_id="QG-DM-10",
            passed=False,
            message=f"Projection freshness advisory: current projection missing for program {program_id}; run `vertex ledger replay --program {program_id}`.",
            exit_code=1,
            forceable=True,
        )

    projection = canonical_projection_dump(projection_path)
    projection_meta = next(iter(projection.get("projection_meta", [])), None)
    projection_watermark = projection_meta.get("event_watermark") if isinstance(projection_meta, dict) else None
    ledger_head = events[-1].event_id
    if projection_watermark != ledger_head:
        return GateEvaluation(
            gate_id="QG-DM-10",
            passed=False,
            message=(
                f"Projection freshness advisory: current projection watermark {projection_watermark or '<missing>'} "
                f"lags ledger head {ledger_head}; run `vertex ledger replay --program {program_id}`."
            ),
            exit_code=1,
            forceable=True,
        )

    return GateEvaluation(
        gate_id="QG-DM-10",
        passed=True,
        message="Projection freshness gate passed. Current projection watermark matches the ledger head.",
        exit_code=1,
        forceable=True,
    )


def evaluate_exec_summary_staleness_gate(
    edition_name: str | None,
    issue_number: int | None,
) -> GateEvaluation:
    if edition_name is None or issue_number is None:
        return GateEvaluation(
            gate_id="QG-23",
            passed=True,
            message="Exec summary staleness gate passed (skipped: edition or issue number not provided).",
            exit_code=0,
            forceable=True,
        )

    try:
        from src.core.exec_summary_diff_engine import check_exec_summary_staleness
        from src.core.narrative_store import REPORTS_ROOT

        findings = check_exec_summary_staleness(
            edition=edition_name,
            issue_number=issue_number,
            reports_root=REPORTS_ROOT,
        )
        stale_findings = [finding for finding in findings if finding.is_stale]
        if not stale_findings:
            return GateEvaluation(
                gate_id="QG-23",
                passed=True,
                message="Exec summary staleness gate passed. No stale bullets detected.",
                exit_code=0,
                forceable=True,
            )

        if len(stale_findings) == 1:
            workstream_description = stale_findings[0].workstream_id
        else:
            workstream_description = ", ".join(finding.workstream_id for finding in stale_findings)

        return GateEvaluation(
            gate_id="QG-23",
            passed=False,
            message=f"Exec summary bullet for {workstream_description} appears stale — workstream lead changed but exec bullet did not.",
            exit_code=1,
            forceable=True,
        )
    except Exception as exc:
        return GateEvaluation(
            gate_id="QG-23",
            passed=False,
            message=f"Exec summary staleness gate failed with error: {exc}",
            exit_code=1,
            forceable=True,
        )


def evaluate_metric_injection_and_ado_hygiene_gate(
    *,
    program_id: str | None,
    narratives: Mapping[str, str] | Iterable[str],
    items: tuple[WorkItem, ...] | list[WorkItem] = (),
) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation(
            gate_id="QG-24",
            passed=True,
            message="Metric injection and ADO hygiene gate passed.",
            exit_code=0,
            forceable=True,
        )

    try:
        from src.core.ado_enrichment import ADO_RISK_ASSESSMENT_COMMENT_FIELD, ADO_RISK_ASSESSMENT_FIELD
        from src.core.reality_store import RealityStore

        metric_ids: set[str] = set()
        narrative_texts = list(narratives.values()) if isinstance(narratives, Mapping) else list(narratives)
        metric_pattern = re.compile(r"<!--\s*vertex:metric:\s*(\S+)\s*-->", re.IGNORECASE)
        for text in narrative_texts:
            for match in metric_pattern.finditer(text):
                metric_ids.add(match.group(1).strip())

        store = RealityStore(program_id)
        store.initialize()

        missing_metrics = [metric_id for metric_id in metric_ids if not store.list_metric_observations(metric_id)]

        field_hygiene_errors: list[str] = []
        if items:
            # risk_assessment and risk_assessment_comment are first-class WorkItem fields
            # (populated from Custom.RiskAssessment / Custom.RiskAssessmentComment during gather).
            # Check those directly — they are never stored in item.custom_fields.
            has_risk_field = any(item.risk_assessment is not None for item in items)
            has_comment_field = any(item.risk_assessment_comment is not None for item in items)

            if not has_risk_field:
                field_hygiene_errors.append(
                    f"No work items have '{ADO_RISK_ASSESSMENT_FIELD}' set; "
                    "field may not exist in this ADO project or no items have been assessed."
                )
            if not has_comment_field:
                field_hygiene_errors.append(
                    f"No work items have '{ADO_RISK_ASSESSMENT_COMMENT_FIELD}' set; "
                    "field may not exist in this ADO project or no items have comments."
                )

        details: list[str] = []
        if missing_metrics:
            details.append(
                f"Metric injection placeholder(s) cannot be resolved from reality_store: {', '.join(missing_metrics)}"
            )
        if field_hygiene_errors:
            details.extend(field_hygiene_errors)

        if details:
            return GateEvaluation(
                gate_id="QG-24",
                passed=False,
                message="; ".join(details),
                exit_code=1,
                forceable=True,
            )

        return GateEvaluation(
            gate_id="QG-24",
            passed=True,
            message="Metric injection and ADO hygiene gate passed.",
            exit_code=0,
            forceable=True,
        )
    except Exception as exc:
        return GateEvaluation(
            gate_id="QG-24",
            passed=False,
            message=f"Metric injection and ADO hygiene gate failed with error: {exc}",
            exit_code=1,
            forceable=True,
        )


def evaluate_email_signal_coverage_gate(
    *,
    channel_states: dict[str, dict[str, Any]] | None,
    program_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> GateEvaluation:
    if program_id is None:
        return GateEvaluation(
            gate_id="QG-25",
            passed=True,
            message="Email signal coverage gate passed (skipped: program_id not provided).",
            exit_code=0,
            forceable=True,
        )

    if channel_states is None:
        return GateEvaluation(
            gate_id="QG-25",
            passed=True,
            message="Email signal coverage gate passed (skipped: channel_states not provided).",
            exit_code=0,
            forceable=True,
        )

    workiq_state = channel_states.get("workiq", {})
    if not workiq_state.get("active"):
        return GateEvaluation(
            gate_id="QG-25",
            passed=True,
            message="Email signal coverage gate passed (WorkIQ channel not active).",
            exit_code=0,
            forceable=True,
        )

    email_signals = workiq_state.get("email_signals", 0)
    if email_signals > 0:
        return GateEvaluation(
            gate_id="QG-25",
            passed=True,
            message=f"Email signal coverage gate passed ({email_signals} email signal(s) collected).",
            exit_code=0,
            forceable=True,
        )

    last_error = workiq_state.get("last_error")
    if last_error is not None:
        return GateEvaluation(
            gate_id="QG-25",
            passed=True,
            message=f"Email signal coverage gate passed (WorkIQ error present; not a coverage gap): {last_error}",
            exit_code=0,
            forceable=True,
        )

    try:
        from src.core.edition_resolver import load_program

        program = load_program(program_id, programs_root=programs_root)
        has_email_filters = _program_has_email_subject_filters(program)
    except Exception:
        has_email_filters = False

    if not has_email_filters:
        return GateEvaluation(
            gate_id="QG-25",
            passed=True,
            message="Email signal coverage gate passed (no email_subject_filters configured).",
            exit_code=0,
            forceable=True,
        )

    try:
        from src.core.gather_state_store import load_gather_state

        gather_state = load_gather_state(program_id, programs_root=programs_root)
        consecutive_empty_runs = _count_consecutive_email_empty_runs(
            gather_state,
            channel_states,
            programs_root=programs_root,
        )
        if consecutive_empty_runs >= 3:
            return GateEvaluation(
                gate_id="QG-25",
                passed=True,
                message=f"[WARN QG-25] Email signal count is 0 but email_subject_filters are configured. Circuit breaker: {consecutive_empty_runs} consecutive empty runs — email extraction will be disabled until operator confirmation.",
                exit_code=0,
                forceable=True,
            )
        run_description = f"run {consecutive_empty_runs + 1}" if consecutive_empty_runs > 0 else "this run"
    except Exception:
        run_description = "this run"

    return GateEvaluation(
        gate_id="QG-25",
        passed=True,
        message=f"[WARN QG-25] Email signal count is 0 on {run_description} but email_subject_filters are configured. Verify AgencyBridge auth and WorkIQ connectivity.",
        exit_code=0,
        forceable=True,
    )


def _program_has_email_subject_filters(program: Any) -> bool:
    if not hasattr(program, "workstreams"):
        return False
    for workstream in program.workstreams:
        if hasattr(workstream, "signal_sources") and workstream.signal_sources is not None:
            if hasattr(workstream.signal_sources, "email_subject_filters"):
                if workstream.signal_sources.email_subject_filters:
                    return True
    return False


def evaluate_kpi_degradation_gate(
    *,
    program_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> GateEvaluation:
    """QG-28 (WS-1 PB-4): forceable gate that fires when any KPI query is degraded.

    Reads gather_state.query_states and flags any entry where is_degraded=True.
    Vacuous pass when program_id is None or no gather state exists.
    """
    if program_id is None:
        return GateEvaluation(
            gate_id="QG-28",
            passed=True,
            message="QG-28: KPI degradation gate passed (n/a: no program_id).",
            exit_code=0,
            forceable=True,
        )
    try:
        from src.core.gather_state_store import load_gather_state

        gather_state = load_gather_state(program_id, programs_root=programs_root)
    except Exception:
        return GateEvaluation(
            gate_id="QG-28",
            passed=True,
            message="QG-28: KPI degradation gate passed (n/a: gather state unavailable).",
            exit_code=0,
            forceable=True,
        )
    if gather_state is None:
        return GateEvaluation(
            gate_id="QG-28",
            passed=True,
            message="QG-28: KPI degradation gate passed (n/a: no gather state recorded).",
            exit_code=0,
            forceable=True,
        )
    query_states: dict[str, Any] = getattr(gather_state, "query_states", {}) or {}
    degraded = [q_id for q_id, state in query_states.items() if state.get("is_degraded")]
    if not degraded:
        return GateEvaluation(
            gate_id="QG-28",
            passed=True,
            message=f"QG-28: All {len(query_states)} KPI queries healthy.",
            exit_code=0,
            forceable=True,
        )
    sample = degraded[:5]
    suffix = f" (+{len(degraded) - 5} more)" if len(degraded) > 5 else ""
    return GateEvaluation(
        gate_id="QG-28",
        passed=False,
        message=f"QG-28: {len(degraded)} KPI query(-ies) degraded: {', '.join(sample)}{suffix}. Run `vertex gather` to refresh or use --force to override.",
        exit_code=1,
        forceable=True,
    )


def _count_consecutive_email_empty_runs(
    gather_state: Any,
    current_channel_states: dict[str, dict[str, Any]],
    *,
    programs_root: Path,
) -> int:
    if gather_state is None:
        return 0

    current_email_signals = current_channel_states.get("workiq", {}).get("email_signals", 0)
    if current_email_signals > 0:
        return 0

    previous_channels = getattr(gather_state, "previous_channels", None)
    if not previous_channels:
        return 1

    previous_email_signals = previous_channels.get("workiq", {}).get("email_signals", 0)
    if previous_email_signals > 0:
        return 1

    prior_previous_channels = getattr(gather_state, "previous_channels", None)
    if prior_previous_channels is None:
        return 2

    prior_previous_email = prior_previous_channels.get("workiq", {}).get("email_signals", 0)
    return 3 if prior_previous_email == 0 else 2
