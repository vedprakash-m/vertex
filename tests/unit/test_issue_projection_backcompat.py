from __future__ import annotations

from src.core.issue_projection import IssueProjection
from src.core.models import Confidence


def test_issue_projection_backcompat_defaults_raid_chain() -> None:
    payload = {
        "work_item_id": 1001,
        "source_type": "ado_blocked",
        "severity": "block",
        "summary": "Blocked item",
        "owner_alias": "demo",
        "workstream_id": "ws-demo",
        "ado_url": "https://example.invalid/1001",
        "linked_entity_ids": ("risk-1",),
    }

    projection = IssueProjection(**payload)

    assert projection.confidence is Confidence.NONE
    assert projection.raid_chain is None