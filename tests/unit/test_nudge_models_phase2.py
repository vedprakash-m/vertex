"""Tests for Phase 2 nudge model additions: NudgeWaiver, ActionDuePolicy variants, AssessedDeadline."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from src.core.nudge_models import (
    AssessedDeadline,
    ExplicitActionDue,
    MilestoneRelativeActionDue,
    NudgeWaiver,
    ResolvedNudgeSection,
    SendDateOffsetActionDue,
    NudgeSectionCriteria,
    NudgeSectionSpec,
)


# ---------------------------------------------------------------------------
# NudgeWaiver
# ---------------------------------------------------------------------------


class TestNudgeWaiver:
    def _make_waiver(self, expires: date) -> NudgeWaiver:
        return NudgeWaiver(
            work_item_id=42,
            owner_alias="jsmith",
            reason="On PTO",
            created=date(2026, 6, 1),
            expires=expires,
        )

    def test_expired_when_past(self):
        today = datetime.now(timezone.utc).date()
        waiver = self._make_waiver(expires=date(today.year, today.month, today.day))
        # expires == today → expired (>= today)
        assert waiver.expired is True

    def test_not_expired_when_future(self):
        future = date(2099, 12, 31)
        waiver = self._make_waiver(expires=future)
        assert waiver.expired is False

    def test_not_expired_yesterday_edge(self):
        # Yesterday = not expired (still in the future relative to creation)
        # Actually "expires" means the day it stops being valid → >= today means expired
        today = datetime.now(timezone.utc).date()
        yesterday = date(today.year, today.month, today.day - 1) if today.day > 1 else date(today.year, today.month - 1, 28)
        waiver = self._make_waiver(expires=yesterday)
        assert waiver.expired is True

    def test_fields_accessible(self):
        waiver = NudgeWaiver(
            work_item_id=100,
            owner_alias="alice",
            reason="Milestone pending",
            created=date(2026, 6, 15),
            expires=date(2099, 7, 1),
        )
        assert waiver.work_item_id == 100
        assert waiver.owner_alias == "alice"
        assert waiver.reason == "Milestone pending"
        assert waiver.created == date(2026, 6, 15)
        assert waiver.expires == date(2099, 7, 1)


# ---------------------------------------------------------------------------
# ActionDuePolicy discriminated union
# ---------------------------------------------------------------------------


class TestExplicitActionDue:
    def test_mode_field_is_explicit(self):
        p = ExplicitActionDue(date=date(2026, 7, 1))
        assert p.mode == "explicit"

    def test_date_stored(self):
        d = date(2026, 7, 1)
        p = ExplicitActionDue(date=d)
        assert p.date == d



class TestSendDateOffsetActionDue:
    def test_mode_field(self):
        p = SendDateOffsetActionDue(business_days=3)
        assert p.mode == "send_date_offset"

    def test_default_business_days(self):
        p = SendDateOffsetActionDue()
        assert p.business_days == 3

    def test_custom_business_days(self):
        p = SendDateOffsetActionDue(business_days=5)
        assert p.business_days == 5


class TestMilestoneRelativeActionDue:
    def test_mode_field(self):
        p = MilestoneRelativeActionDue(milestone_id="m6-ramp")
        assert p.mode == "milestone_relative"

    def test_milestone_id(self):
        p = MilestoneRelativeActionDue(milestone_id="m7-full-ramp", business_days_before=5)
        assert p.milestone_id == "m7-full-ramp"
        assert p.business_days_before == 5

    def test_default_business_days_before(self):
        p = MilestoneRelativeActionDue(milestone_id="m1")
        assert p.business_days_before == 3


# ---------------------------------------------------------------------------
# AssessedDeadline
# ---------------------------------------------------------------------------


class TestAssessedDeadline:
    def test_explicit_authority(self):
        ad = AssessedDeadline(
            date=date(2026, 7, 1),
            milestone_id=None,
            truth_level=None,
            disputed=False,
            stale=False,
            provisional_inputs=False,
            evidence=(),
            authority="operator_override",
            resolution_status="explicit",
        )
        assert ad.authority == "operator_override"
        assert ad.resolution_status == "explicit"
        assert ad.date == date(2026, 7, 1)

    def test_assessed_resolved(self):
        ad = AssessedDeadline(
            date=date(2026, 8, 31),
            milestone_id="m7-full-ramp",
            truth_level="high",
            disputed=False,
            stale=False,
            provisional_inputs=False,
            evidence=("source-a", "source-b"),
            authority="assessed",
            resolution_status="resolved",
        )
        assert ad.authority == "assessed"
        assert ad.resolution_status == "resolved"
        assert len(ad.evidence) == 2

    def test_none_resolution(self):
        ad = AssessedDeadline(
            date=None, milestone_id=None, truth_level=None,
            disputed=False, stale=False, provisional_inputs=False,
            evidence=(), authority="none", resolution_status="none",
        )
        assert ad.date is None
        assert ad.resolution_status == "none"



# ---------------------------------------------------------------------------
# ResolvedNudgeSection
# ---------------------------------------------------------------------------


class TestResolvedNudgeSection:
    def _make_rs(self, action_due: date | None, required: bool = False) -> ResolvedNudgeSection:
        sec = NudgeSectionSpec(
            id="s1", title="Section 1",
            criteria=NudgeSectionCriteria(source="registry"),
            stale_business_days=5,
            letter="A",
            required=required,
        )
        assessed = AssessedDeadline(
            date=None, milestone_id=None, truth_level=None,
            disputed=False, stale=False, provisional_inputs=False,
            evidence=(), authority="none", resolution_status="none",
        )
        return ResolvedNudgeSection(spec=sec, action_due_at=action_due, target_date_ceiling=assessed)

    def test_fields_accessible(self):
        rs = self._make_rs(date(2026, 7, 1), required=True)
        assert rs.action_due_at == date(2026, 7, 1)
        assert rs.spec.required is True

    def test_action_due_none(self):
        rs = self._make_rs(None)
        assert rs.action_due_at is None

