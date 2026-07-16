"""ADF-W5.12 (Section 8.15 / Appendix A.8): cross-feature autonomy state,
promotion/demotion, and sampled review for the five AISchemaGateway-pattern
proposal classes built this session (RiskProposal, MeetingAction,
TopThreeCandidateProposal, GovernanceDecisionBriefProposal,
DependencyBlastRadiusProposal).

Extends ``programs/<id>/earned_autonomy_state.yaml``'s ``proposal_classes``
block (Appendix A.8) via ``src/core/maturity_engine.py``'s additive
read/write functions -- that module owns the file's pre-existing
``schema_version``/``earned_tier``/``maturity_score`` fields (FR-SG-39, a
different, older, single-global-tier mechanism); this module only ever
reads and writes individual ``proposal_classes`` entries.

Section 8.15.1's ladder: L0 (deterministic only) -> L1 (advisory) -> L2
(guided batch review) -> L3 (approved-batch/sampled review) -> L4 (standing
policy). Promotion evidence for L0->L1->L2 is computable today from
``proposal_audit.jsonl`` (reviewed population + acceptance rate). L3/L4
evidence ("zero material downstream regressions", "independent review") is
NOT computable from any existing signal -- reversal/material-error tracking
does not exist anywhere in the codebase yet (confirmed absent by
investigation before writing this module, not an oversight). The automatic
evaluator therefore only ever proposes L0/L1/L2; promotion to L3/L4 requires
an explicit human-gated call (``promote_proposal_class_explicit``) that
still cannot exceed the configured governance ceiling.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.adf_config import load_arch_data_fix
from src.core.alerts import append_or_suppress_alert
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.maturity_engine import (
    ProposalClassAutonomyState,
    ProposalClassCounters,
    load_earned_autonomy_state,
    write_proposal_class_state,
)
from src.core.proposal_audit import ProposalType, read_proposal_audit

#: The five proposal classes this ladder governs today -- exactly the
#: ProposalType values proposal_audit.jsonl already records against, reused
#: directly rather than inventing a parallel naming scheme.
PROPOSAL_CLASSES: tuple[ProposalType, ...] = (
    "risk", "meeting_action", "top_three", "governance_decision_brief", "dependency_blast_radius",
)

_LEVEL_ORDER = ["l0", "l1", "l2", "l3", "l4"]

#: Ceiling applied when a proposal_class has no explicit governance
#: `autonomy_ceiling` entry -- conservative, matches the existing default
#: ceiling map's philosophy of never silently reaching standing-policy.
_DEFAULT_CEILING_WHEN_UNCONFIGURED = "l2"

#: Section 8.15.1's L1->L2 promotion evidence floor. Deliberately
#: conservative given reversal/material-error telemetry doesn't exist yet
#: to corroborate a higher bar.
_PROMOTE_MIN_POPULATION = 10
_PROMOTE_MIN_ACCEPTANCE_RATE = 0.90

#: Below this acceptance rate (once there is enough population to trust
#: it), auto-demote one level -- Section 8.15.1's "quality-floor breach
#: demotes the class to the last safe level."
_DEMOTE_ACCEPTANCE_RATE_FLOOR = 0.60

#: Section 8.15.2's "sampling rate ... may not fall below platform floors" --
#: a class at L3+ must still have a human individually review at least this
#: fraction of its batch, however high governance sets its own ceiling.
MIN_SAMPLE_RATE = 0.05


def _level_index(level: str) -> int:
    return _LEVEL_ORDER.index(level)


def compute_proposal_class_counters(
    program_id: str,
    proposal_class: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ProposalClassCounters:
    """Aggregates ``proposal_audit.jsonl`` into Appendix A.8's counters
    shape. ``edited`` stays 0 -- no existing signal distinguishes an
    edited-then-approved proposal from an as-is approval. ``reversals``/
    ``material_errors`` (ADF-W5.12 P4) are both derived from the same
    ``"reversed"`` audit event an operator records via
    ``vertex ai-proposals flag-regression`` -- this codebase has no finer
    distinction yet between "a decision was later reversed" and "a decision
    caused material downstream harm," so a flagged reversal is treated as a
    material error too (an honest, conservative mapping, not an
    oversight)."""
    records = read_proposal_audit(program_id, programs_root=programs_root)
    class_records = [r for r in records if r.proposal_type == proposal_class]
    proposed = sum(1 for r in class_records if r.event == "proposed")
    accepted = sum(1 for r in class_records if r.event == "approved")
    rejected = sum(1 for r in class_records if r.event == "rejected")
    reversed_count = sum(1 for r in class_records if r.event == "reversed")
    return ProposalClassCounters(
        proposals=proposed, accepted=accepted, rejected=rejected,
        reversals=reversed_count, material_errors=reversed_count,
    )


@dataclass(frozen=True, slots=True)
class PromotionEvaluation:
    proposal_class: str
    current_level: str
    proposed_level: str
    action: str  # "promoted" | "demoted" | "unchanged"
    reason: str
    counters: ProposalClassCounters
    ceiling: str


def resolve_ceiling(proposal_class: str, *, program_id: str, programs_root: Path = PROGRAMS_ROOT) -> str:
    config = load_arch_data_fix(program_id, programs_root=programs_root)
    return config.governance.autonomy_ceiling.get(proposal_class, _DEFAULT_CEILING_WHEN_UNCONFIGURED)


def evaluate_promotion(
    proposal_class: str,
    *,
    current_level: str,
    counters: ProposalClassCounters,
    ceiling: str,
) -> PromotionEvaluation:
    """Pure evidence evaluator -- Section 8.15.1's automatic L0->L1->L2 half
    only (see module docstring for why L3/L4 stay human-gated)."""
    total_reviewed = counters.accepted + counters.rejected
    acceptance_rate = (counters.accepted / total_reviewed) if total_reviewed else None
    ceiling_idx = _level_index(ceiling)
    current_idx = _level_index(current_level)

    def _unchanged(reason: str) -> PromotionEvaluation:
        return PromotionEvaluation(
            proposal_class=proposal_class, current_level=current_level, proposed_level=current_level,
            action="unchanged", reason=reason, counters=counters, ceiling=ceiling,
        )

    # Material-regression demotion always wins, and outranks the acceptance-
    # rate check below -- Section 8.15.1's L3/L4 promotion floor requires
    # "zero material downstream regressions"; a single flagged regression at
    # L3+ means that floor is no longer met, regardless of acceptance rate.
    if current_idx >= _level_index("l3") and counters.material_errors > 0:
        new_level = _LEVEL_ORDER[current_idx - 1]
        return PromotionEvaluation(
            proposal_class=proposal_class, current_level=current_level, proposed_level=new_level,
            action="demoted",
            reason=(
                f"{counters.material_errors} material regression(s) flagged -- violates L3/L4's "
                "'zero material downstream regressions' promotion floor (Section 8.15.1)"
            ),
            counters=counters, ceiling=ceiling,
        )

    # Demotion always wins over promotion: a quality-floor breach, once
    # there's enough population to trust it, demotes one level immediately.
    if (
        current_idx > 0
        and total_reviewed >= _PROMOTE_MIN_POPULATION
        and acceptance_rate is not None
        and acceptance_rate < _DEMOTE_ACCEPTANCE_RATE_FLOOR
    ):
        new_level = _LEVEL_ORDER[current_idx - 1]
        return PromotionEvaluation(
            proposal_class=proposal_class, current_level=current_level, proposed_level=new_level,
            action="demoted",
            reason=(
                f"acceptance_rate={acceptance_rate:.2f} below demotion floor "
                f"{_DEMOTE_ACCEPTANCE_RATE_FLOOR:.2f} over {total_reviewed} reviewed proposals"
            ),
            counters=counters, ceiling=ceiling,
        )

    if current_idx >= ceiling_idx:
        return _unchanged("at governance-configured ceiling")

    if current_idx >= 2:
        return _unchanged("L3/L4 require explicit human-gated promotion (Section 8.15.1's independent-review/outbox-proven evidence is not automatically computable)")

    if current_level == "l0":
        # L1 is advisory-only ("reviews or ignores") -- Section 8.15.1's own
        # minimum bar requires no evidence floor to reach it.
        return PromotionEvaluation(
            proposal_class=proposal_class, current_level=current_level, proposed_level="l1",
            action="promoted",
            reason="advisory proposal capability available (L1 requires no evidence floor per Section 8.15.1)",
            counters=counters, ceiling=ceiling,
        )

    if current_level == "l1":
        if total_reviewed >= _PROMOTE_MIN_POPULATION and acceptance_rate is not None and acceptance_rate >= _PROMOTE_MIN_ACCEPTANCE_RATE:
            return PromotionEvaluation(
                proposal_class=proposal_class, current_level=current_level, proposed_level="l2",
                action="promoted",
                reason=f"acceptance_rate={acceptance_rate:.2f} >= {_PROMOTE_MIN_ACCEPTANCE_RATE:.2f} over {total_reviewed} reviewed proposals",
                counters=counters, ceiling=ceiling,
            )
        if total_reviewed < _PROMOTE_MIN_POPULATION:
            return _unchanged(f"needs {_PROMOTE_MIN_POPULATION - total_reviewed} more reviewed proposals to reach L2's minimum population")
        return _unchanged(f"acceptance_rate={acceptance_rate:.2f} below {_PROMOTE_MIN_ACCEPTANCE_RATE:.2f} floor for L2")

    return _unchanged("no automatic evaluation rule for this level")


def advance_proposal_class_autonomy(
    program_id: str,
    proposal_class: str,
    *,
    now: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> PromotionEvaluation:
    """Runs the automatic evidence-based evaluator for one proposal class,
    persists the result, and (on promotion/demotion) emits an entity-scoped
    cockpit alert -- reusing ADF-W5.8's alert mechanism rather than a new
    one."""
    resolved_now = now or datetime.now(timezone.utc)
    state = load_earned_autonomy_state(program_id, programs_root=programs_root)
    existing = state.proposal_classes.get(proposal_class) if state else None
    current_level = existing.level if existing else "l0"
    counters = compute_proposal_class_counters(program_id, proposal_class, programs_root=programs_root)
    ceiling = resolve_ceiling(proposal_class, program_id=program_id, programs_root=programs_root)
    evaluation = evaluate_promotion(proposal_class, current_level=current_level, counters=counters, ceiling=ceiling)

    changed = evaluation.action != "unchanged"
    new_entry = ProposalClassAutonomyState(
        level=evaluation.proposed_level,
        promoted_at=resolved_now if evaluation.action == "promoted" else (existing.promoted_at if existing else None),
        demoted_at=resolved_now if evaluation.action == "demoted" else (existing.demoted_at if existing else None),
        last_change_reason=evaluation.reason if changed else (existing.last_change_reason if existing else ""),
        evidence_window_start=resolved_now if changed else (existing.evidence_window_start if existing else resolved_now),
        counters=counters,
        sample_rate=existing.sample_rate if existing else 1.0,
    )
    write_proposal_class_state(program_id, proposal_class, new_entry, programs_root=programs_root)

    if changed:
        try:
            append_or_suppress_alert(
                program_id=program_id,
                category="autonomy_ladder_change",
                entity_type="proposal_class",
                entity_id=proposal_class,
                severity="info" if evaluation.action == "promoted" else "warn",
                message=f"{proposal_class}: {evaluation.action} {evaluation.current_level}->{evaluation.proposed_level} ({evaluation.reason})",
                next_command=f"vertex cockpit autonomy-evaluate --program {program_id} --class {proposal_class}",
                programs_root=programs_root,
                now=resolved_now,
            )
        except Exception:
            pass
    return evaluation


def promote_proposal_class_explicit(
    program_id: str,
    proposal_class: str,
    to_level: str,
    reason: str,
    *,
    now: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    sample_rate: float | None = None,
) -> ProposalClassAutonomyState:
    """The human-gated path for L3/L4 (or any explicit override) --
    Section 8.15.1's independent-review/outbox-proven evidence for L3/L4 is
    an attestation this function trusts the caller to have verified, not
    something computed here. Still hard-capped at the governance ceiling --
    an explicit call can never exceed policy.

    ``sample_rate`` (ADF-W5.12 P4, Section 8.15.2) is the fraction of a
    batch a human must still individually review at L3/L4's "sampled
    review" authority -- e.g. 0.2 means 20% reviewed, 80% auto-approved by
    extension of that sample's trust. Only meaningful at L3+; ignored (kept
    at the existing/default 1.0 -- full review) below L3, since sampling
    below L3 isn't an authorized review mode (Section 8.15.1's ladder).
    Must not fall below ``MIN_SAMPLE_RATE`` -- Section 8.15.2's own
    "may not fall below platform floors" rule."""
    if to_level not in _LEVEL_ORDER:
        raise ValueError(f"Unknown autonomy level {to_level!r}; must be one of {_LEVEL_ORDER}.")
    ceiling = resolve_ceiling(proposal_class, program_id=program_id, programs_root=programs_root)
    if _level_index(to_level) > _level_index(ceiling):
        raise ValueError(f"Cannot promote {proposal_class!r} to {to_level!r}: exceeds governance ceiling {ceiling!r}.")
    if sample_rate is not None:
        if _level_index(to_level) < _level_index("l3"):
            raise ValueError(f"--sample-rate only applies at L3/L4 (target level is {to_level!r}).")
        if not (MIN_SAMPLE_RATE <= sample_rate <= 1.0):
            raise ValueError(f"sample_rate must be between {MIN_SAMPLE_RATE} and 1.0 (Section 8.15.2's platform floor); got {sample_rate!r}.")
    resolved_now = now or datetime.now(timezone.utc)
    state = load_earned_autonomy_state(program_id, programs_root=programs_root)
    existing = state.proposal_classes.get(proposal_class) if state else None
    counters = compute_proposal_class_counters(program_id, proposal_class, programs_root=programs_root)
    resolved_sample_rate = sample_rate if sample_rate is not None else (existing.sample_rate if existing else 1.0)
    entry = ProposalClassAutonomyState(
        level=to_level,
        promoted_at=resolved_now,
        demoted_at=existing.demoted_at if existing else None,
        last_change_reason=reason,
        evidence_window_start=resolved_now,
        counters=counters,
        sample_rate=resolved_sample_rate,
    )
    write_proposal_class_state(program_id, proposal_class, entry, programs_root=programs_root)
    try:
        append_or_suppress_alert(
            program_id=program_id,
            category="autonomy_ladder_change",
            entity_type="proposal_class",
            entity_id=proposal_class,
            severity="info",
            message=f"{proposal_class}: explicit promotion to {to_level} ({reason})",
            next_command=f"vertex cockpit autonomy-evaluate --program {program_id} --class {proposal_class}",
            programs_root=programs_root,
            now=resolved_now,
        )
    except Exception:
        pass
    return entry


def demote_proposal_class_explicit(
    program_id: str,
    proposal_class: str,
    reason: str,
    *,
    now: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ProposalClassAutonomyState:
    """Manual demotion to one level below current (never below l0) --
    Section 8.15.1's "material contradiction, duplicate effect, policy
    violation, or quality-floor breach demotes the class" for cases an
    operator observes but the automatic evaluator cannot detect (no
    reversal/material-error telemetry exists yet)."""
    resolved_now = now or datetime.now(timezone.utc)
    state = load_earned_autonomy_state(program_id, programs_root=programs_root)
    existing = state.proposal_classes.get(proposal_class) if state else None
    current_level = existing.level if existing else "l0"
    new_level = _LEVEL_ORDER[max(0, _level_index(current_level) - 1)]
    counters = compute_proposal_class_counters(program_id, proposal_class, programs_root=programs_root)
    entry = ProposalClassAutonomyState(
        level=new_level,
        promoted_at=existing.promoted_at if existing else None,
        demoted_at=resolved_now,
        last_change_reason=reason,
        evidence_window_start=resolved_now,
        counters=counters,
        sample_rate=existing.sample_rate if existing else 1.0,
    )
    write_proposal_class_state(program_id, proposal_class, entry, programs_root=programs_root)
    try:
        append_or_suppress_alert(
            program_id=program_id,
            category="autonomy_ladder_change",
            entity_type="proposal_class",
            entity_id=proposal_class,
            severity="warn",
            message=f"{proposal_class}: manual demotion {current_level}->{new_level} ({reason})",
            next_command=f"vertex cockpit autonomy-evaluate --program {program_id} --class {proposal_class}",
            programs_root=programs_root,
            now=resolved_now,
        )
    except Exception:
        pass
    return entry


__all__ = [
    "MIN_SAMPLE_RATE",
    "PROPOSAL_CLASSES",
    "PromotionEvaluation",
    "advance_proposal_class_autonomy",
    "compute_proposal_class_counters",
    "demote_proposal_class_explicit",
    "evaluate_promotion",
    "promote_proposal_class_explicit",
    "resolve_ceiling",
]
