from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from itertools import combinations
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from src.commands import gather
from src.commands.gather_workiq_helpers import WorkIQQueryPlan, workiq_timestamp
from src.commands.gather_pipeline.workiq_prefetch_stage import (
    resolve_workiq_signals, workiq_signals_to_payload,
)
from src.core.models import Confidence, RiskLevel
from src.core.m365_router_interface import IM365TopicRouter
from src.core.models_v2 import (
    ADOConfig, IntegrationError, M365Config, Program, Signal, WorkIQRetrievalConfig,
    Workstream, WorkstreamSignalSources,
)
from src.core.prefetch_store import write_prefetch_snapshot
from src.core.signal_classification import classify_signal
from src.m365.agency_bridge import AgencyBridge


AS_OF = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
SOURCE_TIME = "2026-10-23T09:00:00Z"


class _FloatingTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


def _parse(
    record: dict[str, Any], *, topic_router: IM365TopicRouter | None = None,
    integration_error_sink: list[IntegrationError] | None = None,
) -> tuple[Signal, ...]:
    return gather._signals_from_workiq_payload(
        payload={"emails": [record]}, query_name="feedback_search:fixture",
        question="Find status", program_id="fixture", as_of=AS_OF,
        items_by_id={}, workstreams=(), topic_router=topic_router,
        integration_error_sink=integration_error_sink,
    )


@pytest.mark.parametrize("identity", [{}, {"conversationId": "thread"}, {"meetingId": "series"}])
@pytest.mark.parametrize("time", [
    {}, {"receivedDateTime": "invalid"}, {"receivedDateTime": "2026-10-23T09:00:00"},
])
def test_unqualified_observations_produce_diagnostics_not_fabricated_evidence(
    identity: dict[str, object], time: dict[str, object],
) -> None:
    signals = _parse({
        "summary": "Private source body", "sender": "private@example.com", **identity, **time,
    })
    assert len(signals) == 1
    diagnostic = signals[0]
    assert diagnostic.source == "system"
    assert diagnostic.confidence is Confidence.NONE
    assert diagnostic.metadata["quarantined_record_count"] == 1
    assert diagnostic.metadata["source_event_time"] is None
    assert diagnostic.metadata["temporal_confidence"] == "unknown"
    assert diagnostic.metadata["retrieved_at"] == AS_OF.isoformat()
    assert "Private source body" not in diagnostic.text
    assert "private@example.com" not in repr(diagnostic)
    assert not diagnostic.entity_refs


@pytest.mark.parametrize("field", ["receivedDateTime", "sentDateTime", "createdDateTime", "timestamp", "startDateTime"])
@pytest.mark.parametrize("identity_field", ["id", "messageId", "emailId", "eventId", "transcriptId"])
def test_native_identity_and_source_time_remain_separate_from_retrieval(
    field: str, identity_field: str,
) -> None:
    signals = _parse({identity_field: "native-1", field: SOURCE_TIME, "summary": "Known observation"})
    assert len(signals) == 1
    signal = signals[0]
    assert signal.source == "workiq/email"
    assert signal.timestamp == datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
    assert signal.metadata["native_message_id"] == "native-1"
    assert signal.metadata["message_id_kind"] == "native"
    assert signal.metadata["source_event_time"] == signal.timestamp.isoformat()
    assert signal.metadata["retrieved_at"] == AS_OF.isoformat()
    assert signal.metadata["temporal_confidence"] == "source_reported"


@pytest.mark.parametrize("field", ["receivedDateTime", "sentDateTime", "createdDateTime", "timestamp", "startDateTime"])
@pytest.mark.parametrize("source_time", [
    pytest.param("0001-01-01T00:00:00+14:00", id="iso-utc-underflow"),
    pytest.param("9999-12-31T23:59:59-14:00", id="iso-utc-overflow"),
    pytest.param(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14))), id="datetime-utc-underflow"),
    pytest.param(datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone(-timedelta(hours=14))), id="datetime-utc-overflow"),
    pytest.param(datetime(2026, 10, 23, 9, tzinfo=_FloatingTimezone()), id="datetime-without-offset"),
])
def test_unstructured_gather_quarantines_unusable_utc_source_times(
    field: str, source_time: str | datetime,
) -> None:
    router = Mock(spec=IM365TopicRouter)
    router.route_artifact.side_effect = AssertionError("No routing of unqualified observations")
    errors: list[IntegrationError] = []
    signals = _parse(
        {"id": "native-private", field: source_time, "summary": "Private source body", "sender": "private@example.invalid"},
        topic_router=router, integration_error_sink=errors,
    )
    router.route_artifact.assert_not_called()
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata["quarantined_record_count"] == 1
    assert signals[0].metadata["source_event_time"] is None
    assert signals[0].metadata["temporal_confidence"] == "unknown"
    assert signals[0].entity_refs == ()
    assert errors[0].stage == "quarantine"
    assert errors[0].retryable is False
    assert "timezone-qualified source time" in errors[0].message
    assert all(value not in repr((signals, errors)) for value in ("native-private", "Private source body", "private@example.invalid"))


