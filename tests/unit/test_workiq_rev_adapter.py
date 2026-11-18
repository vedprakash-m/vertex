"""RELO-W3.1 — WorkIQ acquisition behind the REV ports (Tech Spec §8.3.4)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import CandidateEnumerator, ContentHydrator, EnumeratedCandidate
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Forbidden, Incomplete, Success, Unsupported
from src.m365.rev.hydrator import RUNG_METADATA_ONLY
from src.m365.rev.workiq_enumerator import WorkIQContext, WorkIQEnumerator, WorkIQHydrator

CTX = WorkIQContext(tenant_id="tenant-1", principal_mailbox="pm@contoso.com")


def _intent(limit: int = 25, entity_type: EntityType = EntityType.MESSAGE) -> RetrievalIntent:
    return RetrievalIntent(entity_type=entity_type, subject_terms=("Atlas",), limit=limit)


def _enumerator(records: list[dict[str, Any]], calls: list[RetrievalIntent] | None = None) -> WorkIQEnumerator:
    def query(intent: RetrievalIntent) -> list[dict[str, Any]]:
        if calls is not None:
            calls.append(intent)
        return records

    return WorkIQEnumerator(query, CTX)


def _ids(result: Any) -> list[str]:
    return [c.locator.resource_id for c in result.value]


def test_adapters_satisfy_rev_port_protocols() -> None:
    enumerator: CandidateEnumerator = _enumerator([])
    hydrator: ContentHydrator = WorkIQHydrator()
    assert enumerator.entity_type is EntityType.MESSAGE
    assert callable(hydrator.hydrate)


def test_enumerate_maps_record_metadata_and_preserves_source_ids_in_utc() -> None:
    calls: list[RetrievalIntent] = []
    record = {
        "id": "AAMkAD-msg-1",
        "conversationId": "AAQkAD-conv-1",
        "subject": "Atlas status",
        "from": "lead@contoso.com",
        "preview": "Atlas is on track.",
        "receivedDateTime": "2026-11-10T09:30:00-08:00",
        "webLink": "https://outlook.office.com/mail/x",
    }
    intent = _intent()
    result = _enumerator([record], calls).enumerate(intent, correlation_id="cid-1")

    assert calls == [intent]
    assert isinstance(result, Success)
    (candidate,) = result.value
    assert candidate.locator.resource_id == "AAMkAD-msg-1"
    assert candidate.locator.source_type is EntityType.MESSAGE
    assert candidate.locator.tenant_id == "tenant-1"
    assert candidate.locator.principal_mailbox == "pm@contoso.com"
    assert candidate.received_at == datetime(2026, 11, 10, 17, 30, tzinfo=timezone.utc)
    assert candidate.enumerator == "workiq"
    assert candidate.correlation_id == "cid-1"
    meta = candidate.partial_metadata
    assert meta["subject"] == "Atlas status"
    assert meta["sender"] == "lead@contoso.com"
    assert meta["preview"] == "Atlas is on track."
    assert meta["conversation_id"] == "AAQkAD-conv-1"
    assert meta["received_at"] == "2026-11-10T17:30:00+00:00"
    assert meta["identity_kind"] == "native_message"
    assert meta["web_link"] == "https://outlook.office.com/mail/x"


def test_enumerate_orders_deterministically_newest_first_then_id() -> None:
    records = [
        {"id": "b", "receivedDateTime": "2026-11-01T00:00:00Z"},
        {"id": "c"},
        {"id": "a", "receivedDateTime": "2026-11-01T00:00:00Z"},
        {"id": "d", "receivedDateTime": "2026-11-05T00:00:00Z"},
    ]
    first = _enumerator(records).enumerate(_intent(), correlation_id="x")
    second = _enumerator(list(reversed(records))).enumerate(_intent(), correlation_id="x")
    assert _ids(first) == ["d", "a", "b", "c"]
    assert _ids(second) == _ids(first)


def test_enumerate_dedupes_by_resource_id() -> None:
    records = [
        {"id": "m1", "subject": "one", "receivedDateTime": "2026-11-01T00:00:00Z"},
        {"id": "m1", "subject": "one dup", "receivedDateTime": "2026-11-01T00:00:00Z"},
    ]
    result = _enumerator(records).enumerate(_intent(), correlation_id="x")
    assert _ids(result) == ["m1"]


def test_enumerate_falls_back_to_thread_identity() -> None:
    result = _enumerator([{"conversationId": "AAQk-thread"}]).enumerate(_intent(), correlation_id="x")
    assert isinstance(result, Success)
    (candidate,) = result.value
    assert candidate.partial_metadata["identity_kind"] == "native_thread"
    assert candidate.locator.resource_id


def test_enumerate_drops_unidentifiable_records_as_incomplete() -> None:
    records = [
        {"id": "good"},
        {"id": "https://outlook.office.com/mail/deeplink"},
        {"id": "turn1search2"},
        {"subject": "no id"},
    ]
    result = _enumerator(records).enumerate(_intent(), correlation_id="x")
    assert isinstance(result, Incomplete)
    assert result.reason == "unidentified_records:3"
    assert _ids(result) == ["good"]


def test_enumerate_naive_timestamp_is_not_trusted() -> None:
    result = _enumerator([{"id": "m1", "receivedDateTime": "2026-11-01T00:00:00"}]).enumerate(
        _intent(), correlation_id="x"
    )
    (candidate,) = result.value
    assert candidate.received_at is None
    assert candidate.partial_metadata["received_at"] is None


def test_enumerate_applies_intent_limit_as_page_cut() -> None:
    records = [{"id": f"m{i}", "receivedDateTime": f"2026-11-0{i}T00:00:00Z"} for i in range(1, 4)]
    result = _enumerator(records).enumerate(_intent(limit=2), correlation_id="x")
    assert isinstance(result, Incomplete)
    assert result.reason == "page_cut"
    assert _ids(result) == ["m3", "m2"]


def test_enumerate_rejects_mismatched_entity_type_without_querying() -> None:
    calls: list[RetrievalIntent] = []
    result = _enumerator([{"id": "m1"}], calls).enumerate(
        _intent(entity_type=EntityType.DRIVE_ITEM), correlation_id="x"
    )
    assert isinstance(result, Unsupported)
    assert calls == []


def test_enumerate_contains_query_failures() -> None:
    def failing(intent: RetrievalIntent) -> list[dict[str, Any]]:
        raise RuntimeError("workiq down")

    result = WorkIQEnumerator(failing, CTX).enumerate(_intent(), correlation_id="x")
    assert isinstance(result, Incomplete)
    assert result.value == ()
    assert result.reason == "provider_error:RuntimeError"


def _candidate(**meta: Any) -> EnumeratedCandidate:
    result = _enumerator([{"id": "m1", "conversationId": "AAQk-c1", **meta}]).enumerate(
        _intent(), correlation_id="cid"
    )
    return result.value[0]


def test_hydrate_builds_canonical_content_from_preview() -> None:
    candidate = _candidate(subject="Atlas status", preview="Atlas ships Friday.")
    result = WorkIQHydrator().hydrate(candidate, correlation_id="cid")
    assert isinstance(result, Success)
    hydrated = result.value
    assert hydrated.identity.resource_id == "m1"
    assert hydrated.identity.tenant_id == "tenant-1"
    assert hydrated.metadata_only is False
    assert hydrated.hydration_rung == "workiq_preview"
    assert "Atlas ships Friday." in hydrated.canonical_text
    assert hydrated.chunks
    assert hydrated.normalized_source_hash.startswith("sha256:")
    assert hydrated.route_metadata["conversation_id"] == "AAQk-c1"
    assert hydrated.correlation_id == "cid"


def test_hydrate_without_preview_is_metadata_only() -> None:
    result = WorkIQHydrator().hydrate(_candidate(subject="Atlas status"), correlation_id="cid")
    assert isinstance(result, Success)
    assert result.value.metadata_only is True
    assert result.value.hydration_rung == RUNG_METADATA_ONLY
    assert result.value.canonical_text == "Atlas status"


def test_hydrate_is_deterministic() -> None:
    candidate = _candidate(subject="s", preview="body text")
    first = WorkIQHydrator().hydrate(candidate, correlation_id="cid")
    second = WorkIQHydrator().hydrate(candidate, correlation_id="cid")
    assert first == second


def test_hydrate_quarantines_credentials() -> None:
    candidate = _candidate(subject="keys", preview="password=hunter2SuperSecret123")
    result = WorkIQHydrator().hydrate(candidate, correlation_id="cid")
    assert isinstance(result, Forbidden)
    assert result.scope == "content"


@pytest.mark.parametrize("source_type", [EntityType.MESSAGE, EntityType.CHAT_MESSAGE, EntityType.EVENT])
def test_entity_type_is_configurable(source_type: EntityType) -> None:
    enumerator = WorkIQEnumerator(lambda intent: [{"id": "x1"}], CTX, entity_type=source_type)
    result = enumerator.enumerate(_intent(entity_type=source_type), correlation_id="x")
    assert result.value[0].locator.source_type is source_type
