"""specs/backlog.md BL-C7: OM-1/2/4/5 outcome-metric measurement.

Lifts the OM-1/2/4/5 definitions out of the archived, untracked
`.archive/specs/arch-fix.md` §11/§0.2 into a durably tracked (see
`governance/outcome-metrics.md`) and live-queryable form, so BL-C6's canary
window has something instrumented to collect during it.

Every function returns an ``OutcomeMetricResult`` carrying an honest
``ValueConfidence`` tier — a metric with no real data source yet is
``UNAVAILABLE``, never silently promoted to ``MEASURED`` (INV-ADF-11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.cockpit_models import ValueConfidence
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.ledger.event_log import read_events
from src.core.quality_gates.ai_release_audit import ReleaseTerminal, released_terminal_for_run

#: ai.application_receipt.v1 receipt values that mean the AI output was
#: genuinely consumed (as opposed to "not_applied"/"failed", where nothing
#: reached a published artifact or written fact).
_CONSUMED_RECEIPTS = frozenset({"rendered", "proposed", "applied"})


@dataclass(frozen=True, slots=True)
class OutcomeMetricResult:
    metric_id: str
    value: float | int | None
    unit: str
    confidence: ValueConfidence
    detail: str
    evidence_refs: tuple[str, ...]


def compute_om1_hallucination_rate(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> OutcomeMetricResult:
    """OM-1: zero hallucinated/ungrounded facts reach a published newsletter.

    Unavailable: BL-C2's AI safety boundary (semantic validation /
    hallucination detection) does not yet cover the REV extractor/judge
    path -- the highest-consequence path, and the one BL-C2's own
    reopening found bypasses the gateway entirely. There is no durable
    grounding/hallucination-check record to query yet. See
    governance/outcome-metrics.md's "What OM-1 needs" for the concrete
    unblock.
    """
    return OutcomeMetricResult(
        metric_id="om1_hallucination_rate",
        value=None,
        unit="ratio",
        confidence=ValueConfidence.UNAVAILABLE,
        detail=(
            "Unmeasurable on the path that matters: BL-C2's semantic validation does not yet cover the "
            "REV extractor/judge call sites, so no durable grounding/hallucination-check record exists to "
            "query. See governance/outcome-metrics.md."
        ),
        evidence_refs=(),
    )


def compute_om2_duplicate_entities(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> OutcomeMetricResult:
    """OM-2: zero duplicate ADO entities created.

    Unavailable: src/core/actuation_outbox.py is not yet wired to a live
    ADO mutation domain (cockpit_builder.py's own _build_reliability_summary
    docstring), so there is no real create-task traffic to compute a
    duplicate-detection ratio from. The related-but-distinct
    duplicate_preventions counter (real; counts
    actuation.duplicate_prevented.v1 events from the search-before-create
    safeguard) is surfaced separately in ReliabilityCockpitSummary -- it
    measures prevention attempts, not this outcome.
    """
    try:
        events = read_events(program_id, programs_root=programs_root)
    except Exception:
        events = ()
    duplicate_preventions = sum(1 for event in events if event.event_type == "actuation.duplicate_prevented.v1")
    return OutcomeMetricResult(
        metric_id="om2_duplicate_entities",
        value=None,
        unit="count",
        confidence=ValueConfidence.UNAVAILABLE,
        detail=(
            "No live ADO mutation domain is wired through the actuation outbox yet, so there is no real "
            f"create-task traffic to compute a duplicate-entity count from ({duplicate_preventions} "
            "search-before-create prevention(s) recorded, a related but distinct signal). See "
            "governance/outcome-metrics.md."
        ),
        evidence_refs=(),
    )


def compute_om4_audit_coverage(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> OutcomeMetricResult:
    """OM-4: zero unaudited AI outputs consumed.

    Measured: ai.application_receipt.v1 / ai.release_decision.v1 are real,
    SOURCE_AUTHORITATIVE ledger events BL-C3 writes on every AI consumption
    it covers. audit_coverage = (consumed AI runs with a durable 'released'
    terminal) / (all consumed AI runs). 1.0 == fully compliant.
    """
    try:
        events = read_events(program_id, programs_root=programs_root)
    except Exception:
        return OutcomeMetricResult(
            metric_id="om4_audit_coverage",
            value=None,
            unit="ratio",
            confidence=ValueConfidence.UNAVAILABLE,
            detail="Could not read this program's event ledger.",
            evidence_refs=(),
        )

    consumed_run_ids: set[str] = set()
    for event in events:
        if event.event_type != "ai.application_receipt.v1":
            continue
        receipt = event.payload.get("receipt")
        ai_run_id = event.payload.get("ai_run_id")
        if receipt in _CONSUMED_RECEIPTS and isinstance(ai_run_id, str) and ai_run_id:
            consumed_run_ids.add(ai_run_id)

    if not consumed_run_ids:
        return OutcomeMetricResult(
            metric_id="om4_audit_coverage",
            value=None,
            unit="ratio",
            confidence=ValueConfidence.UNAVAILABLE,
            detail="No AI outputs have been consumed (rendered/proposed/applied) yet for this program.",
            evidence_refs=(),
        )

    released_count = sum(
        1
        for ai_run_id in consumed_run_ids
        if released_terminal_for_run(ai_run_id, program_id=program_id, programs_root=programs_root)
        is ReleaseTerminal.RELEASED
    )
    coverage = released_count / len(consumed_run_ids)
    unaudited = len(consumed_run_ids) - released_count
    detail = (
        f"All {len(consumed_run_ids)} consumed AI output(s) have a durable 'released' authorization."
        if unaudited == 0
        else (
            f"{released_count}/{len(consumed_run_ids)} consumed AI outputs have a durable 'released' "
            f"authorization ({unaudited} unaudited)."
        )
    )
    return OutcomeMetricResult(
        metric_id="om4_audit_coverage",
        value=round(coverage, 4),
        unit="ratio",
        confidence=ValueConfidence.MEASURED,
        detail=detail,
        evidence_refs=tuple(sorted(consumed_run_ids))[:20],
    )


def compute_om5_operator_friction(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> OutcomeMetricResult:
    """OM-5: operator completes a weekly cycle with zero forced overrides
    in the steady state.

    Unavailable: no measurement protocol is defined yet -- "operator
    friction" needs an explicit answer to *what is timed, for whom, against
    what baseline* before anything can be counted (a product/DRI decision,
    not inferable from existing telemetry). See governance/outcome-metrics.md's
    "What OM-5 needs."
    """
    return OutcomeMetricResult(
        metric_id="om5_operator_friction",
        value=None,
        unit="count",
        confidence=ValueConfidence.UNAVAILABLE,
        detail=(
            "No measurement protocol defined yet: what is timed, for whom, and against what baseline "
            "are unanswered product/DRI decisions. See governance/outcome-metrics.md."
        ),
        evidence_refs=(),
    )


def compute_all_outcome_metrics(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> tuple[OutcomeMetricResult, ...]:
    """OM-1, OM-2, OM-4, OM-5 in that order. OM-3 (frontier $/cycle cost) is
    explicitly out of BL-C7's scope -- see governance/outcome-metrics.md."""
    return (
        compute_om1_hallucination_rate(program_id, programs_root=programs_root),
        compute_om2_duplicate_entities(program_id, programs_root=programs_root),
        compute_om4_audit_coverage(program_id, programs_root=programs_root),
        compute_om5_operator_friction(program_id, programs_root=programs_root),
    )


