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
from src.core.exceptions import ConfigError
from src.core.ledger.event_log import EventEnvelope, read_events
from src.core.ledger.event_types import validate_event_payload
from src.core.quality_gates.ai_release_audit import ApplicationReceipt, ReleaseTerminal

#: ai.application_receipt.v1 receipt values that mean the AI output was
#: genuinely consumed (as opposed to "not_applied"/"failed", where nothing
#: reached a published artifact or written fact).
_CONSUMED_RECEIPTS = frozenset({
    ApplicationReceipt.RENDERED.value, ApplicationReceipt.PROPOSED.value, ApplicationReceipt.APPLIED.value,
})
_NON_CONSUMED_RECEIPTS = frozenset({ApplicationReceipt.NOT_APPLIED.value, ApplicationReceipt.FAILED.value})
_KNOWN_RECEIPTS = _CONSUMED_RECEIPTS | _NON_CONSUMED_RECEIPTS


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


def _metric_events(
    program_id: str, *, programs_root: Path, events: tuple[EventEnvelope, ...] | None = None,
) -> tuple[EventEnvelope, ...]:
    if events is None:
        events = read_events(program_id, programs_root=programs_root, repair_corruption=False)
    if any(event.program_id != program_id for event in events):
        raise ConfigError("Event snapshot does not match the requested program.")
    return events


def _unavailable_ledger_metric(metric_id: str, unit: str) -> OutcomeMetricResult:
    return OutcomeMetricResult(
        metric_id=metric_id,
        value=None,
        unit=unit,
        confidence=ValueConfidence.UNAVAILABLE,
        detail="Could not read this program's event ledger.",
        evidence_refs=(),
    )


def compute_om2_duplicate_entities(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT, events: tuple[EventEnvelope, ...] | None = None,
) -> OutcomeMetricResult:
    """OM-2: zero duplicate ADO entities created.

    Unavailable: the create-task writer uses the actuation outbox, but
    outbox statuses and prevention events do not measure duplicate ADO
    entities. The related-but-distinct duplicate_preventions counter counts
    actuation.duplicate_prevented.v1 events from the search-before-create
    safeguard and is surfaced separately in ReliabilityCockpitSummary; it
    measures prevention attempts, not this outcome.
    """
    try:
        events = _metric_events(program_id, programs_root=programs_root, events=events)
    except (OSError, ConfigError, UnicodeError):
        return _unavailable_ledger_metric("om2_duplicate_entities", "count")
    duplicate_preventions = sum(1 for event in events if event.event_type == "actuation.duplicate_prevented.v1")
    return OutcomeMetricResult(
        metric_id="om2_duplicate_entities",
        value=None,
        unit="count",
        confidence=ValueConfidence.UNAVAILABLE,
        detail=(
            "Duplicate outcomes are not measured; outbox status and prevention events do not establish "
            f"whether duplicate ADO entities were created ({duplicate_preventions} search-before-create "
            "prevention(s) recorded, a related but distinct signal). See governance/outcome-metrics.md."
        ),
        evidence_refs=(),
    )


def compute_om4_audit_coverage(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT, events: tuple[EventEnvelope, ...] | None = None,
) -> OutcomeMetricResult:
    """OM-4: zero unaudited AI outputs consumed.

    For recognized, identifiable application receipts, measure the fraction
    of recorded consumed runs with a latest 'released' terminal. Invalid
    receipts or matching schema-invalid release decisions make this result
    unavailable. This does not establish complete receipt coverage or
    authorization at the time of consumption. Reuse a supplied event tuple,
    or read without repairing the ledger.
    """
    try:
        events = _metric_events(program_id, programs_root=programs_root, events=events)
    except (OSError, ConfigError, UnicodeError):
        return _unavailable_ledger_metric("om4_audit_coverage", "ratio")

    consumed_run_ids: set[str] = set()
    for event in events:
        if event.event_type != "ai.application_receipt.v1":
            continue
        receipt = event.payload.get("receipt")
        ai_run_id = event.payload.get("ai_run_id")
        if (
            not isinstance(receipt, str)
            or receipt not in _KNOWN_RECEIPTS
            or not isinstance(ai_run_id, str)
            or not ai_run_id
        ):
            return OutcomeMetricResult(
                metric_id="om4_audit_coverage",
                value=None,
                unit="ratio",
                confidence=ValueConfidence.UNAVAILABLE,
                detail="Audit coverage is unavailable because an application receipt is invalid or unrecognized.",
                evidence_refs=(),
            )
        if receipt in _CONSUMED_RECEIPTS:
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

    latest_terminals: dict[str, ReleaseTerminal] = {}
    for event in events:
        if event.event_type != "ai.release_decision.v1":
            continue
        ai_run_id = event.payload.get("ai_run_id")
        if isinstance(ai_run_id, str) and ai_run_id in consumed_run_ids:
            try:
                validate_event_payload(event.event_type, event.payload)
            except ValueError:
                return OutcomeMetricResult(
                    metric_id="om4_audit_coverage",
                    value=None,
                    unit="ratio",
                    confidence=ValueConfidence.UNAVAILABLE,
                    detail="Audit coverage is unavailable because a matching release decision payload is invalid.",
                    evidence_refs=(),
                )
            latest_terminals[ai_run_id] = ReleaseTerminal(event.payload["terminal"])
    released_count = sum(latest_terminals.get(run_id) is ReleaseTerminal.RELEASED for run_id in consumed_run_ids)
    coverage = released_count / len(consumed_run_ids)
    unaudited = len(consumed_run_ids) - released_count
    reported_coverage = round(coverage, 4)
    if 0.0 < coverage < 1.0 and reported_coverage in (0.0, 1.0):
        reported_coverage = coverage
    detail = (
        f"All {len(consumed_run_ids)} AI output(s) recorded as consumed have a latest 'released' terminal."
        if unaudited == 0
        else (
            f"{released_count}/{len(consumed_run_ids)} AI outputs recorded as consumed have a latest 'released' "
            f"terminal ({unaudited} unaudited by this measure)."
        )
    )
    return OutcomeMetricResult(
        metric_id="om4_audit_coverage",
        value=reported_coverage,
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
    try:
        events = _metric_events(program_id, programs_root=programs_root)
    except (OSError, ConfigError, UnicodeError):
        om2 = _unavailable_ledger_metric("om2_duplicate_entities", "count")
        om4 = _unavailable_ledger_metric("om4_audit_coverage", "ratio")
    else:
        om2 = compute_om2_duplicate_entities(program_id, programs_root=programs_root, events=events)
        om4 = compute_om4_audit_coverage(program_id, programs_root=programs_root, events=events)
    return (
        compute_om1_hallucination_rate(program_id, programs_root=programs_root),
        om2,
        om4,
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
