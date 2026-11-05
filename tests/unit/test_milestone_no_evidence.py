"""Shared assessment never treats missing milestone evidence as ON_TRACK.

Section 8.10.3 / INV-ADF-12: "Past-due milestone without completion evidence
cannot be ON_TRACK." Covers the Issue-079-style regression where a milestone
declared ON_TRACK with zero linked work items silently stayed ON_TRACK past
its target date, and RELO's no-evidence UNKNOWN rule for future targets.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

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
        program_id="samplealpha",
        name="Issue-079-style milestone",
        target_date=target_date,
        owner_alias="maintainer",
        status=status,
        exit_criteria=("Ship it",),
        linked_workstream_ids=("samplealpha",),
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


@pytest.mark.parametrize("target_date", [date(2026, 7, 12), date(2026, 12, 1)])
@pytest.mark.parametrize("status", [
    MilestoneStatus.ON_TRACK, MilestoneStatus.COMPLETED, MilestoneStatus.AT_RISK,
])
@pytest.mark.parametrize("linked_ids", [(), (901,)])
def test_unobserved_current_or_future_milestone_is_unknown(
    target_date: date, status: MilestoneStatus, linked_ids: tuple[int, ...],
) -> None:
    from src.core.models import Confidence

    milestone = _milestone(target_date=target_date, status=status, linked_work_item_ids=linked_ids)

    assessment = assess_milestone_health(milestone, (), {}, _AS_OF)

    assert assessment.computed_health == MilestoneStatus.UNKNOWN
    assert assessment.coverage_gap is True
    assert assessment.confidence == Confidence.NONE
    assert assessment.completion_date is None
    assert assessment.slip_probability == 0.5
    assert "observed" in assessment.reasoning.lower()
    assert "declared complete" not in assessment.reasoning.lower()


@pytest.mark.parametrize("linked_ids", [(), (901,)])
def test_unobserved_future_deferred_milestone_stays_deferred(linked_ids: tuple[int, ...]) -> None:
    milestone = _milestone(
        target_date=date(2026, 12, 1), status=MilestoneStatus.DEFERRED,
        linked_work_item_ids=linked_ids,
    )
    assessment = assess_milestone_health(milestone, (), {}, _AS_OF)
    assert assessment.computed_health == MilestoneStatus.DEFERRED
    assert not assessment.coverage_gap


def test_report_health_summary_counts_unknown_alongside_known_statuses() -> None:
    from dataclasses import replace
    from src.commands.report_health import _build_milestone_health_summary
    from src.core.models_v2 import MilestoneAssessment
    from src.core.models import Confidence

    milestone = _milestone(target_date=date(2026, 12, 1))
    unknown = MilestoneAssessment(
        milestone_id=milestone.id, computed_health=MilestoneStatus.UNKNOWN,
        blocked_criteria=(), slip_probability=0.5, critical_path=False,
        confidence=Confidence.NONE, reasoning="No observed linked work items.", coverage_gap=True,
    )
    other = replace(milestone, id="known-milestone")
    known = replace(unknown, milestone_id=other.id, computed_health=MilestoneStatus.ON_TRACK)
    assert _build_milestone_health_summary((milestone,), (unknown,)) == "Milestones: 1 unknown."
    assert _build_milestone_health_summary((milestone, other), (unknown, known)) == "Milestones: 1 on track, 1 unknown."


def test_future_gap_emission_preserves_actual_reason_not_past_due_claim(tmp_path: Path) -> None:
    from src.core.context_gap_store import load_context_gaps
    from src.core.stages.milestone_stage import _emit_coverage_gap_findings

    milestone = _milestone(target_date=date(2026, 12, 1))
    assessment = assess_milestone_health(milestone, (), {}, _AS_OF)
    _emit_coverage_gap_findings("samplealpha", (assessment,), programs_root=tmp_path)
    records = load_context_gaps("samplealpha", programs_root=tmp_path)

    assert len(records) == 1
    assert "computed_health=unknown" in records[0].message
    assert assessment.reasoning in records[0].message
    assert "past target" not in records[0].message
    assert records[0].severity == "quality_degraded"


@pytest.mark.parametrize("linked_ids", [(1,), (1, 901), (901,)])
@pytest.mark.parametrize("future_target", [False, True])
def test_missing_links_are_gaps_without_erasing_observed_risk(
    linked_ids: tuple[int, ...], future_target: bool,
) -> None:
    from src.core.models import RiskLevel, WorkItem

    milestone = _milestone(
        target_date=date(2026, 12, 1) if future_target else date(2026, 6, 1),
        linked_work_item_ids=linked_ids,
    )
    item = WorkItem(
        id=1,
        type="Feature",
        title="Gate",
        state="Active",
        assigned_to="Vertex Maintainer",
        assigned_to_email="maintainer@example.com",
        area_path="One\\Adventure\\Samplealpha",
        iteration_path="Sprint 1",
        target_date=date(2026, 5, 30),
        risk_level=RiskLevel.MEDIUM,
        tags=["samplealpha"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=_AS_OF,
    )

    assessment = assess_milestone_health(milestone, (item,), {1: ()}, _AS_OF)

    expected = (
        MilestoneStatus.UNKNOWN if future_target and linked_ids == (901,)
        else MilestoneStatus.AT_RISK if future_target else MilestoneStatus.MISSED
    )
    assert assessment.computed_health == expected
    assert assessment.coverage_gap == (901 in linked_ids)
    if not future_target:
        assert assessment.slip_probability == 1.0
