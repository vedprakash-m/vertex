from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.incident_journal_store import append_incident_entry, get_incident_journal_path, read_incident_entries
from src.core.models import Confidence
from src.core.models_v2 import IncidentEntry


def test_append_and_read_incident_entries_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = IncidentEntry(
        program_id="acme",
        incident_id="12345",
        signal_id="sig-incident-1",
        observed_at=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        belief_change_summary="IcM 12345: Fleet capacity alert; status=Active; age=2h",
        workstream_id="acme",
        owning_team="Adventure Core",
        severity=2,
        source_path="agency",
        query_id="icm-active",
        linked_work_item_ids=(101, 202),
        ado_entity_refs=("WI:101", "WI:202"),
        raw_ref="icm:12345",
        confidence=Confidence.HIGH,
    )

    target = append_incident_entry(entry, programs_root=programs_root)

    assert target == get_incident_journal_path("acme", programs_root=programs_root)
    assert target.exists()
    assert read_incident_entries("acme", programs_root=programs_root) == (entry,)


def test_read_incident_entries_filters_by_recorded_window(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    older = IncidentEntry(
        program_id="acme",
        incident_id="12345",
        signal_id="sig-incident-1",
        observed_at=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        belief_change_summary="older incident",
        workstream_id=None,
        confidence=Confidence.MEDIUM,
    )
    newer = IncidentEntry(
        program_id="acme",
        incident_id="12346",
        signal_id="sig-incident-2",
        observed_at=datetime(2026, 5, 11, 6, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc),
        belief_change_summary="newer incident",
        workstream_id="acme",
        confidence=Confidence.HIGH,
    )

    append_incident_entry(older, programs_root=programs_root)
    append_incident_entry(newer, programs_root=programs_root)

    entries = read_incident_entries(
        "acme",
        start=datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert entries == (newer,)


def test_read_incident_entries_rejects_numeric_string_linked_work_item_ids(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":["101"],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="linked_work_item_ids must contain integers only"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_list_linked_work_item_ids(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":"101","ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="linked_work_item_ids must be a list of integers"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_string_incident_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":12345,"signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="incident_id must be a string"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_string_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":123,"belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="recorded_at must be a string"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_naive_observed_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="observed_at must include timezone information"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_naive_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="recorded_at must include timezone information"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_string_workstream_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":999,"owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="workstream_id must be a string"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_string_raw_ref(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":12345,"confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="raw_ref must be a string"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_numeric_string_severity(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":"2","source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="severity must be an integer"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_string_ado_entity_refs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":[101],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="ado_entity_refs must contain strings only"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_string_source_path(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":123,"query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":"high"}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="source_path must be a string"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_string_confidence(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        '{"schema_version":"1.0","program_id":"acme","incident_id":"12345","signal_id":"sig-incident-1","observed_at":"2026-05-10T06:00:00+00:00","recorded_at":"2026-05-10T08:00:00+00:00","belief_change_summary":"incident","workstream_id":"acme","owning_team":"Adventure Core","severity":2,"source_path":"agency","query_id":"icm-active","linked_work_item_ids":[101],"ado_entity_refs":["WI:101"],"raw_ref":"icm:12345","confidence":2}\n',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="confidence must be a string"):
        read_incident_entries("acme", programs_root=programs_root)


def test_read_incident_entries_rejects_non_object_rows(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    journal_path = get_incident_journal_path("acme", programs_root=programs_root)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text('["not","an","object"]\n', encoding="utf-8")

    with pytest.raises(TypeError, match="incident journal rows must be JSON objects"):
        read_incident_entries("acme", programs_root=programs_root)