@pytest.mark.parametrize(("source_time", "expected"), [
    ("0001-01-01T14:00:00+14:00", datetime.min.replace(tzinfo=timezone.utc)),
    ("9999-12-31T09:59:59-14:00", datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
    (datetime(2026, 10, 23, 12, 30, tzinfo=timezone(timedelta(hours=3, minutes=30))), datetime(2026, 10, 23, 9, tzinfo=timezone.utc)),
    ("2026-10-23T12:30:00+03:30", datetime(2026, 10, 23, 9, tzinfo=timezone.utc)),
])
def test_unstructured_workiq_preserves_valid_utc_conversion(
    source_time: str | datetime, expected: datetime,
) -> None:
    assert workiq_timestamp({"receivedDateTime": source_time}) == expected


@pytest.mark.parametrize("preferred_time", [
    "invalid", "2026-10-23T09:00:00", "0001-01-01T00:00:00+14:00",
    datetime(2026, 10, 23, 9, tzinfo=_FloatingTimezone()),
])
def test_invalid_preferred_source_time_does_not_fall_back_to_another_field(
    preferred_time: str | datetime,
) -> None:
    errors: list[IntegrationError] = []
    signals = _parse(
        {"id": "native-1", "receivedDateTime": preferred_time, "sentDateTime": SOURCE_TIME},
        integration_error_sink=errors,
    )
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert errors[0].stage == "quarantine"


@pytest.mark.parametrize("preferred_time", [None, ""])
def test_absent_preferred_source_time_keeps_existing_fallback(preferred_time: str | None) -> None:
    signals = _parse({"id": "native-1", "receivedDateTime": preferred_time, "sentDateTime": SOURCE_TIME})
    assert len(signals) == 1
    assert signals[0].source == "workiq/email"
    assert signals[0].timestamp == datetime(2026, 10, 23, 9, tzinfo=timezone.utc)


def test_first_valid_source_time_keeps_existing_precedence() -> None:
    signals = _parse({
        "id": "native-1", "receivedDateTime": SOURCE_TIME, "sentDateTime": "2026-10-20T09:00:00Z",
    })
    assert len(signals) == 1
    assert signals[0].timestamp == datetime(2026, 10, 23, 9, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("invalid_field", "valid_field"),
    tuple(combinations(("id", "messageId", "emailId", "eventId", "transcriptId"), 2)),
)
@pytest.mark.parametrize("invalid", [42, True, {"unexpected": True}, ["unexpected"], " \t ", None, "", 0, {}, []])
def test_unstructured_gather_uses_later_valid_native_identity(
    invalid_field: str, valid_field: str, invalid: object,
) -> None:
    errors: list[IntegrationError] = []
    signals = _parse(
        {invalid_field: invalid, valid_field: "native-fallback", "receivedDateTime": SOURCE_TIME},
        integration_error_sink=errors,
    )
    assert errors == []
    assert len(signals) == 1
    assert signals[0].source == "workiq/email"
    assert signals[0].metadata["native_message_id"] == "native-fallback"
    assert signals[0].metadata["message_id"] == "native-fallback"
    assert signals[0].timestamp == datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)


def test_unstructured_gather_preserves_first_valid_identity_value() -> None:
    signals = _parse({
        "id": " native-first ", "messageId": "native-second",
        "receivedDateTime": SOURCE_TIME,
    })
    assert len(signals) == 1
    assert signals[0].metadata["native_message_id"] == " native-first "


@pytest.mark.parametrize("native", [
    " native-fallback ", "AAMkABC+/foo==",
])
def test_unstructured_gather_preserves_selected_fallback_value(native: str) -> None:
    signals = _parse({"id": 42, "messageId": native, "receivedDateTime": SOURCE_TIME})
    assert len(signals) == 1
    assert signals[0].metadata["native_message_id"] == native


@pytest.mark.parametrize("unqualified", [
    "semantic:preview", "turn1search2", "http://[",
    "https://example.invalid/?threadId=semantic%3Apreview",
    "https://example.invalid/?threadId=native-thread",
    "https://example.invalid/opaque-resource",
    " HTTP://example.invalid/?threadId=native-thread ",
])
def test_unstructured_gather_does_not_newly_admit_unqualified_identity_fallback(
    unqualified: str,
) -> None:
    errors: list[IntegrationError] = []
    signals = _parse(
        {"id": 42, "messageId": unqualified, "receivedDateTime": SOURCE_TIME},
        integration_error_sink=errors,
    )
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata["quarantined"] is True
    assert unqualified not in repr(signals) + repr(errors)


@pytest.mark.parametrize("unqualified", [
    "semantic:preview", "turn1search2",
    "https://example.invalid/?threadId=native-thread",
    "https://example.invalid/opaque-resource",
])
def test_unstructured_gather_skips_unqualified_identity_for_later_native_fallback(
    unqualified: str,
) -> None:
    signals = _parse({
        "id": 42, "messageId": unqualified, "eventId": "native-fallback",
        "receivedDateTime": SOURCE_TIME,
    })
    assert len(signals) == 1
    assert signals[0].source == "workiq/email"
    assert signals[0].metadata["native_message_id"] == "native-fallback"


def test_unstructured_gather_skips_semantic_message_preview_for_native_fallback() -> None:
    signals = _parse({
        "id": "semantic:preview", "messageId": "native-fallback",
        "receivedDateTime": SOURCE_TIME,
    })
    assert len(signals) == 1
    assert signals[0].source == "workiq/email"
    assert signals[0].metadata["native_message_id"] == "native-fallback"


def test_unstructured_gather_keeps_native_thread_fallback_distinct_from_message_id() -> None:
    signals = _parse({
        "conversationId": "native-thread", "id": "semantic:preview",
        "receivedDateTime": SOURCE_TIME,
    })
    assert len(signals) == 1
    assert signals[0].source == "workiq/email"
    assert signals[0].metadata["native_message_id"] is None
    assert signals[0].metadata["source_identity_kind"] == "native_thread"
    assert signals[0].metadata["thread_id"] == "native-thread"


def test_unstructured_gather_fallback_keeps_fragment_parent_identity() -> None:
    signals = _parse({
        "id": 42, "messageId": "native-fallback", "receivedDateTime": SOURCE_TIME,
        "summary": "WI 101 needs owner review.\n\nWI 102 has a delivery blocker.",
    })
    assert len(signals) == 2
    assert len({signal.raw_ref for signal in signals}) == 2
    for signal in signals:
        assert signal.metadata["native_message_id"] == "native-fallback"
        assert signal.metadata["message_id_kind"] == "derived_fragment"
        assert signal.metadata["message_id"] != "native-fallback"


def test_unstructured_gather_fallback_survives_prefetch_replay(tmp_path: Path) -> None:
    signals = _parse({
        "id": 42, "messageId": "native-fallback", "receivedDateTime": SOURCE_TIME,
        "summary": "Synthetic valid observation",
    })
    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=workiq_signals_to_payload(signals),
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=AS_OF,
    )
    live = Mock(side_effect=AssertionError("Expected the committed synthetic snapshot"))
    errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path, live_fetch_fn=live,
        now=AS_OF, integration_error_sink=errors,
    )
    live.assert_not_called()
    assert errors == []
    assert len(restored) == 1
    assert restored[0].metadata["native_message_id"] == "native-fallback"
    assert restored[0].timestamp == signals[0].timestamp


def test_unstructured_gather_quarantines_when_all_identity_aliases_are_invalid() -> None:
    errors: list[IntegrationError] = []
    signals = _parse(
        {"id": 42, "messageId": " \t ", "receivedDateTime": SOURCE_TIME},
        integration_error_sink=errors,
    )
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata["quarantined"] is True
    assert errors[0].stage == "quarantine"
    assert errors[0].retryable is False


