from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from src.commands.report_fetch import _work_item_from_sources
from src.core.ado_enrichment import ADO_RISK_ASSESSMENT_FIELD, infer_ado_risk_level
from src.core.ado_reconcile import build_ado_reconcile_report
from src.core.ado_hydration import (
    ADOHydrationConfig,
    ADOHydrationProvider,
    _normalize_ado_comment_html,
    _work_item_from_batch_row,
)
from src.core.config_loader import ScorecardDimensionSettings, ScorecardSettings
from src.core.exceptions import AuthError, CredentialExpired
from src.core.integration_types import ChannelRegistration, HydrationMode, PaginationOutcome, RegistrationStatus
from src.core.models import RiskLevel, WorkItem
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides


def _registration(ref_id: str, *, last_verified_at: datetime | None = None) -> ChannelRegistration:
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
        last_verified_at=last_verified_at,
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
                    "System.Tags": "EXAMPLETAG1; Blocked",
                },
            }
            for work_item_id in work_item_ids
        ]

    def list_work_item_revisions(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
        self.revision_calls.append(work_item_id)
        rows: list[dict[str, object]] = [
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
        callback = kwargs.get("on_pagination")
        if callable(callback):
            callback(PaginationOutcome(total_fetched=len(rows), page_count=1, is_truncated=False))
        return rows

    def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
        self.comment_calls.append(work_item_id)
        rows: list[dict[str, object]] = [
            {
                "id": 7,
                "createdBy": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                "createdDate": "2026-05-24T12:00:00Z",
                "text": "Ready for review",
            }
        ]
        callback = kwargs.get("on_pagination")
        if callable(callback):
            callback(PaginationOutcome(total_fetched=len(rows), page_count=1, is_truncated=False))
        return rows


class _IncompleteHistoryClient(_FakeADOClient):
    def __init__(self, failures: dict[str, str]) -> None:
        super().__init__()
        self.failures = failures

    def _history(
        self,
        kind: str,
        work_item_id: int,
        kwargs: dict[str, object],
        loader: Callable[..., list[dict[str, object]]],
    ) -> list[dict[str, object]]:
        failure = self.failures.get(kind) if work_item_id == 101 else None
        if failure is None:
            return loader(work_item_id, **kwargs)
        if failure == "query_error":
            from src.core.exceptions import QueryError

            calls = self.revision_calls if kind == "revisions" else self.comment_calls
            calls.append(work_item_id)
            raise QueryError("429 confidential-provider-detail")
        if failure in ("auth_error", "credential_expired"):
            calls = self.revision_calls if kind == "revisions" else self.comment_calls
            calls.append(work_item_id)
            if failure == "credential_expired":
                raise CredentialExpired("confidential-provider-detail", auth_method="ADO_PAT", connector="ADO")
            raise AuthError("confidential-provider-detail")
        if failure.startswith("timestamp_"):
            rows = loader(work_item_id, **kwargs)
            row = rows[0]
            if kind == "revisions":
                row["revisedDate"] = "2026-05-24T10:00:00Z"
                fields = row["fields"]
                assert isinstance(fields, dict)
                row = fields
                timestamp_key = "System.ChangedDate"
            else:
                timestamp_key = "createdDate"
            if failure == "timestamp_missing":
                del row[timestamp_key]
            else:
                row[timestamp_key] = {
                    "timestamp_none": None,
                    "timestamp_blank": " ",
                    "timestamp_type": 123,
                    "timestamp_invalid": "confidential-provider-detail",
                    "timestamp_overflow": "0001-01-01T00:00:00+14:00",
                }[failure]
            return rows
        rows = loader(work_item_id)
        if failure == "bool_count":
            rows = rows[:1]
        callback = kwargs.get("on_pagination")
        if callable(callback) and failure != "missing":
            if failure == "wrong_type":
                callback("untrusted outcome")
            else:
                total: Any = True if failure == "bool_count" else len(rows) + (failure == "count")
                pages: Any = True if failure == "bool_pages" else (0 if failure == "pages" else 1)
                truncated: Any = 1 if failure == "flag" else failure == "truncated"
                outcome = PaginationOutcome(
                    total_fetched=total,
                    page_count=pages,
                    is_truncated=truncated,
                )
                callback(outcome)
                if failure == "duplicate":
                    callback(outcome)
        return rows

    def list_work_item_revisions(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
        return self._history("revisions", work_item_id, kwargs, super().list_work_item_revisions)

    def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
        return self._history("comments", work_item_id, kwargs, super().list_work_item_comments)


def _risk_row(
    *,
    risk_assessment: str | None,
    state: str = "Active",
    tags: str = "",
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "System.Id": 990001,
        "System.WorkItemType": "Feature",
        "System.Title": "Synthetic risk assessment",
        "System.State": state,
        "System.Tags": tags,
    }
    if risk_assessment is not None:
        fields[ADO_RISK_ASSESSMENT_FIELD] = risk_assessment
    return {"id": 990001, "fields": fields}


def _hydrated_item(
    *,
    risk_assessment: str | None,
    state: str = "Active",
    tags: str = "",
) -> WorkItem:
    return _work_item_from_batch_row(
        _risk_row(risk_assessment=risk_assessment, state=state, tags=tags),
        revision_rows=[],
        comment_rows=[],
        fetched_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("risk_assessment", "expected"),
    [
        ("On Track", RiskLevel.LOW),
        ("At Risk", RiskLevel.MEDIUM),
        ("Off Track", RiskLevel.HIGH),
    ],
)
def test_hydration_maps_recognized_explicit_ado_risk_assessment(
    risk_assessment: str,
    expected: RiskLevel,
) -> None:
    row = _risk_row(risk_assessment=risk_assessment)
    item = _work_item_from_batch_row(
        row,
        revision_rows=[],
        comment_rows=[],
        fetched_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
    )
    report_item = _work_item_from_sources(
        {"id": 990001},
        row,
        datetime(2026, 10, 29, tzinfo=timezone.utc),
    )

    assert item.risk_level is expected
    assert item.risk_assessment == risk_assessment
    assert report_item.risk_level is expected
    assert item.risk_level is infer_ado_risk_level(
        item.state,
        item.tags,
        item.risk_assessment,
    )
    reconciliation = build_ado_reconcile_report(
        program_id="synthetic",
        items=(report_item,),
        workstreams=(),
        scorecards=(
            ScorecardSettings(
                name="Synthetic",
                dimensions=(
                    ScorecardDimensionSettings(
                        name="Release",
                        description=None,
                        ado_filter="",
                    ),
                ),
            ),
        ),
        overrides_document=OverridesDocument(
            issue_number=990001,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Synthetic",
                    dimensions=(DimensionOverride(name="Release", risk=expected),),
                ),
            ),
        ),
        open_claims=(),
    )
    assert reconciliation.discrepancies == ()


@pytest.mark.parametrize("risk_assessment", [None, "", "Pending"])
def test_hydration_keeps_missing_blank_and_unsupported_assessment_unknown(
    risk_assessment: str | None,
) -> None:
    item = _hydrated_item(risk_assessment=risk_assessment)

    assert item.risk_level is RiskLevel.UNKNOWN


@pytest.mark.parametrize(
    ("state", "tags", "risk_assessment", "expected"),
    [
        ("Closed", "", "Off Track", RiskLevel.DONE),
        ("Done", "", None, RiskLevel.DONE),
        ("Resolved", "", None, RiskLevel.DONE),
        ("Completed", "", None, RiskLevel.DONE),
        ("Removed", "", None, RiskLevel.DONE),
        ("Blocked", "", "On Track", RiskLevel.LOW),
        ("Active", "Blocked", "On Track", RiskLevel.LOW),
        ("Active", "Blocker", "On Track", RiskLevel.LOW),
        ("Active", "High Risk", "On Track", RiskLevel.LOW),
        ("Active", "Risk", "Off Track", RiskLevel.HIGH),
        ("Active", "At Risk", "Off Track", RiskLevel.HIGH),
        ("Active", "Blocked", None, RiskLevel.HIGH),
        ("Active", "Blocker", "Pending", RiskLevel.HIGH),
        ("Active", "High Risk", "", RiskLevel.HIGH),
        ("Active", "Risk", None, RiskLevel.MEDIUM),
        ("Active", "At Risk", "Pending", RiskLevel.MEDIUM),
        ("Active", "", "Pending", RiskLevel.UNKNOWN),
        ("Active", "Blocked", " on_track ", RiskLevel.LOW),
    ],
)
def test_ado_consumers_share_authored_assessment_and_fallback_precedence(
    state: str,
    tags: str,
    risk_assessment: str | None,
    expected: RiskLevel,
) -> None:
    item = _hydrated_item(state=state, tags=tags, risk_assessment=risk_assessment)
    row = _risk_row(risk_assessment=risk_assessment, state=state, tags=tags)
    report_item = _work_item_from_sources(
        {"id": 990001}, row, datetime(2026, 10, 29, tzinfo=timezone.utc),
    )

    assert item.risk_level is expected
    assert report_item.risk_level is expected
    assert infer_ado_risk_level(item.state, item.tags, item.risk_assessment) is expected


@pytest.mark.parametrize("with_known", [False, True])
def test_relation_quarantine_surfaces_without_failing_hydrated_items(with_known: bool) -> None:
    class _RelationClient(_FakeADOClient):
        def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
            relations = [{
                "rel": "Custom.Future-Forward",
                "url": "https://dev.azure.com/fixture/Project/_apis/wit/workItems/103",
            }]
            if with_known:
                relations.append({
                    "rel": "System.LinkTypes.Dependency-Forward",
                    "url": "https://dev.azure.com/fixture/Project/_apis/wit/workItems/102",
                })
            return [{"id": item_id, "relations": relations} for item_id in work_item_ids]

    result = ADOHydrationProvider(_RelationClient()).hydrate(
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo", ADOHydrationConfig(), mode=HydrationMode.FULL,
    )
    assert result.hydrated_ref_ids == (("101", "work_item"),)
    assert result.failed_ref_ids == ()
    assert result.resources.work_items[0].id == 101
    assert {edge.target_id for edge in result.resources.relations} == (
        {"102", "103"} if with_known else {"103"}
    )
    assert len(result.errors) == 1
    assert result.errors[0].stage == "relations"
    assert result.errors[0].retryable is False
    assert "Custom.Future-Forward" in result.errors[0].message


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


def test_ado_hydration_parallelizes_independent_initial_detail_with_isolated_clients() -> None:
    """Initial detail stays complete without serially exhausting the channel budget."""

    class _Tracker:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.factory_calls = 0

        def run(self) -> None:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.03)
            finally:
                with self.lock:
                    self.active -= 1

    tracker = _Tracker()

    class _IsolatedDetailClient(_FakeADOClient):
        def list_work_item_revisions(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
            tracker.run()
            return super().list_work_item_revisions(work_item_id, **kwargs)

        def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
            tracker.run()
            return super().list_work_item_comments(work_item_id, **kwargs)

    def _make_detail_client() -> _IsolatedDetailClient:
        with tracker.lock:
            tracker.factory_calls += 1
        return _IsolatedDetailClient()

    provider = ADOHydrationProvider(
        _FakeADOClient(),  # type: ignore[arg-type]
        detail_client_factory=_make_detail_client,  # type: ignore[arg-type]
    )
    result = provider.hydrate(
        (_registration("101"), _registration("102"), _registration("103")),
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(detail_max_workers=3),
        mode=HydrationMode.FULL,
    )

    assert tracker.max_active >= 2
    assert 2 <= tracker.factory_calls <= 3
    assert result.api_call_count == 7  # one batch plus revisions/comments for all three items
    assert result.hydrated_ref_ids == (("101", "work_item"), ("102", "work_item"), ("103", "work_item"))
    assert all(item.revisions and item.comments for item in result.resources.work_items)


def test_ado_hydration_skips_detail_fetch_when_unchanged_since_last_verification() -> None:
    """ADF-W2.2 (Section 8.4.1): a rolling `since` window (broad) must not
    force a re-fetch of an item that hasn't changed since ITS OWN last
    verification -- the fixture row's ChangedDate is 2026-05-24T10:00:00Z;
    last_verified_at is set later than that, so no detail fetch should occur
    even though `since` (passed to hydrate) is much older/broader."""
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101", last_verified_at=datetime(2026, 5, 24, 11, 0, tzinfo=timezone.utc)),),
        datetime(2026, 5, 1, tzinfo=timezone.utc),  # broad rolling lookback
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FULL,
    )

    assert client.revision_calls == []
    assert client.comment_calls == []
    assert result.resources.work_items[0].revisions == []
    assert result.resources.work_items[0].comments == []


