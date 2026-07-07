from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.catchup_scan import WatchPollResult
from src.core.catchup_state_store import CatchupState, get_catchup_state_path, load_catchup_state, write_catchup_state
from src.core.models import Confidence
from src.core.models_v2 import CatchupEvent


def test_catchup_state_store_round_trips_last_result(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state = CatchupState(
        last_catchup_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        last_catchup_source="ado",
        last_scan_cursor_ado=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        last_result=WatchPollResult(
            program_id="acme",
            since=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
            scanned_items=12,
            discovered_signals=4,
            new_signals=2,
            auto_reviews_written=2,
            trajectory_updates=1,
            ado_calls=5,
            total_changed_items=700,
            catchup_events=(
                CatchupEvent(
                    event_id="evt-1",
                    program_id="acme",
                    detected_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
                    kind="eta_slip",
                    work_item_id=1234,
                    workstream_id="deployment",
                    summary="ETA slip: ADO#1234 moved from 2026-06-15 to 2026-06-22.",
                    severity="warn",
                    salience_score=0.8,
                    confidence=Confidence.HIGH,
                    signal_id="sig-1",
                ),
            ),
        ),
    )

    path = write_catchup_state("acme", state, programs_root=programs_root)
    loaded = load_catchup_state("acme", programs_root=programs_root)

    assert path == get_catchup_state_path("acme", programs_root=programs_root)
    assert loaded == state


def test_load_catchup_state_rejects_non_string_last_catchup_source(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version": "1.0", "last_catchup_at": "2026-05-19T18:00:00+00:00", "last_catchup_source": 1, "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"}}',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="last_catchup_source must be a string"):
        load_catchup_state("acme", programs_root=programs_root)


def test_load_catchup_state_rejects_missing_last_catchup_source(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version": "1.0", "last_catchup_at": "2026-05-19T18:00:00+00:00", "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"}}',
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="last_catchup_source must be a string"):
        load_catchup_state("acme", programs_root=programs_root)


def test_load_catchup_state_rejects_numeric_string_last_result_counter(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
        {
            "schema_version": "1.0",
            "last_catchup_at": "2026-05-19T18:00:00+00:00",
            "last_catchup_source": "ado",
            "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"},
            "last_result": {
                "since": "2026-05-19T17:00:00+00:00",
                "polled_at": "2026-05-19T18:00:00+00:00",
                "scanned_items": "12",
                "discovered_signals": 4,
                "new_signals": 2,
                "auto_reviews_written": 2,
                "trajectory_updates": 1,
                "ado_calls": 5,
                "new_signal_summaries": [],
                "catchup_events": []
            }
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="scanned_items must be an integer"):
        load_catchup_state("acme", programs_root=programs_root)


def test_load_catchup_state_rejects_non_string_new_signal_summary(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
        {
            "schema_version": "1.0",
            "last_catchup_at": "2026-05-19T18:00:00+00:00",
            "last_catchup_source": "ado",
            "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"},
            "last_result": {
                "since": "2026-05-19T17:00:00+00:00",
                "polled_at": "2026-05-19T18:00:00+00:00",
                "scanned_items": 12,
                "discovered_signals": 4,
                "new_signals": 2,
                "auto_reviews_written": 2,
                "trajectory_updates": 1,
                "ado_calls": 5,
                "new_signal_summaries": [1],
                "catchup_events": []
            }
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="new_signal_summaries entries must be strings"):
        load_catchup_state("acme", programs_root=programs_root)


def test_load_catchup_state_rejects_non_mapping_catchup_event(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
        {
            "schema_version": "1.0",
            "last_catchup_at": "2026-05-19T18:00:00+00:00",
            "last_catchup_source": "ado",
            "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"},
            "last_result": {
                "since": "2026-05-19T17:00:00+00:00",
                "polled_at": "2026-05-19T18:00:00+00:00",
                "scanned_items": 12,
                "discovered_signals": 4,
                "new_signals": 2,
                "auto_reviews_written": 2,
                "trajectory_updates": 1,
                "ado_calls": 5,
                "new_signal_summaries": [],
                "catchup_events": [1]
            }
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="catchup_events entries must be mappings"):
        load_catchup_state("acme", programs_root=programs_root)


def test_load_catchup_state_rejects_non_string_catchup_event_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
        {
            "schema_version": "1.0",
            "last_catchup_at": "2026-05-19T18:00:00+00:00",
            "last_catchup_source": "ado",
            "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"},
            "last_result": {
                "since": "2026-05-19T17:00:00+00:00",
                "polled_at": "2026-05-19T18:00:00+00:00",
                "scanned_items": 12,
                "discovered_signals": 4,
                "new_signals": 2,
                "auto_reviews_written": 2,
                "trajectory_updates": 1,
                "ado_calls": 5,
                "new_signal_summaries": [],
                "catchup_events": [
                    {
                        "event_id": 1,
                        "program_id": "acme",
                        "detected_at": "2026-05-19T18:00:00+00:00",
                        "kind": "eta_slip",
                        "work_item_id": 1234,
                        "workstream_id": "deployment",
                        "summary": "ETA slip",
                        "severity": "warn",
                        "salience_score": 0.8,
                        "confidence": "high",
                        "signal_id": "sig-1"
                    }
                ]
            }
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="event_id must be a string"):
        load_catchup_state("acme", programs_root=programs_root)


def test_load_catchup_state_rejects_numeric_string_catchup_event_salience_score(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
        {
            "schema_version": "1.0",
            "last_catchup_at": "2026-05-19T18:00:00+00:00",
            "last_catchup_source": "ado",
            "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"},
            "last_result": {
                "since": "2026-05-19T17:00:00+00:00",
                "polled_at": "2026-05-19T18:00:00+00:00",
                "scanned_items": 12,
                "discovered_signals": 4,
                "new_signals": 2,
                "auto_reviews_written": 2,
                "trajectory_updates": 1,
                "ado_calls": 5,
                "new_signal_summaries": [],
                "catchup_events": [
                    {
                        "event_id": "evt-1",
                        "program_id": "acme",
                        "detected_at": "2026-05-19T18:00:00+00:00",
                        "kind": "eta_slip",
                        "work_item_id": 1234,
                        "workstream_id": "deployment",
                        "summary": "ETA slip",
                        "severity": "warn",
                        "salience_score": "0.8",
                        "confidence": "high",
                        "signal_id": "sig-1"
                    }
                ]
            }
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="salience_score must be numeric"):
        load_catchup_state("acme", programs_root=programs_root)


def test_load_catchup_state_rejects_numeric_string_total_changed_items(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = get_catchup_state_path("acme", programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """
        {
            "schema_version": "1.0",
            "last_catchup_at": "2026-05-19T18:00:00+00:00",
            "last_catchup_source": "ado",
            "last_scan_cursor": {"ado": "2026-05-19T18:00:00+00:00"},
            "last_result": {
                "since": "2026-05-19T17:00:00+00:00",
                "polled_at": "2026-05-19T18:00:00+00:00",
                "scanned_items": 12,
                "discovered_signals": 4,
                "new_signals": 2,
                "auto_reviews_written": 2,
                "trajectory_updates": 1,
                "ado_calls": 5,
                "new_signal_summaries": [],
                "total_changed_items": "700",
                "catchup_events": []
            }
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="total_changed_items must be an integer"):
        load_catchup_state("acme", programs_root=programs_root)