def test_unstructured_gather_does_not_bypass_missing_time_on_identity_fallback() -> None:
    errors: list[IntegrationError] = []
    signals = _parse(
        {"id": 42, "messageId": "native-fallback"},
        integration_error_sink=errors,
    )
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata["source_event_time"] is None
    assert errors[0].stage == "quarantine"
    assert "timezone-qualified source time" in errors[0].message
    assert "native observation ID" not in errors[0].message


@pytest.mark.parametrize("invalid", [
    {"messageId": "native-1"}, {"receivedDateTime": SOURCE_TIME},
    {"messageId": "native-1", "receivedDateTime": datetime(2026, 10, 23, 9)},
])
def test_quarantine_precedes_routing_and_preserves_explicit_nonretryable_error(
    invalid: dict[str, object],
) -> None:
    router = Mock(side_effect=AssertionError("No routing of unqualified observations"))
    router.route_artifact.side_effect = AssertionError("No routing of unqualified observations")
    errors: list[IntegrationError] = []
    signals = _parse(
        {"summary": "Needs review", **invalid}, topic_router=router,
        integration_error_sink=errors,
    )
    router.route_artifact.assert_not_called()
    assert len(errors) == 1
    assert errors[0].source == "workiq"
    assert errors[0].stage == "quarantine"
    assert errors[0].retryable is False
    assert "native" in errors[0].operator_action
    assert signals[0].metadata["quarantined"] is True


def test_mixed_records_preserve_valid_observations_and_bound_diagnostics() -> None:
    errors: list[IntegrationError] = []
    signals = gather._signals_from_workiq_payload(
        payload={"emails": [
            {"messageId": "native-1", "receivedDateTime": SOURCE_TIME, "summary": "Valid"},
            *[{"summary": f"Private body {index}"} for index in range(100)],
        ]}, query_name="feedback_search:fixture", question="Find status",
        program_id="fixture", as_of=AS_OF, items_by_id={}, workstreams=(),
        integration_error_sink=errors,
    )
    assert len(signals) == 2
    assert signals[0].metadata["native_message_id"] == "native-1"
    assert signals[1].source == "system"
    assert signals[1].metadata["quarantined_record_count"] == 100
    assert len(errors) == 1


def test_fragment_identity_is_not_mislabeled_as_native() -> None:
    signals = _parse({
        "messageId": "native-1", "receivedDateTime": SOURCE_TIME,
        "summary": "WI 101 needs owner review.\n\nWI 102 has a delivery blocker.",
    })
    assert len(signals) == 2
    assert len({signal.id for signal in signals}) == 2
    assert len({signal.raw_ref for signal in signals}) == 2
    for signal in signals:
        assert signal.metadata["native_message_id"] == "native-1"
        assert signal.metadata["message_id_kind"] == "derived_fragment"
        assert signal.metadata["message_id"] != "native-1"
        assert signal.metadata["source_event_time"] == SOURCE_TIME.replace("Z", "+00:00")


def test_transcript_enrichment_uses_nonempty_native_reference() -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import _signals_as_enrichments

    signal = Signal(
        id="signal-fragment-0",
        timestamp=AS_OF,
        source="transcript",
        program_id="fixture",
        workstream_id="lane",
        entity_refs=(),
        text="Known transcript fragment for native reference coverage.",
        raw_ref="workiq:transcript:fragment-0",
        confidence=Confidence.NONE,
        metadata={
            "native_message_id": "native-occurrence-42",
            "message_id": "native-occurrence-42:seg:0",
            "message_id_kind": "derived_fragment",
        },
    )

    enrichments = _signals_as_enrichments([signal])

    assert len(enrichments) == 1
    assert enrichments[0].source_id == "workiq:transcript:native-occurrence-42"


@pytest.mark.parametrize("native_message_id", [None, ""])
def test_transcript_enrichment_uses_legacy_reference_when_native_id_is_empty(
    native_message_id: str | None,
) -> None:
    from src.commands.gather_pipeline.evidence_extraction_stage import _signals_as_enrichments

    signal = _parse({
        "messageId": "native-1",
        "receivedDateTime": SOURCE_TIME,
        "summary": "Known transcript fragment for fallback coverage.",
    })[0]
    legacy_signal = replace(
        signal,
        metadata={**(signal.metadata or {}), "native_message_id": native_message_id},
    )

    enrichments = _signals_as_enrichments([legacy_signal])

    assert len(enrichments) == 1
    assert enrichments[0].source_id == signal.raw_ref
    assert legacy_signal.metadata["message_id"] == signal.metadata["message_id"]
    assert legacy_signal.raw_ref == signal.raw_ref
    assert legacy_signal.id == signal.id
    no_reference = replace(legacy_signal, raw_ref=None)
    assert _signals_as_enrichments([no_reference])[0].source_id == signal.id
    assert _signals_as_enrichments([replace(legacy_signal, text="")]) == []
    no_metadata = replace(signal, metadata=None)
    assert _signals_as_enrichments([no_metadata])[0].source_id == signal.raw_ref


def test_cached_quarantine_restores_error_visibility_without_live_provider(tmp_path: Path) -> None:
    signals = _parse({"summary": "Unknown source"})
    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=workiq_signals_to_payload(signals),
        watermark=None, completeness="complete", latency_ms=0,
        ttl_seconds=3600, programs_root=tmp_path, now=AS_OF,
    )
    live = Mock(side_effect=AssertionError("Use the committed synthetic snapshot"))
    errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path, live_fetch_fn=live,
        now=AS_OF, integration_error_sink=errors,
    )
    assert restored == tuple(classify_signal(signal) for signal in signals)
    assert len(errors) == 1
    assert errors[0].stage == "quarantine"
    assert errors[0].retryable is False
    live.assert_not_called()


@pytest.mark.parametrize("identity", [" ", 12, True, {"id": "nested"}, ["native"]])
def test_invalid_native_identity_is_not_coerced_into_provider_identity(identity: object) -> None:
    signals = _parse({"id": identity, "receivedDateTime": SOURCE_TIME, "summary": "Needs review"})
    assert len(signals) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata["quarantined"] is True


def test_summary_only_response_is_visibly_quarantined() -> None:
    errors: list[IntegrationError] = []
    signals = gather._signals_from_workiq_payload(
        payload={"response": "Private summary", "conversationId": "thread"},
        query_name="feedback_search:fixture", question="Find status", program_id="fixture",
        as_of=AS_OF, items_by_id={}, workstreams=(), integration_error_sink=errors,
    )
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert "Private summary" not in signals[0].text
    assert "thread" not in signals[0].text


