"""RELO-W3.7 — SharePointEnumerator (CandidateEnumerator over engms_pages.yaml pages)."""

from __future__ import annotations

import pytest

from src.core.models_v2 import EngMsPage
from src.core.rev.entity_types import EntityType
from src.core.rev.ports import CandidateEnumerator
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Forbidden, Incomplete, RateLimited, Success, Unsupported
from src.m365.rev.sharepoint_enumerator import SharePointEnumerator
from src.m365.rev.sharepoint_hydrator import (
    FakeRevSharePointClient,
    GraphDriveItem,
    SharePointContext,
    SharePointHydrator,
)

_INTENT = RetrievalIntent(entity_type=EntityType.DRIVE_ITEM)


def _page(page_id: str, **kwargs: object) -> EngMsPage:
    return EngMsPage(id=page_id, title=page_id.upper(), url=f"https://contoso.sharepoint.com/{page_id}.docx", **kwargs)  # type: ignore[arg-type]


def _item(item_id: str) -> GraphDriveItem:
    return GraphDriveItem(item_id=item_id, drive_id="drv", name=f"{item_id}.txt", etag=f"e-{item_id}", site_id="site")


def _resolver(mapping: dict[str, object]):
    calls: list[str] = []

    def resolve(page: EngMsPage, correlation_id: str):
        calls.append(page.id)
        outcome = mapping[page.id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _enumerator(pages, resolve, **kwargs) -> SharePointEnumerator:
    return SharePointEnumerator(
        pages=tuple(pages), resolve=resolve, tenant_id="t", principal_mailbox="tpm@example.com", **kwargs
    )


def test_conforms_to_protocol_and_orders_deterministically() -> None:
    resolve = _resolver({"b": Success(_item("i-b")), "a": Success(_item("i-a"))})
    enumerator: CandidateEnumerator = _enumerator([_page("b"), _page("a", source_subtype="ref_doc")], resolve)

    result = enumerator.enumerate(_INTENT, correlation_id="cid-1")

    assert isinstance(result, Success)
    assert [c.locator.resource_id for c in result.value] == ["i-a", "i-b"]
    first = result.value[0]
    assert first.locator.source_type is EntityType.DRIVE_ITEM
    assert first.locator.container == "drv"
    assert first.locator.etag_hint == "e-i-a"
    assert first.correlation_id == "cid-1"
    assert first.enumerator == "engms_pages"
    assert first.partial_metadata["page_id"] == "a"
    assert first.partial_metadata["drive_id"] == "drv"
    assert first.partial_metadata["source_subtype"] == "ref_doc"


def test_wrong_entity_type_is_unsupported_without_calls() -> None:
    resolve = _resolver({"a": Success(_item("i-a"))})
    result = _enumerator([_page("a")], resolve).enumerate(
        RetrievalIntent(entity_type=EntityType.MESSAGE), correlation_id="c"
    )
    assert isinstance(result, Unsupported)
    assert resolve.calls == []  # type: ignore[attr-defined]


def test_no_configured_pages_is_typed_unsupported_not_silent_empty() -> None:
    result = _enumerator([], _resolver({})).enumerate(_INTENT, correlation_id="c")
    assert isinstance(result, Unsupported)
    assert "no_sharepoint_pages_configured" in result.reason


def test_max_pages_bounds_resolution_and_reports_budget_stop() -> None:
    resolve = _resolver({p: Success(_item(f"i-{p}")) for p in "abc"})
    result = _enumerator([_page("c"), _page("a"), _page("b")], resolve, max_pages=2).enumerate(
        _INTENT, correlation_id="c"
    )
    assert isinstance(result, Incomplete)
    assert result.reason == "budget_stop:max_pages"
    assert [c.locator.resource_id for c in result.value] == ["i-a", "i-b"]
    assert resolve.calls == ["a", "b"]  # type: ignore[attr-defined]


def test_candidate_limit_uses_smaller_of_intent_and_max_candidates() -> None:
    resolve = _resolver({p: Success(_item(f"i-{p}")) for p in "abc"})
    pages = [_page("a"), _page("b"), _page("c")]
    by_intent = _enumerator(pages, resolve).enumerate(
        RetrievalIntent(entity_type=EntityType.DRIVE_ITEM, limit=1), correlation_id="c"
    )
    by_ceiling = _enumerator(pages, resolve, max_candidates=2).enumerate(_INTENT, correlation_id="c")
    assert isinstance(by_intent, Incomplete) and by_intent.reason == "page_cut"
    assert [c.locator.resource_id for c in by_intent.value] == ["i-a"]
    assert isinstance(by_ceiling, Incomplete) and by_ceiling.reason == "page_cut"
    assert len(by_ceiling.value) == 2


def test_duplicate_items_across_pages_are_deduped() -> None:
    resolve = _resolver({"a": Success(_item("same")), "b": Success(_item("same"))})
    result = _enumerator([_page("a"), _page("b")], resolve).enumerate(_INTENT, correlation_id="c")
    assert isinstance(result, Success)
    assert [c.partial_metadata["page_id"] for c in result.value] == ["a"]


@pytest.mark.parametrize("source_subtype", ["lt_deck", "ref_doc"])
def test_preserves_acl_and_source_subtype_without_authorizing_it(source_subtype: str) -> None:
    item = GraphDriveItem(
        item_id="i-a",
        drive_id="drv",
        name="governance.pptx",
        etag="e-1",
        version="3",
        site_id="site",
        acl=({"kind": "group", "id": "group-1", "roles": ("read",)},),
    )
    result = _enumerator(
        [_page("a", source_subtype=source_subtype)],
        _resolver({"a": Success(item)}),
    ).enumerate(_INTENT, correlation_id="c")

    assert isinstance(result, Success)
    metadata = result.value[0].partial_metadata
    assert metadata["acl"] == item.acl
    assert metadata["source_subtype"] == source_subtype
    assert metadata["version"] == "3"
    assert metadata["acl_authorization_claimed"] is False


@pytest.mark.parametrize("status", ["deleted", "inaccessible"])
def test_status_changes_are_visible_for_fail_closed_hydration(status: str) -> None:
    item = GraphDriveItem(
        item_id="i-a",
        drive_id="drv",
        deleted=status == "deleted",
        inaccessible=status == "inaccessible",
    )
    result = _enumerator(
        [_page("a")],
        _resolver({"a": Success(item)}),
    ).enumerate(_INTENT, correlation_id="c")

    assert isinstance(result, Success)
    assert result.value[0].partial_metadata[status] is True


@pytest.mark.parametrize(
    "failure",
    [Forbidden(scope="files", reason="no consent"), RateLimited(provider="graph_sharepoint", retry_after_seconds=2.0)],
)
def test_all_pages_failing_returns_first_typed_failure(failure: object) -> None:
    resolve = _resolver({"a": failure, "b": failure})
    result = _enumerator([_page("a"), _page("b")], resolve).enumerate(_INTENT, correlation_id="c")
    assert result == failure


def test_partial_failures_salvage_successes_as_incomplete() -> None:
    resolve = _resolver(
        {
            "a": Success(_item("i-a")),
            "b": Forbidden(scope="files", reason="denied"),
            "c": RuntimeError("boom"),
        }
    )
    result = _enumerator([_page("a"), _page("b"), _page("c")], resolve).enumerate(_INTENT, correlation_id="c")
    assert isinstance(result, Incomplete)
    assert [c.locator.resource_id for c in result.value] == ["i-a"]
    assert result.reason == "page_failures:b=Forbidden,c=RuntimeError"


def test_resolver_exception_only_never_crosses_port() -> None:
    resolve = _resolver({"a": ValueError("bad url")})
    result = _enumerator([_page("a")], resolve).enumerate(_INTENT, correlation_id="c")
    assert isinstance(result, Incomplete)
    assert result.value == ()
    assert result.reason == "page_failures:a=ValueError"


def test_candidates_hydrate_through_existing_sharepoint_hydrator() -> None:
    item = GraphDriveItem(
        item_id="i-a", drive_id="drv", name="notes.txt", mime_type="text/plain", file_content=b"Launch is on track."
    )
    resolve = _resolver({"a": Success(item)})
    enumerated = _enumerator([_page("a")], resolve).enumerate(_INTENT, correlation_id="c")
    assert isinstance(enumerated, Success)

    hydrator = SharePointHydrator(FakeRevSharePointClient((item,)), SharePointContext(tenant_id="t", principal_mailbox="p"))
    hydrated = hydrator.hydrate(enumerated.value[0], correlation_id="c")

    assert isinstance(hydrated, Success)
    assert "Launch is on track." in hydrated.value.canonical_text


def test_hydrated_fixture_preserves_acl_without_claiming_authorization() -> None:
    acl = ({"kind": "group", "id": "launch-readers", "roles": ("read",)},)
    item = GraphDriveItem(
        item_id="i-a",
        drive_id="drv",
        name="notes.txt",
        mime_type="text/plain",
        file_content=b"Launch is on track.",
        acl=acl,
    )
    enumerated = _enumerator(
        [_page("a")], _resolver({"a": Success(item)}),
    ).enumerate(_INTENT, correlation_id="c")
    assert isinstance(enumerated, Success)
    hydrator = SharePointHydrator(
        FakeRevSharePointClient((item,)),
        SharePointContext(tenant_id="t", principal_mailbox="p"),
    )

    hydrated = hydrator.hydrate(enumerated.value[0], correlation_id="c")

    assert isinstance(hydrated, Success)
    assert hydrated.value.route_metadata["acl"] == acl
    assert hydrated.value.route_metadata["acl_authorization_claimed"] is False


def test_invalid_limits_rejected() -> None:
    with pytest.raises(ValueError):
        _enumerator([_page("a")], _resolver({}), max_pages=0)
    with pytest.raises(ValueError):
        _enumerator([_page("a")], _resolver({}), max_candidates=0)
