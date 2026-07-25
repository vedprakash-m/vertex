from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.signal_dedup import build_deterministic_signal_id, dedupe_signals


def test_dedupe_signals_skips_same_signal_fingerprint() -> None:
    original = Signal(
        id="sig-001",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="ado/odata",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="State changed from Proposed to Active.",
        raw_ref="workitems/1001",
        confidence=Confidence.HIGH,
        metadata={"field": "State", "prior": "Proposed", "current": "Active"},
    )
    repeated = Signal(
        id="sig-002",
        timestamp=datetime(2026, 5, 8, 12, 5, tzinfo=timezone.utc),
        source="ado/odata",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="State changed from Proposed to Active.",
        raw_ref="workitems/1001",
        confidence=Confidence.HIGH,
        metadata={"field": "State", "prior": "Proposed", "current": "Active"},
    )

    kept = dedupe_signals((original, repeated))

    assert kept == (original,)


def test_deterministic_signal_id_uses_semantic_content_not_extractor_id() -> None:
    original = Signal(
        id="extractor-attempt-one",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="ado/odata",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="State changed from Proposed to Active.",
        raw_ref="workitems/1001",
        confidence=Confidence.HIGH,
        metadata={"field": "State", "prior": "Proposed", "current": "Active"},
    )
    replay = Signal(
        id="extractor-attempt-two",
        timestamp=original.timestamp,
        source=original.source,
        program_id=original.program_id,
        workstream_id=original.workstream_id,
        entity_refs=original.entity_refs,
        text=original.text,
        raw_ref=original.raw_ref,
        confidence=original.confidence,
        metadata=original.metadata,
    )

    assert build_deterministic_signal_id(original) == build_deterministic_signal_id(replay)
    assert build_deterministic_signal_id(original).startswith("sig_")


