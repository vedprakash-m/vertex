"""Contract tests for NQD governance checks (specs/nudge-gaps.md §10).

Tests NQD-3..NQD-12 using the internal check functions directly so they
run without a full programs/ workspace (no ADO calls, no disk I/O).
"""
from __future__ import annotations

import datetime
from typing import Any

from src.commands.doctor_checks.nudge_checks import (
    _nqd3_checks,
    _nqd4_checks,
    _nqd5_checks,
    _nqd6_checks,
    _nqd8_checks,
    _nqd10_checks,
    _nqd12_checks,
)
from src.commands.doctor_checks.models import DoctorCheck


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(fn, fh: dict[str, Any], program_id: str = "nova") -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    fn(checks, fh, program_id)
    return checks


def _statuses(checks: list[DoctorCheck]) -> list[str]:
    return [c.status for c in checks]


def _details(checks: list[DoctorCheck]) -> str:
    return " ".join(c.detail for c in checks)


# ---------------------------------------------------------------------------
# NQD-3: Subject / preheader
# ---------------------------------------------------------------------------


class TestNQD3:
    def test_ok_when_both_present(self):
        fh = {
            "email_subject_label": "NOVA Hygiene | Week of {date}",
            "preheader": "Weekly sweep",
        }
        checks = _run(_nqd3_checks, fh)
        assert _statuses(checks) == ["ok"]

    def test_warn_missing_subject(self):
        fh = {"preheader": "Weekly sweep"}
        checks = _run(_nqd3_checks, fh)
        assert "warn" in _statuses(checks)
        assert "email_subject_label" in _details(checks)

    def test_warn_default_subject(self):
        fh = {
            "email_subject_label": "ADO Hygiene Report",
            "preheader": "Weekly sweep",
        }
        checks = _run(_nqd3_checks, fh)
        assert "warn" in _statuses(checks)

    def test_warn_missing_preheader(self):
        fh = {"email_subject_label": "Custom subject"}
        checks = _run(_nqd3_checks, fh)
        assert "warn" in _statuses(checks)
        assert "preheader" in _details(checks)


# ---------------------------------------------------------------------------
# NQD-4: Deadline health
# ---------------------------------------------------------------------------


class TestNQD4:
    def test_ok_no_requires_milestone_sections(self):
        fh = {"sections": [{"id": "s1", "title": "S1"}]}
        checks = _run(_nqd4_checks, fh)
        assert _statuses(checks) == ["ok"]

    def test_ok_requires_milestone_with_id(self):
        fh = {"sections": [{
            "id": "s1", "requires_milestone": True, "deadline_milestone_id": "m6-ramp",
        }]}
        checks = _run(_nqd4_checks, fh)
        assert _statuses(checks) == ["ok"]

    def test_fail_requires_milestone_missing_id(self):
        fh = {"sections": [{
            "id": "s1", "requires_milestone": True,
        }]}
        checks = _run(_nqd4_checks, fh)
        assert "fail" in _statuses(checks)
        assert "requires_milestone" in _details(checks)

    def test_ok_empty_sections(self):
        fh = {"sections": []}
        checks = _run(_nqd4_checks, fh)
        assert _statuses(checks) == ["ok"]


# ---------------------------------------------------------------------------
# NQD-5: Hardcoded deadline drift
# ---------------------------------------------------------------------------


class TestNQD5:
    def test_ok_no_deadlines(self):
        fh = {"sections": [{"id": "s1", "title": "S1"}]}
        checks = _run(_nqd5_checks, fh)
        assert _statuses(checks) == ["ok"]

    def test_ok_future_deadline_no_milestone(self):
        future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        fh = {"sections": [{"id": "s1", "deadline": future}]}
        checks = _run(_nqd5_checks, fh)
        assert _statuses(checks) == ["ok"]

    def test_warn_past_deadline_no_milestone_id(self):
        past = "2020-01-01"
        fh = {"sections": [{"id": "s1", "deadline": past}]}
        checks = _run(_nqd5_checks, fh)
        assert "warn" in _statuses(checks)
        assert "s1" in _details(checks)

    def test_ok_past_deadline_with_milestone_id(self):
        past = "2020-01-01"
        fh = {"sections": [{"id": "s1", "deadline": past, "deadline_milestone_id": "m1"}]}
        checks = _run(_nqd5_checks, fh)
        assert _statuses(checks) == ["ok"]


# ---------------------------------------------------------------------------
# NQD-6: Waiver governance
# ---------------------------------------------------------------------------


