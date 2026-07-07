"""Tests for Phase 2 waiver filtering in NudgeAuditEvent telemetry and active_waiver_ids set.

These tests verify the model-level waiver logic (expired/active) and that waiver counts
are tracked correctly in the audit event fields, without needing the full orchestrator.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.core.nudge_models import (
    NudgeWaiver,
    NudgeAuditEvent,
)


# ---------------------------------------------------------------------------
# Waiver active/expired set logic
# ---------------------------------------------------------------------------


def _make_waiver(work_item_id: int, expires: date) -> NudgeWaiver:
    return NudgeWaiver(
        work_item_id=work_item_id,
        owner_alias="alias",
        reason="testing",
        created=date(2026, 1, 1),
        expires=expires,
    )


class TestActiveWaiverFiltering:
    """Verify active_waiver_ids construction mirrors what _orchestrate does."""

    def _build_active_ids(self, waivers: tuple[NudgeWaiver, ...]) -> frozenset[int]:
        return frozenset(w.work_item_id for w in waivers if not w.expired)

    def test_all_active(self):
        waivers = (
            _make_waiver(1, date(2099, 12, 31)),
            _make_waiver(2, date(2099, 12, 31)),
        )
        ids = self._build_active_ids(waivers)
        assert ids == frozenset({1, 2})

    def test_all_expired(self):
        waivers = (
            _make_waiver(1, date(2000, 1, 1)),
            _make_waiver(2, date(2000, 1, 1)),
        )
        ids = self._build_active_ids(waivers)
        assert ids == frozenset()

    def test_mixed(self):
        waivers = (
            _make_waiver(10, date(2099, 12, 31)),  # active
            _make_waiver(20, date(2000, 1, 1)),    # expired
            _make_waiver(30, date(2099, 12, 31)),  # active
        )
        ids = self._build_active_ids(waivers)
        assert ids == frozenset({10, 30})

    def test_empty_waivers(self):
        ids = self._build_active_ids(())
        assert ids == frozenset()


# ---------------------------------------------------------------------------
# NudgeAuditEvent fields include total_waiver_filtered
# ---------------------------------------------------------------------------


class TestNudgeAuditEventWaiverField:
    def _make_audit_event(self, total_waiver_filtered: int = 0) -> NudgeAuditEvent:
        from src.core.nudge_models import NUDGE_AUDIT_SCHEMA_VERSION, NudgeAuditEvent
        return NudgeAuditEvent(
            event_type="nudge_generated",
            schema_version=NUDGE_AUDIT_SCHEMA_VERSION,
            run_id="test-run-001",
            program_id="nova",
            triggered_at=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
            dry_run=False,
            sections=(),
            total_items=10,
            total_staleness_filtered=3,
            total_exempt_filtered=1,
            total_waiver_filtered=total_waiver_filtered,
            recipient="user@example.com",
            optional_recipient_failures=(),
            degraded_section_ids=(),
            warnings=(),
            eml_paths=(),
        )

    def test_default_is_zero(self):
        evt = self._make_audit_event(total_waiver_filtered=0)
        assert evt.total_waiver_filtered == 0

    def test_nonzero_recorded(self):
        evt = self._make_audit_event(total_waiver_filtered=5)
        assert evt.total_waiver_filtered == 5

    def test_comment_fetch_fields_default_zero(self):
        evt = self._make_audit_event()
        assert evt.comment_fetch_skipped_total == 0
        assert evt.comment_fetch_errors_total == 0

    def test_comment_fetch_fields_settable(self):
        from src.core.nudge_models import NUDGE_AUDIT_SCHEMA_VERSION
        evt = NudgeAuditEvent(
            event_type="nudge_generated",
            schema_version=NUDGE_AUDIT_SCHEMA_VERSION,
            run_id="r1",
            program_id="nova",
            triggered_at=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
            dry_run=False,
            sections=(),
            total_items=5,
            total_staleness_filtered=0,
            total_exempt_filtered=0,
            total_waiver_filtered=2,
            comment_fetch_skipped_total=10,
            comment_fetch_errors_total=3,
            recipient=None,
            optional_recipient_failures=(),
            degraded_section_ids=(),
            warnings=(),
            eml_paths=(),
        )
        assert evt.total_waiver_filtered == 2
        assert evt.comment_fetch_skipped_total == 10
        assert evt.comment_fetch_errors_total == 3

    def test_build_audit_line_includes_waiver(self):
        """build_audit_line serializes total_waiver_filtered to JSON."""
        from src.core.nudge_models import build_audit_line
        evt = self._make_audit_event(total_waiver_filtered=7)
        line = build_audit_line(evt)
        import json
        d = json.loads(line)
        assert d.get("total_waiver_filtered") == 7
