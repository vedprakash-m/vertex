"""ADF-W0.8: build a read-only ``CockpitSnapshot`` from existing stores.

The cockpit is a projection, never a new source of truth (audit
reconciliation, Section 2: "Build another observability subsystem" ->
Rejected). Every field is either a real value read from an existing durable
store, or an explicit ``None``/``0``/empty value paired with a
``CockpitFinding`` explaining what has not landed yet and which work item
owns it. No field is ever a fabricated or formula-guessed placeholder
(INV-ADF-11 spirit, applied to every summary, not only value metrics).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.core.adf_config import ArchDataFixConfig, ExecutionMode, load_arch_data_fix
from src.core.ai_telemetry import read_ai_telemetry
from src.core.cockpit_models import (
    COCKPIT_SCHEMA_VERSION,
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ProposalClassTrustSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    TrustCockpitSummary,
    ValueCockpitSummary,
    ValueConfidence,
    ValueMetric,
    finalize_cockpit_snapshot,
)
from src.core.context_proposal_review import load_pending_context_proposal_rows
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.fact_lineage_coverage import compute_lineage_coverage
from src.core.maturity_engine import load_earned_autonomy_state
from src.core.outcome_metrics import canary_window_status, compute_om4_audit_coverage
from src.core.program_synthesis import load_latest_released_program_synthesis
from src.core.ledger.event_log import read_events
from src.core.measurement_store import read_measurements, tier_decision_store_path
from src.core.proposal_autonomy_ladder import PROPOSAL_CLASSES, resolve_ceiling
from src.core.risk_register_engine import compute_risk_score, load_risk_register
from src.core.run_telemetry import read_run_telemetry

#: Section 8.15.1's "Authority" column, by ladder level.
_AUTONOMY_LEVEL_PERMITTED_ACTION = {
    "l0": "Deterministic detection/render only",
    "l1": "Advisory proposal",
    "l2": "Proposal with guided batch review",
    "l3": "Approved-batch execution or sampled review",
    "l4": "Standing-policy low-risk execution",
}

_ACTIVE_RISK_STATUS_VALUES = frozenset({"open", "escalated"})
_HIGH_RISK_SCORE_THRESHOLD = 9  # e.g. HIGH impact (3) * LIKELY probability (3)
#: Findings whose next_command is worth surfacing as the single cockpit
#: headline action (Section 10.2: "up to three next actions"; this picks the
#: single highest-priority one for ProgramCockpitSummary.next_action).
_ACTIONABLE_FINDING_STATUSES = ("blocked", "warn")


def build_cockpit_snapshot(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    edition_id: str | None = None,
    now: datetime | None = None,
) -> CockpitSnapshot:
    generated_at = now or datetime.now(timezone.utc)
    config = _load_config_safely(program_id, programs_root)

    findings: list[CockpitFinding] = []

    economics_summary, economics_findings = _build_economics_summary(
        program_id, programs_root=programs_root, observed_at=generated_at
    )
    findings.extend(economics_findings)

    program_summary, risk_findings = _build_program_summary(
        program_id, programs_root=programs_root, observed_at=generated_at
    )
    findings.extend(risk_findings)

    source_summary, source_findings = _build_source_summary(config, observed_at=generated_at)
    findings.extend(source_findings)

    intelligence_summary, intelligence_findings = _build_intelligence_summary(
        program_id, programs_root=programs_root, observed_at=generated_at
    )
    findings.extend(intelligence_findings)

    value_summary, value_findings = _build_value_summary(
        program_id, edition_id=edition_id, programs_root=programs_root, observed_at=generated_at
    )
    findings.extend(value_findings)

    reliability_summary, reliability_findings = _build_reliability_summary(
        program_id, programs_root=programs_root, observed_at=generated_at
    )
    findings.extend(reliability_findings)

    trust_summary, trust_findings = _build_trust_summary(
        program_id, programs_root=programs_root, observed_at=generated_at
    )
    findings.extend(trust_findings)
    findings.extend(
        _build_context_proposal_findings(
            program_id, programs_root=programs_root, observed_at=generated_at
        )
    )

    if config.is_off:
        findings.append(
            CockpitFinding(
                finding_id="platform.governance.mode_off",
                area="platform",
                status="info",
                summary="arch_data_fix governance is off for this program.",
                detail=(
                    "programs/<id>/program.yaml has no arch_data_fix block (or mode: off). Telemetry "
                    "still records; enforcement gates stay in observe/inactive state."
                ),
                owner=None,
                next_command=None,
                evidence_refs=(),
                observed_at=generated_at,
            )
        )

    as_of = generated_at

    # ADF-W1.11: the single highest-priority next action, surfaced once all
    # findings are known (blocked beats warn; first-found wins within a tier,
    # matching the deterministic findings-append order above).
    next_action = _pick_next_action(findings)
    if next_action is not None:
        program_summary = replace(program_summary, next_action=next_action)

    snapshot = CockpitSnapshot(
        schema_version=COCKPIT_SCHEMA_VERSION,
        program_id=program_id,
        edition_id=edition_id,
        generated_at=generated_at,
        as_of=as_of,
        program_summary=program_summary,
        source_summary=source_summary,
        intelligence_summary=intelligence_summary,
        economics_summary=economics_summary,
        value_summary=value_summary,
        reliability_summary=reliability_summary,
        findings=tuple(findings),
        input_hash="",
        trust_summary=trust_summary,
    )
    return finalize_cockpit_snapshot(snapshot)


def _load_config_safely(program_id: str, programs_root: Path) -> ArchDataFixConfig:
    try:
        return load_arch_data_fix(program_id, programs_root=programs_root)
    except Exception:
        return ArchDataFixConfig(program_id=program_id, mode=ExecutionMode.OFF)


def _build_context_proposal_findings(
    program_id: str, *, programs_root: Path, observed_at: datetime
) -> list[CockpitFinding]:
    """Expose pending NCFL revisions without treating them as applied state."""
    try:
        rows = load_pending_context_proposal_rows(program_id, programs_root=programs_root)
    except Exception:
        # The cockpit remains a best-effort read-only projection if an optional
        # proposal artifact is concurrently replaced or damaged.
        return []
    return [
        CockpitFinding(
            finding_id=f"trust.context_proposal.{row.proposal_id}",
            area="trust",
            status="warn",
            summary=f"Pending context revision: {row.target}",
            detail=(
                f"proposed_value={row.proposed_value!r}; current_value_hash={row.current_hash_label}; "
                f"evidence={row.evidence}; conflict_state={row.conflict_state}; "
                f"next_command={row.next_command}"
            ),
            owner=None,
            next_command=row.next_command,
            evidence_refs=(row.evidence,),
            observed_at=observed_at,
        )
        for row in rows
    ]


def _build_economics_summary(
    program_id: str, *, programs_root: Path, observed_at: datetime
) -> tuple[EconomicsCockpitSummary, list[CockpitFinding]]:
    findings: list[CockpitFinding] = []
    telemetry_rows = read_ai_telemetry(program_id, programs_root=programs_root)
    frontier_cost_usd = sum((row.cost_usd or 0.0) for row in telemetry_rows)
    context_tokens_in = sum((row.tokens_in or 0) for row in telemetry_rows)

    tier_rows = read_measurements(tier_decision_store_path(program_id, programs_root=programs_root))
    total_decisions = len(tier_rows)
    frontier_avoidance: float | None = None
    cache_hit_rate: float | None = None
    if total_decisions > 0:
        frontier_calls = sum(1 for row in tier_rows if row.get("outcome") == "frontier_call")
        cache_hits = sum(1 for row in tier_rows if row.get("chosen_tier") == "cache" or row.get("cache_hit") is True)
        frontier_avoidance = 1.0 - (frontier_calls / total_decisions)
        cache_hit_rate = cache_hits / total_decisions
    else:
        findings.append(
            CockpitFinding(
                finding_id="economics.tier_decisions.empty",
                area="economics",
                status="info",
                summary="No AI tier-routing decisions recorded yet.",
                detail="frontier_avoidance and cache_hit_rate are unavailable until at least one routed AI call occurs.",
                owner=None,
                next_command=None,
                evidence_refs=(),
                observed_at=observed_at,
            )
        )

    if not telemetry_rows:
        findings.append(
            CockpitFinding(
                finding_id="economics.ai_telemetry.empty",
                area="economics",
                status="info",
                summary="No AI telemetry recorded yet.",
                detail="frontier_cost_usd and context_tokens_in are 0 because no provider call has been made for this program.",
                owner=None,
                next_command=None,
                evidence_refs=(),
                observed_at=observed_at,
            )
        )

    summary = EconomicsCockpitSummary(
        frontier_avoidance=frontier_avoidance,
        frontier_cost_usd=round(frontier_cost_usd, 6),
        cache_hit_rate=cache_hit_rate,
        context_tokens_in=context_tokens_in,
    )
    return summary, findings


def _build_program_summary(
    program_id: str, *, programs_root: Path, observed_at: datetime
) -> tuple[ProgramCockpitSummary, list[CockpitFinding]]:
    findings: list[CockpitFinding] = []
    try:
        risks = load_risk_register(program_id, programs_root=programs_root)
    except Exception:
        risks = ()

    active_risks = [risk for risk in risks if getattr(risk.status, "value", risk.status).lower() in _ACTIVE_RISK_STATUS_VALUES]
    high_risk_ids: list[str] = []
    for risk in active_risks:
        try:
            score = compute_risk_score(risk)
        except Exception:
            continue
        if score >= _HIGH_RISK_SCORE_THRESHOLD:
            high_risk_ids.append(str(risk.id))
    high_risk_count = len(high_risk_ids)

    if high_risk_count > 0:
        overall_risk = "red"
    elif active_risks:
        overall_risk = "yellow"
    else:
        overall_risk = "green"

    if high_risk_count > 0:
        findings.append(
            CockpitFinding(
                finding_id="program.risk.high_active_count",
                area="program",
                status="warn",
                summary=f"{high_risk_count} active risk(s) at high probability*impact.",
                detail=(
                    "Derived from the existing human-assessed risk register (probability x impact >= "
                    f"{_HIGH_RISK_SCORE_THRESHOLD}); this is a summary of already-assessed entries, not a "
                    "new risk judgment (INV-ADF-13)."
                ),
                owner=None,
                next_command=f"vertex doctor --edition {program_id}",
                evidence_refs=tuple(high_risk_ids),
                observed_at=observed_at,
            )
        )

    summary = ProgramCockpitSummary(
        overall_risk=overall_risk,
        readiness_percent=None,
        blocker_count=high_risk_count,
        top_three_candidates=(),
        next_action=None,
    )
    return summary, findings


def _build_source_summary(
    config: ArchDataFixConfig, *, observed_at: datetime
) -> tuple[SourceCockpitSummary, list[CockpitFinding]]:
    findings: list[CockpitFinding] = []
    required_channels = tuple(name for name, budget in config.channels.items() if budget.required)
    summary = SourceCockpitSummary(
        required_healthy=0,
        required_total=len(required_channels),
        stale_sources=(),
        degraded_sources=(),
        manual_sources=(),
        newest_watermarks={},
    )
    findings.append(
        CockpitFinding(
            finding_id="source.health.not_probed",
            area="source",
            status="info",
            summary="Source health and watermarks are not probed yet (QG-38).",
            detail=(
                "Channel execution policy and watermark recording land with ADF-W1.4/ADF-W2.2 (Slice 1-2). "
                "required_healthy/newest_watermarks stay at their empty defaults rather than an assumed value."
            ),
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=observed_at,
        )
    )
    return summary, findings


def _build_value_summary(
    program_id: str, *, edition_id: str | None, programs_root: Path, observed_at: datetime
) -> tuple[ValueCockpitSummary, list[CockpitFinding]]:
    """ADF-W1.11: a real, measured report wall-time before/after metric.

    Sourced from ``run_telemetry.jsonl`` (WS-17), which already exists and
    is written on every real gather/report run -- no new measurement
    mechanism, matching the audit reconciliation's "no new observability
    subsystem" decision.
    """
    findings: list[CockpitFinding] = []
    try:
        records = read_run_telemetry(program_id, programs_root=programs_root, window=10)
    except Exception:
        records = ()

    metrics: list[ValueMetric] = []
    if len(records) >= 2:
        earliest, latest = records[0], records[-1]
        metrics.append(
            ValueMetric(
                metric_id="report_wall_time_seconds",
                program_id=program_id,
                edition_id=edition_id,
                scope="program_aggregate",
                label="Report wall-time (oldest retained run -> latest run)",
                value=latest.wall_time_seconds,
                unit="seconds",
                confidence=ValueConfidence.MEASURED,
                baseline_value=earliest.wall_time_seconds,
                delta_value=earliest.wall_time_seconds - latest.wall_time_seconds,
                formula_version="run_telemetry.v1",
                evidence_refs=(f"run:{earliest.run_id}", f"run:{latest.run_id}"),
                period_start=earliest.started_at,
                period_end=latest.finished_at,
            )
        )
    else:
        findings.append(
            CockpitFinding(
                finding_id="value.report_wall_time.insufficient_history",
                area="value",
                status="info",
                summary="Not enough run_telemetry history yet for a report wall-time before/after metric.",
                detail=(
                    f"{len(records)} run(s) recorded; at least 2 are needed to compute a before/after delta. "
                    "Run 'vertex report'/'vertex gather' a few more times to populate history."
                ),
                owner=None,
                next_command=None,
                evidence_refs=(),
                observed_at=observed_at,
            )
        )

    findings.append(
        CockpitFinding(
            finding_id="value.metrics.formula_derived_retired",
            area="value",
            status="info",
            summary="Only measured value metrics are shown (QG-36); no formula-derived estimate is substituted.",
            detail=(
                "ADF-W1.12 retired the formula-based productivity_dividend_hours claim. Additional measured "
                "workflow metrics land with ADF-W2.11/ADF-W3.8/ADF-W4.8/ADF-W5.5."
            ),
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=observed_at,
        )
    )
    return ValueCockpitSummary(metrics=tuple(metrics), time_savings_certification=None), findings


#: ADF-W2.4/W2.5: defect share above this fraction escalates the cockpit
#: finding from "info" to "warn" -- an arbitrary-but-reasonable early bar,
#: not a ratified quality gate threshold (no QG enforces this ratio yet).
_LINEAGE_DEFECT_WARN_RATIO = 0.10


def _latest_released_program_synthesis_kwargs(program_id: str, *, programs_root: Path) -> dict[str, str | None]:
    """ADF-W2.9: a deterministic, Zone-A-only read of the most recent
    QG-29-*released* ``ProgramSynthesis``, or all-``None`` if none exists
    yet (nothing has called ``src.ai.program_synthesizer.generate_program_synthesis``
    for this program). Never surfaces an unreleased draft."""
    try:
        synthesis = load_latest_released_program_synthesis(program_id, programs_root=programs_root)
    except Exception:
        synthesis = None
    if synthesis is None:
        return {
            "program_synthesis_through_line": None,
            "program_synthesis_generated_at": None,
            "program_synthesis_ai_run_id": None,
        }
    return {
        "program_synthesis_through_line": synthesis.through_line,
        "program_synthesis_generated_at": synthesis.generated_at.isoformat(),
        "program_synthesis_ai_run_id": synthesis.ai_run_id,
    }


def _build_intelligence_summary(
    program_id: str, *, programs_root: Path, observed_at: datetime
) -> tuple[IntelligenceCockpitSummary, list[CockpitFinding]]:
    """ADF-W2.4/W2.5: ``lineage_coverage`` is real -- computed by
    ``fact_lineage_coverage.py`` as lineaged / (lineaged + waived + defect)
    across the program's active fact-store revisions (Section 8.14.2's
    three-way denominator). ``verification_coverage``/``extraction_quality``/
    ``contradiction_count`` remain unmeasured: separate, not-yet-built
    AI-extraction-quality and contradiction-resolution work, not part of
    this item.
    """
    findings: list[CockpitFinding] = []
    synthesis_kwargs = _latest_released_program_synthesis_kwargs(program_id, programs_root=programs_root)
    if synthesis_kwargs["program_synthesis_through_line"] is not None:
        findings.append(
            CockpitFinding(
                finding_id="intelligence.synthesis.released",
                area="intelligence",
                status="ok",
                summary="A released program synthesis is available (ADF-W2.9, Section 8.10.5).",
                detail=f"ai_run_id={synthesis_kwargs['program_synthesis_ai_run_id']}",
                owner=None,
                next_command=None,
                evidence_refs=(),
                observed_at=observed_at,
            )
        )

    try:
        report = compute_lineage_coverage(program_id, programs_root=programs_root, now=observed_at)
    except Exception:
        report = None

    if report is None or report.total_count == 0:
        summary = IntelligenceCockpitSummary(
            lineage_coverage=None,
            verification_coverage=None,
            extraction_quality=(),
            contradiction_count=0,
            **synthesis_kwargs,
        )
        findings.append(
            CockpitFinding(
                finding_id="intelligence.lineage.no_facts",
                area="intelligence",
                status="info",
                summary="No program facts recorded yet; lineage coverage cannot be measured.",
                detail="lineage_coverage stays unset until at least one active fact-store revision exists.",
                owner=None,
                next_command=None,
                evidence_refs=(),
                observed_at=observed_at,
            )
        )
        return summary, findings

    summary = IntelligenceCockpitSummary(
        lineage_coverage=report.coverage_ratio,
        verification_coverage=None,
        extraction_quality=(),
        contradiction_count=0,
        **synthesis_kwargs,
    )
    if report.defect_count > 0:
        defect_ratio = report.defect_count / report.total_count
        findings.append(
            CockpitFinding(
                finding_id="intelligence.lineage.defects_present",
                area="intelligence",
                status="warn" if defect_ratio > _LINEAGE_DEFECT_WARN_RATIO else "info",
                summary=f"{report.defect_count} of {report.total_count} fact(s) have no traceable provenance.",
                detail=(
                    f"lineaged={report.lineaged_count}, waived={report.waived_count}, defect={report.defect_count}. "
                    f"Sample defect natural_key(s): {', '.join(report.sample_defect_natural_keys) or 'none'}. "
                    "Backfill lineage where durable source evidence exists, or grant an explicit, owned, "
                    "time-bounded waiver via programs/<id>/fact_lineage_waivers.yaml (Section 8.14.2)."
                ),
                owner=None,
                next_command=None,
                evidence_refs=report.sample_defect_natural_keys,
                observed_at=observed_at,
            )
        )
    else:
        findings.append(
            CockpitFinding(
                finding_id="intelligence.lineage.fully_covered",
                area="intelligence",
                status="ok",
                summary=f"All {report.total_count} fact(s) are lineaged or explicitly waived.",
                detail=f"lineaged={report.lineaged_count}, waived={report.waived_count}.",
                owner=None,
                next_command=None,
                evidence_refs=(),
                observed_at=observed_at,
            )
        )
    return summary, findings


def _build_reliability_summary(
    program_id: str, *, programs_root: Path, observed_at: datetime
) -> tuple[ReliabilityCockpitSummary, list[CockpitFinding]]:
    """ADF-W1.11: actuation safety state. ``duplicate_preventions`` is real
    (counts ``actuation.duplicate_prevented.v1`` ledger events emitted by the
    ADF-W1.2 search-before-create safeguard); the outbox-derived counts stay
    at 0 until ADF-W1.3 wires a live mutation domain through it.

    specs/backlog.md BL-C7: ``audit_coverage`` is now OM-4's live value
    (``src.core.outcome_metrics.compute_om4_audit_coverage``) rather than a
    hardcoded ``None`` placeholder -- see governance/outcome-metrics.md.
    """
    findings: list[CockpitFinding] = []
    try:
        events = read_events(program_id, programs_root=programs_root)
    except Exception:
        events = ()
    duplicate_preventions = sum(1 for event in events if event.event_type == "actuation.duplicate_prevented.v1")

    om4 = compute_om4_audit_coverage(program_id, programs_root=programs_root)
    summary = ReliabilityCockpitSummary(
        outbox_pending=0,
        uncertain_remote_state=0,
        dead_letter_count=0,
        duplicate_preventions=duplicate_preventions,
        audit_coverage=om4.value,
    )
    findings.append(
        CockpitFinding(
            finding_id="reliability.outbox.not_wired",
            area="reliability",
            status="info",
            summary="Actuation outbox is not yet wired to a live mutation domain.",
            detail=(
                "outbox_pending/uncertain_remote_state/dead_letter_count stay at 0 (ADF-W1.3 is not done). "
                f"duplicate_preventions ({duplicate_preventions}) is real: it counts "
                "actuation.duplicate_prevented.v1 ledger events from the ADF-W1.2 search-before-create safeguard."
            ),
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=observed_at,
        )
    )
    findings.append(
        CockpitFinding(
            finding_id="reliability.om4_audit_coverage",
            area="reliability",
            status="ok" if om4.confidence == ValueConfidence.MEASURED and (om4.value or 0) >= 1.0 else "info",
            summary=f"OM-4 (zero unaudited AI outputs consumed): {om4.confidence.value}.",
            detail=om4.detail,
            owner=None,
            next_command=None,
            evidence_refs=om4.evidence_refs,
            observed_at=observed_at,
        )
    )
    canary = canary_window_status(today=observed_at.date())
    findings.append(
        CockpitFinding(
            finding_id="reliability.bl_c6_canary_window",
            area="reliability",
            status="ok" if canary.elapsed else "info",
            summary=(
                f"BL-C6 re-baseline gate: {canary.elapsed_weeks:.1f}/{canary.window_weeks} live-canary weeks elapsed "
                f"(started {canary.start_date.isoformat()})."
            ),
            detail=(
                "The re-baseline gate for arch-fix.md Part B (AF-4..AF-10B) requires an 8-week live-canary "
                "observation window before OM-1/2/4/5 are measured against the DoD and each phase is "
                "re-authorized/re-scoped/cancelled. Reaching the window is necessary, not sufficient -- "
                "the actual OM-1/2/4/5 measurement and re-authorization decision still happens once elapsed=True."
            ),
            owner=None,
            next_command=None if not canary.elapsed else "See specs/bklg.md BL-C6 for the re-authorization steps.",
            evidence_refs=(),
            observed_at=observed_at,
        )
    )
    return summary, findings


def _build_trust_summary(
    program_id: str, *, programs_root: Path, observed_at: datetime
) -> tuple[TrustCockpitSummary, list[CockpitFinding]]:
    """ADF-W5.12 (Section 8.15.4): the trust cockpit, one row per proposal
    class governed by the autonomy ladder. Reads ``earned_autonomy_state.yaml``
    directly (a durable store, matching every other summary's read-only
    projection contract) rather than recomputing evaluation live -- the
    displayed state is exactly what the last ``autonomy-evaluate``/explicit
    promotion call persisted, not a fresh recomputation on every cockpit view."""
    findings: list[CockpitFinding] = []
    try:
        state = load_earned_autonomy_state(program_id, programs_root=programs_root)
    except Exception:
        state = None
    proposal_classes = state.proposal_classes if state else {}

    rows: list[ProposalClassTrustSummary] = []
    for proposal_class in PROPOSAL_CLASSES:
        entry = proposal_classes.get(proposal_class)
        level = entry.level if entry else "l0"
        counters = entry.counters if entry else None
        total_reviewed = (counters.accepted + counters.rejected) if counters else 0
        acceptance_rate = (counters.accepted / total_reviewed) if counters and total_reviewed else None
        reject_rate = (counters.rejected / total_reviewed) if counters and total_reviewed else None
        try:
            ceiling = resolve_ceiling(proposal_class, program_id=program_id, programs_root=programs_root)
        except Exception:
            ceiling = "l2"
        remaining = "no evaluation has run yet -- run `vertex cockpit autonomy-evaluate`" if entry is None else (
            "at governance ceiling" if level == ceiling else "see last_change_reason for the most recent evaluation outcome"
        )
        rows.append(ProposalClassTrustSummary(
            proposal_class=proposal_class,
            level=level,
            permitted_action=_AUTONOMY_LEVEL_PERMITTED_ACTION.get(level, level),
            ceiling=ceiling,
            acceptance_rate=acceptance_rate,
            reject_rate=reject_rate,
            reversal_rate=None,  # no reversal telemetry exists yet (honest, not an oversight)
            review_count=total_reviewed,
            current_sample_rate=entry.sample_rate if entry else 1.0,
            last_change_reason=entry.last_change_reason if entry else "",
            remaining_evidence=remaining,
        ))
        if entry and entry.demoted_at is not None and (entry.promoted_at is None or entry.demoted_at >= entry.promoted_at):
            findings.append(CockpitFinding(
                finding_id=f"trust.{proposal_class}.demoted",
                area="trust",
                status="warn",
                summary=f"{proposal_class} autonomy was demoted to {level}.",
                detail=entry.last_change_reason,
                owner=None,
                next_command=f"vertex cockpit autonomy-evaluate --program {program_id} --class {proposal_class}",
                evidence_refs=(),
                observed_at=observed_at,
            ))

    if not proposal_classes:
        findings.append(CockpitFinding(
            finding_id="trust.autonomy_ladder.not_evaluated",
            area="trust",
            status="info",
            summary="No proposal class has been evaluated for autonomy promotion yet.",
            detail="All classes default to L0 (Section 8.15.1's upcast rule) until `vertex cockpit autonomy-evaluate` runs.",
            owner=None,
            next_command=f"vertex cockpit autonomy-evaluate --program {program_id}",
            evidence_refs=(),
            observed_at=observed_at,
        ))

    return TrustCockpitSummary(classes=tuple(rows)), findings


def _pick_next_action(findings: list[CockpitFinding]) -> str | None:
    """ADF-W1.11: the single highest-priority next action across all
    findings -- blocked beats warn; first-found wins within a tier."""
    for status in _ACTIONABLE_FINDING_STATUSES:
        for finding in findings:
            if finding.status == status and finding.next_command:
                return finding.next_command
    return None


__all__ = ["build_cockpit_snapshot"]
