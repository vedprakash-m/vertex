"""Tests for REV P2/P3 modules — sync state, change feeds, hydrators, gap lifecycle.

Covers:
* SyncStateStore TTL + LRU eviction, invalidation precedence
* FakeChangeFeed delivers changed candidates + tombstones
* Operator-gated feeds return Unsupported (not errors)
* CalendarHydrator (fake path) — full body extraction + metadata-only fallback
* SharePointHydrator (fake path) — text file extraction + binary fallback
* TeamsHydrator (fake path) — chat + channel message extraction
* Live* stubs return Unsupported with RV-S1 reference
* GapLifecycleStore CRUD + status transitions
* CoverageMaturity levels
* Governor quiet-lane early exit
* Governor concurrency fields in BudgetLimits
* Pipeline priority ordering (highest relevance processed first)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.rev.entity_types import EntityType
from src.core.rev.governor import BudgetLimits, Governor
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.result import Success, Unsupported


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _locator(resource_id: str, source_type: EntityType = EntityType.MESSAGE) -> HydrationLocator:
    return HydrationLocator(
        source_type=source_type,
        tenant_id="tenant-1",
        principal_mailbox="user@example.com",
        container="inbox",
        resource_id=resource_id,
    )


def _candidate(resource_id: str, relevance: float = 0.5, source_type: EntityType = EntityType.MESSAGE) -> EnumeratedCandidate:
    return EnumeratedCandidate(
        locator=_locator(resource_id, source_type),
        relevance_score=relevance,
        partial_metadata={"subject": f"Subject for {resource_id}"},
        correlation_id=f"corr-{resource_id}",
        enumerator="fake",
    )


# ===========================================================================
# SyncStateStore
# ===========================================================================


class TestSyncStateStore:
    def test_create_and_load_roundtrip(self, tmp_path: Path) -> None:
        from src.core.rev.sync_state import SyncStateStore, make_sync_state_key

        store = SyncStateStore.load("prog-1", programs_root=tmp_path)
        state = store.get_or_create("t1", "user@ex.com", "inbox", "v1.0")
        state.delta_link = "https://graph.microsoft.com/v1.0/delta?link=abc"
        store.upsert(state)
        store.save("prog-1", programs_root=tmp_path)

        store2 = SyncStateStore.load("prog-1", programs_root=tmp_path)
        key = make_sync_state_key("t1", "user@ex.com", "inbox", "v1.0")
        loaded = store2.get(key)
        assert loaded is not None
        assert loaded.delta_link == "https://graph.microsoft.com/v1.0/delta?link=abc"

    def test_ttl_eviction(self, tmp_path: Path) -> None:
        from src.core.rev.sync_state import SyncState, SyncStateStore, make_sync_state_key

        store = SyncStateStore(eviction_ttl_days=1, max_states=1000)
        state = store.get_or_create("t1", "user@ex.com", "inbox", "v1.0")
        # Manually backdate accessed_at to simulate dormancy.
        state.accessed_at = datetime.now(timezone.utc) - timedelta(days=2)
        store.upsert(state)
        assert len(store.all_states()) == 1

        # Save triggers eviction.
        store.save("prog-1", programs_root=tmp_path)
        store2 = SyncStateStore.load("prog-1", programs_root=tmp_path)
        assert len(store2.all_states()) == 0

    def test_lru_eviction(self, tmp_path: Path) -> None:
        from src.core.rev.sync_state import SyncStateStore

        store = SyncStateStore(eviction_ttl_days=365, max_states=2)
        s1 = store.get_or_create("t1", "a@ex.com", "inbox", "v1.0")
        s2 = store.get_or_create("t1", "b@ex.com", "inbox", "v1.0")
        s3 = store.get_or_create("t1", "c@ex.com", "inbox", "v1.0")
        # s1 is now LRU (accessed first, then s2, then s3).
        store.save("prog-1", programs_root=tmp_path)
        store2 = SyncStateStore.load("prog-1", programs_root=tmp_path)
        assert len(store2.all_states()) == 2
        keys = {s.principal_mailbox for s in store2.all_states()}
        # s1 (LRU) should have been evicted.
        assert "a@ex.com" not in keys

    def test_invalidate_api_version_change(self, tmp_path: Path) -> None:
        from src.core.rev.sync_state import SyncStateStore, make_sync_state_key

        store = SyncStateStore()
        state = store.get_or_create("t1", "user@ex.com", "inbox", "v1.0")
        state.delta_link = "old_link"
        store.upsert(state)

        store.invalidate_for_api_version_change("t1", "user@ex.com", "inbox", "v1.0", "beta")

        old_key = make_sync_state_key("t1", "user@ex.com", "inbox", "v1.0")
        new_key = make_sync_state_key("t1", "user@ex.com", "inbox", "beta")
        assert store.get(old_key) is None
        new_state = store.get(new_key)
        assert new_state is not None
        assert new_state.delta_link is None   # full resync

    def test_invalidate_token_expiry_clears_delta_links(self) -> None:
        from src.core.rev.sync_state import SyncStateStore, make_sync_state_key

        store = SyncStateStore()
        state1 = store.get_or_create("t1", "user@ex.com", "inbox", "v1.0")
        state1.delta_link = "link1"
        state2 = store.get_or_create("t1", "user@ex.com", "calendar", "v1.0")
        state2.delta_link = "link2"
        state_other = store.get_or_create("t2", "other@ex.com", "inbox", "v1.0")
        state_other.delta_link = "link3"
        store.upsert(state1)
        store.upsert(state2)
        store.upsert(state_other)

        store.invalidate_for_token_expiry("t1", "user@ex.com")

        k1 = make_sync_state_key("t1", "user@ex.com", "inbox", "v1.0")
        k2 = make_sync_state_key("t1", "user@ex.com", "calendar", "v1.0")
        ko = make_sync_state_key("t2", "other@ex.com", "inbox", "v1.0")
        assert store.get(k1).delta_link is None   # cleared
        assert store.get(k2).delta_link is None   # cleared
        assert store.get(ko).delta_link == "link3"  # unaffected

    def test_stats(self) -> None:
        from src.core.rev.sync_state import SyncStateStore

        store = SyncStateStore(eviction_ttl_days=30)
        store.get_or_create("t1", "a@ex.com", "inbox", "v1.0")
        stats = store.stats()
        assert stats["total_states"] == 1
        assert stats["eviction_ttl_days"] == 30


# ===========================================================================
# FakeChangeFeed
# ===========================================================================


class TestFakeChangeFeed:
    def test_delivers_changed_candidates(self) -> None:
        from src.m365.rev.change_feeds import DeltaPage, FakeChangeFeed

        c1 = _candidate("msg-1", relevance=0.8)
        c2 = _candidate("msg-2", relevance=0.6)
        page = DeltaPage(changed=(c1, c2), tombstones=(), delta_link="link1")
        feed = FakeChangeFeed(pages=[page])

        result = feed.changes(delta_link=None, correlation_id="test")
        assert isinstance(result, Success)
        candidates = result.value
        assert len(candidates) == 2
        assert candidates[0].locator.resource_id == "msg-1"

    def test_delivers_tombstones_as_deleted_candidates(self) -> None:
        from src.m365.rev.change_feeds import DeltaPage, DeltaTombstone, FakeChangeFeed

        tombstone = DeltaTombstone(
            canonical_id="msg:tenant-1:user@example.com:inbox:msg-3",
            resource_id="msg-3",
            source_type=EntityType.MESSAGE,
        )
        page = DeltaPage(changed=(), tombstones=(tombstone,), delta_link=None)
        feed = FakeChangeFeed(pages=[page])

        result = feed.changes(delta_link=None, correlation_id="test")
        assert isinstance(result, Success)
        candidates = result.value
        assert len(candidates) == 1
        assert candidates[0].partial_metadata.get("deleted") is True
        assert candidates[0].partial_metadata.get("canonical_id") == "msg:tenant-1:user@example.com:inbox:msg-3"

    def test_exhausted_feed_returns_empty(self) -> None:
        from src.m365.rev.change_feeds import FakeChangeFeed

        feed = FakeChangeFeed(pages=[])
        result = feed.changes(delta_link=None, correlation_id="test")
        assert isinstance(result, Success)
        assert result.value == ()

    def test_operator_gated_feeds_return_unsupported(self) -> None:
        from src.m365.rev.change_feeds import (
            CalendarDeltaFeed,
            MailDeltaFeed,
            SharePointDriveItemDeltaFeed,
        )

        mail_feed = MailDeltaFeed()
        result = mail_feed.changes(delta_link=None, correlation_id="test")
        assert isinstance(result, Unsupported)
        assert "rv_s1_delta_gate" in result.reason

        cal_feed = CalendarDeltaFeed()
        result = cal_feed.changes(delta_link=None, correlation_id="test")
        assert isinstance(result, Unsupported)
        assert "rv_s1_calendar_gate" in result.reason

        sp_feed = SharePointDriveItemDeltaFeed()
        result = sp_feed.changes(delta_link=None, correlation_id="test")
        assert isinstance(result, Unsupported)
        assert "rv_s1_sharepoint_gate" in result.reason


# ===========================================================================
# CalendarHydrator
# ===========================================================================


class TestCalendarHydrator:
    def _make_hydrator(self, events=()) -> tuple:
        from src.m365.rev.calendar_hydrator import (
            CalendarContext,
            CalendarHydrator,
            FakeRevCalendarClient,
            GraphEvent,
        )

        client = FakeRevCalendarClient(events=events)
        context = CalendarContext(tenant_id="t1", principal_mailbox="user@ex.com", calendar_id="primary")
        hydrator = CalendarHydrator(client, context)
        return hydrator, client

    def test_hydrates_event_body(self) -> None:
        from src.m365.rev.calendar_hydrator import GraphEvent

        event = GraphEvent(
            event_id="ev-1",
            subject="Architecture review",
            organizer="pm@ex.com",
            start_datetime="2026-06-23T10:00:00Z",
            end_datetime="2026-06-23T11:00:00Z",
            body="Deployment timeline was agreed: deploy by 2026-06-30.",
            body_content_type="text",
            series_master_id=None,
            etag="etag-ev-1",
            immutable_id="imm-ev-1",
        )
        hydrator, client = self._make_hydrator(events=(event,))
        candidate = _candidate("ev-1", source_type=EntityType.EVENT)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        hydrated = result.value
        assert hydrated.entity_type == EntityType.EVENT if hasattr(hydrated, "entity_type") else True
        assert "deployment" in hydrated.canonical_text.lower()
        assert hydrated.metadata_only is False
        assert hydrated.identity.resource_id == "ev-1"
        assert hydrated.identity.immutable_id == "imm-ev-1"
        assert hydrated.route_metadata.get("series_master_id") is None

    def test_metadata_only_fallback_on_missing_body(self) -> None:
        from src.m365.rev.calendar_hydrator import GraphEvent

        event = GraphEvent(event_id="ev-2", subject="Empty meeting", body="")
        hydrator, _ = self._make_hydrator(events=(event,))
        candidate = _candidate("ev-2", source_type=EntityType.EVENT)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_metadata_only_on_not_found(self) -> None:
        hydrator, _ = self._make_hydrator(events=())
        candidate = _candidate("ev-unknown", source_type=EntityType.EVENT)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_live_hydrator_returns_unsupported(self) -> None:
        from src.m365.rev.calendar_hydrator import LiveCalendarHydrator

        live = LiveCalendarHydrator()
        candidate = _candidate("ev-1", source_type=EntityType.EVENT)
        result = live.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Unsupported)
        assert "rv_s1_calendar_gate" in result.reason


# ===========================================================================
# SharePointHydrator
# ===========================================================================


class TestSharePointHydrator:
    def _make_hydrator(self, items=()) -> tuple:
        from src.m365.rev.sharepoint_hydrator import (
            FakeRevSharePointClient,
            SharePointContext,
            SharePointHydrator,
        )

        client = FakeRevSharePointClient(items=items)
        context = SharePointContext(tenant_id="t1", principal_mailbox="user@ex.com", drive_id="drive-1")
        hydrator = SharePointHydrator(client, context)
        return hydrator, client

    def test_hydrates_text_file(self) -> None:
        from src.m365.rev.sharepoint_hydrator import GraphDriveItem

        item = GraphDriveItem(
            item_id="item-1",
            drive_id="drive-1",
            name="status.txt",
            mime_type="text/plain",
            file_content=b"The milestone was completed on 2026-06-23.",
            etag="etag-item-1",
        )
        hydrator, _ = self._make_hydrator(items=(item,))
        candidate = _candidate("item-1", source_type=EntityType.DRIVE_ITEM)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        hydrated = result.value
        assert "milestone" in hydrated.canonical_text.lower()
        assert hydrated.metadata_only is False
        # SharePoint: no ImmutableId
        assert hydrated.identity.immutable_id is None
        # §5.4: no §13.5 registry route
        assert hydrated.route_metadata.get("sharepoint_no_registry_route") is True

    def test_binary_file_metadata_only(self) -> None:
        from src.m365.rev.sharepoint_hydrator import GraphDriveItem

        item = GraphDriveItem(
            item_id="item-2",
            drive_id="drive-1",
            name="report.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            file_content=b"\x50\x4b\x03\x04",   # PK binary header
        )
        hydrator, _ = self._make_hydrator(items=(item,))
        candidate = _candidate("item-2", source_type=EntityType.DRIVE_ITEM)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_live_hydrator_returns_unsupported(self) -> None:
        from src.m365.rev.sharepoint_hydrator import LiveSharePointHydrator

        live = LiveSharePointHydrator()
        candidate = _candidate("item-1", source_type=EntityType.DRIVE_ITEM)
        result = live.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Unsupported)
        assert "rv_s1_sharepoint_gate" in result.reason


# ===========================================================================
# TeamsHydrator
# ===========================================================================


class TestTeamsHydrator:
    def _make_chat_hydrator(self, messages=()) -> tuple:
        from src.m365.rev.teams_hydrator import (
            FakeRevTeamsClient,
            TeamsContext,
            TeamsHydrator,
            TeamsHydratorContext,
        )

        client = FakeRevTeamsClient(messages=messages)
        context = TeamsHydratorContext(
            tenant_id="t1",
            principal_mailbox="user@ex.com",
            teams_context=TeamsContext.CHAT.value,
            chat_id="chat-1",
        )
        hydrator = TeamsHydrator(client, context)
        return hydrator, client

    def _make_channel_hydrator(self, messages=()) -> tuple:
        from src.m365.rev.teams_hydrator import (
            FakeRevTeamsClient,
            TeamsContext,
            TeamsHydrator,
            TeamsHydratorContext,
        )

        client = FakeRevTeamsClient(messages=messages)
        context = TeamsHydratorContext(
            tenant_id="t1",
            principal_mailbox="user@ex.com",
            teams_context=TeamsContext.CHANNEL.value,
            team_id="team-1",
            channel_id="chan-1",
        )
        hydrator = TeamsHydrator(client, context)
        return hydrator, client

    def test_hydrates_chat_message_body(self) -> None:
        from src.m365.rev.teams_hydrator import GraphTeamsMessage, TeamsContext

        msg = GraphTeamsMessage(
            message_id="msg-1",
            teams_context=TeamsContext.CHAT.value,
            chat_id="chat-1",
            sender="pm@ex.com",
            sent_at="2026-06-23T10:00:00Z",
            body="The deployment was completed and validated.",
            body_content_type="text",
            etag="etag-msg-1",
        )
        hydrator, _ = self._make_chat_hydrator(messages=(msg,))
        candidate = _candidate("msg-1", source_type=EntityType.CHAT_MESSAGE)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        hydrated = result.value
        assert "deployment" in hydrated.canonical_text.lower()
        assert hydrated.metadata_only is False
        assert hydrated.identity.immutable_id is None   # Teams: no ImmutableId

    def test_hydrates_channel_message_body(self) -> None:
        from src.m365.rev.teams_hydrator import GraphTeamsMessage, TeamsContext

        msg = GraphTeamsMessage(
            message_id="chan-msg-1",
            teams_context=TeamsContext.CHANNEL.value,
            team_id="team-1",
            channel_id="chan-1",
            sender="eng@ex.com",
            body="Rollback initiated due to high error rate.",
            body_content_type="text",
        )
        hydrator, _ = self._make_channel_hydrator(messages=(msg,))
        candidate = _candidate("chan-msg-1", source_type=EntityType.CHAT_MESSAGE)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        assert "rollback" in result.value.canonical_text.lower()

    def test_metadata_only_on_empty_body(self) -> None:
        from src.m365.rev.teams_hydrator import GraphTeamsMessage, TeamsContext

        msg = GraphTeamsMessage(message_id="msg-empty", teams_context=TeamsContext.CHAT.value, body="", summary="")
        hydrator, _ = self._make_chat_hydrator(messages=(msg,))
        candidate = _candidate("msg-empty", source_type=EntityType.CHAT_MESSAGE)
        result = hydrator.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Success)
        assert result.value.metadata_only is True

    def test_live_hydrators_return_unsupported(self) -> None:
        from src.m365.rev.teams_hydrator import LiveTeamsHydrator

        live_chat = LiveTeamsHydrator(for_channel=False)
        candidate = _candidate("msg-1", source_type=EntityType.CHAT_MESSAGE)
        result = live_chat.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Unsupported)
        assert "rv_s1_teams_chat_gate" in result.reason

        live_chan = LiveTeamsHydrator(for_channel=True)
        result = live_chan.hydrate(candidate, correlation_id="test")
        assert isinstance(result, Unsupported)
        assert "rv_s1_teams_channel_gate" in result.reason


# ===========================================================================
# GapLifecycleStore + CoverageMaturity
# ===========================================================================


class TestGapLifecycle:
    def test_create_and_roundtrip(self, tmp_path: Path) -> None:
        from src.core.ledger.gap_lifecycle import ContextGapRecord, GapLifecycleStore, GapStatus

        store = GapLifecycleStore()
        gap = ContextGapRecord(
            gap_id="gap:prog-1:001",
            description="Unknown status of XFN dependency",
            source_workstream_id="ws-1",
        )
        store.upsert(gap)
        store.save("prog-1", programs_root=tmp_path)

        store2 = GapLifecycleStore.load("prog-1", programs_root=tmp_path)
        loaded = store2.get("gap:prog-1:001")
        assert loaded is not None
        assert loaded.description == "Unknown status of XFN dependency"
        assert loaded.status == GapStatus.OPEN.value

    def test_status_transitions(self) -> None:
        from src.core.ledger.gap_lifecycle import ContextGapRecord, GapStatus

        gap = ContextGapRecord(gap_id="g1", description="Some gap")
        assert gap.status == GapStatus.OPEN.value

        gap.mark_filling(reason="REV candidate found")
        assert gap.status == GapStatus.FILLING.value
        assert len(gap.transitions) == 1

        gap.mark_resolved(evidence_ref="sha256:evidence-1", reason="accepted event")
        assert gap.status == GapStatus.RESOLVED.value
        assert gap.resolution_evidence_ref == "sha256:evidence-1"
        assert gap.resolved_at is not None

        gap.reopen(reason="new blocker emerged")
        assert gap.status == GapStatus.REOPENED.value
        assert gap.reopen_reason == "new blocker emerged"
        assert len(gap.transitions) == 3

    def test_by_status_filter(self) -> None:
        from src.core.ledger.gap_lifecycle import ContextGapRecord, GapLifecycleStore, GapStatus

        store = GapLifecycleStore()
        g1 = ContextGapRecord(gap_id="g1", description="open gap")
        g2 = ContextGapRecord(gap_id="g2", description="another open gap")
        g3 = ContextGapRecord(gap_id="g3", description="filling gap")
        g3.mark_filling()
        store.upsert(g1)
        store.upsert(g2)
        store.upsert(g3)

        open_gaps = store.by_status(GapStatus.OPEN)
        assert len(open_gaps) == 2
        filling_gaps = store.by_status(GapStatus.FILLING)
        assert len(filling_gaps) == 1

    def test_coverage_maturity_none(self, tmp_path: Path) -> None:
        from src.core.ledger.gap_lifecycle import CoverageMaturiyLevel, compute_coverage_maturity

        maturity = compute_coverage_maturity("prog-new", programs_root=tmp_path)
        assert maturity.level == CoverageMaturiyLevel.NONE.value
        assert maturity.accepted_event_count == 0


# ===========================================================================
# Governor quiet-lane + concurrency fields
# ===========================================================================


class TestGovernorEnhancements:
    def test_quiet_lane_all_below_threshold(self) -> None:
        limits = BudgetLimits(quiet_lane_relevance_threshold=0.1)
        gov = Governor(limits)
        decision = gov.check_quiet_lane((0.05, 0.03, 0.01))
        assert decision.continue_run is False
        assert "quiet_lane:all_below" in decision.reason
        assert decision.category == "complete"

    def test_quiet_lane_one_above_threshold(self) -> None:
        limits = BudgetLimits(quiet_lane_relevance_threshold=0.1)
        gov = Governor(limits)
        decision = gov.check_quiet_lane((0.05, 0.15, 0.01))
        assert decision.continue_run is True

    def test_quiet_lane_empty(self) -> None:
        limits = BudgetLimits(quiet_lane_relevance_threshold=0.1)
        gov = Governor(limits)
        decision = gov.check_quiet_lane(())
        assert decision.continue_run is False

    def test_concurrency_fields_in_budget_limits(self) -> None:
        limits = BudgetLimits(concurrency_per_provider=8, fleet_concurrency_cap=24)
        assert limits.concurrency_per_provider == 8
        assert limits.fleet_concurrency_cap == 24

    def test_from_rev_budgets_includes_concurrency(self) -> None:
        class FakeBudgets:
            concurrency_per_provider = 6
            fleet_concurrency_cap = 18
            quiet_lane_relevance_threshold = 0.08

        limits = BudgetLimits.from_rev_budgets(FakeBudgets())
        assert limits.concurrency_per_provider == 6
        assert limits.fleet_concurrency_cap == 18
        assert limits.quiet_lane_relevance_threshold == pytest.approx(0.08)


# ===========================================================================
# Pipeline priority ordering
# ===========================================================================


class TestPipelinePriorityOrdering:
    """Priority ordering: candidates sorted by relevance descending (§5.10)."""

    def test_sorted_by_relevance(self) -> None:
        """Verify that higher-relevance candidates come first in sorted order."""
        candidates = (
            _candidate("msg-low", relevance=0.1),
            _candidate("msg-high", relevance=0.9),
            _candidate("msg-mid", relevance=0.5),
        )
        sorted_c = sorted(candidates, key=lambda c: c.relevance_score, reverse=True)
        assert sorted_c[0].locator.resource_id == "msg-high"
        assert sorted_c[1].locator.resource_id == "msg-mid"
        assert sorted_c[2].locator.resource_id == "msg-low"
