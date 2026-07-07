"""Unit tests for IcMSignalExtractor."""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.icm_signal_extractor import IcMSignalExtractor
from src.core.integration_types import IcMHydrationOutput, IncidentState

_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)


def _incident(*, workstream_ids: tuple[str, ...] = ("ws-a",)) -> IncidentState:
    return IncidentState(
        incident_id="98765",
        title="Disk full on storage node",
        severity=1,
        status="Active",
        owning_team="StoragePM",
        updated_at=_NOW,
        workstream_ids=workstream_ids,
    )


def test_extract_incident_emits_signal() -> None:
    output = IcMHydrationOutput(incident_states=(_incident(),))
    result = IcMSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 1
    sig = result.signals[0]
    assert sig.source == "icm"
    assert sig.program_id == "prog1"
    assert sig.workstream_id == "ws-a"
    assert "icm:98765" in sig.entity_refs
    assert "Sev 1" in sig.text
    assert "Disk full" in sig.text


def test_extract_fans_out_per_workstream() -> None:
    output = IcMHydrationOutput(incident_states=(_incident(workstream_ids=("ws-a", "ws-b")),))
    result = IcMSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 2
    assert {s.workstream_id for s in result.signals} == {"ws-a", "ws-b"}
    assert {s.entity_refs for s in result.signals} == {
        ("icm:98765", "WS:ws-a"),
        ("icm:98765", "WS:ws-b"),
    }


def test_extract_incident_preserves_explicit_work_item_refs() -> None:
    incident = IncidentState(
        incident_id="22222",
        title="WI:45678 rollout validation regressed after failover",
        severity=2,
        status="Active",
        owning_team="StoragePM",
        updated_at=_NOW,
        workstream_ids=("ws-a",),
    )
    output = IcMHydrationOutput(incident_states=(incident,))
    result = IcMSignalExtractor().extract(output, "prog1")

    assert result.signals[0].entity_refs == ("icm:22222", "WS:ws-a", "WI:45678")


def test_extract_empty_output_returns_empty_signals() -> None:
    output = IcMHydrationOutput(incident_states=())
    result = IcMSignalExtractor().extract(output, "prog1")

    assert result.signals == ()
    assert result.errors == ()
    assert result.channel == "icm"


def test_extract_no_workstream_ids_uses_none_workstream() -> None:
    incident = IncidentState(
        incident_id="11111",
        title="Test",
        severity=2,
        status="Mitigated",
        owning_team="Team",
        updated_at=_NOW,
        workstream_ids=(),
    )
    output = IcMHydrationOutput(incident_states=(incident,))
    result = IcMSignalExtractor().extract(output, "prog1")

    assert len(result.signals) == 1
    assert result.signals[0].workstream_id is None


def test_extract_severity_none_shows_unknown() -> None:
    incident = IncidentState(
        incident_id="22222",
        title="Unknown sev",
        severity=None,
        status="Active",
        owning_team="Team",
        updated_at=_NOW,
        workstream_ids=("ws-a",),
    )
    output = IcMHydrationOutput(incident_states=(incident,))
    result = IcMSignalExtractor().extract(output, "prog1")

    assert "Sev ?" in result.signals[0].text


def test_extract_signal_ids_are_deterministic() -> None:
    output = IcMHydrationOutput(incident_states=(_incident(),))
    result1 = IcMSignalExtractor().extract(output, "prog1")
    result2 = IcMSignalExtractor().extract(output, "prog1")

    assert result1.signals[0].id == result2.signals[0].id


def test_extract_metadata_has_expected_fields() -> None:
    output = IcMHydrationOutput(incident_states=(_incident(),))
    result = IcMSignalExtractor().extract(output, "prog1")

    meta = result.signals[0].metadata
    assert meta is not None
    assert meta["incident_id"] == "98765"
    assert meta["severity"] == 1
    assert meta["owning_team"] == "StoragePM"
