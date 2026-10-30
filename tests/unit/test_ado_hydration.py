from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

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
from src.core.integration_types import ChannelRegistration, HydrationMode, RegistrationStatus
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
        del kwargs  # ADF-W2.1: real client now accepts on_pagination/page_size/max_pages
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

    def list_work_item_comments(self, work_item_id: int, **kwargs: object) -> list[dict[str, object]]:
        del kwargs  # ADF-W2.1: real client now accepts on_pagination/max_pages
        self.comment_calls.append(work_item_id)
        return [
            {
                "id": 7,
                "createdBy": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                "createdDate": "2026-05-24T12:00:00Z",
                "text": "Ready for review",
            }
        ]


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
        ("Closed", "", "Off Track", RiskLevel.LOW),
        ("Blocked", "", "On Track", RiskLevel.HIGH),
        ("Active", "Blocked", "On Track", RiskLevel.HIGH),
        ("Active", "Blocker", "On Track", RiskLevel.HIGH),
        ("Active", "High Risk", "On Track", RiskLevel.HIGH),
        ("Active", "Risk", "Off Track", RiskLevel.MEDIUM),
        ("Active", "At Risk", "Off Track", RiskLevel.MEDIUM),
    ],
)
def test_hydration_state_and_tag_precedence_over_explicit_assessment(
    state: str,
    tags: str,
    risk_assessment: str,
    expected: RiskLevel,
) -> None:
    item = _hydrated_item(state=state, tags=tags, risk_assessment=risk_assessment)

    assert item.risk_level is expected


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
            del kwargs
            self.comment_calls.append(work_item_id)
            return [
                {
                    "id": 7,
                    "createdBy": {"displayName": "Owner", "uniqueName": "owner@example.com"},
                    "createdDate": "2026-05-24T12:00:00Z",
                    "text": '<div>Ready for review — see <a href="https://example/wi/1234">WI 1234</a></div>',
                }
            ]

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
