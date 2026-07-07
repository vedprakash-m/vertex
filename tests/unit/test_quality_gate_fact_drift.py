"""Direct coverage for the extracted Program Fact drift gate (QG-SG-20).

Guards the D-09 / Phase 3 peel of the fact-drift cluster from the
``src/core/quality_gates`` package into
``src/core/quality_gates/fact_drift.py`` (re-exported from the package
``__init__``).
"""

from __future__ import annotations

from types import SimpleNamespace

from src.core.quality_gates import (
    evaluate_program_fact_drift_from_draft,
    evaluate_program_fact_drift_gate,
)
from src.core.quality_gates import fact_drift


def test_drift_gate_no_snapshot_is_noop() -> None:
    assert evaluate_program_fact_drift_gate(snapshot_id=None, drifted_facts=()).results == ()
    assert evaluate_program_fact_drift_gate(snapshot_id="", drifted_facts=("x",)).results == ()


def test_drift_gate_no_drift_passes() -> None:
    report = evaluate_program_fact_drift_gate(snapshot_id="snap-1", drifted_facts=())
    assert len(report.results) == 1
    gate = report.results[0]
    assert gate.gate_id == "QG-SG-20" and gate.passed is True and gate.exit_code == 0


def test_drift_gate_with_drift_blocks_and_samples() -> None:
    facts = (
        SimpleNamespace(fact_type="action", natural_key="ACT-123456789012"),
        SimpleNamespace(fact_type="risk", natural_key="RISK-9"),
    )
    report = evaluate_program_fact_drift_gate(snapshot_id="snap-1", drifted_facts=facts)
    gate = report.results[0]
    assert gate.passed is False and gate.exit_code == 3
    assert "State Drift Warning" in gate.message
    assert "2 accepted revision(s) drifted since snap-1" in gate.message
    # natural_key is truncated to 12 chars in the sample: "ACT-123456789012"[:12] == "ACT-12345678"
    assert "action:ACT-12345678" in gate.message
    assert "risk:RISK-9" in gate.message


def test_from_draft_noop_when_program_id_missing() -> None:
    assert evaluate_program_fact_drift_from_draft(draft_state={}, program_id=None).results == ()


def test_from_draft_noop_when_no_snapshot_pin() -> None:
    assert evaluate_program_fact_drift_from_draft(draft_state={}, program_id="acme").results == ()
    assert (
        evaluate_program_fact_drift_from_draft(
            draft_state={"program_fact_snapshot": {"snapshot_id": "  "}}, program_id="acme"
        ).results
        == ()
    )


def test_from_draft_program_id_mismatch_blocks() -> None:
    report = evaluate_program_fact_drift_from_draft(
        draft_state={"program_fact_snapshot": {"snapshot_id": "snap-1", "program_id": "other"}},
        program_id="acme",
    )
    gate = report.results[0]
    assert gate.passed is False and gate.gate_id == "QG-SG-20"
    # The mismatch is encoded as a single drifted "fact"; the gate samples it as
    # "fact:" (a bare string has no fact_type/natural_key), so assert the block.
    assert "1 accepted revision(s) drifted since snap-1" in gate.message


def test_from_draft_uses_store_detect_drift(monkeypatch) -> None:
    class _FakeStore:
        def __init__(self, program_id, *, db_root=None):
            self.program_id = program_id

        def detect_drift(self, snapshot_id):
            assert snapshot_id == "snap-9"
            return (SimpleNamespace(fact_type="milestone", natural_key="MS-1"),)

    monkeypatch.setattr(fact_drift, "ProgramFactStore", _FakeStore)
    report = evaluate_program_fact_drift_from_draft(
        draft_state={"program_fact_snapshot": {"snapshot_id": "snap-9", "program_id": "acme"}},
        program_id="acme",
    )
    gate = report.results[0]
    assert gate.passed is False and gate.gate_id == "QG-SG-20"


def test_from_draft_clean_store_passes(monkeypatch) -> None:
    class _CleanStore:
        def __init__(self, program_id, *, db_root=None):
            pass

        def detect_drift(self, snapshot_id):
            return ()

    monkeypatch.setattr(fact_drift, "ProgramFactStore", _CleanStore)
    report = evaluate_program_fact_drift_from_draft(
        draft_state={"program_fact_snapshot": {"snapshot_id": "snap-9", "program_id": "acme"}},
        program_id="acme",
    )
    assert report.results[0].passed is True
