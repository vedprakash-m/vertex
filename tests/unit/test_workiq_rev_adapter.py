"""RELO-W3.1 — WorkIQ acquisition behind the REV ports (Tech Spec §8.3.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any

import pytest

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import CandidateEnumerator, ContentHydrator, EnumeratedCandidate
from src.core.rev.privacy import LocalCheckResult
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import Forbidden, Incomplete, Success, Unsupported
from src.m365.rev.hydrator import RUNG_METADATA_ONLY
from src.m365.rev.workiq_enumerator import (
    WorkIQContext,
    WorkIQEnumerator,
    WorkIQHydrator,
    WorkIQPage,
)

CTX = WorkIQContext(tenant_id="tenant-1", principal_mailbox="pm@contoso.com")


def _intent(
    limit: int = 25,
    entity_type: EntityType = EntityType.MESSAGE,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> RetrievalIntent:
    return RetrievalIntent(
        entity_type=entity_type,
        subject_terms=("Atlas",),
        limit=limit,
        from_date=from_date,
        to_date=to_date,
    )


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


def test_checkpoint_page_preserves_latest_edit_and_suppresses_tombstone() -> None:
    records = (
        {"id": "edited", "subject": "old", "receivedDateTime": "2026-11-01T00:00:00Z"},
        {"id": "deleted", "receivedDateTime": "2026-11-01T00:00:00Z"},
        {"id": "edited", "subject": "new", "receivedDateTime": "2026-11-02T00:00:00Z"},
        {"id": "deleted", "@removed": {"reason": "deleted"}},
    )
    enumerator = WorkIQEnumerator(
        lambda _intent, *, cursor: WorkIQPage(records, None),
        CTX,
        program_id="synthetic",
    )

    result = enumerator.enumerate(_intent(), correlation_id="x")

    assert isinstance(result, Success)
    assert [candidate.locator.resource_id for candidate in result.value] == ["edited"]
    assert result.value[0].partial_metadata["subject"] == "new"


def test_occurrence_ids_are_not_collapsed_by_series_identity() -> None:
    records = [
        {
            "id": "occurrence-1",
            "seriesMasterId": "series-1",
            "startDateTime": "2026-11-01T09:00:00Z",
        },
        {
            "id": "occurrence-2",
            "seriesMasterId": "series-1",
            "startDateTime": "2026-11-08T09:00:00Z",
        },
    ]

    result = WorkIQEnumerator(
        lambda _intent: records, CTX, entity_type=EntityType.EVENT,
    ).enumerate(_intent(entity_type=EntityType.EVENT), correlation_id="x")

    assert _ids(result) == ["occurrence-2", "occurrence-1"]


def test_checkpoint_replays_uncommitted_page_and_resumes_after_restart(tmp_path) -> None:
    calls: list[str | None] = []

    def query(_intent: RetrievalIntent, *, cursor: str | None) -> WorkIQPage:
        calls.append(cursor)
        if cursor is None:
            return WorkIQPage(
                ({"id": "page-1", "receivedDateTime": "2026-11-01T00:00:00Z"},),
                "cursor-2",
            )
        assert cursor == "cursor-2"
        return WorkIQPage((), None)

    first = WorkIQEnumerator(
        query, CTX, program_id="synthetic", programs_root=tmp_path,
    )
    first_result = first.enumerate(_intent(), correlation_id="first")
    assert first_result.value[0].locator.resource_id == "page-1"

    restarted = WorkIQEnumerator(
        query, CTX, program_id="synthetic", programs_root=tmp_path,
    )
    restarted_result = restarted.enumerate(_intent(), correlation_id="restart")
    assert restarted_result.value[0].locator.resource_id == "page-1"
    restarted.commit_enumeration_checkpoint()

    resumed = WorkIQEnumerator(
        query, CTX, program_id="synthetic", programs_root=tmp_path,
    )
    assert resumed.enumerate(_intent(), correlation_id="resume").value == ()
    resumed.commit_enumeration_checkpoint()

    completed = WorkIQEnumerator(
        query, CTX, program_id="synthetic", programs_root=tmp_path,
    )
    assert completed.enumerate(_intent(), correlation_id="complete").value == ()
    assert calls == [None, None, "cursor-2"]


def test_checkpoint_persists_empty_initial_window_as_unverified(tmp_path) -> None:
    calls = 0

    def query(_intent: RetrievalIntent, *, cursor: str | None) -> WorkIQPage:
        nonlocal calls
        calls += 1
        assert cursor is None
        return WorkIQPage((), None)

    first = WorkIQEnumerator(
        query, CTX, program_id="synthetic", programs_root=tmp_path,
    )
    result = first.enumerate(_intent(), correlation_id="first")
    assert isinstance(result, Incomplete)
    assert result.reason == "coverage_unverified:empty"
    first.commit_enumeration_checkpoint()

    restarted = WorkIQEnumerator(
        query, CTX, program_id="synthetic", programs_root=tmp_path,
    )
    replay = restarted.enumerate(_intent(), correlation_id="restart")
    assert isinstance(replay, Incomplete)
    assert replay.reason == "coverage_unverified:empty"
    assert calls == 1


def test_enumerate_falls_back_to_thread_identity() -> None:
    result = _enumerator([{"conversationId": "AAQk-thread"}]).enumerate(_intent(), correlation_id="x")
    assert isinstance(result, Success)
    (candidate,) = result.value
    assert candidate.partial_metadata["identity_kind"] == "native_thread"
    assert candidate.locator.resource_id


def test_enumerate_validates_each_native_identity_candidate_independently() -> None:
    result = _enumerator([{
        "id": "turn1search2",
        "messageId": "native-message",
        "conversationId": "semantic:preview",
        "threadId": "native-thread",
    }]).enumerate(_intent(), correlation_id="x")

    assert isinstance(result, Success)
    (candidate,) = result.value
    assert candidate.locator.resource_id == "native-message"
    assert candidate.partial_metadata["conversation_id"] == "native-thread"


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


def test_enumerate_quarantines_exact_id_mismatch() -> None:
    intent = _intent()
    intent = replace(intent, entity_ids=("requested-message",))

    result = _enumerator([{"id": "different-message"}]).enumerate(
        intent, correlation_id="x",
    )

    assert isinstance(result, Incomplete)
    assert result.reason == "mismatched_identity:1"
    assert result.value == ()


def test_enumerate_naive_timestamp_is_not_trusted() -> None:
    result = _enumerator([{"id": "m1", "receivedDateTime": "2026-11-01T00:00:00"}]).enumerate(
        _intent(), correlation_id="x"
    )
    (candidate,) = result.value
    assert candidate.received_at is None
    assert candidate.partial_metadata["received_at"] is None


def test_enumerate_invalid_timestamp_is_reported_incomplete() -> None:
    result = _enumerator(
        [{"id": "m1", "receivedDateTime": "not-a-timestamp"}],
    ).enumerate(_intent(), correlation_id="x")

    assert isinstance(result, Incomplete)
    assert result.reason == "unqualified_source_time:1"
    (candidate,) = result.value
    assert candidate.received_at is None
    assert candidate.partial_metadata["received_at"] is None


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        ({"emailAddress": {"address": "lead@contoso.com"}}, "lead@contoso.com"),
        ({"emailAddress": {"name": "Program Lead"}}, "Program Lead"),
        ({"emailAddress": "opaque"}, ""),
        ({"name": "Envelope Sender"}, "Envelope Sender"),
    ],
)
def test_enumerate_preserves_supported_sender_shapes(
    sender: object,
    expected: str,
) -> None:
    result = _enumerator([{"id": "m1", "from": sender}]).enumerate(
        _intent(),
        correlation_id="x",
    )

    assert result.value[0].partial_metadata["sender"] == expected


def test_enumerate_reports_non_mapping_provider_records_as_incomplete() -> None:
    enumerator = WorkIQEnumerator(
        lambda _intent: [None],  # type: ignore[list-item]
        CTX,
    )

    result = enumerator.enumerate(_intent(), correlation_id="x")

    assert isinstance(result, Incomplete)
    assert result.reason == "unidentified_records:1"
    assert result.value == ()


def test_enumerate_applies_intent_limit_as_page_cut() -> None:
    records = [{"id": f"m{i}", "receivedDateTime": f"2026-11-0{i}T00:00:00Z"} for i in range(1, 4)]
    result = _enumerator(records).enumerate(_intent(limit=2), correlation_id="x")
    assert isinstance(result, Incomplete)
    assert result.reason == "page_cut"
    assert _ids(result) == ["m3", "m2"]


def test_enumerate_requeries_bounded_weekly_windows() -> None:
    calls: list[RetrievalIntent] = []

    def query(intent: RetrievalIntent) -> list[dict[str, Any]]:
        calls.append(intent)
        assert intent.from_date is not None and intent.to_date is not None
        return [{
            "id": intent.to_date.isoformat(),
            "receivedDateTime": f"{intent.to_date.isoformat()}T12:00:00+00:00",
        }]

    enumerator = WorkIQEnumerator(query, CTX, window_days=7, max_windows=4)
    intent = RetrievalIntent(
        entity_type=EntityType.MESSAGE,
        subject_terms=("Atlas",),
        from_date=date(2026, 10, 15),
        to_date=date(2026, 11, 11),
        limit=25,
    )

    result = enumerator.enumerate(intent, correlation_id="windowed")

    assert isinstance(result, Success)
    assert len(result.value) == 4
    assert len(calls) == 4
    assert [(window.from_date, window.to_date) for window in calls] == [
        (date(2026, 11, 5), date(2026, 11, 11)),
        (date(2026, 10, 29), date(2026, 11, 4)),
        (date(2026, 10, 22), date(2026, 10, 28)),
        (date(2026, 10, 15), date(2026, 10, 21)),
    ]


def test_enumerate_discloses_window_budget_stop() -> None:
    calls: list[RetrievalIntent] = []

    def query(intent: RetrievalIntent) -> list[dict[str, Any]]:
        calls.append(intent)
        return [{"id": f"m-{intent.to_date}", "receivedDateTime": f"{intent.to_date}T12:00:00Z"}]

    enumerator = WorkIQEnumerator(query, CTX, window_days=7, max_windows=2)
    intent = RetrievalIntent(
        entity_type=EntityType.MESSAGE,
        from_date=date(2026, 10, 1),
        to_date=date(2026, 10, 31),
        limit=25,
    )

    result = enumerator.enumerate(intent, correlation_id="windowed")

    assert isinstance(result, Incomplete)
    assert result.reason == "budget_stop:max_windows"
    assert len(calls) == 2
    assert len(result.value) == 2


@pytest.mark.parametrize("window_days,max_windows", [(0, 1), (1, 0), (True, 1), (1, False)])
def test_enumerate_rejects_invalid_window_bounds(window_days: int, max_windows: int) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        WorkIQEnumerator(lambda _: [], CTX, window_days=window_days, max_windows=max_windows)


def test_enumerate_rejects_reversed_window_without_querying() -> None:
    calls: list[RetrievalIntent] = []
    intent = RetrievalIntent(
        entity_type=EntityType.MESSAGE,
        from_date=date(2026, 11, 2),
        to_date=date(2026, 11, 1),
    )

    result = WorkIQEnumerator(lambda value: calls.append(value) or [], CTX).enumerate(
        intent, correlation_id="x"
    )

    assert isinstance(result, Incomplete)
    assert result.reason == "invalid_window"
    assert result.value == ()
    assert calls == []


def test_enumerate_keeps_unknown_source_time_without_fabrication() -> None:
    result = _enumerator([{"id": "m1", "receivedDateTime": "0001-01-01T00:00:00+14:00"}]).enumerate(
        _intent(), correlation_id="x"
    )

    assert isinstance(result, Incomplete)
    assert result.reason == "unqualified_source_time:1"
    (candidate,) = result.value
    assert candidate.received_at is None
    assert candidate.partial_metadata["received_at"] is None
    assert candidate.partial_metadata["source_time_qualified"] is False


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
    assert hydrated.route_metadata["route"] == "workiq"
    assert hydrated.route_metadata["subject"] == "Atlas status"
    assert hydrated.route_metadata["sender"] == ""
    assert hydrated.route_metadata["source_time_qualified"] is False
    assert hydrated.route_metadata["source_time_state"] == "missing"
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


def test_hydrate_refuses_sensitivity_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.m365.rev.workiq_enumerator.run_local_checks",
        lambda *_args, **_kwargs: LocalCheckResult(
            passed=False,
            sensitivity_denied=True,
            reason="sensitivity_denied:restricted",
        ),
    )

    result = WorkIQHydrator().hydrate(
        _candidate(subject="status"),
        correlation_id="cid",
    )

    assert isinstance(result, Forbidden)
    assert result.scope == "sensitivity"
    assert result.reason == "sensitivity_denied:restricted"


def test_hydrate_refuses_content_over_configured_size_limit() -> None:
    result = WorkIQHydrator(max_body_bytes=4).hydrate(
        _candidate(subject="Atlas", preview="later"),
        correlation_id="cid",
    )

    assert isinstance(result, Unsupported)
    assert result.entity_type == EntityType.MESSAGE.value
    assert result.reason.startswith("size_exceeded:")


@pytest.mark.parametrize("source_type", [EntityType.MESSAGE, EntityType.CHAT_MESSAGE, EntityType.EVENT])
def test_entity_type_is_configurable(source_type: EntityType) -> None:
    enumerator = WorkIQEnumerator(lambda intent: [{"id": "x1"}], CTX, entity_type=source_type)
    result = enumerator.enumerate(_intent(entity_type=source_type), correlation_id="x")
    assert result.value[0].locator.source_type is source_type