def test_ado_hydration_fetches_detail_when_changed_since_last_verification() -> None:
    """Mirror case: last_verified_at predates the row's ChangedDate, so the
    item DID change since it was last verified -- detail is still fetched
    even though last_verified_at is more recent than the broad `since`."""
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101", last_verified_at=datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc)),),
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FULL,
    )

    assert client.revision_calls == [101]
    assert client.comment_calls == [101]
    assert len(result.resources.work_items[0].revisions) == 2


def test_ado_hydration_no_last_verified_at_falls_back_to_since() -> None:
    """A never-before-verified registration (fresh registration, first
    gather) has no watermark to prefer -- falls back to the caller's
    `since`, matching pre-ADF-W2.2 behavior exactly."""
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101"),),  # last_verified_at=None
        datetime(2026, 5, 23, tzinfo=timezone.utc),  # before the row's ChangedDate -> still "changed"
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FULL,
    )

    assert client.revision_calls == [101]
    assert len(result.resources.work_items[0].revisions) == 2


def test_ado_hydration_deep_first_fetches_detail_for_never_verified_item_even_when_row_predates_since() -> None:
    """ADF-W2.2 (Section 8.4.1): "on first registration ... fetch complete
    bounded revisions and comments" is unconditional -- it must not be gated
    by the same changed-since check used for already-verified items. The
    fixture row's ChangedDate is 2026-05-24T10:00:00Z; `since` here is
    2026-06-01 (AFTER the row's ChangedDate), so `_row_changed_since` alone
    would report "unchanged" and skip the fetch. But this registration has
    never been verified (`last_verified_at=None`), so this is genuinely its
    first-ever hydration -- deep-first must still fetch full detail
    regardless of the row looking "unchanged" relative to the window."""
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101"),),  # last_verified_at=None
        datetime(2026, 6, 1, tzinfo=timezone.utc),  # after the row's ChangedDate -> "unchanged" per the window
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FULL,
    )

    assert client.revision_calls == [101]
    assert client.comment_calls == [101]
    assert len(result.resources.work_items[0].revisions) == 2
    assert len(result.resources.work_items[0].comments) == 1


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


