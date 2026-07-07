from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.action_mapper import build_meeting_action_proposal, map_actions_to_work_items
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Workstream


def test_map_actions_to_work_items_resolves_workstream_and_owner() -> None:
    action = ActionItem(
        id="act-1",
        program_id="acme",
        text="Confirm UD mitigation plan with leadership.",
        owner_alias="alice",
        due_date=date(2026, 6, 1),
        status=ActionStatus.PROPOSED,
        source_signal_id="sig-1",
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=(101,),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )

    mappings = map_actions_to_work_items(
        (action,),
        item_rows_by_id={
            101: {
                "id": 101,
                "rev": 17,
                "fields": {
                    "System.Id": 101,
                    "System.Title": "UD chunking",
                    "System.AreaPath": "Acme\\UD",
                    "System.AssignedTo": {"uniqueName": "alice@contoso.com"},
                    "System.Rev": 17,
                },
            }
        },
        workstreams=(Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),),
    )

    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.resolved_workstream_id == "ud"
    assert mapping.is_net_new is False
    assert mapping.needs_owner is False
    assert mapping.needs_due_date is False
    assert mapping.matched_items[0].assigned_to == "alice"


def test_build_meeting_action_proposal_only_includes_mapped_actions() -> None:
    mapped_action = ActionItem(
        id="act-1",
        program_id="acme",
        text="Confirm UD mitigation plan with leadership.",
        owner_alias="alice",
        due_date=date(2026, 6, 1),
        status=ActionStatus.PROPOSED,
        source_signal_id="sig-1",
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=(101,),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )
    net_new_action = ActionItem(
        id="act-2",
        program_id="acme",
        text="Send readiness recap to the broader launch list.",
        owner_alias="unknown",
        due_date=None,
        status=ActionStatus.PROPOSED,
        source_signal_id="sig-1",
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=(),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )

    mappings = map_actions_to_work_items(
        (mapped_action, net_new_action),
        item_rows_by_id={
            101: {
                "id": 101,
                "rev": 17,
                "fields": {
                    "System.Id": 101,
                    "System.Title": "UD chunking",
                    "System.AreaPath": "Acme\\UD",
                    "System.Rev": 17,
                },
            }
        },
        workstreams=(Workstream(id="ud", name="UD", area_paths=("Acme\\UD",)),),
    )

    proposal = build_meeting_action_proposal(
        program_id="acme",
        meeting_id="lt-sync-123",
        meeting_title="LT Sync",
        mappings=mappings,
        created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert proposal.id == "meeting-action-lt-sync-123"
    assert proposal.update_type == "meeting_action"
    assert len(proposal.entries) == 1
    assert proposal.entries[0].work_item_id == 101
    assert proposal.entries[0].action == "add_comment"


def test_map_actions_to_work_items_prefers_longest_workstream_match() -> None:
    action = ActionItem(
        id="act-1",
        program_id="acme",
        text="Confirm checkout launch plan.",
        owner_alias="alice",
        due_date=date(2026, 6, 1),
        status=ActionStatus.PROPOSED,
        source_signal_id="sig-1",
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=(101,),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )

    mappings = map_actions_to_work_items(
        (action,),
        item_rows_by_id={
            101: {
                "id": 101,
                "rev": 17,
                "fields": {
                    "System.Id": 101,
                    "System.Title": "Checkout readiness",
                    "System.AreaPath": "Acme\\UD\\Checkout",
                    "System.Rev": 17,
                },
            }
        },
        workstreams=(
            Workstream(id="broad", name="UD", area_paths=("Acme\\UD",)),
            Workstream(id="specific", name="Checkout", area_paths=("Acme\\UD\\Checkout",)),
        ),
    )

    assert len(mappings) == 1
    assert mappings[0].resolved_workstream_id == "specific"
