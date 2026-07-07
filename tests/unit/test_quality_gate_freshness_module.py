"""Guards the D-09 peel of freshness/scoping quality-gate helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from src.core.models import FreshnessItem, FreshnessReport
from src.core.quality_gates import freshness as freshness_module


def _freshness_item(work_item_id: int, severity: Literal["block", "warn", "info"]) -> FreshnessItem:
    return FreshnessItem(
        work_item_id=work_item_id,
        rule_id=f"rule-{work_item_id}",
        severity=severity,
        message="freshness",
        suggested_fix=None,
    )


def test_filter_freshness_report_recomputes_counts_for_publishable_scope() -> None:
    report = FreshnessReport(
        issue_number=78,
        items=(
            _freshness_item(1001, "block"),
            _freshness_item(1002, "warn"),
            _freshness_item(1003, "info"),
        ),
        blocks=1,
        warns=1,
        infos=1,
    )

    scoped = freshness_module.filter_freshness_report(report, {1002, 1003})

    assert [item.work_item_id for item in scoped.items] == [1002, 1003]
    assert scoped.blocks == 0
    assert scoped.warns == 1
    assert scoped.infos == 1


def test_filter_item_ids_to_scope_filters_and_normalizes_ids() -> None:
    assert freshness_module.filter_item_ids_to_scope([1001, 1002, 1003], {1002, 1003}) == (1002, 1003)


def test_evaluate_freshness_gate_blocks_when_report_has_blockers() -> None:
    report = FreshnessReport(
        issue_number=78,
        items=(_freshness_item(1001, "block"),),
        blocks=1,
        warns=0,
        infos=0,
    )

    result = freshness_module.evaluate_freshness_gate(report)

    assert result.gate_id == "QG-1"
    assert result.passed is False


def test_coerce_datetime_normalizes_date_input_to_utc_midnight() -> None:
    coerced = freshness_module.coerce_datetime(date(2026, 6, 6))

    assert coerced == datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc)
