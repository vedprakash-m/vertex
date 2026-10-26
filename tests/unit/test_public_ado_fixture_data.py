from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.support.ado_cassettes import CASSETTES_DIR, load_cassette_payload, load_cassette_work_items


@pytest.mark.parametrize(("name", "count"), [("cold_start", 4), ("partial_empty", 2), ("large", 540)])
def test_report_cassettes_are_authored_public_synthetic_inputs(name: str, count: int) -> None:
    assert CASSETTES_DIR == Path(__file__).resolve().parents[1] / "data" / "ado"
    payload = load_cassette_payload(name)
    assert payload["fixture_kind"] == "authored_synthetic"
    items, calls = load_cassette_work_items(name, datetime(2026, 5, 5, tzinfo=timezone.utc))
    assert len(items) == count
    assert calls == 1
    assert all(item.area_path.startswith("ExampleProject\\") for item in items)
    assert all(item.assigned_to in {None, "Example Owner"} for item in items)
    if name == "large":
        assert any(item.assigned_to is None for item in items)