#: specs/backlog.md BL-C6, decided with the operator 2026-07-26: the
#: mandatory re-baseline gate's live-canary observation window starts today
#: rather than being backdated to 2026-07-22 (when its preconditions,
#: BL-C2/C3/C4, actually shipped) -- backdating would credit interim usage
#: nobody deliberately observed as "the canary," so the clock starts clean.
CANARY_WINDOW_START = date(2026, 7, 26)

#: arch-fix.md's re-baseline gate (A.RE) and its arch-data-fix.md successor
#: (ADF-W6.4) both specify an 8-week live-canary window.
CANARY_WINDOW_WEEKS = 8


@dataclass(frozen=True, slots=True)
class CanaryWindowStatus:
    start_date: date
    window_weeks: int
    elapsed_weeks: float
    elapsed: bool


def canary_window_status(*, today: date | None = None) -> CanaryWindowStatus:
    """BL-C6: how far into the re-baseline gate's live-canary window we are.

    ``elapsed`` becoming True is necessary, not sufficient, for the gate --
    the row's own Action still requires actually measuring OM-1/2/4/5
    against arch-fix.md's DoD once the window closes, not just waiting out
    the calendar."""
    as_of = today if today is not None else datetime.now(timezone.utc).date()
    elapsed_days = (as_of - CANARY_WINDOW_START).days
    elapsed_weeks = max(0.0, elapsed_days / 7)
    return CanaryWindowStatus(
        start_date=CANARY_WINDOW_START,
        window_weeks=CANARY_WINDOW_WEEKS,
        elapsed_weeks=elapsed_weeks,
        elapsed=elapsed_weeks >= CANARY_WINDOW_WEEKS,
    )