@pytest.mark.parametrize("kind", ["revisions", "comments"])
@pytest.mark.parametrize(
    "failure",
    [
        "truncated", "missing", "duplicate", "wrong_type", "count", "bool_count",
        "pages", "bool_pages", "flag", "query_error", "auth_error", "credential_expired",
        "timestamp_missing", "timestamp_none", "timestamp_blank", "timestamp_type",
        "timestamp_invalid", "timestamp_overflow",
    ],
)
def test_incomplete_history_preserves_independent_item_data(kind: str, failure: str) -> None:
    client = _IncompleteHistoryClient({kind: failure})
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101"), _registration("102")),
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(),
    )

    assert [item.id for item in result.resources.work_items] == [101, 102]
    item = result.resources.work_items[0]
    assert bool(item.revisions) is (kind != "revisions")
    assert bool(item.comments) is (kind != "comments")
    assert result.resources.freshness_items == result.resources.work_items
    assert result.hydrated_ref_ids == (("102", "work_item"),)
    assert result.failed_ref_ids == (("101", "work_item"),)
    assert len(result.errors) == 1
    assert result.errors[0].ref_id == "101" and result.errors[0].retryable
    assert kind in result.errors[0].message.lower()
    assert "confidential-provider-detail" not in repr(result.errors)
    if failure.startswith("timestamp_"):
        assert result.errors[0].message == f"Failed to hydrate work item 101 {kind}: invalid timestamp."
        assert result.api_call_count == 5
    if failure in ("auth_error", "credential_expired"):
        expected = "credential expired" if failure == "credential_expired" else "authentication failed"
        assert expected in result.errors[0].message
        assert result.errors[0].operator_action == "Refresh Azure DevOps credentials and retry hydration."
    assert client.revision_calls == [101, 102]
    assert client.comment_calls == [101, 102]