def test_retrieval_clock_does_not_change_native_evidence_identity() -> None:
    kwargs = dict(
        payload={"emails": [{"id": "native-1", "receivedDateTime": "2026-10-23T11:00:00+02:00"}]},
        query_name="feedback_search:fixture", question="Find status",
        program_id="fixture", items_by_id={}, workstreams=(),
    )
    first = gather._signals_from_workiq_payload(**kwargs, as_of=AS_OF)[0]
    later = gather._signals_from_workiq_payload(
        **kwargs, as_of=AS_OF.replace(hour=13),
    )[0]
    assert first.id == later.id
    assert first.timestamp == later.timestamp == datetime(2026, 10, 23, 9, tzinfo=timezone.utc)
    assert first.metadata["retrieved_at"] != later.metadata["retrieved_at"]


@pytest.mark.parametrize("filters", [
    {"exclude_keywords": ("excluded",)}, {"allowed_thread_ids": ("allowed",)},
])
def test_intentionally_filtered_records_do_not_raise_quarantine(filters: dict[str, Any]) -> None:
    errors: list[IntegrationError] = []
    signals = gather._signals_from_workiq_payload(
        payload={"emails": [{"summary": "excluded", "conversationId": "other"}]},
        query_name="feedback_search:fixture", question="Find status", program_id="fixture",
        as_of=AS_OF, items_by_id={}, workstreams=(), integration_error_sink=errors, **filters,
    )
    assert signals == ()
    assert errors == []


@pytest.mark.parametrize("value", ["none", "NONE", " None "])
def test_none_confidence_round_trips_to_the_actual_member(value: str) -> None:
    assert Confidence.from_string(value) is Confidence.NONE


def test_unknown_risk_uses_actual_member_and_preserves_invalid_input_default() -> None:
    assert RiskLevel.from_string("unknown") is RiskLevel.UNKNOWN
    assert RiskLevel.from_string("invalid") is RiskLevel.UNKNOWN


def test_distinct_messages_in_the_same_conversation_keep_native_identity() -> None:
    signals = gather._signals_from_workiq_payload(
        payload={"messages": [
            {"id": identity, "conversationId": "shared", "timestamp": SOURCE_TIME, "summary": "Observation"}
            for identity in ("native-1", "native-2")
        ]}, query_name="teams_search:fixture", question="Find status", program_id="fixture",
        as_of=AS_OF, items_by_id={}, workstreams=(),
    )
    assert len(signals) == 2
    assert {signal.metadata["native_message_id"] for signal in signals} == {"native-1", "native-2"}
    assert signals[0].id != signals[1].id


def test_live_builder_forwards_quarantine_to_existing_error_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture",
        ado=ADOConfig(
            organization="fixture", project="Fixture", area_paths=(),
            work_item_types=("Feature",), excluded_states=(), date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Find status"}),
    )
    bridge = Mock(spec=AgencyBridge)
    bridge.probe.return_value.available = True
    bridge.probe.return_value.has_workiq = True
    bridge.ask_workiq.return_value = {"emails": [{"summary": "Unqualified observation"}]}
    monkeypatch.setattr(
        gather, "_build_workiq_query_plans",
        lambda **_kwargs: (WorkIQQueryPlan(query_name="feedback_search:fixture", question="Find status"),),
    )
    errors: list[IntegrationError] = []
    signals = gather._build_workiq_signals(
        program=program, program_id="fixture", as_of=AS_OF, items=(), workstreams=(),
        bridge=lambda: bridge, m365_topic_router=Mock(spec=IM365TopicRouter),
        integration_error_sink=errors,
    )
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert errors[0].stage == "quarantine"
    assert errors[0].retryable is False
    bridge.ask_workiq.assert_called_once()


@pytest.mark.parametrize("attempted", [0, 1])
@pytest.mark.parametrize("with_sink", [False, True])
def test_workiq_plan_budget_reports_skipped_plans_and_preserves_partial_results(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, attempted: int, with_sink: bool,
) -> None:
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture",
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
    )
    plans = tuple(
        WorkIQQueryPlan(
            query_name=f"feedback_search:private-lane-{index}", question=f"Private question {index}",
            mcp_tool="search_emails" if index == 2 else None,
        )
        for index in range(3)
    )
    monkeypatch.setattr(gather, "_build_workiq_query_plans", lambda **_: plans)
    clock = [0.0]
    monkeypatch.setattr(gather, "monotonic", lambda: clock[0])
    bridge = Mock(spec=AgencyBridge)
    bridge.probe.return_value.available = True
    bridge.probe.return_value.has_workiq = True

    def ask(*_args: object, **_kwargs: object) -> dict[str, object]:
        clock[0] = 5.0
        return {"emails": [{"id": "valid-message", "receivedDateTime": SOURCE_TIME, "summary": "Synthetic status"}]}

    bridge.ask_workiq.side_effect = ask
    errors: list[IntegrationError] = []
    signals = gather._build_workiq_signals(
        program=program, program_id="fixture", as_of=AS_OF, items=(), workstreams=(),
        bridge=lambda: bridge, total_budget_seconds=5 if attempted else 0,
        integration_error_sink=errors if with_sink else None,
    )

    diagnostics = tuple(signal for signal in signals if signal.source == "system")
    observations = tuple(signal for signal in signals if signal.source.startswith("workiq/"))
    assert len(diagnostics) == 1
    assert len(observations) == attempted
    diagnostic = diagnostics[0]
    assert diagnostic.confidence is Confidence.NONE
    assert diagnostic.entity_refs == ()
    assert diagnostic.metadata["workiq_plan_budget_exhausted"] is True
    assert diagnostic.metadata["error"] == "WorkIQ query-plan budget exhausted; at least one plan was not attempted."
    assert diagnostic.metadata["source_event_time"] is None
    assert diagnostic.metadata["temporal_confidence"] == "unknown"
    assert len(errors) == int(with_sink)
    if errors:
        assert errors[0].source == "workiq"
        assert errors[0].stage == "budget"
        assert errors[0].retryable is True
        assert errors[0].message == diagnostic.metadata["error"]
        assert errors[0].operator_action == "Review the WorkIQ query-plan time budget and retry."
    assert bridge.ask_workiq.call_count == attempted
    bridge.invoke_mcp_tool.assert_not_called()
    for private in ("private-lane", "Private question"):
        assert private not in repr(diagnostic) + repr(errors) + caplog.text


