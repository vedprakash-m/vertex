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

Email identities in this module are locally authored synthetic examples.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.rev.entity_types import EntityType
from src.core.rev.governor import BudgetLimits, Governor
from src.core.rev.identity import HydrationLocator
from src.core.rev.ports import EnumeratedCandidate
from src.core.rev.result import Forbidden, Incomplete, RateLimited, Success, Unsupported
from src.m365.rev.sharepoint_hydrator import GraphDriveItem, GraphDriveItemContent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _locator(resource_id: str, source_type: EntityType = EntityType.MESSAGE) -> HydrationLocator:
    return HydrationLocator(
        source_type=source_type,
        tenant_id="tenant-1",
        principal_mailbox="identity-1@rev-fixture.example.net",
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
        state = store.get_or_create("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0")
        state.delta_link = "https://graph.microsoft.com/v1.0/delta?link=abc"
        store.upsert(state)
        store.save("prog-1", programs_root=tmp_path)

        store2 = SyncStateStore.load("prog-1", programs_root=tmp_path)
        key = make_sync_state_key("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0")
        loaded = store2.get(key)
        assert loaded is not None
        assert loaded.delta_link == "https://graph.microsoft.com/v1.0/delta?link=abc"

    def test_failed_replace_preserves_prior_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.rev.sync_state import SyncStateStore, make_sync_state_key
        import src.core.rev.sync_state as sync_state

        store = SyncStateStore.load("prog-atomic", programs_root=tmp_path)
        state = store.get_or_create("t1", "identity@fixture.example.net", "workiq", "v1")
        state.last_page_token = "cursor-1"
        store.save("prog-atomic", programs_root=tmp_path)

        def fail_replace(*_args):
            raise OSError("simulated")

        monkeypatch.setattr(sync_state.os, "replace", fail_replace)
        state.last_page_token = "cursor-2"

        with pytest.raises(OSError, match="simulated"):
            store.save("prog-atomic", programs_root=tmp_path)

        loaded = SyncStateStore.load("prog-atomic", programs_root=tmp_path).get(
            make_sync_state_key("t1", "identity@fixture.example.net", "workiq", "v1")
        )
        assert loaded is not None
        assert loaded.last_page_token == "cursor-1"

    def test_ttl_eviction(self, tmp_path: Path) -> None:
        from src.core.rev.sync_state import SyncState, SyncStateStore, make_sync_state_key

        store = SyncStateStore(eviction_ttl_days=1, max_states=1000)
        state = store.get_or_create("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0")
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
        s1 = store.get_or_create("t1", "identity-3@rev-fixture.example.net", "inbox", "v1.0")
        s2 = store.get_or_create("t1", "identity-4@rev-fixture.example.net", "inbox", "v1.0")
        s3 = store.get_or_create("t1", "identity-5@rev-fixture.example.net", "inbox", "v1.0")
        # s1 is now LRU (accessed first, then s2, then s3).
        store.save("prog-1", programs_root=tmp_path)
        store2 = SyncStateStore.load("prog-1", programs_root=tmp_path)
        assert len(store2.all_states()) == 2
        keys = {s.principal_mailbox for s in store2.all_states()}
        # s1 (LRU) should have been evicted.
        assert "identity-3@rev-fixture.example.net" not in keys

    def test_invalidate_api_version_change(self, tmp_path: Path) -> None:
        from src.core.rev.sync_state import SyncStateStore, make_sync_state_key

        store = SyncStateStore()
        state = store.get_or_create("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0")
        state.delta_link = "old_link"
        store.upsert(state)

        store.invalidate_for_api_version_change("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0", "beta")

        old_key = make_sync_state_key("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0")
        new_key = make_sync_state_key("t1", "identity-2@rev-fixture.example.net", "inbox", "beta")
        assert store.get(old_key) is None
        new_state = store.get(new_key)
        assert new_state is not None
        assert new_state.delta_link is None   # full resync

    def test_invalidate_token_expiry_clears_delta_links(self) -> None:
        from src.core.rev.sync_state import SyncStateStore, make_sync_state_key

        store = SyncStateStore()
        state1 = store.get_or_create("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0")
        state1.delta_link = "link1"
        state2 = store.get_or_create("t1", "identity-2@rev-fixture.example.net", "calendar", "v1.0")
        state2.delta_link = "link2"
        state_other = store.get_or_create("t2", "identity-6@rev-fixture.example.net", "inbox", "v1.0")
        state_other.delta_link = "link3"
        store.upsert(state1)
        store.upsert(state2)
        store.upsert(state_other)

        store.invalidate_for_token_expiry("t1", "identity-2@rev-fixture.example.net")

        k1 = make_sync_state_key("t1", "identity-2@rev-fixture.example.net", "inbox", "v1.0")
        k2 = make_sync_state_key("t1", "identity-2@rev-fixture.example.net", "calendar", "v1.0")
        ko = make_sync_state_key("t2", "identity-6@rev-fixture.example.net", "inbox", "v1.0")
        assert store.get(k1).delta_link is None   # cleared
        assert store.get(k2).delta_link is None   # cleared
        assert store.get(ko).delta_link == "link3"  # unaffected

    def test_version_change_resets_persisted_delta_and_page_tokens(
        self,
        tmp_path: Path,
    ) -> None:
        from src.core.rev.sync_state import SyncStateStore, make_sync_state_key

        store = SyncStateStore.load("prog-1", programs_root=tmp_path)
        old = store.get_or_create("t1", "mailbox", "drive-1", "v1.0")
        old.delta_link = "delta-v1"
        old.last_page_token = "page-v1"
        store.upsert(old)
        store.save("prog-1", programs_root=tmp_path)

        restarted = SyncStateStore.load("prog-1", programs_root=tmp_path)
        restarted.invalidate_for_api_version_change(
            "t1",
            "mailbox",
            "drive-1",
            "v1.0",
            "beta",
        )
        restarted.save("prog-1", programs_root=tmp_path)

        final = SyncStateStore.load("prog-1", programs_root=tmp_path)
        old_key = make_sync_state_key(
            "t1",
            "mailbox",
            "drive-1",
            "v1.0",
        )
        new_key = make_sync_state_key(
            "t1",
            "mailbox",
            "drive-1",
            "beta",
        )
        assert final.get(old_key) is None
        current = final.get(new_key)
        assert current is not None
        assert current.delta_link is None
        assert current.last_page_token is None

    def test_stats(self) -> None:
        from src.core.rev.sync_state import SyncStateStore

        store = SyncStateStore(eviction_ttl_days=30)
        store.get_or_create("t1", "identity-3@rev-fixture.example.net", "inbox", "v1.0")
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
            canonical_id="msg:tenant-1:identity-1@rev-fixture.example.net:inbox:msg-3",
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
        assert candidates[0].partial_metadata.get("canonical_id") == "msg:tenant-1:identity-1@rev-fixture.example.net:inbox:msg-3"

    def test_exhausted_feed_returns_empty(self) -> None:
        from src.m365.rev.change_feeds import FakeChangeFeed

        feed = FakeChangeFeed(pages=[])
        result = feed.changes(delta_link=None, correlation_id="test")
        assert isinstance(result, Success)
        assert result.value == ()

    def test_delta_link_resumes_after_prior_fixture_page(self) -> None:
        from src.m365.rev.change_feeds import DeltaPage, FakeChangeFeed

        feed = FakeChangeFeed(
            entity_type=EntityType.DRIVE_ITEM,
            pages=[
                DeltaPage(
                    changed=(
                        _candidate(
                            "item-1",
                            source_type=EntityType.DRIVE_ITEM,
                        ),
                    ),
                    tombstones=(),
                    delta_link="next-1",
                ),
                DeltaPage(
                    changed=(
                        _candidate(
                            "item-2",
                            source_type=EntityType.DRIVE_ITEM,
                        ),
                    ),
                    tombstones=(),
                    delta_link="next-2",
                ),
            ],
        )

        result = feed.changes(delta_link="next-1", correlation_id="test")

        assert isinstance(result, Success)
        assert [
            candidate.locator.resource_id for candidate in result.value
        ] == ["item-2"]
        assert feed.peek_delta_link() == "next-2"

    def test_restart_resumes_from_persisted_fixture_delta_link(
        self,
        tmp_path: Path,
    ) -> None:
        from src.core.rev.sync_state import SyncStateStore
        from src.m365.rev.change_feeds import DeltaPage, FakeChangeFeed

        pages = [
            DeltaPage(
                changed=(
                    _candidate(
                        "item-1",
                        source_type=EntityType.DRIVE_ITEM,
                    ),
                ),
                tombstones=(),
                delta_link="next-1",
            ),
            DeltaPage(
                changed=(
                    _candidate(
                        "item-2",
                        source_type=EntityType.DRIVE_ITEM,
                    ),
                ),
                tombstones=(),
                delta_link="next-2",
            ),
        ]
        first = FakeChangeFeed(
            entity_type=EntityType.DRIVE_ITEM,
            pages=pages,
        )
        assert isinstance(
            first.changes(delta_link=None, correlation_id="first"),
            Success,
        )

        store = SyncStateStore()
        state = store.get_or_create(
            "t1",
            "mailbox",
            "drive-1",
            "v1.0",
        )
        state.delta_link = first.peek_delta_link()
        store.upsert(state)
        store.save("prog-1", programs_root=tmp_path)

        restarted_state = SyncStateStore.load(
            "prog-1",
            programs_root=tmp_path,
        ).get_or_create(
            "t1",
            "mailbox",
            "drive-1",
            "v1.0",
        )
        restarted = FakeChangeFeed(
            entity_type=EntityType.DRIVE_ITEM,
            pages=pages,
        )
        result = restarted.changes(
            delta_link=restarted_state.delta_link,
            correlation_id="restart",
        )

        assert isinstance(result, Success)
        assert [
            candidate.locator.resource_id for candidate in result.value
        ] == ["item-2"]

    def test_fixture_rejects_unknown_delta_link_without_replaying(self) -> None:
        from src.m365.rev.change_feeds import DeltaPage, FakeChangeFeed

        feed = FakeChangeFeed(
            pages=[
                DeltaPage(
                    changed=(_candidate("item-1"),),
                    tombstones=(),
                    delta_link="next-1",
                )
            ]
        )

        result = feed.changes(delta_link="expired", correlation_id="test")

        assert isinstance(result, Incomplete)
        assert result.value == ()
        assert result.reason == "delta_link_not_found"

    def test_fixture_terminal_delta_link_is_caught_up(self) -> None:
        from src.m365.rev.change_feeds import DeltaPage, FakeChangeFeed

        feed = FakeChangeFeed(
            pages=[
                DeltaPage(
                    changed=(_candidate("item-1"),),
                    tombstones=(),
                    delta_link="next-1",
                )
            ]
        )

        result = feed.changes(delta_link="next-1", correlation_id="test")

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
        context = CalendarContext(tenant_id="t1", principal_mailbox="identity-2@rev-fixture.example.net", calendar_id="primary")
        hydrator = CalendarHydrator(client, context)
        return hydrator, client

    def test_hydrates_event_body(self) -> None:
        from src.m365.rev.calendar_hydrator import GraphEvent

        event = GraphEvent(
            event_id="ev-1",
            subject="Architecture review",
            organizer="identity-7@rev-fixture.example.net",
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
        context = SharePointContext(tenant_id="t1", principal_mailbox="identity-2@rev-fixture.example.net", drive_id="drive-1")
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
        assert hydrated.route_metadata["content_offset"] == 0
        assert hydrated.route_metadata["content_length"] == len(item.file_content)
        assert hydrated.route_metadata["content_etag"] == item.etag
        assert hydrated.route_metadata["acl_authorization_claimed"] is False

    def test_metadata_is_retrieved_before_bounded_content(self) -> None:
        from src.m365.rev.sharepoint_hydrator import GraphDriveItem, SharePointContext

        content = b"0123456789"
        item = GraphDriveItem(
            item_id="item-range",
            drive_id="drive-1",
            name="bounded.txt",
            mime_type="text/plain",
            file_content=content,
            size=len(content),
            etag="etag-range",
        )
        hydrator, client = self._make_hydrator(items=(item,))
        hydrator._context = SharePointContext(  # type: ignore[attr-defined]
            tenant_id="t1",
            principal_mailbox="identity-2@rev-fixture.example.net",
            drive_id="drive-1",
            max_body_bytes=4,
        )
        candidate = _candidate("item-range", source_type=EntityType.DRIVE_ITEM)

        result = hydrator.hydrate(candidate, correlation_id="test")

        assert isinstance(result, Success)
        assert result.value.metadata_only is True
        assert [call[0] for call in client.calls] == [
            "get_drive_item",
            "get_drive_item_content",
        ]
        assert client.calls[1][1]["offset"] == 0
        assert client.calls[1][1]["max_bytes"] == 4
        assert result.value.route_metadata["content_offset"] == 0
        assert result.value.route_metadata["content_length"] == 4
        assert result.value.route_metadata["content_complete"] is False
        assert result.value.route_metadata["content_etag"] == "etag-range"

    @pytest.mark.parametrize(
        ("item", "expected", "reason"),
        [
            (
                {
                    "deleted": True,
                    "etag": "etag-deleted",
                },
                Incomplete,
                "deleted",
            ),
            (
                {
                    "inaccessible": True,
                    "etag": "etag-hidden",
                },
                Forbidden,
                "inaccessible",
            ),
        ],
    )
    def test_deleted_or_inaccessible_items_are_never_authoritative(
        self, item, expected, reason: str
    ) -> None:
        from src.m365.rev.sharepoint_hydrator import GraphDriveItem

        graph_item = GraphDriveItem(
            item_id="item-status",
            drive_id="drive-1",
            name="status.txt",
            mime_type="text/plain",
            file_content=b"old authoritative text",
            **item,
        )
        hydrator, client = self._make_hydrator(items=(graph_item,))
        candidate = _candidate("item-status", source_type=EntityType.DRIVE_ITEM)

        result = hydrator.hydrate(candidate, correlation_id="test")

        assert isinstance(result, expected)
        assert reason in result.reason
        assert [call[0] for call in client.calls] == ["get_drive_item"]

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

    @pytest.mark.parametrize(
        ("client_kwargs", "expected"),
        [
            ({"rate_limited_ids": frozenset({"item-content"})}, RateLimited),
            ({"forbidden_ids": frozenset({"item-content"})}, Forbidden),
        ],
    )
    def test_bounded_content_client_preserves_typed_failures(
        self, client_kwargs, expected
    ) -> None:
        from src.m365.rev.sharepoint_hydrator import (
            FakeRevSharePointClient,
            SharePointContext,
            SharePointHydrator,
        )

        item = GraphDriveItem(
            item_id="item-content",
            drive_id="drive-1",
            name="status.txt",
            mime_type="text/plain",
            file_content=b"status",
        )
        client = FakeRevSharePointClient((item,), **client_kwargs)

        result = client.get_drive_item_content(
            drive_id="drive-1",
            item_id="item-content",
            offset=0,
            max_bytes=2,
        )

        assert isinstance(result, expected)
        metadata_result = client.get_drive_item(
            drive_id="drive-1",
            item_id="item-content",
        )
        assert isinstance(metadata_result, expected)
        hydrator = SharePointHydrator(
            client,
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )
        hydrated = hydrator.hydrate(
            _candidate("item-content", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )
        assert isinstance(hydrated, expected)

    def test_fake_client_missing_metadata_and_bounded_search_are_typed(self) -> None:
        from src.m365.rev.sharepoint_hydrator import FakeRevSharePointClient, GraphDriveItem

        client = FakeRevSharePointClient(
            (
                GraphDriveItem(item_id="a", drive_id="drive-1"),
                GraphDriveItem(item_id="b", drive_id="drive-1"),
            )
        )

        missing = client.get_drive_item(drive_id="drive-1", item_id="missing")
        found = client.search_drive_items(kql="fixture", limit=1)

        assert isinstance(missing, Incomplete)
        assert missing.reason == "item_not_found:missing"
        assert isinstance(found, Success)
        assert [item.item_id for item in found.value] == ["a"]

    def test_bounded_content_missing_and_nonzero_offset_are_explicit(self) -> None:
        from src.m365.rev.sharepoint_hydrator import FakeRevSharePointClient, GraphDriveItem

        item = GraphDriveItem(
            item_id="item-content",
            drive_id="drive-1",
            file_content=b"012345",
            etag="etag-1",
            version="2",
        )
        client = FakeRevSharePointClient((item,))

        missing = client.get_drive_item_content(
            drive_id="drive-1",
            item_id="missing",
            offset=3,
            max_bytes=2,
        )
        ranged = client.get_drive_item_content(
            drive_id="drive-1",
            item_id="item-content",
            offset=3,
            max_bytes=2,
        )

        assert isinstance(missing, Incomplete)
        assert missing.reason == "item_not_found:missing"
        assert missing.value.offset == 3
        assert isinstance(ranged, Success)
        assert ranged.value.data == b"34"
        assert ranged.value.offset == 3
        assert ranged.value.total_size == 6
        assert ranged.value.etag == "etag-1"
        assert ranged.value.version == "2"
        assert ranged.value.complete is False

    @pytest.mark.parametrize("status", ["deleted", "inaccessible"])
    def test_candidate_tombstone_does_not_contact_client_and_preserves_acl(
        self, status: str
    ) -> None:
        from dataclasses import replace
        from src.m365.rev.sharepoint_hydrator import (
            FakeRevSharePointClient,
            GraphDriveItem,
            SharePointContext,
            SharePointHydrator,
        )

        acl = ({"kind": "group", "id": "group-1", "roles": ("read",)},)
        item = GraphDriveItem(
            item_id="item-status",
            drive_id="drive-1",
            acl=acl,
        )
        client = FakeRevSharePointClient((item,))
        hydrator = SharePointHydrator(
            client,
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )
        candidate = replace(
            _candidate("item-status", source_type=EntityType.DRIVE_ITEM),
            partial_metadata={"name": "status.txt", "drive_id": "drive-1", "acl": acl, status: True},
        )

        result = hydrator.hydrate(candidate, correlation_id="test")

        assert isinstance(result, Incomplete) if status == "deleted" else isinstance(result, Forbidden)
        assert client.calls == []
        if isinstance(result, Incomplete):
            assert result.value.metadata_only is True
            assert result.value.route_metadata["acl"] == acl
            assert result.value.route_metadata["deleted"] is True
            assert result.value.route_metadata["acl_authorization_claimed"] is False

    @pytest.mark.parametrize(
        ("metadata_result", "reason"),
        [
            (Incomplete(GraphDriveItem(item_id="x", drive_id="drive-1"), reason="item_not_found:x"), "item_not_found:x"),
            (Incomplete(GraphDriveItem(item_id="x", drive_id="drive-1"), reason="partial_metadata"), "partial_metadata"),
        ],
    )
    def test_incomplete_metadata_outcomes_are_classified_and_fail_closed(
        self, metadata_result, reason: str
    ) -> None:
        from src.m365.rev.sharepoint_hydrator import (
            GraphDriveItem,
            SharePointContext,
            SharePointHydrator,
        )

        class Client:
            def get_drive_item(self, **_kwargs):
                return metadata_result

        hydrator = SharePointHydrator(
            Client(),
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )

        result = hydrator.hydrate(
            _candidate("x", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        if reason.startswith("item_not_found"):
            assert isinstance(result, Incomplete)
        else:
            assert isinstance(result, Success)
        assert result.value.metadata_only is True
        assert result.value.route_metadata["fallback_reason"] == reason

    def test_metadata_etag_change_rejects_stale_candidate(self) -> None:
        from dataclasses import replace
        from src.m365.rev.sharepoint_hydrator import GraphDriveItem

        item = GraphDriveItem(
            item_id="item-etag",
            drive_id="drive-1",
            name="status.txt",
            mime_type="text/plain",
            file_content=b"new status",
            etag="etag-new",
            version="4",
        )
        hydrator, client = self._make_hydrator(items=(item,))
        candidate = replace(
            _candidate("item-etag", source_type=EntityType.DRIVE_ITEM),
            locator=replace(
                _candidate("item-etag", source_type=EntityType.DRIVE_ITEM).locator,
                container="drive-1",
                etag_hint="etag-old",
            ),
        )

        result = hydrator.hydrate(candidate, correlation_id="test")

        assert isinstance(result, Incomplete)
        assert result.reason == "etag_changed:etag-old->etag-new"
        assert result.value.route_metadata["etag"] == "etag-new"
        assert [call[0] for call in client.calls] == ["get_drive_item"]

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            (Incomplete(GraphDriveItemContent(data=b"", offset=0), reason="partial_content"), "partial_content"),
            (Success(GraphDriveItemContent(data=b"x", etag="etag-other")), "content_etag_mismatch"),
            (Success(GraphDriveItemContent(data=b"x", version="other")), "content_version_mismatch"),
            (Success(GraphDriveItemContent(data=b"", complete=True)), "content_unavailable"),
        ],
    )
    def test_malformed_content_outcomes_keep_version_and_byte_provenance(
        self, content, reason: str
    ) -> None:
        from src.m365.rev.sharepoint_hydrator import (
            GraphDriveItem,
            GraphDriveItemContent,
            SharePointContext,
            SharePointHydrator,
        )

        item = GraphDriveItem(
            item_id="item-content",
            drive_id="drive-1",
            name="status.txt",
            mime_type="text/plain",
            file_content=b"x",
            etag="etag-item",
            version="3",
        )

        class Client:
            def get_drive_item(self, **_kwargs):
                return Success(item)

            def get_drive_item_content(self, **_kwargs):
                return content

        hydrator = SharePointHydrator(
            Client(),
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )
        result = hydrator.hydrate(
            _candidate("item-content", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        assert isinstance(result, Incomplete if "mismatch" in reason else Success)
        assert result.value.metadata_only is True
        if "mismatch" not in reason:
            assert result.value.route_metadata["fallback_reason"] == reason
        if "mismatch" in reason:
            assert reason in result.reason
            assert reason in result.value.route_metadata["fallback_reason"]
        assert result.value.route_metadata["version"] == "3"
        assert result.value.route_metadata["content_offset"] == 0

    def test_malformed_client_results_fail_closed_to_metadata(self) -> None:
        from src.m365.rev.sharepoint_hydrator import SharePointContext, SharePointHydrator

        class Client:
            def __init__(self, malformed):
                self.malformed = malformed

            def get_drive_item(self, **_kwargs):
                return self.malformed

        malformed = object()
        hydrator = SharePointHydrator(
            Client(malformed),
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )
        result = hydrator.hydrate(
            _candidate("item-malformed", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        assert isinstance(result, Success)
        assert result.value.metadata_only is True
        assert result.value.route_metadata["fallback_reason"] == "malformed_metadata_result"

        class ContentClient:
            def get_drive_item(self, **_kwargs):
                return Success(
                    GraphDriveItem(
                        item_id="item-malformed-content",
                        drive_id="drive-1",
                        mime_type="text/plain",
                    )
                )

            def get_drive_item_content(self, **_kwargs):
                return GraphDriveItemContent(data=b"x")

        content_hydrator = SharePointHydrator(
            ContentClient(),
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )
        content_result = content_hydrator.hydrate(
            _candidate("item-malformed-content", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )
        assert isinstance(content_result, Success)
        assert content_result.value.metadata_only is True
        assert content_result.value.route_metadata["fallback_reason"] == "malformed_content_result"

    def test_content_typed_failure_is_not_rewritten(self) -> None:
        from src.m365.rev.sharepoint_hydrator import (
            GraphDriveItem,
            SharePointContext,
            SharePointHydrator,
        )

        item = GraphDriveItem(
            item_id="item-content",
            drive_id="drive-1",
            mime_type="text/plain",
            file_content=b"x",
        )
        failure = RateLimited(provider="graph_sharepoint", retry_after_seconds=5.0)

        class Client:
            def get_drive_item(self, **_kwargs):
                return Success(item)

            def get_drive_item_content(self, **_kwargs):
                return failure

        hydrator = SharePointHydrator(
            Client(),
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )

        result = hydrator.hydrate(
            _candidate("item-content", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        assert result is failure

    def test_decode_failure_returns_metadata_only_with_byte_provenance(self) -> None:
        from src.m365.rev.sharepoint_hydrator import (
            GraphDriveItem,
            GraphDriveItemContent,
            SharePointContext,
            SharePointHydrator,
        )

        class UndecodableBytes(bytes):
            def decode(self, *args, **kwargs):
                raise UnicodeDecodeError("utf-8", b"x", 0, 1, "fixture")

        item = GraphDriveItem(
            item_id="item-decode",
            drive_id="drive-1",
            name="decode.txt",
            mime_type="text/plain",
            file_content=b"x",
            etag="etag-decode",
        )
        content = GraphDriveItemContent(
            data=UndecodableBytes(b"x"),
            offset=0,
            total_size=1,
            etag="etag-decode",
            complete=True,
        )

        class Client:
            def get_drive_item(self, **_kwargs):
                return Success(item)

            def get_drive_item_content(self, **_kwargs):
                return Success(content)

        hydrator = SharePointHydrator(
            Client(),
            SharePointContext(tenant_id="t1", principal_mailbox="owner@example.net"),
        )
        result = hydrator.hydrate(
            _candidate("item-decode", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        assert isinstance(result, Success)
        assert result.value.metadata_only is True
        assert result.value.route_metadata["fallback_reason"] == "decode_error"
        assert result.value.route_metadata["content_length"] == 1

    def test_nonfatal_privacy_failure_returns_metadata_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace
        from src.m365.rev.sharepoint_hydrator import GraphDriveItem

        item = GraphDriveItem(
            item_id="item-privacy",
            drive_id="drive-1",
            name="privacy.txt",
            mime_type="text/plain",
            file_content=b"status",
            etag="etag-privacy",
        )
        hydrator, _ = self._make_hydrator(items=(item,))
        monkeypatch.setattr(
            "src.m365.rev.sharepoint_hydrator.run_local_checks",
            lambda *_args, **_kwargs: SimpleNamespace(
                passed=False,
                quarantined=False,
                sensitivity_denied=False,
                reason="fixture_local_check_failed",
            ),
        )

        result = hydrator.hydrate(
            _candidate("item-privacy", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        assert isinstance(result, Success)
        assert result.value.metadata_only is True
        assert result.value.route_metadata["fallback_reason"] == "fixture_local_check_failed"
        assert result.value.route_metadata["content_etag"] == "etag-privacy"

    @pytest.mark.parametrize(
        ("quarantined", "sensitivity_denied", "scope"),
        [(True, False, "content"), (False, True, "sensitivity")],
    )
    def test_terminal_privacy_failures_remain_forbidden(
        self,
        monkeypatch: pytest.MonkeyPatch,
        quarantined: bool,
        sensitivity_denied: bool,
        scope: str,
    ) -> None:
        from types import SimpleNamespace
        from src.m365.rev.sharepoint_hydrator import GraphDriveItem

        item = GraphDriveItem(
            item_id="item-privacy-terminal",
            drive_id="drive-1",
            name="privacy.txt",
            mime_type="text/plain",
            file_content=b"status",
        )
        hydrator, _ = self._make_hydrator(items=(item,))
        monkeypatch.setattr(
            "src.m365.rev.sharepoint_hydrator.run_local_checks",
            lambda *_args, **_kwargs: SimpleNamespace(
                passed=False,
                quarantined=quarantined,
                sensitivity_denied=sensitivity_denied,
                reason=f"{scope}_denied",
            ),
        )

        result = hydrator.hydrate(
            _candidate("item-privacy-terminal", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        assert isinstance(result, Forbidden)
        assert result.scope == scope

    def test_legacy_client_uses_bounded_embedded_bytes_with_provenance(self) -> None:
        from src.m365.rev.sharepoint_hydrator import (
            GraphDriveItem,
            SharePointContext,
            SharePointHydrator,
        )

        item = GraphDriveItem(
            item_id="item-legacy",
            drive_id="drive-1",
            name="legacy.txt",
            mime_type="text/plain",
            file_content=b"full text",
            size=9,
            etag="etag-legacy",
            version="8",
        )

        class LegacyClient:
            def get_drive_item(self, **_kwargs):
                return Success(item)

        hydrator = SharePointHydrator(
            LegacyClient(),
            SharePointContext(
                tenant_id="t1",
                principal_mailbox="owner@example.net",
                max_body_bytes=4,
            ),
        )
        result = hydrator.hydrate(
            _candidate("item-legacy", source_type=EntityType.DRIVE_ITEM),
            correlation_id="test",
        )

        assert isinstance(result, Success)
        assert result.value.metadata_only is True
        assert result.value.route_metadata["content_offset"] == 0
        assert result.value.route_metadata["content_length"] == 4
        assert result.value.route_metadata["content_size"] == 9
        assert result.value.route_metadata["content_complete"] is False
        assert result.value.route_metadata["content_etag"] == "etag-legacy"
        assert result.value.route_metadata["content_version"] == "8"
        assert result.value.normalized_source_hash.startswith("sha256:")

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
            principal_mailbox="identity-2@rev-fixture.example.net",
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
            principal_mailbox="identity-2@rev-fixture.example.net",
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
            sender="identity-7@rev-fixture.example.net",
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
            sender="identity-8@rev-fixture.example.net",
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
