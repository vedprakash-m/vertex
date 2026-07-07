"""Tests for src/core/nudge_resolution.py — Phase 2 deadline + action-due engine."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.core.nudge_models import (
    AssessedDeadline,
    ExplicitActionDue,
    MilestoneRelativeActionDue,
    NudgeSectionCriteria,
    NudgeSectionSpec,
    ResolvedNudgeSection,
    SendDateOffsetActionDue,
)
from src.core.nudge_resolution import (
    _subtract_business_days,
    build_subject_prefix,
    resolve_sections,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    sections: tuple[NudgeSectionSpec, ...] = (),
    action_due_policy=None,
    context_subject_prefix: bool = False,
    context_subject_prefix_template: str = "[Action DUE {due} EOD]",
    context_subject_overdue_template: str = "[OVERDUE since {due}]",
    context_subject_lookahead_days: int = 14,
) -> MagicMock:
    """Return a minimal config mock used by resolve_sections / build_subject_prefix."""
    evaluation = MagicMock()
    evaluation.action_due_policy = action_due_policy

    presentation = MagicMock()
    presentation.context_subject_prefix = context_subject_prefix
    presentation.context_subject_prefix_template = context_subject_prefix_template
    presentation.context_subject_overdue_template = context_subject_overdue_template
    presentation.context_subject_lookahead_days = context_subject_lookahead_days

    config = MagicMock()
    config.sections = sections
    config.evaluation = evaluation
    config.presentation = presentation
    return config


_DEFAULT_CRITERIA = NudgeSectionCriteria(source="registry")


def _make_section(
    section_id: str = "test",
    deadline: date | None = None,
    deadline_milestone_id: str | None = None,
    required: bool = False,
) -> NudgeSectionSpec:
    return NudgeSectionSpec(
        id=section_id,
        title=f"Section {section_id}",
        criteria=_DEFAULT_CRITERIA,
        stale_business_days=5,
        letter="A",
        deadline=deadline,
        deadline_milestone_id=deadline_milestone_id,
        required=required,
    )


def _make_milestone_fa(target_date: date | None, disputed: bool = False, stale: bool = False, provisional_inputs: bool = False) -> MagicMock:
    rec = MagicMock()
    rec.target_date = target_date
    fa = MagicMock()
    fa.record = rec
    fa.disputed = disputed
    fa.stale = stale
    fa.provisional_inputs = provisional_inputs
    fa.truth_level = "high"
    fa.evidence = []
    return fa


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _subtract_business_days
# ---------------------------------------------------------------------------


class TestSubtractBusinessDays:
    def test_zero_days(self):
        d = date(2026, 6, 22)  # Monday
        assert _subtract_business_days(d, 0) == d

    def test_three_business_days_from_monday(self):
        # Monday 2026-06-22 minus 3 bd → Wednesday 2026-06-17
        result = _subtract_business_days(date(2026, 6, 22), 3)
        assert result == date(2026, 6, 17)

    def test_skips_weekend(self):
        # Monday 2026-06-22 minus 1 bd → Friday 2026-06-19
        result = _subtract_business_days(date(2026, 6, 22), 1)
        assert result == date(2026, 6, 19)

    def test_five_business_days_crosses_weekend(self):
        # Friday 2026-06-19 minus 5 bd → Friday 2026-06-12
        result = _subtract_business_days(date(2026, 6, 19), 5)
        assert result == date(2026, 6, 12)

    def test_negative_days_returns_anchor(self):
        d = date(2026, 6, 22)
        assert _subtract_business_days(d, -1) == d


# ---------------------------------------------------------------------------
# resolve_sections — no reality
# ---------------------------------------------------------------------------


class TestResolveSectionsNoReality:
    def test_explicit_deadline_no_reality(self):
        sec = _make_section(deadline=date(2026, 7, 1))
        config = _make_config(sections=(sec,))
        result = resolve_sections(config, None, program_id="nova")
        assert len(result) == 1
        assessed = result[0].target_date_ceiling
        assert assessed.authority == "operator_override"
        assert assessed.resolution_status == "explicit"
        assert assessed.date == date(2026, 7, 1)

    def test_milestone_deadline_no_reality_returns_unavailable(self):
        sec = _make_section(deadline_milestone_id="m6-ramp")
        config = _make_config(sections=(sec,))
        result = resolve_sections(config, None, program_id="nova")
        assessed = result[0].target_date_ceiling
        assert assessed.resolution_status == "unavailable"
        assert assessed.authority == "none"
        assert assessed.date is None

    def test_no_deadline_returns_none(self):
        sec = _make_section()
        config = _make_config(sections=(sec,))
        result = resolve_sections(config, None, program_id="nova")
        assessed = result[0].target_date_ceiling
        assert assessed.resolution_status == "none"
        assert assessed.authority == "none"
        assert assessed.date is None

    def test_returns_tuple(self):
        config = _make_config(sections=())
        result = resolve_sections(config, None, program_id="nova")
        assert isinstance(result, tuple)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# resolve_sections — with reality
# ---------------------------------------------------------------------------


class TestResolveSectionsWithReality:
    def test_milestone_resolved(self):
        fa = _make_milestone_fa(target_date=date(2026, 7, 15))
        reality = MagicMock()
        reality.milestones.return_value = [fa]
        fa.record.id = "m6-ramp"  # needed for milestone_by_id key

        # Force the milestone dict to have the right key
        sec = _make_section(deadline_milestone_id="m6-ramp")
        config = _make_config(sections=(sec,))

        # Manually patch: the reality.milestones() list's record.id must match
        fa_inner = MagicMock()
        fa_inner.record = MagicMock()
        fa_inner.record.id = "m6-ramp"
        fa_inner.record.target_date = date(2026, 7, 15)
        fa_inner.disputed = False
        fa_inner.stale = False
        fa_inner.provisional_inputs = False
        fa_inner.truth_level = "high"
        fa_inner.evidence = []
        reality.milestones.return_value = [fa_inner]

        result = resolve_sections(config, reality, program_id="nova")
        assessed = result[0].target_date_ceiling
        assert assessed.resolution_status == "resolved"
        assert assessed.date == date(2026, 7, 15)
        assert assessed.authority == "assessed"

    def test_milestone_disputed_returns_unconfirmed(self):
        fa = MagicMock()
        fa.record = MagicMock()
        fa.record.id = "m6-ramp"
        fa.record.target_date = date(2026, 7, 15)
        fa.disputed = True
        fa.stale = False
        fa.provisional_inputs = False
        fa.truth_level = "low"
        fa.evidence = []
        reality = MagicMock()
        reality.milestones.return_value = [fa]

        sec = _make_section(deadline_milestone_id="m6-ramp")
        config = _make_config(sections=(sec,))
        result = resolve_sections(config, reality, program_id="nova")
        assessed = result[0].target_date_ceiling
        assert assessed.resolution_status == "unconfirmed"
        assert assessed.disputed is True

    def test_milestone_not_found_in_non_empty_dict_returns_unconfirmed(self):
        fa = MagicMock()
        fa.record = MagicMock()
        fa.record.id = "other-milestone"
        fa.record.target_date = date(2026, 7, 15)
        fa.disputed = False
        fa.stale = False
        fa.provisional_inputs = False
        fa.truth_level = "high"
        fa.evidence = []
        reality = MagicMock()
        reality.milestones.return_value = [fa]

        sec = _make_section(deadline_milestone_id="m6-ramp")  # not in reality
        config = _make_config(sections=(sec,))
        result = resolve_sections(config, reality, program_id="nova")
        assessed = result[0].target_date_ceiling
        assert assessed.resolution_status == "unconfirmed"

    def test_reality_raises_returns_gracefully(self):
        reality = MagicMock()
        reality.milestones.side_effect = RuntimeError("unavailable")
        sec = _make_section(deadline_milestone_id="m6-ramp")
        config = _make_config(sections=(sec,))
        result = resolve_sections(config, reality, program_id="nova")
        # Should not raise; returns unavailable
        assessed = result[0].target_date_ceiling
        assert assessed.resolution_status == "unavailable"


# ---------------------------------------------------------------------------
# action_due_at computation
# ---------------------------------------------------------------------------


class TestActionDueAt:
    def test_no_policy_returns_none(self):
        sec = _make_section()
        config = _make_config(sections=(sec,), action_due_policy=None)
        result = resolve_sections(config, None, program_id="nova", now_utc=_utc(2026, 6, 22))
        assert result[0].action_due_at is None

    def test_explicit_policy(self):
        policy = ExplicitActionDue(date=date(2026, 6, 30))
        sec = _make_section()
        config = _make_config(sections=(sec,), action_due_policy=policy)
        result = resolve_sections(config, None, program_id="nova")
        assert result[0].action_due_at == date(2026, 6, 30)

    def test_send_date_offset_policy(self):
        # Monday anchor → 3 bd before = Wednesday prior week
        policy = SendDateOffsetActionDue(business_days=3)
        sec = _make_section()
        config = _make_config(sections=(sec,), action_due_policy=policy)
        anchor = date(2026, 6, 22)  # Monday
        result = resolve_sections(
            config, None, program_id="nova",
            now_utc=_utc(2026, 6, 22),
            planned_send_at=anchor,
        )
        # 3 bd before Monday = Wednesday 6/17
        assert result[0].action_due_at == date(2026, 6, 17)

    def test_milestone_relative_no_reality(self):
        policy = MilestoneRelativeActionDue(milestone_id="m6-ramp", business_days_before=3)
        sec = _make_section()
        config = _make_config(sections=(sec,), action_due_policy=policy)
        result = resolve_sections(config, None, program_id="nova")
        assert result[0].action_due_at is None  # milestone not found

    def test_milestone_relative_with_reality(self):
        policy = MilestoneRelativeActionDue(milestone_id="m6-ramp", business_days_before=3)
        sec = _make_section()
        config = _make_config(sections=(sec,), action_due_policy=policy)

        fa = MagicMock()
        fa.record = MagicMock()
        fa.record.id = "m6-ramp"
        fa.record.target_date = date(2026, 6, 22)  # Monday
        fa.disputed = False
        fa.stale = False
        fa.provisional_inputs = False
        fa.truth_level = "high"
        fa.evidence = []
        reality = MagicMock()
        reality.milestones.return_value = [fa]

        result = resolve_sections(config, reality, program_id="nova")
        # 3 bd before 6/22 (Mon) = 6/17 (Wed)
        assert result[0].action_due_at == date(2026, 6, 17)


# ---------------------------------------------------------------------------
# build_subject_prefix
# ---------------------------------------------------------------------------


class TestBuildSubjectPrefix:
    def _rs(self, action_due: date | None, required: bool = True) -> ResolvedNudgeSection:
        sec = NudgeSectionSpec(
            id="s", title="S",
            criteria=_DEFAULT_CRITERIA, stale_business_days=5, letter="A",
            required=required,
        )
        assessed = AssessedDeadline(date=None, milestone_id=None, truth_level=None,
                                    disputed=False, stale=False, provisional_inputs=False,
                                    evidence=(), authority="none", resolution_status="none")
        return ResolvedNudgeSection(spec=sec, action_due_at=action_due, target_date_ceiling=assessed)

    def test_no_prefix_flag_returns_empty(self):
        rs = self._rs(date(2026, 6, 30))
        config = _make_config(context_subject_prefix=False)
        assert build_subject_prefix((rs,), config) == ""

    def test_no_required_sections_returns_empty(self):
        rs = self._rs(date(2026, 6, 30), required=False)
        config = _make_config(context_subject_prefix=True)
        assert build_subject_prefix((rs,), config) == ""

    def test_no_action_due_returns_empty(self):
        rs = self._rs(None, required=True)
        config = _make_config(context_subject_prefix=True)
        assert build_subject_prefix((rs,), config) == ""

    def test_upcoming_within_lookahead(self):
        today = date(2026, 6, 22)
        due = date(2026, 6, 29)  # 7 days away, within lookahead=14
        rs = self._rs(due, required=True)
        config = _make_config(context_subject_prefix=True, context_subject_lookahead_days=14)
        result = build_subject_prefix((rs,), config, now_date=today)
        assert result == "[Action DUE 6/29 EOD]"

    def test_overdue(self):
        today = date(2026, 6, 22)
        due = date(2026, 6, 20)  # 2 days ago
        rs = self._rs(due, required=True)
        config = _make_config(context_subject_prefix=True)
        result = build_subject_prefix((rs,), config, now_date=today)
        assert result == "[OVERDUE since 6/20]"

    def test_overdue_takes_precedence_over_upcoming(self):
        today = date(2026, 6, 22)
        rs_overdue = self._rs(date(2026, 6, 20), required=True)
        rs_upcoming = self._rs(date(2026, 6, 28), required=True)
        config = _make_config(context_subject_prefix=True)
        result = build_subject_prefix((rs_overdue, rs_upcoming), config, now_date=today)
        assert result.startswith("[OVERDUE")

    def test_beyond_lookahead_returns_empty(self):
        today = date(2026, 6, 22)
        due = date(2026, 7, 31)  # beyond 14-day lookahead
        rs = self._rs(due, required=True)
        config = _make_config(context_subject_prefix=True, context_subject_lookahead_days=14)
        assert build_subject_prefix((rs,), config, now_date=today) == ""

    def test_multiple_overdue_selects_oldest(self):
        today = date(2026, 6, 22)
        rs1 = self._rs(date(2026, 6, 18), required=True)  # older
        rs2 = self._rs(date(2026, 6, 20), required=True)
        config = _make_config(context_subject_prefix=True)
        result = build_subject_prefix((rs1, rs2), config, now_date=today)
        assert result == "[OVERDUE since 6/18]"

    def test_custom_template(self):
        today = date(2026, 6, 22)
        due = date(2026, 6, 29)
        rs = self._rs(due, required=True)
        config = _make_config(
            context_subject_prefix=True,
            context_subject_prefix_template="ACTION NEEDED by {due}",
        )
        result = build_subject_prefix((rs,), config, now_date=today)
        assert result == "ACTION NEEDED by 6/29"
