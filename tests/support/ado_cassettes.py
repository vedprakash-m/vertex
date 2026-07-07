from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.commands.report import _work_item_from_raw
from src.core.models import WorkItem


CASSETTES_DIR = Path(__file__).resolve().parents[1] / "cassettes"

_IDENTITY_KEYS = frozenset({"AssignedTo", "AssignedToEmail", "ChangedBy", "CreatedBy", "author", "email"})


def load_cassette_payload(name: str) -> dict[str, Any]:
    path = CASSETTES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Cassette not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Cassette payload must be a JSON object: {path}")
    return _sanitize_payload(payload)


def load_cassette_work_items(name: str, as_of: datetime) -> tuple[tuple[WorkItem, ...], int]:
    payload = load_cassette_payload(name)
    rows = [dict(row) for row in payload.get("work_items", []) if isinstance(row, dict)]
    generator = payload.get("generator")
    if generator is not None:
        rows.extend(_expand_generated_rows(generator, as_of))
    return tuple(_work_item_from_raw(row, as_of) for row in rows), 1


def _expand_generated_rows(generator: Any, as_of: datetime) -> list[dict[str, Any]]:
    if not isinstance(generator, dict):
        raise ValueError("Cassette generator must be a JSON object.")

    template = str(generator.get("template", "")).strip().lower()
    if template != "large_mixed":
        raise ValueError(f"Unsupported cassette generator template: {template!r}")

    item_count = int(generator.get("item_count", 0))
    if item_count <= 0:
        raise ValueError("Cassette generator item_count must be > 0.")

    base_id = int(generator.get("base_id", 930000))
    area_paths = _string_list(generator.get("area_paths")) or [
        "One\\Adventure\\Acme\\Deployment",
        "One\\Adventure\\Acme\\Networking",
        "One\\Adventure\\Acme\\OS",
        "One\\Adventure\\Fabrikam\\Acme\\Scenarios",
        "One\\Adventure\\Acme\\XSSE",
        "One\\Adventure\\Acme\\Repairs",
        "One\\Adventure\\Acme\\PFInfra",
        "One\\Adventure\\Contoso\\ControlPlane",
    ]
    work_item_types = _string_list(generator.get("work_item_types")) or ["Feature", "Risk", "Scenario", "Key Result"]
    states = _string_list(generator.get("states")) or ["Active", "Proposed", "At Risk", "Blocked"]
    raw_tag_sets = generator.get("tag_sets")
    if isinstance(raw_tag_sets, list) and raw_tag_sets:
        tag_sets = [
            [str(tag).strip() for tag in tag_group if str(tag).strip()]
            if isinstance(tag_group, list)
            else [str(tag_group).strip()]
            for tag_group in raw_tag_sets
        ]
    else:
        tag_sets = [["Safety"], ["SCHIE"], ["Blocked"], []]

    rows: list[dict[str, Any]] = []
    for index in range(item_count):
        item_id = base_id + index
        tag_group = tag_sets[index % len(tag_sets)]
        rows.append(
            {
                "WorkItemId": item_id,
                "WorkItemType": work_item_types[index % len(work_item_types)],
                "Title": f"Synthetic cassette item {item_id}",
                "State": states[index % len(states)],
                "AssignedTo": "" if index % 13 == 0 else "Vertex Maintainer",
                "AssignedToEmail": "" if index % 13 == 0 else "maintainer@example.com",
                "AreaPath": area_paths[index % len(area_paths)],
                "IterationPath": f"FY26\\Sprint {20 + (index % 4)}",
                "TargetDate": (as_of.date() + timedelta(days=(index % 21) - 10)).isoformat(),
                "Tags": ";".join(tag_group),
            }
        )
    return rows


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _sanitize_payload(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {item_key: _sanitize_payload(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item, key=key) for item in value]
    if isinstance(value, str):
        if key in _IDENTITY_KEYS:
            return "maintainer@example.com" if "email" in (key or "").lower() else "Vertex Maintainer"
        if "@microsoft.com" in value.lower():
            return "maintainer@example.com"
        return value
    return value

