"""Nudge section resolution engine — Phase 2 of specs/nudge-gaps.md.

Resolves authored NudgeSectionSpec objects into ResolvedNudgeSection values
by evaluating deadline_milestone_id against ProgramReality and computing
action_due_at from the ActionDuePolicy.

Public API:
    resolve_sections(config, reality, *, program_id, now_utc) -> tuple[ResolvedNudgeSection, ...]
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.core.context_gap_store import append_context_gap
from src.core.nudge_models import (
    ActionDuePolicy,
    AssessedDeadline,
    ExplicitActionDue,
    MilestoneRelativeActionDue,
    NudgeConfig,
    NudgeSectionSpec,
    ResolvedNudgeSection,
    SendDateOffsetActionDue,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_sections(
    config: NudgeConfig,
    reality: Any,  # ProgramReality — avoid circular import; duck-typed
    *,
    program_id: str,
    now_utc: datetime | None = None,
    planned_send_at: date | None = None,
) -> tuple[ResolvedNudgeSection, ...]:
    """Resolve all NudgeSectionSpec objects into ResolvedNudgeSection.

    Args:
        config: Parsed NudgeConfig (authored YAML).
        reality: ProgramReality instance (may be None → unavailable).
        program_id: Program ID (for context-gap logging).
        now_utc: Override for current UTC time (tests).
        planned_send_at: Anchor for SendDateOffset action-due (default: today).
    """
    _now = now_utc or datetime.now(timezone.utc)
    _anchor = planned_send_at or _now.date()

    # Build milestone lookup if reality is available
    milestone_by_id: dict[str, Any] = {}
    if reality is not None:
        try:
            for fa in reality.milestones():  # method, not property
                rec = getattr(fa, "record", None)
                if rec is not None:
                    mid = str(getattr(rec, "id", "") or "").strip()
                    if mid:
                        milestone_by_id[mid] = fa
        except Exception:
            pass  # reality unavailable — sections will render "unavailable"

    out: list[ResolvedNudgeSection] = []
    for s in config.sections:
        target_date_ceiling = _resolve_deadline(s, milestone_by_id, program_id)
        action_due_at = _action_due(s, config, milestone_by_id, _now, _anchor)
        is_retired = _is_section_retired(s, milestone_by_id)
        out.append(ResolvedNudgeSection(
            spec=s,
            action_due_at=action_due_at,
            target_date_ceiling=target_date_ceiling,
            is_retired=is_retired,
        ))
    return tuple(out)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_section_retired(
    s: NudgeSectionSpec,
    milestone_by_id: dict[str, Any],
) -> bool:
    """Return True when the section's retire_when_milestone_done milestone is done."""
    if not s.retire_when_milestone_done:
        return False
    fa = milestone_by_id.get(s.retire_when_milestone_done)
    if fa is None:
        return False
    rec = getattr(fa, "record", None)
    if rec is None:
        return False
    # Check milestone status — treat "done", "completed", "closed" as retirement triggers
    status_raw = str(getattr(rec, "status", "") or "").strip().lower()
    return status_raw in ("done", "completed", "closed", "complete")


def _resolve_deadline(
    s: NudgeSectionSpec,
    milestone_by_id: dict[str, Any],
    program_id: str,
) -> AssessedDeadline:
    """Build an AssessedDeadline for one section."""
    if s.deadline is not None:
        # Operator-authored explicit deadline
        return AssessedDeadline(
            date=s.deadline,
            milestone_id=None,
            truth_level=None,
            disputed=False,
            stale=False,
            provisional_inputs=False,
            evidence=(),
            authority="operator_override",
            resolution_status="explicit",
        )

    if s.deadline_milestone_id:
        fa = milestone_by_id.get(s.deadline_milestone_id)
        if fa is None:
            append_context_gap(
                feature="nudge",
                program=program_id,
                lane=s.id,
                field="deadline_milestone_id",
                severity="quality_degraded",
                message=f"milestone {s.deadline_milestone_id!r} not found in ProgramReality",
                impact_estimate="medium",
            )
            if not milestone_by_id:
                # ProgramReality was unavailable entirely
                status = "unavailable"
            else:
                status = "unconfirmed"
            return AssessedDeadline(
                date=None,
                milestone_id=s.deadline_milestone_id,
                truth_level=None,
                disputed=False,
                stale=False,
                provisional_inputs=False,
                evidence=(),
                authority="none",
                resolution_status=status,  # type: ignore[arg-type]
            )

        rec = getattr(fa, "record", None)
        target_date: date | None = getattr(rec, "target_date", None) if rec else None
        disputed = bool(getattr(fa, "disputed", False))
        stale = bool(getattr(fa, "stale", False))
        provisional = bool(getattr(fa, "provisional_inputs", False))
        truth_level = str(getattr(fa, "truth_level", "") or "")
        evidence = tuple(str(e) for e in (getattr(fa, "evidence", None) or ()))
        bad = disputed or provisional or stale
        return AssessedDeadline(
            date=target_date,
            milestone_id=s.deadline_milestone_id,
            truth_level=truth_level or None,
            disputed=disputed,
            stale=stale,
            provisional_inputs=provisional,
            evidence=evidence,
            authority="assessed",
            resolution_status="unconfirmed" if bad else "resolved",
        )

    # No deadline specified
    return AssessedDeadline(
        date=None,
        milestone_id=None,
        truth_level=None,
        disputed=False,
        stale=False,
        provisional_inputs=False,
        evidence=(),
        authority="none",
        resolution_status="none",
    )


