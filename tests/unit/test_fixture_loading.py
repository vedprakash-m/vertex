from __future__ import annotations

import json
from pathlib import Path


def test_nova_ado_sample_fixture_loads() -> None:
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "nova_ado_sample.json"
    if not fixture_path.exists():
        import pytest
        pytest.skip("Requires local fixture data")
    with fixture_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    area_paths = {item["AreaPath"] for item in payload["work_items"]}
    work_item_types = {item["WorkItemType"] for item in payload["work_items"]}

    assert payload["metadata"]["organization"] == "contoso"
    assert payload["metadata"]["source"] == "phase0-canonical-fixture"
    assert payload["metadata"]["item_count"] == 184
    assert len(payload["work_items"]) == 184
    assert len(area_paths) == 10
    assert work_item_types == {"Feature", "Risk", "Scenario", "Key Result"}
    assert payload["work_items"][0]["WorkItemType"] == "Feature"
