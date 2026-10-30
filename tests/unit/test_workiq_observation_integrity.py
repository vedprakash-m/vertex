from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from src.commands import gather
from src.commands.gather_workiq_helpers import WorkIQQueryPlan
from src.commands.gather_pipeline.workiq_prefetch_stage import (
    resolve_workiq_signals, workiq_signals_to_payload,
)
from src.core.models import Confidence, RiskLevel
from src.core.m365_router_interface import IM365TopicRouter
from src.core.models_v2 import ADOConfig, IntegrationError, M365Config, Program, Signal
from src.core.prefetch_store import write_prefetch_snapshot
from src.core.signal_classification import classify_signal
from src.m365.agency_bridge import AgencyBridge


AS_OF = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
SOURCE_TIME = "2026-10-23T09:00:00Z"


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
