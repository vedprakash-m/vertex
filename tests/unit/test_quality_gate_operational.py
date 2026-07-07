"""Guards the D-09 peel of operational quality-gate helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import Signal
from src.core.quality_gates import operational as operational_module


def test_preview_work_item_ids_limits_output() -> None:
    preview = operational_module.preview_work_item_ids([1, 2, 3, 4, 5, 6])

    assert preview == "WI:1, WI:2, WI:3, WI:4, WI:5, and 1 more"


def test_has_milestone_risk_linkage_accepts_linked_milestone_id() -> None:
    milestone = SimpleNamespace(
        id="ms-1",
        linked_workstream_ids=("velocity",),
        linked_work_item_ids=(1001,),
    )
    risk = SimpleNamespace(
        linked_milestone_ids=("ms-1",),
        linked_workstream_ids=(),
        linked_work_item_ids=(),
    )

    assert operational_module.has_milestone_risk_linkage(milestone=milestone, open_risks=(risk,)) is True


def test_format_cross_program_cascade_gate_line_includes_work_item_trigger() -> None:
    cascade = SimpleNamespace(
        trigger_kind="drift",
        work_item_id=1001,
        source_item="WI#1001",
        target_item="fabrikam:buildouts",
    )

    assert operational_module.format_cross_program_cascade_gate_line(cascade) == "WI#1001 -> fabrikam:buildouts (drift WI:1001)"


def test_evaluate_high_risk_coverage_gate_flags_uncovered_high_risk_item(monkeypatch) -> None:
    item: Any = SimpleNamespace(id=1001, risk_level=RiskLevel.HIGH, state="Active")
    gap = SimpleNamespace(work_item_id=1001)

    monkeypatch.setattr(
        operational_module,
        "build_coverage_gaps",
        lambda *args, **kwargs: (gap,),
    )

    result = operational_module.evaluate_high_risk_coverage_gate(
        items=(item,),
        approved_signals=(),
        narratives=(),
        as_of=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
    )

    assert result.gate_id == "QG-13"
    assert result.passed is False
    assert "WI:1001" in result.message


def test_evaluate_overdue_target_gate_ignores_terminal_items() -> None:
    terminal_item: Any = SimpleNamespace(id=1001, target_date=date(2026, 5, 1), state="Closed")

    result = operational_module.evaluate_overdue_target_gate((terminal_item,), date(2026, 6, 6))

    assert result.gate_id == "QG-9"
    assert result.passed is True


def test_evaluate_cross_program_dependency_cascade_gate_passes_without_program_id() -> None:
    signal = Signal(
        id="signal-1",
        timestamp=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="velocity",
        entity_refs=("WI:1001",),
        text="Readiness slipped again.",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata=None,
        thread_id=None,
    )
    item: Any = SimpleNamespace(id=1001)

    result = operational_module.evaluate_cross_program_dependency_cascade_gate(
        items=(item,),
        approved_signals=(signal,),
        as_of=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        program_id=None,
        programs_root=Path("."),
    )

    assert result.gate_id == "QG-19"
    assert result.passed is True
