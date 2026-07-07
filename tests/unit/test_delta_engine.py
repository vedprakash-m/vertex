from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.delta_engine import build_deltas
from src.core.models import EditionType, RiskLevel, Snapshot, SnapshotItem, WorkItem


def _snapshot_item(
    work_item_id: int,
    *,
    state: str,
    assigned_to: str | None,
    assigned_to_email: str | None,
    target_date: date | None,
    risk_level: RiskLevel,
) -> SnapshotItem:
    return SnapshotItem(
        id=work_item_id,
        type="Feature",
        title=f"Item {work_item_id}",
        state=state,
        assigned_to=assigned_to,
        area_path="One\\Adventure\\Acme",
        target_date=target_date,
        risk_level=risk_level,
        tags=[],
    )


def _work_item(
    work_item_id: int,
    *,
    state: str,
    assigned_to: str | None,
    assigned_to_email: str | None,
    target_date: date | None,
    risk_level: RiskLevel,
) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=f"Item {work_item_id}",
        state=state,
        assigned_to=assigned_to,
        assigned_to_email=assigned_to_email,
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=target_date,
        risk_level=risk_level,
        tags=[],
        custom_fields={},
    )


def test_build_deltas_detects_new_closed_risk_eta_and_owner_changes() -> None:
    previous_snapshot = Snapshot(
        issue_number=77,
        generated_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            _snapshot_item(1, state="Active", assigned_to="Operator", assigned_to_email="operator@example.com", target_date=date(2026, 6, 1), risk_level=RiskLevel.MEDIUM),
            _snapshot_item(2, state="Active", assigned_to="Operator", assigned_to_email="operator@example.com", target_date=date(2026, 6, 10), risk_level=RiskLevel.HIGH),
            _snapshot_item(3, state="Active", assigned_to="Old Owner", assigned_to_email="old@example.com", target_date=date(2026, 6, 15), risk_level=RiskLevel.LOW),
            _snapshot_item(4, state="Active", assigned_to="Operator", assigned_to_email="operator@example.com", target_date=date(2026, 6, 20), risk_level=RiskLevel.LOW),
        ),
        scorecards=(),
    )
    current_items = (
        _work_item(1, state="Closed", assigned_to="Operator", assigned_to_email="operator@example.com", target_date=date(2026, 6, 1), risk_level=RiskLevel.MEDIUM),
        _work_item(2, state="Active", assigned_to="Operator", assigned_to_email="operator@example.com", target_date=date(2026, 6, 10), risk_level=RiskLevel.DONE),
        _work_item(3, state="Active", assigned_to="New Owner", assigned_to_email="new@example.com", target_date=date(2026, 6, 25), risk_level=RiskLevel.LOW),
        _work_item(4, state="Active", assigned_to="Operator", assigned_to_email="operator@example.com", target_date=date(2026, 6, 20), risk_level=RiskLevel.LOW),
        _work_item(5, state="Active", assigned_to="Operator", assigned_to_email="operator@example.com", target_date=date(2026, 7, 1), risk_level=RiskLevel.HIGH),
    )

    deltas = build_deltas(current_items, previous_snapshot, issue_number=78, previous_issue_number=77)

    assert len(deltas.new_items) == 1
    assert deltas.new_items[0].work_item_id == 5
    assert len(deltas.closed_items) == 1
    assert deltas.closed_items[0].work_item_id == 1
    assert len(deltas.risk_changes) == 1
    assert deltas.risk_changes[0].work_item_id == 2
    assert len(deltas.eta_changes) == 1
    assert deltas.eta_changes[0].work_item_id == 3
    assert deltas.eta_changes[0].kind.value == "eta_changed"
    assert len(deltas.owner_changes) == 1
    assert deltas.owner_changes[0].work_item_id == 3
    assert deltas.owner_changes[0].kind.value == "owner_changed"
    assert deltas.unchanged_count == 1