def test_dedupe_signals_keeps_equivalent_payloads_from_different_sources() -> None:
    ado_signal = Signal(
        id="sig-001",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="ado/odata",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Target date moved later.",
        raw_ref="workitems/1001",
        confidence=Confidence.HIGH,
        metadata={"field": "TargetDate", "prior": "2026-05-10", "current": "2026-05-17"},
    )
    manual_signal = Signal(
        id="sig-002",
        timestamp=datetime(2026, 5, 8, 12, 1, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Target date moved later.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata=None,
    )

    kept = dedupe_signals((ado_signal, manual_signal))

    assert kept == (ado_signal, manual_signal)


def test_dedupe_signals_treats_icm_family_sources_as_same_incident() -> None:
    original = Signal(
        id="sig-icm-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="icm",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("ICM:12345",),
        text="IcM 12345: Sev2 incident active.",
        raw_ref="icm:12345",
        confidence=Confidence.HIGH,
        metadata={"incident_id": "12345", "severity": 2},
    )
    repeated = Signal(
        id="sig-icm-2",
        timestamp=datetime(2026, 5, 8, 12, 5, tzinfo=timezone.utc),
        source="icm/incident",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("ICM:12345",),
        text="IcM 12345: Sev2 incident active.",
        raw_ref="icm:12345",
        confidence=Confidence.HIGH,
        metadata={"incident_id": "12345", "severity": 2},
    )

    kept = dedupe_signals((original, repeated))

    assert kept == (original,)


def test_dedupe_signals_keeps_kusto_kpi_time_series_points_with_same_value() -> None:
    original = Signal(
        id="sig-kpi-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="kusto_kpi",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="KPI Deploy P50 (hrs): 4.2",
        raw_ref="kusto_kpi:acme-deployment-velocity:2026-05-08T12:00:00+00:00",
        confidence=Confidence.HIGH,
        metadata={
            "query_id": "acme-deployment-velocity",
            "event_timestamp": "2026-05-08T12:00:00+00:00",
            "result_value": "4.2",
        },
    )
    repeated = Signal(
        id="sig-kpi-2",
        timestamp=datetime(2026, 5, 8, 13, 0, tzinfo=timezone.utc),
        source="kusto_kpi",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="KPI Deploy P50 (hrs): 4.2",
        raw_ref="kusto_kpi:acme-deployment-velocity:2026-05-08T13:00:00+00:00",
        confidence=Confidence.HIGH,
        metadata={
            "query_id": "acme-deployment-velocity",
            "event_timestamp": "2026-05-08T13:00:00+00:00",
            "result_value": "4.2",
        },
    )

    kept = dedupe_signals((original, repeated))

    assert kept == (original, repeated)


def test_dedupe_signals_keeps_workiq_near_duplicates_below_maturity_gate() -> None:
    original = Signal(
        id="sig-workiq-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment readiness remains blocked because diagnostics telemetry is still incomplete for the staged rollout review.",
        raw_ref="workiq:msg-1",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-1"},
    )
    repeated = Signal(
        id="sig-workiq-2",
        timestamp=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
        source="workiq/teams",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment readiness is still blocked because diagnostics telemetry remains incomplete for the staged rollout review.",
        raw_ref="workiq:msg-2",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-2"},
    )

    kept = dedupe_signals((original, repeated), program_maturity_level=1)

    assert kept == (original, repeated)


def test_dedupe_signals_semantically_dedupes_workiq_near_duplicates_at_l2() -> None:
    original = Signal(
        id="sig-workiq-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment readiness remains blocked because diagnostics telemetry is still incomplete for the staged rollout review.",
        raw_ref="workiq:msg-1",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-1"},
    )
    repeated = Signal(
        id="sig-workiq-2",
        timestamp=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
        source="workiq/teams",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment readiness is still blocked because diagnostics telemetry remains incomplete for the staged rollout review.",
        raw_ref="workiq:msg-2",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-2"},
    )

    kept = dedupe_signals((original, repeated), program_maturity_level=2)

    assert kept == (original,)


def test_dedupe_signals_semantically_dedupes_when_workstream_ids_merely_overlap_at_l2() -> None:
    """BL-F2 decision (2026-07-24): "same workstream" for dedup means ANY
    shared workstream, not an exact scalar match -- a signal explicitly
    shared across two workstreams (one of which matches the other signal's
    single workstream) must still be treated as a potential duplicate."""
    original = Signal(
        id="sig-workiq-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment readiness remains blocked because diagnostics telemetry is still incomplete for the staged rollout review.",
        raw_ref="workiq:msg-1",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-1"},
    )
    repeated = Signal(
        id="sig-workiq-2",
        timestamp=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
        source="workiq/teams",
        program_id="acme",
        workstream_id="rollout_safety",
        workstream_ids=("rollout_safety", "deployment_readiness"),
        entity_refs=("WI:1001",),
        text="Deployment readiness is still blocked because diagnostics telemetry remains incomplete for the staged rollout review.",
        raw_ref="workiq:msg-2",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-2"},
    )

    kept = dedupe_signals((original, repeated), program_maturity_level=2)

    assert kept == (original,)


def test_dedupe_signals_keeps_workiq_near_duplicates_with_different_entities_at_l2() -> None:
    original = Signal(
        id="sig-workiq-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Deployment readiness remains blocked because diagnostics telemetry is still incomplete for the staged rollout review.",
        raw_ref="workiq:msg-1",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-1"},
    )
    repeated = Signal(
        id="sig-workiq-2",
        timestamp=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
        source="workiq/teams",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:2002",),
        text="Deployment readiness is still blocked because diagnostics telemetry remains incomplete for the staged rollout review.",
        raw_ref="workiq:msg-2",
        confidence=Confidence.HIGH,
        metadata={"message_id": "msg-2"},
    )

    kept = dedupe_signals((original, repeated), program_maturity_level=2)

    assert kept == (original, repeated)


def test_dedupe_signals_coalesces_same_day_vertex_catchup_events_by_kind_and_new_value() -> None:
    original = Signal(
        id="sig-catchup-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="vertex/catchup",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="ADO#1001 target date changed from 2026-05-10 to 2026-05-17.",
        raw_ref="wi:1001:rev:7:targetdate",
        confidence=Confidence.HIGH,
        metadata={
            "work_item_id": 1001,
            "field": "TargetDate",
            "prior": "2026-05-10",
            "current": "2026-05-17",
            "catchup_origin": "ado/revision",
        },
    )
    repeated = Signal(
        id="sig-catchup-2",
        timestamp=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        source="vertex/catchup",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="ADO#1001 target date changed from 2026-05-12 to 2026-05-17.",
        raw_ref="wi:1001:rev:8:targetdate",
        confidence=Confidence.HIGH,
        metadata={
            "work_item_id": 1001,
            "field": "TargetDate",
            "prior": "2026-05-12",
            "current": "2026-05-17",
            "catchup_origin": "ado/revision",
        },
    )

    kept = dedupe_signals((original, repeated))

    assert kept == (original,)


def test_dedupe_signals_keeps_vertex_catchup_events_with_different_new_values() -> None:
    original = Signal(
        id="sig-catchup-1",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="vertex/catchup",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="ADO#1001 target date changed from 2026-05-10 to 2026-05-17.",
        raw_ref="wi:1001:rev:7:targetdate",
        confidence=Confidence.HIGH,
        metadata={
            "work_item_id": 1001,
            "field": "TargetDate",
            "prior": "2026-05-10",
            "current": "2026-05-17",
            "catchup_origin": "ado/revision",
        },
    )
    changed = Signal(
        id="sig-catchup-2",
        timestamp=datetime(2026, 5, 8, 16, 30, tzinfo=timezone.utc),
        source="vertex/catchup",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="ADO#1001 target date changed from 2026-05-17 to 2026-05-21.",
        raw_ref="wi:1001:rev:8:targetdate",
        confidence=Confidence.HIGH,
        metadata={
            "work_item_id": 1001,
            "field": "TargetDate",
            "prior": "2026-05-17",
            "current": "2026-05-21",
            "catchup_origin": "ado/revision",
        },
    )

    kept = dedupe_signals((original, changed))

    assert kept == (original, changed)