@pytest.mark.parametrize("total_budget", [None, 5])
def test_workiq_plan_budget_does_not_report_when_all_plans_were_attempted(
    monkeypatch: pytest.MonkeyPatch, total_budget: int | None,
) -> None:
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture",
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
    )
    plan_count = 2 if total_budget is None else 1
    plans = tuple(WorkIQQueryPlan(query_name=f"feedback_search:{i}", question="Synthetic query") for i in range(plan_count))
    monkeypatch.setattr(gather, "_build_workiq_query_plans", lambda **_: plans)
    clock = [0.0]
    monkeypatch.setattr(gather, "monotonic", lambda: clock[0])
    bridge = Mock(spec=AgencyBridge)
    bridge.probe.return_value.available = True
    bridge.probe.return_value.has_workiq = True
    calls = [0]

    def ask(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls[0] += 1
        clock[0] = 5.0
        return {"emails": [{"id": f"valid-{calls[0]}", "receivedDateTime": SOURCE_TIME, "summary": "Synthetic status"}]}

    bridge.ask_workiq.side_effect = ask
    errors: list[IntegrationError] = []
    signals = gather._build_workiq_signals(
        program=program, program_id="fixture", as_of=AS_OF, items=(), workstreams=(),
        bridge=lambda: bridge, total_budget_seconds=total_budget, integration_error_sink=errors,
    )

    assert bridge.ask_workiq.call_count == plan_count
    assert len(signals) == plan_count
    assert all(signal.source.startswith("workiq/") for signal in signals)
    assert errors == []


def test_workiq_plan_budget_coexists_with_quarantine_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture",
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
    )
    plans = (_structured_plan(), _structured_plan())
    monkeypatch.setattr(gather, "_build_workiq_query_plans", lambda **_: plans)
    clock = [0.0]
    monkeypatch.setattr(gather, "monotonic", lambda: clock[0])
    bridge = Mock(spec=AgencyBridge)
    bridge.probe.return_value.available = True
    bridge.probe.return_value.has_workiq = True

    def ask(*_args: object, **_kwargs: object) -> dict[str, object]:
        clock[0] = 5.0
        return {"emails": [{"id": "private-record", "receivedDateTime": "invalid"}]}

    bridge.ask_workiq.side_effect = ask
    errors: list[IntegrationError] = []
    signals = gather._build_workiq_signals(
        program=program, program_id="fixture", as_of=AS_OF, items=(), workstreams=(),
        bridge=lambda: bridge, total_budget_seconds=5, integration_error_sink=errors,
    )

    assert len(signals) == len(errors) == 2
    assert {error.stage for error in errors} == {"budget", "quarantine"}
    assert sum((signal.metadata or {}).get("workiq_plan_budget_exhausted") is True for signal in signals) == 1
    assert sum((signal.metadata or {}).get("quarantined") is True for signal in signals) == 1
    assert "private-record" not in repr(signals) + repr(errors)


@pytest.mark.parametrize("error", [None, "", 12])
def test_malformed_cached_quarantine_uses_existing_corrupt_snapshot_recovery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: object,
) -> None:
    payload = workiq_signals_to_payload(_parse({"summary": "Unknown source"}))
    payload["signals"][0]["meta"]["error"] = error
    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=payload, watermark=None,
        completeness="complete", latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=AS_OF,
    )
    live = Mock(return_value=())
    errors: list[IntegrationError] = []
    assert resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path, live_fetch_fn=live,
        now=AS_OF, integration_error_sink=errors,
    ) == ()
    live.assert_called_once()
    assert errors == []
    assert "WorkIQ quarantine diagnostic" in caplog.text


def test_quarantine_diagnostics_are_capped_at_three_reason_combinations() -> None:
    errors: list[IntegrationError] = []
    signals = gather._signals_from_workiq_payload(
        payload={"emails": [
            record for _ in range(100)
            for record in ({}, {"id": "native"}, {"timestamp": SOURCE_TIME})
        ]}, query_name="feedback_search:fixture", question="Find status", program_id="fixture",
        as_of=AS_OF, items_by_id={}, workstreams=(), integration_error_sink=errors,
    )
    assert len(signals) == len(errors) == 3
    assert sum(signal.metadata["quarantined_record_count"] for signal in signals) == 300
    assert all(signal.source == "system" for signal in signals)


def test_top_level_gather_forwards_its_existing_error_sink_to_cached_and_live_workiq() -> None:
    tree = ast.parse(inspect.getsource(gather._gather_program_impl))
    for name in ("_resolve_workiq_signals", "_build_workiq_signals"):
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        ]
        assert len(calls) == 1
        sink = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "integration_error_sink")
        assert isinstance(sink, ast.Name)
        assert sink.id == "integration_error_details"


@pytest.mark.parametrize("payload", [None, {}])
def test_no_observations_preserve_existing_empty_result_contract(payload: dict[str, Any] | None) -> None:
    errors: list[IntegrationError] = []
    assert gather._signals_from_workiq_payload(
        payload=payload, query_name="feedback_search:fixture", question="Find status",
        program_id="fixture", as_of=AS_OF, items_by_id={}, workstreams=(),
        integration_error_sink=errors,
    ) == ()
    assert errors == []


def _structured_gather(
    monkeypatch: pytest.MonkeyPatch,
    plans: tuple[WorkIQQueryPlan, ...],
    payloads: list[dict[str, Any] | None],
) -> tuple[tuple[Signal, ...], list[IntegrationError]]:
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture",
        ado=ADOConfig(
            organization="fixture", project="Fixture", area_paths=(),
            work_item_types=("Feature",), excluded_states=(), date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Find status"}),
    )
    bridge = Mock(spec=AgencyBridge)
    bridge.probe.return_value.available = True
    bridge.probe.return_value.has_workiq = True
    bridge.ask_workiq.side_effect = payloads
    monkeypatch.setattr(gather, "_build_workiq_query_plans", lambda **_kwargs: plans)
    errors: list[IntegrationError] = []
    signals = gather._build_workiq_signals(
        program=program, program_id="fixture", as_of=AS_OF, items=(), workstreams=(),
        bridge=lambda: bridge, m365_topic_router=Mock(spec=IM365TopicRouter),
        integration_error_sink=errors,
    )
    assert bridge.ask_workiq.call_count == len(plans)
    return signals, errors


