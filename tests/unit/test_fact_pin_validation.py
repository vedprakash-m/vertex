from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.core.quality_gates import fact_drift


@pytest.mark.parametrize("pin", [
    None, False, [], "pin", {}, {"snapshot_id": None}, {"snapshot_id": False},
    {"snapshot_id": 42}, {"snapshot_id": ""}, {"snapshot_id": "  "},
    {"snapshot_id": "pin", "program_id": ""}, {"snapshot_id": "pin", "program_id": False},
    {"snapshot_id": "pin", "program_id": []},
])
def test_present_invalid_pin_blocks_before_fact_store_access(monkeypatch, pin) -> None:
    store = Mock(side_effect=AssertionError("Invalid pin reached store"))
    monkeypatch.setattr(fact_drift, "ProgramFactStore", store)
    report = fact_drift.evaluate_program_fact_drift_from_draft(
        draft_state={"program_fact_snapshot": pin}, program_id="fixture",
    )
    assert len(report.results) == 1
    result = report.results[0]
    assert result.gate_id == "QG-SG-20"
    assert result.passed is False
    assert result.exit_code == 3
    assert "Re-run" in result.message
    store.assert_not_called()


@pytest.mark.parametrize("program_id", [None, "", "  "])
def test_declared_pin_requires_evaluation_program(monkeypatch, program_id) -> None:
    store = Mock(side_effect=AssertionError("Unscoped pin reached store"))
    monkeypatch.setattr(fact_drift, "ProgramFactStore", store)
    report = fact_drift.evaluate_program_fact_drift_from_draft(
        draft_state={"program_fact_snapshot": {"snapshot_id": "pin"}}, program_id=program_id,
    )
    assert report.results[0].passed is False
    assert report.results[0].exit_code == 3
    store.assert_not_called()


@pytest.mark.parametrize("program_id", [None, "fixture"])
def test_only_absent_pin_preserves_legacy_noop(monkeypatch, program_id) -> None:
    store = Mock(side_effect=AssertionError("Legacy draft reached store"))
    monkeypatch.setattr(fact_drift, "ProgramFactStore", store)
    assert fact_drift.evaluate_program_fact_drift_from_draft(
        draft_state={}, program_id=program_id,
    ).results == ()
    store.assert_not_called()
