"""Direct coverage for the extracted confirm deserialization helpers.

Guards the D-25 / Phase 3 extraction of the pure draft-state deserialization
cluster from ``src/commands/confirm.py`` into
``src/commands/confirm_stages/deserialization.py``. These helpers must round-trip
persisted draft payloads back into core domain objects without I/O or mutation.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.commands.confirm_stages.deserialization import (
    deserialize_comment,
    deserialize_items,
    deserialize_kusto_sections,
    deserialize_revision,
    deserialize_work_item,
    optional_string,
    parse_date_value,
    parse_datetime_required,
)
from src.core.models import RiskLevel


def test_optional_string_empty_and_none_become_none() -> None:
    assert optional_string(None) is None
    assert optional_string("") is None
    assert optional_string("x") == "x"
    assert optional_string(5) == "5"


def test_parse_date_value_handles_blank_and_iso() -> None:
    assert parse_date_value(None) is None
    assert parse_date_value("") is None
    assert parse_date_value("2026-06-05") == date(2026, 6, 5)


def test_parse_datetime_required_parses_iso() -> None:
    assert parse_datetime_required("2026-06-05T12:30:00") == datetime(2026, 6, 5, 12, 30, 0)


def _minimal_work_item_payload() -> dict:
    return {
        "id": 78,
        "type": "Deliverable",
        "title": "Ship it",
        "state": "Active",
        "area_path": "One\\Adventure\\Acme",
        "iteration_path": "One\\Sprint 1",
        "risk_level": "low",
        "fetched_at": "2026-06-05T00:00:00",
    }


def test_deserialize_work_item_minimal_and_defaults() -> None:
    item = deserialize_work_item(_minimal_work_item_payload())
    assert item.id == 78
    assert item.title == "Ship it"
    assert item.risk_level == RiskLevel.from_string("low")
    assert item.assigned_to is None
    assert item.target_date is None
    assert item.revisions == []
    assert item.comments == []
    assert item.fetched_at == datetime(2026, 6, 5, 0, 0, 0)


def test_deserialize_work_item_with_nested_revisions_and_comments() -> None:
    payload = _minimal_work_item_payload()
    payload["target_date"] = "2026-07-01"
    payload["assigned_to"] = "Ada"
    payload["tags"] = ["alpha", 7]
    payload["revisions"] = [
        {
            "work_item_id": 78,
            "rev_number": 2,
            "changed_by": "Ada",
            "changed_by_email": "ada@example.com",
            "changed_date": "2026-06-04T09:00:00",
            "fields_changed": {"State": ["New", "Active"]},
        }
    ]
    payload["comments"] = [
        {
            "work_item_id": 78,
            "comment_id": 1,
            "created_by": "Bob",
            "created_by_email": "bob@example.com",
            "created_date": "2026-06-04T10:00:00",
            "text": "looks good",
        }
    ]
    item = deserialize_work_item(payload)
    assert item.assigned_to == "Ada"
    assert item.target_date == date(2026, 7, 1)
    assert item.tags == ["alpha", "7"]
    assert len(item.revisions) == 1
    assert item.revisions[0].fields_changed["State"] == ("New", "Active")
    assert len(item.comments) == 1
    assert item.comments[0].text == "looks good"


def test_deserialize_items_round_trips_tuple() -> None:
    items = deserialize_items((_minimal_work_item_payload(),))
    assert len(items) == 1 and items[0].id == 78
    assert deserialize_items(()) == ()


def test_deserialize_revision_and_comment_direct() -> None:
    rev = deserialize_revision(
        {
            "work_item_id": 1,
            "rev_number": 3,
            "changed_by": "C",
            "changed_by_email": "c@e.com",
            "changed_date": "2026-06-01T00:00:00",
            "fields_changed": {},
        }
    )
    assert rev.rev_number == 3 and rev.fields_changed == {}
    comment = deserialize_comment(
        {
            "work_item_id": 1,
            "comment_id": 9,
            "created_by": "C",
            "created_by_email": "c@e.com",
            "created_date": "2026-06-01T00:00:00",
            "text": "hi",
        }
    )
    assert comment.comment_id == 9 and comment.text == "hi"


def test_deserialize_kusto_sections_table_and_metrics() -> None:
    sections = deserialize_kusto_sections(
        (
            {
                "section_id": "s1",
                "title": "Velocity",
                "query_id": "q1",
                "render_mode": "table",
                "source_label": "Kusto",
                "confidence": "high",
                "columns": ["a", "b"],
                "rows": [[{"text": "1", "href": "http://x"}, {"text": "2"}]],
                "metrics": [{"label": "MTTR", "value": "3h"}],
                "caveats": ["stale"],
                "is_degraded": True,
            },
        )
    )
    assert len(sections) == 1
    sec = sections[0]
    assert sec.section_id == "s1"
    assert sec.render_mode == "table"
    assert sec.columns == ("a", "b")
    assert sec.rows[0][0].text == "1" and sec.rows[0][0].href == "http://x"
    assert sec.rows[0][1].href is None
    assert sec.metrics[0].label == "MTTR"
    assert sec.caveats == ("stale",)
    assert sec.is_degraded is True


def test_deserialize_kusto_sections_empty() -> None:
    assert deserialize_kusto_sections(()) == ()


def test_deserialize_work_item_missing_required_key_raises() -> None:
    payload = _minimal_work_item_payload()
    del payload["fetched_at"]
    with pytest.raises(KeyError):
        deserialize_work_item(payload)
