"""Unit tests for src/core/integration_types.py."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.integration_types import (
    ChannelRegistration,
    DiscoveryCompleteness,
    ExtractionResult,
    HydrationResult,
    IcMHydrationOutput,
    IncidentState,
    MeetingEvent,
    RegistrationStatus,
    RegistryDelta,
    TagExpression,
    TeamsHydrationOutput,
    ThreadMessage,
)


# ---------------------------------------------------------------------------
# TagExpression
# ---------------------------------------------------------------------------


class TestTagExpression:
    def test_defaults_are_empty(self) -> None:
        expr = TagExpression()
        assert expr.all_of == ()
        assert expr.any_of == ()

    def test_construction_with_values(self) -> None:
        expr = TagExpression(all_of=("a", "b"), any_of=("c",))
        assert expr.all_of == ("a", "b")
        assert expr.any_of == ("c",)

    def test_frozen(self) -> None:
        expr = TagExpression(all_of=("x",))
        with pytest.raises(AttributeError):
            expr.all_of = ("y",)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RegistryDelta
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_REG = ChannelRegistration(
    channel="ado",
    program_id="prog1",
    provider_instance_id="default",
    ref_id="123",
    ref_kind="work_item",
    status=RegistrationStatus.ACTIVE,
    first_discovered_at=_NOW,
    last_seen_at=_NOW,
    confidence=1.0,
    confidence_source="static_config",
)


def _make_delta(**overrides) -> RegistryDelta:
    base = dict(
        channel="ado",
        program_id="prog1",
        computed_at=_NOW,
        previous_discovery_at=None,
        completeness=DiscoveryCompleteness.FULL,
        added=(),
        removed=(),
        updated=(),
        unchanged_count=0,
        failed_scopes={},
        shrinkage_pct=0.0,
    )
    base.update(overrides)
    return RegistryDelta(**base)


class TestRegistryDelta:
    def test_is_empty_when_no_changes(self) -> None:
        delta = _make_delta()
        assert delta.is_empty is True

    def test_is_not_empty_when_added(self) -> None:
        delta = _make_delta(added=(_REG,))
        assert delta.is_empty is False

    def test_is_not_empty_when_removed(self) -> None:
        delta = _make_delta(removed=(_REG,))
        assert delta.is_empty is False

    def test_is_not_empty_when_updated(self) -> None:
        delta = _make_delta(updated=(_REG,))
        assert delta.is_empty is False

    def test_summary_format(self) -> None:
        delta = _make_delta(added=(_REG,), removed=(_REG, _REG), updated=(_REG,), unchanged_count=10)
        assert delta.summary == "+1 -2 ~1 =10"

    def test_summary_empty(self) -> None:
        delta = _make_delta(unchanged_count=5)
        assert delta.summary == "+0 -0 ~0 =5"

    def test_is_shrinkage_guarded_above_threshold(self) -> None:
        # 30% shrinkage with 5+ removals → should trigger guard
        regs = tuple(_REG for _ in range(5))
        delta = _make_delta(removed=regs, shrinkage_pct=0.30)
        assert delta.is_shrinkage_guarded() is True

    def test_is_shrinkage_guarded_below_floor(self) -> None:
        # 50% shrinkage but only 4 removals → floor not met
        regs = tuple(_REG for _ in range(4))
        delta = _make_delta(removed=regs, shrinkage_pct=0.50)
        assert delta.is_shrinkage_guarded() is False

    def test_is_shrinkage_guarded_below_threshold(self) -> None:
        regs = tuple(_REG for _ in range(10))
        delta = _make_delta(removed=regs, shrinkage_pct=0.10)
        assert delta.is_shrinkage_guarded() is False

    def test_custom_threshold(self) -> None:
        regs = tuple(_REG for _ in range(5))
        delta = _make_delta(removed=regs, shrinkage_pct=0.15)
        assert delta.is_shrinkage_guarded(threshold_pct=0.10, floor=3) is True


# ---------------------------------------------------------------------------
# HydrationResult (generic)
# ---------------------------------------------------------------------------


class TestHydrationResult:
    def test_generic_with_none_resources(self) -> None:
        result: HydrationResult[None] = HydrationResult(
            channel="ado",
            resources=None,
            api_call_count=0,
            errors=(),
            hydrated_ref_ids=(),
            failed_ref_ids=(),
        )
        assert result.resources is None
        assert result.channel == "ado"

    def test_generic_with_typed_resources(self) -> None:
        output = TeamsHydrationOutput(meeting_events=(), thread_messages=())
        result: HydrationResult[TeamsHydrationOutput] = HydrationResult(
            channel="teams",
            resources=output,
            api_call_count=3,
            errors=(),
            hydrated_ref_ids=(("abc", "meeting_series"),),
            failed_ref_ids=(),
        )
        assert result.resources is output
        assert result.api_call_count == 3


# ---------------------------------------------------------------------------
# ExtractionResult
# ---------------------------------------------------------------------------


class TestExtractionResult:
    def test_construction(self) -> None:
        result = ExtractionResult(
            channel="teams",
            signals=(),
            trajectory_points=(),
            side_artifacts={},
            errors=(),
        )
        assert result.channel == "teams"
        assert result.signals == ()

    def test_frozen(self) -> None:
        result = ExtractionResult(
            channel="ado",
            signals=(),
            trajectory_points=(),
            side_artifacts={},
            errors=(),
        )
        with pytest.raises(AttributeError):
            result.channel = "icm"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TeamsHydrationOutput
# ---------------------------------------------------------------------------


class TestTeamsHydrationOutput:
    def test_empty_output(self) -> None:
        out = TeamsHydrationOutput(meeting_events=(), thread_messages=())
        assert out.meeting_events == ()
        assert out.thread_messages == ()

    def test_with_meeting_event(self) -> None:
        event = MeetingEvent(
            event_id="evt-1",
            series_id="series-1",
            thread_id="thread-1",
            title="Weekly Sync",
            started_at=_NOW,
            ended_at=None,
            organizer="pm@example.com",
        )
        out = TeamsHydrationOutput(meeting_events=(event,), thread_messages=())
        assert len(out.meeting_events) == 1
        assert out.meeting_events[0].event_id == "evt-1"

    def test_with_thread_message(self) -> None:
        msg = ThreadMessage(
            message_id="msg-1",
            thread_id="thread-abc",
            sender="user@example.com",
            sent_at=_NOW,
            text="Hello world",
            permalink="https://teams.microsoft.com/l/message/abc",
        )
        out = TeamsHydrationOutput(meeting_events=(), thread_messages=(msg,))
        assert len(out.thread_messages) == 1
        assert out.thread_messages[0].text == "Hello world"


# ---------------------------------------------------------------------------
# ThreadMessage
# ---------------------------------------------------------------------------


class TestThreadMessage:
    def test_required_fields(self) -> None:
        msg = ThreadMessage(
            message_id="m1",
            thread_id="t1",
            sender=None,
            sent_at=_NOW,
            text="",
        )
        assert msg.message_id == "m1"
        assert msg.thread_id == "t1"
        assert msg.permalink is None
        assert msg.workstream_ids == ()
        assert msg.work_item_ids == ()

    def test_with_workstream_ids(self) -> None:
        msg = ThreadMessage(
            message_id="m2",
            thread_id="t2",
            sender="s@s.com",
            sent_at=_NOW,
            text="hi",
            workstream_ids=("ws-1", "ws-2"),
        )
        assert msg.workstream_ids == ("ws-1", "ws-2")

    def test_frozen(self) -> None:
        msg = ThreadMessage(message_id="m", thread_id="t", sender=None, sent_at=_NOW, text="")
        with pytest.raises(AttributeError):
            msg.sender = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# IcMHydrationOutput
# ---------------------------------------------------------------------------


class TestIcMHydrationOutput:
    def test_empty_output(self) -> None:
        out = IcMHydrationOutput(incident_states=())
        assert out.incident_states == ()

    def test_with_incident(self) -> None:
        state = IncidentState(
            incident_id="12345",
            title="Disk full",
            severity=1,
            status="Active",
            owning_team="StoragePM",
            updated_at=_NOW,
        )
        out = IcMHydrationOutput(incident_states=(state,))
        assert len(out.incident_states) == 1
        assert out.incident_states[0].incident_id == "12345"


# ---------------------------------------------------------------------------
# IncidentState
# ---------------------------------------------------------------------------


class TestIncidentState:
    def test_required_fields(self) -> None:
        state = IncidentState(
            incident_id="999",
            title=None,
            severity=None,
            status="",
            owning_team=None,
            updated_at=_NOW,
        )
        assert state.incident_id == "999"
        assert state.workstream_ids == ()

    def test_with_all_fields(self) -> None:
        state = IncidentState(
            incident_id="1",
            title="Test",
            severity=0,
            status="Mitigated",
            owning_team="Team",
            updated_at=_NOW,
            workstream_ids=("ws-1",),
        )
        assert state.severity == 0
        assert state.workstream_ids == ("ws-1",)

    def test_frozen(self) -> None:
        state = IncidentState(
            incident_id="x",
            title=None,
            severity=None,
            status="",
            owning_team=None,
            updated_at=_NOW,
        )
        with pytest.raises(AttributeError):
            state.status = "Active"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Enum membership
# ---------------------------------------------------------------------------


from src.core.integration_types import (
    DiscoveryCompleteness,
    HydrationMode,
    RegistrationStatus,
    ScopeStatusKind,
)


class TestEnumMembership:
    def test_registration_status_values(self) -> None:
        assert set(RegistrationStatus) == {
            RegistrationStatus.ACTIVE,
            RegistrationStatus.STALE,
            RegistrationStatus.EXPIRED,
            RegistrationStatus.RETIRED,
            RegistrationStatus.SUPPRESSED,
        }
        assert RegistrationStatus.ACTIVE == "active"

    def test_discovery_completeness_values(self) -> None:
        assert set(DiscoveryCompleteness) == {
            DiscoveryCompleteness.FULL,
            DiscoveryCompleteness.INCREMENTAL,
            DiscoveryCompleteness.PARTIAL,
        }
        assert DiscoveryCompleteness.FULL == "full"

    def test_scope_status_kind_values(self) -> None:
        assert ScopeStatusKind.SUCCESS == "success"
        assert ScopeStatusKind.ERROR == "error"
        # All non-success statuses are treated as failure paths
        failure_kinds = {ScopeStatusKind.TIMEOUT, ScopeStatusKind.AUTH_ERROR, ScopeStatusKind.RATE_LIMITED, ScopeStatusKind.ERROR}
        assert ScopeStatusKind.SUCCESS not in failure_kinds

    def test_hydration_mode_values(self) -> None:
        assert HydrationMode.FULL == "full"
        assert HydrationMode.FRESHNESS_ONLY == "freshness_only"


# ---------------------------------------------------------------------------
# RunContext
# ---------------------------------------------------------------------------


from src.core.integration_types import RunContext


class TestRunContext:
    def test_defaults(self) -> None:
        ctx = RunContext()
        assert ctx.dry_run is False
        assert ctx.force_discovery is False
        assert ctx.accept_shrinkage is False

    def test_dry_run_flag(self) -> None:
        ctx = RunContext(dry_run=True)
        assert ctx.dry_run is True
        assert ctx.force_discovery is False

    def test_frozen(self) -> None:
        ctx = RunContext()
        with pytest.raises(AttributeError):
            ctx.dry_run = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProviderCapability
# ---------------------------------------------------------------------------


from src.core.integration_types import ProviderCapability


class TestProviderCapability:
    def test_construction(self) -> None:
        cap = ProviderCapability(
            channel="ado",
            discovery_modes=(DiscoveryCompleteness.FULL, DiscoveryCompleteness.INCREMENTAL),
            hydration_modes=(HydrationMode.FULL, HydrationMode.FRESHNESS_ONLY),
            supports_since=True,
            max_batch_size=200,
            rate_limit_rpm=600,
            retry_max_attempts=3,
            retry_backoff_seconds=1.5,
            privacy_class="internal",
            timeout_seconds=30,
        )
        assert cap.channel == "ado"
        assert cap.max_batch_size == 200
        assert cap.write_propose is False
        assert cap.write_apply is False

    def test_write_flags(self) -> None:
        cap = ProviderCapability(
            channel="ado",
            discovery_modes=(DiscoveryCompleteness.FULL,),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=False,
            max_batch_size=100,
            rate_limit_rpm=None,
            retry_max_attempts=1,
            retry_backoff_seconds=0.5,
            privacy_class="internal",
            timeout_seconds=15,
            write_propose=True,
            write_apply=True,
        )
        assert cap.write_propose is True
        assert cap.write_apply is True