def _action_due(
    s: NudgeSectionSpec,
    config: NudgeConfig,
    milestone_by_id: dict[str, Any],
    now_utc: datetime,
    anchor: date,
) -> date | None:
    """Compute action_due_at for a section from the ActionDuePolicy (§6.5 / D-2)."""
    policy: ActionDuePolicy | None = config.evaluation.action_due_policy
    if policy is None:
        return None

    if isinstance(policy, ExplicitActionDue):
        return policy.date

    if isinstance(policy, SendDateOffsetActionDue):
        return _subtract_business_days(anchor, policy.business_days)

    if isinstance(policy, MilestoneRelativeActionDue):
        # Phase 3 — milestone_relative; reality available now
        fa = milestone_by_id.get(policy.milestone_id)
        if fa is None:
            return None
        rec = getattr(fa, "record", None)
        target: date | None = getattr(rec, "target_date", None) if rec else None
        if target is None:
            return None
        return _subtract_business_days(target, policy.business_days_before)

    return None


def _subtract_business_days(anchor: date, n: int) -> date:
    """Return anchor minus n business days (Mon-Fri, no holiday calendar)."""
    if n <= 0:
        return anchor
    current = anchor
    remaining = n
    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() < 5:  # Mon=0 … Fri=4
            remaining -= 1
    return current


def build_subject_prefix(
    resolved_sections: tuple[ResolvedNudgeSection, ...],
    config: NudgeConfig,
    *,
    now_date: date | None = None,
) -> str:
    """Build the [Action DUE …] or [OVERDUE …] subject prefix (§6.5).

    Uses action_due_at from required, non-empty sections.
    Overdue = oldest overdue (most urgent). Future = nearest upcoming.
    Returns empty string if prefix is not configured.
    """
    if not config.presentation.context_subject_prefix:
        return ""

    _today = now_date or date.today()
    tpl_future = config.presentation.context_subject_prefix_template
    tpl_overdue = config.presentation.context_subject_overdue_template
    lookahead = config.presentation.context_subject_lookahead_days

    # Collect action_due_at values from non-empty, non-retired required sections
    due_dates: list[date] = []
    for rs in resolved_sections:
        if rs.is_retired:
            continue
        if rs.spec.required and rs.action_due_at is not None:
            due_dates.append(rs.action_due_at)

    if not due_dates:
        return ""

    overdue = [d for d in due_dates if d < _today]
    upcoming = [d for d in due_dates if _today <= d <= _today + timedelta(days=lookahead)]

    # Only emit the overdue prefix if a non-empty template is configured.
    # An empty template (the default) intentionally skips the overdue case and falls
    # through to the upcoming check — "OVERDUE" signals are demotivating and don't
    # drive action; the nudge content itself is the signal.
    if overdue and tpl_overdue:
        oldest = min(overdue)  # most urgent
        due_str = f"{oldest.month}/{oldest.day}"
        return tpl_overdue.format(due=due_str)

    if upcoming:
        nearest = min(upcoming)
        due_str = f"{nearest.month}/{nearest.day}"
        return tpl_future.format(due=due_str)

    return ""
