from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.chronicle import ProgramEvent, append_program_event, load_program_events


def test_load_program_events_round_trips_event(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_program_event(
        "acme",
        ProgramEvent(
            event_type="commitment",
            event_date=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            description="Committed to the revised checkpoint.",
            source="pm_review",
            actors=("operator",),
            linked_dimensions=("networking",),
            event_id="ev-1",
        ),
        programs_root=programs_root,
    )

    events = load_program_events("acme", programs_root=programs_root)

    assert len(events) == 1
    assert events[0].event_id == "ev-1"
    assert events[0].actors == ("operator",)


def test_load_program_events_rejects_non_string_description(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    chronicle_path = programs_root / "acme" / "chronicle.jsonl"
    chronicle_path.parent.mkdir(parents=True, exist_ok=True)
    chronicle_path.write_text(
        json.dumps(
            {
                "event_type": "commitment",
                "event_date": "2026-05-10T12:00:00+00:00",
                "description": 123,
                "source": "pm_review",
                "actors": ["operator"],
                "linked_dimensions": ["networking"],
                "event_id": "ev-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="description must be a string"):
        load_program_events("acme", programs_root=programs_root)


def test_load_program_events_rejects_non_string_actor_entry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    chronicle_path = programs_root / "acme" / "chronicle.jsonl"
    chronicle_path.parent.mkdir(parents=True, exist_ok=True)
    chronicle_path.write_text(
        json.dumps(
            {
                "event_type": "commitment",
                "event_date": "2026-05-10T12:00:00+00:00",
                "description": "Committed to the revised checkpoint.",
                "source": "pm_review",
                "actors": [123],
                "linked_dimensions": ["networking"],
                "event_id": "ev-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="actors entries must be strings"):
        load_program_events("acme", programs_root=programs_root)


def test_load_program_events_rejects_non_string_event_date(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    chronicle_path = programs_root / "acme" / "chronicle.jsonl"
    chronicle_path.parent.mkdir(parents=True, exist_ok=True)
    chronicle_path.write_text(
        json.dumps(
            {
                "event_type": "commitment",
                "event_date": 123,
                "description": "Committed to the revised checkpoint.",
                "source": "pm_review",
                "actors": ["operator"],
                "linked_dimensions": ["networking"],
                "event_id": "ev-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="event_date must be a string"):
        load_program_events("acme", programs_root=programs_root)