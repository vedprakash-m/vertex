"""Unit tests for schema 2.1 / Phase 2 additions in nudge_config.py.

Tests internal parse helpers directly for speed.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.core.exceptions import ConfigError
from src.core.nudge_config import _parse_action_due_policy, _parse_audience_policy, _parse_waivers
from src.core.nudge_models import (
    ExplicitActionDue,
    MilestoneRelativeActionDue,
    NudgeAudiencePolicy,
    NudgeWaiver,
    SendDateOffsetActionDue,
)


# ---------------------------------------------------------------------------
# _parse_waivers
# ---------------------------------------------------------------------------


class TestParseWaivers:
    def test_empty_list(self):
        assert _parse_waivers([], "nova") == []

    def test_valid_waiver(self):
        raw = [{
            "work_item_id": 42,
            "owner_alias": "jsmith",
            "reason": "On PTO",
            "created": "2026-06-01",
            "expires": "2099-12-31",
        }]
        waivers = _parse_waivers(raw, "nova")
        assert len(waivers) == 1
        w = waivers[0]
        assert w.work_item_id == 42
        assert w.owner_alias == "jsmith"
        assert w.reason == "On PTO"
        assert w.created == date(2026, 6, 1)
        assert w.expires == date(2099, 12, 31)
        assert isinstance(w, NudgeWaiver)

    def test_multiple_waivers(self):
        raw = [
            {"work_item_id": 1, "owner_alias": "a", "reason": "r1", "expires": "2099-01-01"},
            {"work_item_id": 2, "owner_alias": "b", "reason": "r2", "expires": "2099-02-01"},
        ]
        waivers = _parse_waivers(raw, "nova")
        assert len(waivers) == 2
        assert waivers[0].work_item_id == 1
        assert waivers[1].work_item_id == 2

    def test_missing_reason_raises(self):
        raw = [{"work_item_id": 1, "expires": "2099-01-01"}]
        with pytest.raises(ConfigError, match="reason"):
            _parse_waivers(raw, "nova")

    def test_missing_expires_raises(self):
        raw = [{"work_item_id": 1, "reason": "testing"}]
        with pytest.raises(ConfigError, match="expires"):
            _parse_waivers(raw, "nova")

    def test_invalid_expires_date_raises(self):
        raw = [{"work_item_id": 1, "reason": "r", "expires": "not-a-date"}]
        with pytest.raises(ConfigError):
            _parse_waivers(raw, "nova")

    def test_invalid_work_item_id_raises(self):
        raw = [{"work_item_id": "abc", "reason": "r", "expires": "2099-01-01"}]
        with pytest.raises(ConfigError):
            _parse_waivers(raw, "nova")

    def test_zero_work_item_id_raises(self):
        raw = [{"work_item_id": 0, "reason": "r", "expires": "2099-01-01"}]
        with pytest.raises(ConfigError):
            _parse_waivers(raw, "nova")

    def test_not_a_mapping_raises(self):
        raw = ["not-a-dict"]
        with pytest.raises(ConfigError):
            _parse_waivers(raw, "nova")

    def test_created_optional_defaults_to_today(self):
        raw = [{"work_item_id": 1, "reason": "r", "expires": "2099-01-01"}]
        waivers = _parse_waivers(raw, "nova")
        assert waivers[0].created == date.today()


# ---------------------------------------------------------------------------
# _parse_action_due_policy
# ---------------------------------------------------------------------------


class TestParseActionDuePolicy:
    def test_none_returns_none(self):
        assert _parse_action_due_policy(None, "nova") is None

    def test_explicit_mode(self):
        raw = {"mode": "explicit", "date": "2026-07-01"}
        policy = _parse_action_due_policy(raw, "nova")
        assert isinstance(policy, ExplicitActionDue)
        assert policy.date == date(2026, 7, 1)
        assert policy.mode == "explicit"

    def test_explicit_mode_missing_date_raises(self):
        raw = {"mode": "explicit"}
        with pytest.raises(ConfigError, match="date"):
            _parse_action_due_policy(raw, "nova")

    def test_explicit_mode_invalid_date_raises(self):
        raw = {"mode": "explicit", "date": "not-a-date"}
        with pytest.raises(ConfigError):
            _parse_action_due_policy(raw, "nova")

    def test_send_date_offset_mode(self):
        raw = {"mode": "send_date_offset", "business_days": 5}
        policy = _parse_action_due_policy(raw, "nova")
        assert isinstance(policy, SendDateOffsetActionDue)
        assert policy.business_days == 5
        assert policy.mode == "send_date_offset"

    def test_send_date_offset_default_business_days(self):
        raw = {"mode": "send_date_offset"}
        policy = _parse_action_due_policy(raw, "nova")
        assert isinstance(policy, SendDateOffsetActionDue)
        assert policy.business_days == 3

    def test_milestone_relative_mode(self):
        raw = {"mode": "milestone_relative", "milestone_id": "m6-ramp", "business_days_before": 4}
        policy = _parse_action_due_policy(raw, "nova")
        assert isinstance(policy, MilestoneRelativeActionDue)
        assert policy.milestone_id == "m6-ramp"
        assert policy.business_days_before == 4
        assert policy.mode == "milestone_relative"

    def test_milestone_relative_default_business_days_before(self):
        raw = {"mode": "milestone_relative", "milestone_id": "m7"}
        policy = _parse_action_due_policy(raw, "nova")
        assert isinstance(policy, MilestoneRelativeActionDue)
        assert policy.business_days_before == 3

    def test_unknown_mode_raises(self):
        raw = {"mode": "unknown_mode"}
        with pytest.raises(ConfigError, match="mode"):
            _parse_action_due_policy(raw, "nova")

    def test_not_a_mapping_raises(self):
        with pytest.raises(ConfigError):
            _parse_action_due_policy("explicit", "nova")


# ---------------------------------------------------------------------------
# _parse_audience_policy
# ---------------------------------------------------------------------------


class TestParseAudiencePolicy:
    def test_none_returns_none(self):
        assert _parse_audience_policy(None, "nova") is None

    def test_valid_policy(self):
        raw = {
            "allowed_domains": ["microsoft.com", "example.com"],
            "max_recipients": 50,
            "opt_out": ["opt-out@microsoft.com"],
            "opt_out_fallback": "escalate",
            "new_recipient_approval": True,
            "unresolved_owner": "drop",
            "delivery_mode": "to",
        }
        policy = _parse_audience_policy(raw, "nova")
        assert isinstance(policy, NudgeAudiencePolicy)
        assert "microsoft.com" in policy.allowed_domains
        assert "example.com" in policy.allowed_domains
        assert policy.max_recipients == 50
        assert policy.opt_out_fallback == "escalate"
        assert policy.unresolved_owner == "drop"
        assert policy.delivery_mode == "to"

    def test_bcc_delivery_mode(self):
        raw = {"allowed_domains": ["microsoft.com"], "delivery_mode": "bcc"}
        policy = _parse_audience_policy(raw, "nova")
        assert policy is not None
        assert policy.delivery_mode == "bcc"

    def test_invalid_delivery_mode_raises(self):
        raw = {"delivery_mode": "cc"}
        with pytest.raises(ConfigError, match="delivery_mode"):
            _parse_audience_policy(raw, "nova")

    def test_invalid_opt_out_fallback_raises(self):
        raw = {"opt_out_fallback": "skip"}
        with pytest.raises(ConfigError, match="opt_out_fallback"):
            _parse_audience_policy(raw, "nova")

    def test_invalid_unresolved_owner_raises(self):
        raw = {"unresolved_owner": "ignore"}
        with pytest.raises(ConfigError, match="unresolved_owner"):
            _parse_audience_policy(raw, "nova")

    def test_not_a_mapping_raises(self):
        with pytest.raises(ConfigError):
            _parse_audience_policy("microsoft.com", "nova")

    def test_empty_allowed_domains_defaults_to_microsoft(self):
        raw = {}
        policy = _parse_audience_policy(raw, "nova")
        assert policy is not None
        assert "microsoft.com" in policy.allowed_domains

    def test_opt_out_as_frozenset(self):
        raw = {"opt_out": ["a@m.com", "b@m.com"]}
        policy = _parse_audience_policy(raw, "nova")
        assert policy is not None
        assert isinstance(policy.opt_out, frozenset)
        assert "a@m.com" in policy.opt_out