class TestNQD6:
    def test_ok_no_waivers(self):
        checks = _run(_nqd6_checks, {})
        assert _statuses(checks) == ["ok"]

    def test_ok_valid_future_waiver(self):
        future = "2099-12-31"
        fh = {"nudge_waivers": [{"work_item_id": 1, "expires": future}]}
        checks = _run(_nqd6_checks, fh)
        assert _statuses(checks) == ["ok"]

    def test_fail_expired_waiver(self):
        past = "2000-01-01"
        fh = {"nudge_waivers": [{"work_item_id": 42, "expires": past}]}
        checks = _run(_nqd6_checks, fh)
        assert "fail" in _statuses(checks)
        assert "42" in _details(checks)

    def test_fail_missing_expires(self):
        fh = {"nudge_waivers": [{"work_item_id": 7}]}
        checks = _run(_nqd6_checks, fh)
        assert "fail" in _statuses(checks)
        assert "7" in _details(checks)

    def test_fail_invalid_date_format(self):
        fh = {"nudge_waivers": [{"work_item_id": 99, "expires": "not-a-date"}]}
        checks = _run(_nqd6_checks, fh)
        assert "fail" in _statuses(checks)


# ---------------------------------------------------------------------------
# NQD-8: Audience policy
# ---------------------------------------------------------------------------


class TestNQD8:
    def test_ok_no_audience_policy(self):
        checks = _run(_nqd8_checks, {})
        assert _statuses(checks) == ["ok"]
        assert "defaults" in _details(checks)

    def test_ok_valid_policy(self):
        fh = {"audience_policy": {
            "allowed_domains": ["microsoft.com"],
            "delivery_mode": "to",
            "unresolved_owner": "drop",
            "opt_out_fallback": "escalate",
        }}
        checks = _run(_nqd8_checks, fh)
        assert _statuses(checks) == ["ok"]

    def test_fail_not_a_mapping(self):
        fh = {"audience_policy": "invalid"}
        checks = _run(_nqd8_checks, fh)
        assert "fail" in _statuses(checks)

    def test_fail_empty_allowed_domains(self):
        fh = {"audience_policy": {"allowed_domains": [], "delivery_mode": "to"}}
        checks = _run(_nqd8_checks, fh)
        assert "fail" in _statuses(checks)
        assert "allowed_domains" in _details(checks)

    def test_fail_invalid_delivery_mode(self):
        fh = {"audience_policy": {
            "allowed_domains": ["microsoft.com"],
            "delivery_mode": "cc",
        }}
        checks = _run(_nqd8_checks, fh)
        assert "fail" in _statuses(checks)
        assert "delivery_mode" in _details(checks)

    def test_fail_invalid_unresolved_owner(self):
        fh = {"audience_policy": {
            "allowed_domains": ["microsoft.com"],
            "delivery_mode": "to",
            "unresolved_owner": "skip",
        }}
        checks = _run(_nqd8_checks, fh)
        assert "fail" in _statuses(checks)

    def test_bcc_delivery_mode_ok(self):
        fh = {"audience_policy": {
            "allowed_domains": ["microsoft.com"],
            "delivery_mode": "bcc",
            "unresolved_owner": "fail",
            "opt_out_fallback": "gap",
        }}
        checks = _run(_nqd8_checks, fh)
        assert _statuses(checks) == ["ok"]


# ---------------------------------------------------------------------------
# NQD-10: Fact single-seam (ci_contract)
# ---------------------------------------------------------------------------


class TestNQD10:
    def test_no_violations_in_nudge_py(self):
        """nudge.py must not call append_program_event directly."""
        checks: list[DoctorCheck] = []
        _nqd10_checks(checks)
        statuses = [c.status for c in checks]
        details = " ".join(c.detail for c in checks)
        assert "ok" in statuses, f"NQD-10 failed: {details}"

    def test_returns_one_check(self):
        checks: list[DoctorCheck] = []
        _nqd10_checks(checks)
        assert len(checks) == 1


# ---------------------------------------------------------------------------
# NQD-12: No evidence/M365 imports in nudge path (ci_contract)
# ---------------------------------------------------------------------------


class TestNQD12:
    def test_no_evidence_imports_in_nudge_path(self):
        """nudge.py and nudge_resolution.py must not import M365/evidence modules."""
        checks: list[DoctorCheck] = []
        _nqd12_checks(checks)
        statuses = [c.status for c in checks]
        details = " ".join(c.detail for c in checks)
        assert "ok" in statuses, f"NQD-12 failed: {details}"

    def test_returns_one_check(self):
        checks: list[DoctorCheck] = []
        _nqd12_checks(checks)
        assert len(checks) == 1
