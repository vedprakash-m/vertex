"""Contract tests for the ADF-W0.9 gate registry extension.

Verifies: QG-30..QG-40 registration has unique ids and complete Section 12.1
columns, the reservation/collision scanner stays green, and QG-33 delegates
to the existing QG-WS5B cost guard rather than duplicating it.
"""

from __future__ import annotations

from pathlib import Path

from datetime import datetime, timezone

from src.core.quality_gates.adf_cross_surface import GATE_ID as QG34_GATE_ID
from src.core.quality_gates.adf_cross_surface import evaluate_qg34_cross_surface_consistency_gate
from src.core.quality_gates.adf_economics import GATE_ID as QG33_GATE_ID
from src.core.quality_gates.adf_economics import evaluate_qg33_ai_economics_gate
from src.core.quality_gates.ai_budget import evaluate_ai_budget_gate
from src.core.quality_gates.gate_registry import (
    QG_POLICY_MATRIX,
    RESERVED_GATE_IDS,
    assert_no_reservation_collisions,
)
from src.core.quality_gates.narrative import evaluate_contradiction_narrative_gate


def test_policy_matrix_ids_are_unique() -> None:
    ids = [policy.id for policy in QG_POLICY_MATRIX]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids, key=lambda gate_id: int(gate_id.split("-")[1]))


def test_policy_matrix_covers_qg29_through_qg40() -> None:
    expected = {f"QG-{n}" for n in range(29, 41)}
    assert {policy.id for policy in QG_POLICY_MATRIX} == expected


def test_policy_matrix_rows_have_all_section_12_1_columns() -> None:
    for policy in QG_POLICY_MATRIX:
        assert policy.name.strip()
        assert policy.enforcement_point.strip()
        assert policy.enforce_behavior.strip()
        assert policy.forceable.strip()
        assert policy.activates.strip()


def test_reserved_gate_ids_exclude_the_implemented_qg33_delegate() -> None:
    # QG-33 has a real implementation (adf_economics.py); it must not remain
    # "reserved" or assert_no_reservation_collisions would treat it as a bug.
    assert "QG-33" not in RESERVED_GATE_IDS
    # QG-37 (ADF-W1.9, state_authority.py) is likewise implemented and excluded.
    assert "QG-37" not in RESERVED_GATE_IDS
    # QG-29 (ADF-W2.8, ai_release_audit.py) is likewise implemented and excluded.
    assert "QG-29" not in RESERVED_GATE_IDS
    # QG-34 (ADF-W2.10, adf_cross_surface.py -> delegates to QG-17) is likewise excluded.
    assert "QG-34" not in RESERVED_GATE_IDS
    assert set(RESERVED_GATE_IDS) == {f"QG-{n}" for n in range(29, 41)} - {"QG-29", "QG-33", "QG-34", "QG-37"}


def test_no_reservation_collisions() -> None:
    assert_no_reservation_collisions()


def test_qg33_delegates_to_qg_ws5b(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    kwargs = dict(program_id="fixture_prog", budget_usd_per_run=5.0, programs_root=programs_root)

    delegate_report = evaluate_qg33_ai_economics_gate(**kwargs)
    direct_report = evaluate_ai_budget_gate(**kwargs)

    assert QG33_GATE_ID == "QG-33"
    assert len(delegate_report.results) == len(direct_report.results) == 1
    delegate_result = delegate_report.results[0]
    direct_result = direct_report.results[0]

    assert delegate_result.gate_id == "QG-33"
    assert direct_result.gate_id == "QG-WS5B"
    # Same underlying evaluation -- only the id differs (one economics path).
    assert delegate_result.passed == direct_result.passed
    assert delegate_result.message == direct_result.message
    assert delegate_result.exit_code == direct_result.exit_code
    assert delegate_result.forceable == direct_result.forceable


def test_qg33_vacuous_pass_when_no_budget_configured(tmp_path: Path) -> None:
    report = evaluate_qg33_ai_economics_gate(
        program_id="fixture_prog", budget_usd_per_run=0.0, programs_root=tmp_path / "programs"
    )
    assert report.passed is True
    assert report.results[0].gate_id == "QG-33"


def test_qg34_delegates_to_qg17(tmp_path: Path) -> None:
    kwargs = dict(
        items=(),
        approved_signals=(),
        workstream_blurbs=None,
        narratives={},
        as_of=datetime(2026, 7, 1, tzinfo=timezone.utc),
        program_id=None,
        workstreams=(),
        programs_root=tmp_path / "programs",
    )

    delegate_result = evaluate_qg34_cross_surface_consistency_gate(**kwargs)
    direct_result = evaluate_contradiction_narrative_gate(**kwargs)

    assert QG34_GATE_ID == "QG-34"
    assert delegate_result.gate_id == "QG-34"
    assert direct_result.gate_id == "QG-17"
    # Same underlying evaluation -- only the id differs.
    assert delegate_result.passed == direct_result.passed
    assert delegate_result.message == direct_result.message
    assert delegate_result.exit_code == direct_result.exit_code
    assert delegate_result.forceable == direct_result.forceable
