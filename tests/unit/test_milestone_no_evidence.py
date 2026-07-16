"""ADF-W1.7: past-due, no-evidence milestones must never render ON_TRACK.

Section 8.10.3 / INV-ADF-12: "Past-due milestone without completion evidence
cannot be ON_TRACK." Covers the Issue-079-style regression where a milestone
declared ON_TRACK with zero linked work items silently stayed ON_TRACK past
its target date.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.milestone_engine import assess_milestone_health
from src.core.models_v2 import Milestone, MilestoneStatus


def _milestone(
    *,
    milestone_id: str = "m-no-evidence",
    target_date: date,
    status: MilestoneStatus = MilestoneStatus.ON_TRACK,
    linked_work_item_ids: tuple[int, ...] = (),
) -> Milestone:
    return Milestone(
        id=milestone_id,
        program_id="xpf",
        name="Issue-079-style milestone",
        target_date=target_date,
        owner_alias="maintainer",
        status=status,
        exit_criteria=("Ship it",),
        linked_workstream_ids=("xpf",),
        linked_work_item_ids=linked_work_item_ids,
    )


_AS_OF = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def test_past_due_no_linked_items_and_declared_on_track_is_never_on_track() -> None:
    """The Issue-079 regression fixture: declared ON_TRACK, past target, zero evidence."""
    milestone = _milestone(target_date=date(2026, 6, 1), status=MilestoneStatus.ON_TRACK)

    assessment = assess_milestone_health(milestone, (), {}, _AS_OF)

    assert assessment.computed_health != MilestoneStatus.ON_TRACK
    assert assessment.computed_health == MilestoneStatus.MISSED
    assert assessment.coverage_gap is True


def test_past_due_no_linked_items_but_declared_complete_is_unknown_not_missed() -> None:
    """A declared-complete claim with no linked-item evidence is UNKNOWN, not a confident MISSED."""
    milestone = _milestone(target_date=date(2026, 6, 1), status=MilestoneStatus.COMPLETED)

    assessment = assess_milestone_health(milestone, (), {}, _AS_OF)

    assert assessment.computed_health == MilestoneStatus.UNKNOWN
    assert assessment.coverage_gap is True
    assert assessment.slip_probability == 0.5


def test_deferred_past_due_no_linked_items_stays_deferred_not_flagged() -> None:
    """An explicitly deferred milestone is a known state, not a coverage gap."""
    milestone = _milestone(target_date=date(2026, 6, 1), status=MilestoneStatus.DEFERRED)

    assessment = assess_milestone_health(milestone, (), {}, _AS_OF)

    assert assessment.computed_health == MilestoneStatus.DEFERRED
    assert assessment.coverage_gap is False


def test_future_target_no_linked_items_is_unaffected_by_the_fix() -> None:
    """A future-dated milestone with no linked items is not (yet) a coverage gap."""
    milestone = _milestone(target_date=date(2026, 12, 1), status=MilestoneStatus.ON_TRACK)

    assessment = assess_milestone_health(milestone, (), {}, _AS_OF)

    assert assessment.computed_health == MilestoneStatus.ON_TRACK
    assert assessment.coverage_gap is False


def test_missed_via_real_unresolved_evidence_is_not_a_coverage_gap() -> None:
    """MISSED derived from real (present but unresolved) linked items is a normal MISSED, not a gap."""
    from src.core.models import RiskLevel, WorkItem

    milestone = _milestone(target_date=date(2026, 6, 1), linked_work_item_ids=(1,))
    item = WorkItem(
        id=1,
        type="Feature",
        title="Gate",
        state="Active",
        assigned_to="Vertex Maintainer",
        assigned_to_email="maintainer@example.com",
        area_path="One\\Adventure\\Xpf",
        iteration_path="Sprint 1",
        target_date=date(2026, 5, 30),
        risk_level=RiskLevel.MEDIUM,
        tags=["xpf"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=_AS_OF,
    )

    assessment = assess_milestone_health(milestone, (item,), {1: ()}, _AS_OF)

    assert assessment.computed_health == MilestoneStatus.MISSED
    assert assessment.coverage_gap is False
    assert assessment.slip_probability == 1.0