def test_unstructured_gather_preserves_partial_results_and_cached_time_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    signals, errors = _structured_gather(
        monkeypatch, (WorkIQQueryPlan(query_name="feedback_search:fixture", question="Find status"),),
        [{"emails": [
            {"id": "private-invalid", "receivedDateTime": "0001-01-01T00:00:00+14:00", "summary": "Private invalid body"},
            {"id": "native-valid", "receivedDateTime": SOURCE_TIME, "summary": "Valid synthetic observation"},
        ]}],
    )
    assert len(signals) == 2
    assert signals[0].metadata["native_message_id"] == "native-valid"
    assert signals[1].source == "system"
    assert signals[1].metadata["quarantined_record_count"] == 1
    assert len(errors) == 1
    assert errors[0].stage == "quarantine"
    assert errors[0].retryable is False
    assert "private-invalid" not in repr(signals + tuple(errors))
    assert "Private invalid body" not in repr(signals + tuple(errors))

    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=workiq_signals_to_payload(signals),
        watermark=None, completeness="degraded", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=AS_OF,
    )
    live = Mock(side_effect=AssertionError("Expected the committed synthetic snapshot"))
    restored_errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path, live_fetch_fn=live,
        now=AS_OF, integration_error_sink=restored_errors,
    )
    live.assert_not_called()
    assert restored == tuple(classify_signal(signal) for signal in signals)
    assert len(restored_errors) == 1
    assert restored_errors[0].stage == "quarantine"
    assert restored_errors[0].retryable is False


def _structured_plan(*, limit: int = 8) -> WorkIQQueryPlan:
    return WorkIQQueryPlan(
        query_name="feedback_search:fixture", question="Find status",
        structured_window_start=date(2026, 10, 20),
        structured_window_end=date(2026, 10, 24),
        structured_result_limit=limit,
    )