@pytest.mark.parametrize(
    "failures",
    [
        {"revisions": "truncated", "comments": "query_error"},
        {"revisions": "timestamp_missing", "comments": "timestamp_invalid"},
    ],
)
def test_two_incomplete_histories_report_each_failure_once_per_resource(failures: dict[str, str]) -> None:
    client = _IncompleteHistoryClient(failures)
    result = ADOHydrationProvider(client).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    assert [item.id for item in result.resources.work_items] == [101]
    assert result.resources.work_items[0].revisions == []
    assert result.resources.work_items[0].comments == []
    assert result.hydrated_ref_ids == ()
    assert result.failed_ref_ids == (("101", "work_item"),)
    assert len(result.errors) == 2
    assert "revisions" in result.errors[0].message.lower()
    assert "comments" in result.errors[1].message.lower()
    assert "confidential-provider-detail" not in repr(result.errors)


def test_two_history_auth_failures_preserve_item_and_report_each_failure() -> None:
    client = _IncompleteHistoryClient({"revisions": "credential_expired", "comments": "auth_error"})
    result = ADOHydrationProvider(client).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    assert [item.id for item in result.resources.work_items] == [101]
    assert not result.resources.work_items[0].revisions and not result.resources.work_items[0].comments
    assert result.hydrated_ref_ids == () and result.failed_ref_ids == (("101", "work_item"),)
    assert len(result.errors) == 2
    assert "revisions" in result.errors[0].message and "comments" in result.errors[1].message
    assert all(
        error.operator_action == "Refresh Azure DevOps credentials and retry hydration." for error in result.errors
    )
    assert "confidential-provider-detail" not in repr(result.errors)


