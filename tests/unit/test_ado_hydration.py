from __future__ import annotations

from datetime import datetime, timezone

from src.core.ado_hydration import ADOHydrationConfig, ADOHydrationProvider
from src.core.integration_types import ChannelRegistration, HydrationMode, RegistrationStatus


def _registration(ref_id: str) -> ChannelRegistration:
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    return ChannelRegistration(
        channel="ado",
        program_id="demo",
        provider_instance_id="default",
        ref_id=ref_id,
        ref_kind="work_item",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=now,
        last_seen_at=now,
        workstream_ids=("demo.slice",),
    )


class _FakeADOClient:
    def __init__(self) -> None:
        self.batch_calls: list[tuple[int, ...]] = []
        self.revision_calls: list[int] = []
        self.comment_calls: list[int] = []

    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
        del fields
        self.batch_calls.append(tuple(work_item_ids))
        return [
            {
                "id": work_item_id,
                "fields": {
                    "System.Id": work_item_id,
                    "System.WorkItemType": "Feature",
                    "System.Title": f"Item {work_item_id}",
                    "System.State": "Active",
                    "System.AreaPath": "One\\Demo",
                    "System.IterationPath": "One\\Iteration",
                    "System.ChangedDate": "2026-05-24T10:00:00Z",
                    "System.AssignedTo": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                    "System.Tags": "RAMPP1; Blocked",
                },
            }
            for work_item_id in work_item_ids
        ]

    def list_work_item_revisions(self, work_item_id: int) -> list[dict[str, object]]:
        self.revision_calls.append(work_item_id)
        return [
            {
                "rev": 1,
                "fields": {
                    "System.ChangedDate": "2026-05-24T10:00:00Z",
                    "System.ChangedBy": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                    "System.State": "Active",
                },
            },
            {
                "rev": 2,
                "fields": {
                    "System.ChangedDate": "2026-05-24T11:00:00Z",
                    "System.ChangedBy": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                    "System.State": "Closed",
                },
            },
        ]

    def list_work_item_comments(self, work_item_id: int) -> list[dict[str, object]]:
        self.comment_calls.append(work_item_id)
        return [
            {
                "id": 7,
                "createdBy": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                "createdDate": "2026-05-24T12:00:00Z",
                "text": "Ready for review",
            }
        ]


def test_ado_hydration_fetches_batch_and_changed_item_detail() -> None:
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101"),),
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FULL,
    )

    assert result.api_call_count == 3
    assert result.hydrated_ref_ids == (("101", "work_item"),)
    assert result.resources.work_items[0].custom_fields["workstream_ids"] == ("demo.slice",)
    assert len(result.resources.work_items[0].revisions) == 2
    assert len(result.resources.work_items[0].comments) == 1
    assert client.batch_calls == [(101,)]
    assert client.revision_calls == [101]
    assert client.comment_calls == [101]


def test_ado_hydration_freshness_only_skips_revisions_and_comments() -> None:
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101"),),
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FRESHNESS_ONLY,
    )

    assert result.api_call_count == 1
    assert result.resources.freshness_items == result.resources.work_items
    assert client.revision_calls == []
    assert client.comment_calls == []


def test_ado_hydration_empty_registry_returns_empty_result() -> None:
    """Empty registration list returns HydrationResult with zero items and no API calls."""
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (),
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(),
    )

    assert result.api_call_count == 0
    assert result.resources.work_items == ()
    assert result.resources.freshness_items == ()
    assert result.hydrated_ref_ids == ()
    assert result.failed_ref_ids == ()
    assert client.batch_calls == []


def test_ado_hydration_tracks_failed_ref_ids_on_batch_error() -> None:
    """When batch fetch raises QueryError, all ref_ids go to failed_ref_ids."""
    from src.core.ado_client import QueryError

    class _ErrorClient(_FakeADOClient):
        def query_work_items_batch(self, work_item_ids, fields):
            raise QueryError("network timeout")

    provider = ADOHydrationProvider(_ErrorClient())  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("201"), _registration("202")),
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(),
    )

    assert result.hydrated_ref_ids == ()
    assert set(result.failed_ref_ids) == {("201", "work_item"), ("202", "work_item")}
    assert len(result.errors) == 1
    assert result.errors[0].retryable is True


def test_ado_hydration_since_skips_revisions_for_unchanged_items() -> None:
    """`since` scoping: items not changed after `since` do not trigger revision/comment fetches."""
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]
    # The fake client returns ChangedDate = 2026-05-24T10:00:00Z for all items.
    # Set `since` to after that date → item counts as unchanged → no revision/comment calls.
    since_after_change = datetime(2026, 5, 25, tzinfo=timezone.utc)

    result = provider.hydrate(
        (_registration("101"),),
        since_after_change,
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FULL,
    )

    assert result.hydrated_ref_ids == (("101", "work_item"),)
    assert client.revision_calls == [], "revision fetch should be skipped for unchanged item"
    assert client.comment_calls == [], "comment fetch should be skipped for unchanged item"
    # api_call_count is just the batch fetch (1), not 3
    assert result.api_call_count == 1