@pytest.mark.parametrize(
    ("records", "limit", "expected_rejected", "expected_accepted"),
    [
        ([], 8, 0, 0),
        ([{"id": "outside", "receivedDateTime": "2026-09-01T00:00:00Z"}], 8, 0, 0),
        ([{"id": "private-missing", "subject": "Private invalid subject"}], 8, 1, 0),
        ([
            {"id": "private-invalid", "subject": "Private invalid subject", "receivedDateTime": "invalid"},
            {"id": "accepted", "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME},
            {"id": "private-missing", "from": "private@example.net"},
        ], 8, 2, 1),
        ([
            {"id": "accepted", "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME},
            {"id": "private-missing"},
        ], 1, 0, 1),
        ([
            {"id": "private-missing"},
            {"id": "accepted", "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME},
            {"id": "private-invalid", "receivedDateTime": "invalid"},
        ], 1, 1, 1),
    ],
)
def test_structured_gather_keeps_time_rejections_visible(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    records: list[dict[str, Any]], limit: int, expected_rejected: int, expected_accepted: int,
) -> None:
    signals, errors = _structured_gather(monkeypatch, (_structured_plan(limit=limit),), [{"emails": records}])
    observations = [signal for signal in signals if signal.source != "system"]
    diagnostics = [signal for signal in signals if signal.source == "system"]
    assert len(observations) == expected_accepted
    assert len(diagnostics) == len(errors) == int(expected_rejected > 0)
    if expected_rejected:
        diagnostic = diagnostics[0]
        assert diagnostic.metadata["quarantined_record_count"] == expected_rejected
        assert diagnostic.metadata["source_event_time"] is None
        assert diagnostic.metadata["temporal_confidence"] == "unknown"
        assert diagnostic.metadata["retrieved_at"] == AS_OF.isoformat()
        assert errors[0].stage == "quarantine"
        assert errors[0].retryable is False
        assert "inspected" in errors[0].message
        assert "missing or invalid source time" in errors[0].message
    for private_value in ("private-missing", "private-invalid", "Private invalid subject", "private@example.net"):
        assert private_value not in repr(signals)
        assert private_value not in repr(errors)
        assert private_value not in caplog.text


@pytest.mark.parametrize("include_valid", [False, True])
def test_structured_gather_reports_pre_admission_identity_rejections(
    include_valid: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    records = [
        {"subject": "Private missing sender", "receivedDateTime": SOURCE_TIME},
        {"id": "turn1search2", "receivedDateTime": SOURCE_TIME},
        {"receivedDateTime": "2026-09-01T00:00:00Z"},
    ]
    if include_valid:
        records.append({"id": "valid", "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME})
    signals, live_errors = _structured_gather(monkeypatch, (_structured_plan(),), [{"emails": records}])
    observations = [signal for signal in signals if signal.source != "system"]
    diagnostics = [signal for signal in signals if signal.source == "system"]
    assert len(observations) == int(include_valid)
    assert len(diagnostics) == len(live_errors) == 1
    assert diagnostics[0].metadata["quarantined_record_count"] == 2
    assert live_errors[0].stage == "quarantine" and live_errors[0].retryable is False
    assert "identity" in live_errors[0].message
    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=workiq_signals_to_payload(signals),
        watermark=None, completeness="degraded", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=AS_OF,
    )
    cached_errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path,
        live_fetch_fn=Mock(side_effect=AssertionError("Expected the committed synthetic snapshot")),
        now=AS_OF, integration_error_sink=cached_errors,
    )
    assert len(restored) == len(signals)
    assert cached_errors == live_errors
    for private_value in ("Private missing sender", "turn1search2"):
        assert private_value not in repr(signals) + repr(live_errors) + repr(cached_errors) + caplog.text


def test_structured_gather_aggregates_identity_and_time_rejections_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, errors = _structured_gather(monkeypatch, (_structured_plan(), _structured_plan()), [
        {"emails": [
            {"subject": "Private missing sender", "receivedDateTime": SOURCE_TIME},
            {"id": "private-invalid-time", "receivedDateTime": "invalid"},
        ]},
        {"emails": [
            {"id": "turn1search2", "receivedDateTime": SOURCE_TIME},
            {"id": "valid", "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME},
        ]},
    ])
    assert len(signals) == 3 and len(errors) == 2
    assert sum(signal.source != "system" for signal in signals) == 1
    diagnostics = [signal for signal in signals if signal.source == "system"]
    assert sorted(signal.metadata["quarantined_record_count"] for signal in diagnostics) == [1, 2]
    assert any("identity" in error.message for error in errors)
    assert any("source time" in error.message for error in errors)
    assert all(error.stage == "quarantine" and error.retryable is False for error in errors)
    assert "Private missing sender" not in repr(signals) + repr(errors)
    assert "private-invalid-time" not in repr(signals) + repr(errors)
    assert "turn1search2" not in repr(signals) + repr(errors)


@pytest.mark.parametrize("include_valid", [False, True])
def test_structured_gather_and_replay_retain_malformed_and_unsafe_record_diagnostics(
    include_valid: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    plans = tuple(replace(_structured_plan(), query_name=f"feedback_search:private-lane-{index}") for index in range(3))
    records: list[Any] = [
        None,
        {"id": "private-control", "subject": "Private \x1b[2J content", "receivedDateTime": SOURCE_TIME},
        {"id": "private-missing-time"},
    ]
    if include_valid:
        records.append({
            "id": "valid-message", "conversationId": "valid-thread",
            "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME,
        })
    signals, errors = _structured_gather(monkeypatch, plans, [
        {"emails": records},
        {"emails": [
            None,
            {"id": "private-url", "webUrl": "https://untrusted.invalid/private", "receivedDateTime": SOURCE_TIME},
            {"subject": "Private missing sender", "receivedDateTime": SOURCE_TIME},
        ]},
        None,
    ])

    diagnostics = [signal for signal in signals if signal.source == "system"]
    assert len(diagnostics) == len(errors) == 4
    assert sum(signal.source != "system" for signal in signals) == int(include_valid)
    invalid = next(signal for signal in diagnostics if signal.metadata.get("workiq_structured_record_invalid") is True)
    assert invalid.metadata["quarantined_record_count"] == 4
    invalid_error = next(error for error in errors if "malformed or unsafe" in error.message)
    assert invalid_error.stage == "quarantine" and invalid_error.retryable is False
    assert invalid_error.operator_action == "Check record format, remove terminal controls and verify permitted mail URLs before retrying."
    assert sum(error.stage == "discovery" and error.retryable for error in errors) == 1

    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=workiq_signals_to_payload(signals),
        watermark=None, completeness="degraded", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=AS_OF,
    )
    restored_errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path,
        live_fetch_fn=Mock(side_effect=AssertionError("Expected the committed synthetic snapshot")),
        now=AS_OF, integration_error_sink=restored_errors,
    )
    assert len(restored) == len(signals)
    assert {(error.message, error.operator_action, error.retryable) for error in restored_errors} == {
        (error.message, error.operator_action, error.retryable) for error in errors
    }
    assert "private-lane" not in repr(diagnostics) + repr(errors) + repr(restored_errors) + caplog.text
    for private_value in (
        "private-control", "Private", "\\x1b[2J", "private-missing-time",
        "private-url", "untrusted.invalid",
    ):
        assert private_value not in repr(signals) + repr(errors) + repr(restored_errors) + caplog.text


def test_structured_gather_preserves_valid_native_fallback_with_partial_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, errors = _structured_gather(monkeypatch, (_structured_plan(),), [{"emails": [
        {"conversationId": "turn1search2", "id": "native-message", "receivedDateTime": SOURCE_TIME},
        {"subject": "Private missing sender", "receivedDateTime": SOURCE_TIME},
    ]}])
    observations = [signal for signal in signals if signal.source != "system"]
    diagnostics = [signal for signal in signals if signal.source == "system"]
    assert len(observations) == len(diagnostics) == len(errors) == 1
    assert observations[0].metadata["native_message_id"] == "native-message"
    assert diagnostics[0].metadata["quarantined_record_count"] == 1
    assert errors[0].stage == "quarantine" and errors[0].retryable is False
    assert "Private missing sender" not in repr(signals) + repr(errors)
    assert "turn1search2" not in repr(signals) + repr(errors)


@pytest.mark.parametrize("thread_id", [
    "http://[", "turn1search2", "semantic:preview",
    "https://example.invalid/?threadId=semantic%3Apreview", 123,
])
def test_structured_gather_does_not_route_by_invalid_thread_field(
    thread_id: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, errors = _structured_gather(monkeypatch, (_structured_plan(),), [{"emails": [{
        "conversationId": "native-conversation", "threadId": thread_id,
        "id": "native-message", "subject": "Synthetic", "receivedDateTime": SOURCE_TIME,
    }]}])

    assert errors == []
    assert len(signals) == 1
    assert signals[0].metadata["native_message_id"] == "native-message"
    assert signals[0].thread_id == "native-conversation"


def test_structured_gather_quarantines_provisional_preview_from_signal_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, errors = _structured_gather(monkeypatch, (_structured_plan(),), [{"emails": [{
        "conversationId": "http://[", "id": "turn1search2", "subject": "Synthetic",
        "from": "owner@example.com", "receivedDateTime": SOURCE_TIME,
    }]}])

    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata["quarantined_record_count"] == 1
    assert "semantic:" not in repr(signals) + repr(errors)
    assert "Synthetic" not in repr(signals) + repr(errors)


def test_structured_gather_admits_native_thread_without_claiming_native_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, errors = _structured_gather(monkeypatch, (_structured_plan(),), [{"emails": [{
        "conversationId": "turn1search2",
        "threadId": "https://example.invalid/?threadId=native-thread",
        "id": "turn2result3", "subject": "Synthetic", "receivedDateTime": SOURCE_TIME,
    }]}])

    assert errors == []
    assert len(signals) == 1
    assert signals[0].metadata["native_message_id"] is None
    assert signals[0].metadata["source_identity_kind"] == "native_thread"
    assert signals[0].thread_id == "native-thread"


def test_preview_only_workiq_identity_never_becomes_observed_thread_or_transcript_evidence() -> None:
    preview = Signal(
        id="preview",
        timestamp=AS_OF,
        source="workiq/transcript",
        program_id="fixture",
        workstream_id="fixture.lane",
        entity_refs=(),
        text="Provisional transcript preview",
        raw_ref="workiq:transcript:semantic:preview",
        confidence=Confidence.MEDIUM,
        metadata={
            "source_type": "transcript",
            "message_id": "semantic:preview",
            "native_message_id": "semantic:preview",
            "source_identity_kind": "semantic_preview",
        },
        thread_id="semantic:thread",
    )

    assert gather._observed_m365_thread_ids((preview,)) == set()
    assert gather._observed_m365_series_ids((preview,)) == set()
    from src.commands.gather_pipeline.evidence_extraction_stage import _filter_transcript_signals
    assert _filter_transcript_signals((preview,)) == []


def test_structured_gather_normalizes_native_thread_url_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, errors = _structured_gather(monkeypatch, (_structured_plan(),), [{"emails": [{
        "conversationId": "semantic:preview",
        "threadId": "https://example.invalid/?threadId=native-thread",
        "id": "native-message", "subject": "Synthetic", "receivedDateTime": SOURCE_TIME,
    }]}])

    assert errors == []
    assert len(signals) == 1
    assert signals[0].thread_id == "native-thread"
    assert signals[0].metadata["native_message_id"] == "native-message"


def test_distinct_workiq_quarantines_survive_live_deduplication_and_cached_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = (
        _structured_plan(),
        WorkIQQueryPlan(query_name="legacy:missing-id", question="Find status"),
        WorkIQQueryPlan(query_name="legacy:missing-time", question="Find status"),
    )
    signals, live_errors = _structured_gather(monkeypatch, plans, [
        {"emails": [{"id": "private-time-id", "receivedDateTime": "invalid"}]},
        {"emails": [{"timestamp": SOURCE_TIME}]},
        {"emails": [{"id": "native-without-time"}]},
    ])
    assert len(signals) == len(live_errors) == 3
    assert all(signal.source == "system" and signal.metadata["quarantined"] for signal in signals)
    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=workiq_signals_to_payload(signals),
        watermark=None, completeness="degraded", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=AS_OF,
    )
    cached_errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path,
        live_fetch_fn=Mock(side_effect=AssertionError("Expected the committed synthetic snapshot")),
        now=AS_OF, integration_error_sink=cached_errors,
    )
    assert len(restored) == len(cached_errors) == 3
    assert {error.message for error in cached_errors} == {error.message for error in live_errors}


def test_structured_gather_aggregates_inspected_occurrences_without_query_identifiers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    plans = (
        replace(_structured_plan(), query_name="feedback_search:private-lane-one"),
        replace(_structured_plan(), query_name="feedback_search:private-lane-two"),
    )
    signals, errors = _structured_gather(monkeypatch, plans, [
        {"emails": [{"id": "private-message", "receivedDateTime": "invalid"}]},
        {"emails": [{"id": "private-message"}]},
    ])
    assert len(signals) == len(errors) == 1
    assert signals[0].metadata["quarantined_record_count"] == 2
    assert "occurrence(s)" in errors[0].message
    for private_value in ("private-message", "private-lane-one", "private-lane-two"):
        assert private_value not in repr(signals)
        assert private_value not in repr(errors)
        assert private_value not in caplog.text


@pytest.mark.parametrize("payload", [
    None, {}, {"emails": None}, {"emails": {}}, {"response": "Private malformed response"},
])
def test_structured_gather_reports_unusable_responses_without_record_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, payload: dict[str, Any] | None,
) -> None:
    signals, errors = _structured_gather(
        monkeypatch, (replace(_structured_plan(), query_name="feedback_search:private-lane"),), [payload],
    )
    assert len(signals) == len(errors) == 1
    assert signals[0].source == "system"
    assert signals[0].metadata["workiq_structured_response_unusable"] is True
    assert "quarantined_record_count" not in signals[0].metadata
    assert errors[0].stage == "discovery" and errors[0].retryable is True
    assert "1 attempted plan(s)" in errors[0].message
    for private_value in ("private-lane", "Private malformed response"):
        assert private_value not in repr(signals) + repr(errors) + caplog.text


def test_structured_gather_preserves_partial_results_and_distinct_cached_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = tuple(replace(_structured_plan(), query_name=f"feedback_search:private-lane-{index}") for index in range(3))
    signals, errors = _structured_gather(monkeypatch, plans, [
        None,
        {"response": "Private malformed response"},
        {"emails": [
            {"id": "private-missing"},
            {"id": "valid", "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME},
        ]},
    ])
    assert len(signals) == 3 and len(errors) == 2
    assert any(signal.source == "workiq/email" for signal in signals)
    discovery = next(error for error in errors if error.stage == "discovery")
    assert discovery.retryable is True and "2 attempted plan(s)" in discovery.message
    assert any(error.stage == "quarantine" for error in errors)
    write_prefetch_snapshot(
        program_id="fixture", channel="workiq", payload=workiq_signals_to_payload(signals),
        watermark=None, completeness="degraded", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=AS_OF,
    )
    cached_errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="fixture", programs_root=tmp_path,
        live_fetch_fn=Mock(side_effect=AssertionError("Expected the committed synthetic snapshot")),
        now=AS_OF, integration_error_sink=cached_errors,
    )
    assert len(restored) == 3
    assert {(error.stage, error.message, error.retryable, error.operator_action) for error in cached_errors} == {
        (error.stage, error.message, error.retryable, error.operator_action) for error in errors
    }
    diagnostics = tuple(signal for signal in restored if signal.source == "system")
    for private_value in ("private-lane", "private-missing", "Private malformed response"):
        assert private_value not in repr(diagnostics) + repr(cached_errors)
    assert "private-missing" not in repr(restored)


def test_real_structured_planner_carries_time_rejections_into_gather() -> None:
    program = Program(
        schema_version="2.0", id="fixture", name="Fixture",
        ado=ADOConfig(
            organization="fixture", project="Fixture", area_paths=(),
            work_item_types=("Feature",), excluded_states=(), date_window_days=14,
            api_timeout_seconds=30,
        ),
        m365=M365Config(
            enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Find status"},
            retrieval=WorkIQRetrievalConfig(discovery_mode="structured_json"),
        ),
    )
    workstream = Workstream(
        id="synthetic", name="Synthetic lane",
        signal_sources=WorkstreamSignalSources(workiq_keywords=("synthetic",)),
    )
    bridge = Mock(spec=AgencyBridge)
    bridge.probe.return_value.available = True
    bridge.probe.return_value.has_workiq = True
    bridge.ask_workiq.return_value = {"emails": [
        {"id": "private-missing"},
        {"id": "valid", "subject": "Synthetic valid", "receivedDateTime": SOURCE_TIME},
    ]}
    errors: list[IntegrationError] = []

    signals = gather._build_workiq_signals(
        program=program, program_id="fixture", as_of=AS_OF, items=(), workstreams=(workstream,),
        bridge=lambda: bridge, integration_error_sink=errors,
    )

    bridge.ask_workiq.assert_called_once()
    assert "Which of my emails" in bridge.ask_workiq.call_args.args[0]
    assert len(signals) == 2 and len(errors) == 1
    assert signals[0].source == "workiq/email"
    assert signals[1].metadata["quarantined_record_count"] == 1
    assert "private-missing" not in repr(signals) + repr(errors)


def test_workiq_deduplication_preserves_distinct_diagnostics_but_not_exact_replays() -> None:
    first = _parse({"id": "native-without-time"})[0]
    second = _parse({"timestamp": SOURCE_TIME})[0]
    assert len(gather._dedupe_workiq_signals((first, second, first))) == 2