def test_missing_comment_loader_does_not_claim_complete_history(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeADOClient()
    monkeypatch.setattr(client, "list_work_item_comments", None)
    result = ADOHydrationProvider(client).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    assert result.resources.work_items[0].revisions
    assert result.resources.work_items[0].comments == []
    assert result.hydrated_ref_ids == ()
    assert result.failed_ref_ids == (("101", "work_item"),)
    assert len(result.errors) == 1 and "comments" in result.errors[0].message.lower()


def test_partial_batch_failure_keeps_success_and_failure_references_disjoint() -> None:
    from src.core.exceptions import QueryError

    class _PartialBatchClient(_FakeADOClient):
        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            if work_item_ids == [102]:
                raise QueryError("429 confidential-provider-detail")
            return super().query_work_items_batch(work_item_ids, fields)

    result = ADOHydrationProvider(_PartialBatchClient()).hydrate(  # type: ignore[arg-type]
        (_registration("101"), _registration("102"), _registration("103")),
        datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig(batch_size=1),
    )

    assert [item.id for item in result.resources.work_items] == [101]
    assert result.hydrated_ref_ids == (("101", "work_item"),)
    assert result.failed_ref_ids == (("102", "work_item"), ("103", "work_item"))
    assert set(result.hydrated_ref_ids).isdisjoint(result.failed_ref_ids)
    assert "confidential-provider-detail" not in repr(result.errors)


def test_missing_batch_rows_do_not_claim_success() -> None:
    class _EmptyBatchClient(_FakeADOClient):
        def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            return []

    result = ADOHydrationProvider(_EmptyBatchClient()).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    assert result.resources.work_items == ()
    assert result.hydrated_ref_ids == ()
    assert result.failed_ref_ids == (("101", "work_item"),)


@pytest.mark.parametrize("kind", ["revisions", "comments"])
def test_actual_ado_client_page_cap_degrades_hydration(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.ado_client import ADOClient

    client = _FakeADOClient()
    real = object.__new__(ADOClient)
    real._rest_base_url = "https://example.invalid/"
    monkeypatch.setattr(real, "_request_json", lambda *_args, **_kwargs: {"value": [{"rev": 1, "fields": {}}]})
    monkeypatch.setattr(
        real,
        "_request_response",
        lambda *_args, **_kwargs: SimpleNamespace(
            headers={"x-ms-continuationtoken": "more"}, json=lambda: {"comments": [{"id": 1}]}
        ),
    )
    if kind == "revisions":
        def capped(work_item_id: int, **kwargs: object) -> list[dict[str, Any]]:
            return real.list_work_item_revisions(work_item_id, page_size=1, max_pages=1, **kwargs)

        monkeypatch.setattr(client, "list_work_item_revisions", capped)
    else:
        def capped(work_item_id: int, **kwargs: object) -> list[dict[str, Any]]:
            return real.list_work_item_comments(work_item_id, max_pages=1, **kwargs)

        monkeypatch.setattr(client, "list_work_item_comments", capped)

    result = ADOHydrationProvider(client).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    assert [item.id for item in result.resources.work_items] == [101]
    assert result.hydrated_ref_ids == ()
    assert result.failed_ref_ids == (("101", "work_item"),)
    assert len(result.errors) == 1 and "truncat" in result.errors[0].message.lower()
    assert bool(result.resources.work_items[0].revisions) is (kind != "revisions")
    assert bool(result.resources.work_items[0].comments) is (kind != "comments")


@pytest.mark.parametrize("kind", ["revisions", "comments"])
def test_actual_ado_client_401_degrades_history_without_provider_text(
    kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.ado_client import ADOClient

    client = _FakeADOClient()
    real = object.__new__(ADOClient)
    real._rest_base_url = "https://example.invalid/"
    real.auth_method = "pat"
    monkeypatch.setattr(
        real,
        "_request_with_progress",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=401, headers={"WWW-Authenticate": "confidential-provider-detail"}
        ),
    )
    monkeypatch.setattr(client, f"list_work_item_{kind}", getattr(real, f"list_work_item_{kind}"))
    result = ADOHydrationProvider(client).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    assert [item.id for item in result.resources.work_items] == [101]
    assert bool(result.resources.work_items[0].revisions) is (kind != "revisions")
    assert bool(result.resources.work_items[0].comments) is (kind != "comments")
    assert result.hydrated_ref_ids == () and result.failed_ref_ids == (("101", "work_item"),)
    assert len(result.errors) == 1 and "credential expired" in result.errors[0].message
    assert result.errors[0].operator_action == "Refresh Azure DevOps credentials and retry hydration."
    assert "confidential-provider-detail" not in repr(result.errors)


def test_complete_empty_histories_are_verified() -> None:
    class _EmptyHistoryClient(_FakeADOClient):
        def list_work_item_revisions(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
            callback = kwargs.get("on_pagination")
            if callable(callback):
                callback(PaginationOutcome(0, 1, False))
            return []

        def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
            callback = kwargs.get("on_pagination")
            if callable(callback):
                callback(PaginationOutcome(0, 1, False))
            return []

    result = ADOHydrationProvider(_EmptyHistoryClient()).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    assert result.errors == ()
    assert result.failed_ref_ids == ()
    assert result.hydrated_ref_ids == (("101", "work_item"),)
    assert result.resources.work_items[0].revisions == []
    assert result.resources.work_items[0].comments == []


@pytest.mark.parametrize(
    ("published", "created", "expected"),
    [
        ("2026-05-24T14:00:00+02:00", "confidential-provider-detail",
         datetime(2026, 5, 24, 12, tzinfo=timezone.utc)),
        (None, "2026-05-24T12:00:00Z", datetime(2026, 5, 24, 12, tzinfo=timezone.utc)),
        ("", "2026-05-24T12:00:00Z", datetime(2026, 5, 24, 12, tzinfo=timezone.utc)),
        (" ", "2026-05-24T12:00:00Z", None),
        ("confidential-provider-detail", "2026-05-24T12:00:00Z", None),
        (123, "2026-05-24T12:00:00Z", None),
    ],
)
def test_comment_timestamp_preserves_existing_precedence(
    published: object, created: object, expected: datetime | None,
) -> None:
    class _CommentDatesClient(_FakeADOClient):
        def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
            rows = super().list_work_item_comments(work_item_id, **kwargs)
            rows[0]["publishedDate"] = published
            rows[0]["createdDate"] = created
            return rows

    result = ADOHydrationProvider(_CommentDatesClient()).hydrate(  # type: ignore[arg-type]
        (_registration("101"),), datetime(2026, 5, 23, tzinfo=timezone.utc), "demo", ADOHydrationConfig()
    )

    if expected is None:
        assert result.failed_ref_ids == (("101", "work_item"),)
        assert result.resources.work_items[0].comments == []
        assert result.errors[0].message == "Failed to hydrate work item 101 comments: invalid timestamp."
    else:
        assert result.errors == ()
        assert result.hydrated_ref_ids == (("101", "work_item"),)
        assert result.resources.work_items[0].comments[0].created_date == expected


@pytest.mark.parametrize(
    ("kind", "failure"),
    [
        ("revisions", "truncated"),
        ("revisions", "timestamp_missing"),
        ("comments", "timestamp_missing"),
        ("revisions", "timestamp_invalid"),
        ("comments", "timestamp_invalid"),
    ],
)
def test_partial_history_retry_uses_real_registry_and_channel_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, failure: str,
) -> None:
    from src.commands.gather_pipeline import channel_runtime
    from src.core.ado_signal_extractor import ADOSignalExtractor
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelBinding,
        ChannelConfig,
        DiscoveredRef,
        DiscoveryCompleteness,
        DiscoveryResult,
        RegistrationBinding,
        RunContext,
    )

    now = datetime.now(timezone.utc)
    prior = datetime(2026, 5, 24, 9, tzinfo=timezone.utc)
    store = ChannelRegistryStore(tmp_path / "registry.sqlite3", "demo")
    scope = RegistrationBinding(
        workstream_id="demo.slice", scope_id="synthetic", source_type="manual_config",
        confidence=1.0, confidence_source="manual_config",
    )
    refs = tuple(
        DiscoveredRef(
            replace(_registration(ref_id, last_verified_at=prior), first_discovered_at=now, last_seen_at=now),
            (scope,),
        )
        for ref_id in ("101", "102")
    )
    store.apply_discovery_result(DiscoveryResult(
        channel="ado", program_id="demo", discovered_refs=refs, completeness=DiscoveryCompleteness.FULL,
        scope_statuses={}, scope_state_updates={}, errors=(), computed_at=now,
    ))
    client = _IncompleteHistoryClient({kind: failure})
    binding = ChannelBinding(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=SimpleNamespace(),
        discovery_config=SimpleNamespace(),
        hydration_provider=ADOHydrationProvider(client),  # type: ignore[arg-type]
        hydration_config=ADOHydrationConfig(),
        signal_extractor=ADOSignalExtractor(),
    )
    monkeypatch.setattr(ChannelRegistryStore, "is_discovery_stale", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", lambda *_args, **_kwargs: None)

    def run(*, dry_run: bool, since: datetime, verified_at: datetime) -> tuple[Any, Any, list[Any], list[Any]]:
        errors: list[Any] = []
        outcomes: list[Any] = []
        hydration, extraction, _ = channel_runtime.run_channel_with_extraction(
            binding, store, program_id="demo", since=since, verified_at=verified_at,
            run_ctx=RunContext(dry_run=dry_run), integration_error_sink=errors,
            channel_outcome_sink=outcomes, programs_root=tmp_path,
        )
        return hydration, extraction, errors, outcomes

    dry, _, dry_errors, dry_outcomes = run(
        dry_run=True, since=datetime(2026, 5, 1, tzinfo=timezone.utc), verified_at=now,
    )
    assert dry.failed_ref_ids == (("101", "work_item"),)
    assert dry_errors and dry_outcomes[0].degraded
    assert all(reg.last_verified_at == prior and reg.consecutive_hydration_failures == 0
               for reg in store.all_registrations("ado"))

    result, extraction, errors, outcomes = run(
        dry_run=False, since=datetime(2026, 5, 1, tzinfo=timezone.utc), verified_at=now,
    )
    registered = {reg.ref_id: reg for reg in store.all_registrations("ado")}
    assert result.failed_ref_ids == (("101", "work_item"),)
    assert result.hydrated_ref_ids == (("102", "work_item"),)
    assert registered["101"].last_verified_at == prior
    assert registered["101"].consecutive_hydration_failures == 1
    assert registered["102"].last_verified_at == now
    assert errors and outcomes[0].degraded
    signal_refs = {signal.raw_ref for signal in extraction.signals}
    assert any(ref.startswith("ado/comment/101/") for ref in signal_refs) is (kind != "comments")
    assert any(ref.startswith("ado/revision/101/") for ref in signal_refs) is (kind != "revisions")

    client.failures.clear()
    client.revision_calls.clear()
    client.comment_calls.clear()
    retry, extraction, errors, outcomes = run(
        dry_run=False, since=datetime(2026, 6, 1, tzinfo=timezone.utc), verified_at=now + timedelta(minutes=1),
    )
    registered = {reg.ref_id: reg for reg in store.all_registrations("ado")}
    assert client.revision_calls == [101] and client.comment_calls == [101]
    assert retry.failed_ref_ids == () and set(retry.hydrated_ref_ids) == {("101", "work_item"), ("102", "work_item")}
    assert registered["101"].last_verified_at == now + timedelta(minutes=1)
    assert registered["101"].consecutive_hydration_failures == 0
    assert errors == [] and not outcomes[0].degraded
    assert any(signal.raw_ref.startswith("ado/revision/101/") for signal in extraction.signals)


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


def test_normalize_ado_comment_html_strips_tags_preserves_link_target() -> None:
    raw = '<div>Please review <a href="https://dev.azure.com/org/proj/_workitems/edit/1234">Bug 1234</a> before EOD.</div>'
    normalized = _normalize_ado_comment_html(raw)
    assert "<" not in normalized
    assert "Bug 1234" in normalized
    assert "https://dev.azure.com/org/proj/_workitems/edit/1234" in normalized
    assert "Please review" in normalized
    assert "before EOD." in normalized


def test_normalize_ado_comment_html_strips_script_and_style_blocks() -> None:
    raw = "<style>.x{color:red}</style><script>alert(1)</script><p>Real comment text</p>"
    normalized = _normalize_ado_comment_html(raw)
    assert normalized == "Real comment text"


def test_normalize_ado_comment_html_unescapes_entities_and_collapses_whitespace() -> None:
    raw = "<p>Risk &amp; scope   &gt; expected</p>\n<p>See @mention</p>"
    normalized = _normalize_ado_comment_html(raw)
    assert normalized == "Risk & scope > expected See @mention"


def test_normalize_ado_comment_html_empty_input_is_unchanged() -> None:
    assert _normalize_ado_comment_html("") == ""


def test_ado_hydration_strips_html_from_comment_text() -> None:
    class _HtmlCommentClient(_FakeADOClient):
        def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
            self.comment_calls.append(work_item_id)
            rows: list[dict[str, object]] = [
                {
                    "id": 7,
                    "createdBy": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                    "createdDate": "2026-05-24T12:00:00Z",
                    "text": '<div>Ready for review — see <a href="https://example/wi/1234">WI 1234</a></div>',
                }
            ]
            callback = kwargs.get("on_pagination")
            if callable(callback):
                callback(PaginationOutcome(total_fetched=len(rows), page_count=1, is_truncated=False))
            return rows

    client = _HtmlCommentClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]

    result = provider.hydrate(
        (_registration("101"),),
        datetime(2026, 5, 23, tzinfo=timezone.utc),
        "demo",
        ADOHydrationConfig(),
        mode=HydrationMode.FULL,
    )

    comment_text = result.resources.work_items[0].comments[0].text
    assert "<" not in comment_text
    assert "WI 1234" in comment_text
    assert "https://example/wi/1234" in comment_text


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
    """`since` scoping: an ALREADY-VERIFIED item not changed after its own
    last verification does not trigger revision/comment fetches. (Must use
    an already-verified registration here -- a never-verified registration
    always gets deep-first full detail regardless of `since`, per the
    ADF-W2.2 fix below; see
    test_ado_hydration_deep_first_fetches_detail_for_never_verified_item_even_when_row_predates_since.)"""
    client = _FakeADOClient()
    provider = ADOHydrationProvider(client)  # type: ignore[arg-type]
    # The fake client returns ChangedDate = 2026-05-24T10:00:00Z for all items.
    # Set `since` to after that date → item counts as unchanged → no revision/comment calls.
    since_after_change = datetime(2026, 5, 25, tzinfo=timezone.utc)

    result = provider.hydrate(
        (_registration("101", last_verified_at=datetime(2026, 5, 24, 11, 0, tzinfo=timezone.utc)),),
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
