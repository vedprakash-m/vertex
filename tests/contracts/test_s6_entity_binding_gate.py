"""S-6 contract tests: Entity-binding gate harness.

Verifies that entity_binding_gate.evaluate_binding:
  1. Passes when precision >= 95% AND coverage >= 80%.
  2. Fails when precision < 95%.
  3. Fails when coverage < 80%.
  4. Does not gate on small samples (N < 3) — emits warning instead.
  5. Emits operator-intent gate warning when new entities are minted.
  6. Perfect binding (no UNRESOLVED refs) → precision=1.0, coverage=1.0.
  7. Candidate with zero ref slots → does not count against coverage.
  8. to_dict() serialises all gate outputs.
  9. binding_record_from_entity_refs builds records correctly.
"""
from __future__ import annotations

import pytest

from src.core.rev.entity_binding_gate import (
    COVERAGE_FLOOR,
    MIN_SAMPLE_FOR_GATE,
    PRECISION_FLOOR,
    CandidateBindingRecord,
    EntityBindingReport,
    binding_record_from_entity_refs,
    evaluate_binding,
)

_UNRESOLVED = "UNRESOLVED:"


def _rec(
    cid: str,
    *,
    total: int = 2,
    attempted: int | None = None,
    resolved: int = 2,
    unresolved: int = 0,
    minted: int = 0,
) -> CandidateBindingRecord:
    # Default attempted = resolved + unresolved (consistent totals).
    if attempted is None:
        attempted = resolved + unresolved
    return CandidateBindingRecord(
        candidate_id=cid,
        total_ref_slots=total,
        attempted_refs=attempted,
        resolved_refs=resolved,
        unresolved_refs=unresolved,
        minted_refs=minted,
    )


def _batch_pass(n: int = 10) -> list[CandidateBindingRecord]:
    """All refs resolved — should pass both gates."""
    return [_rec(f"c{i}") for i in range(n)]


class TestGatePass:

    def test_perfect_binding_passes(self) -> None:
        report = evaluate_binding(_batch_pass(10), program_id="nova")
        assert report.ok, f"Expected PASS; failures={report.failures}"
        assert report.binding_precision == 1.0
        assert report.binding_coverage == 1.0

    def test_at_threshold_precision_passes(self) -> None:
        # 19/20 resolved → precision = 95 % exactly.
        records = [_rec(f"c{i}", total=1, resolved=1, unresolved=0) for i in range(19)]
        records.append(_rec("cx", total=1, resolved=0, unresolved=1))
        report = evaluate_binding(records, program_id="nova")
        assert report.binding_precision == pytest.approx(0.95)
        assert report.ok

    def test_at_threshold_coverage_passes(self) -> None:
        # 8/10 total slots resolved → coverage = 80 % exactly.
        # All attempted refs are resolved (precision = 100 %).
        # 2 records have total_ref_slots=1 but 0 attempted (unextracted slot).
        records = [_rec(f"c{i}", total=1, resolved=1, unresolved=0) for i in range(8)]
        records += [_rec(f"d{i}", total=1, resolved=0, unresolved=0) for i in range(2)]
        report = evaluate_binding(records, program_id="nova")
        assert report.binding_precision == pytest.approx(1.0)
        assert report.binding_coverage == pytest.approx(0.80)
        assert report.ok


class TestGateFail:

    def test_low_precision_fails(self) -> None:
        # 5/10 resolved → precision = 50 % < 95 %.
        records = [_rec(f"c{i}", total=1, resolved=0, unresolved=1) for i in range(5)]
        records += [_rec(f"d{i}", total=1, resolved=1, unresolved=0) for i in range(5)]
        report = evaluate_binding(records, program_id="nova")
        assert not report.ok
        assert any("precision" in f for f in report.failures)

    def test_low_coverage_fails(self) -> None:
        # 2/20 total slots resolved → coverage = 10 % < 80 %.
        records = [_rec(f"c{i}", total=2, resolved=0, unresolved=0) for i in range(9)]
        records.append(_rec("c9", total=2, resolved=2, unresolved=0))
        report = evaluate_binding(records, program_id="nova")
        assert not report.ok
        assert any("coverage" in f for f in report.failures)

    def test_below_precision_and_coverage_reports_both(self) -> None:
        records = [_rec(f"c{i}", total=4, resolved=0, unresolved=1) for i in range(10)]
        report = evaluate_binding(records, program_id="nova")
        assert not report.ok
        assert len(report.failures) >= 2


class TestSmallSample:

    def test_tiny_batch_skips_gate_emits_warning(self) -> None:
        records = [_rec("c0", resolved=0, unresolved=1)]
        report = evaluate_binding(records, program_id="nova")
        assert report.ok, "Gate must not fire on tiny sample"
        assert any(str(MIN_SAMPLE_FOR_GATE) in w for w in report.warnings)

    def test_exactly_min_sample_fires_gate(self) -> None:
        records = [_rec(f"c{i}", resolved=0, unresolved=1) for i in range(MIN_SAMPLE_FOR_GATE)]
        report = evaluate_binding(records, program_id="nova")
        assert not report.ok


class TestMintGateWarning:

    def test_minted_refs_emit_operator_warning(self) -> None:
        records = _batch_pass(5)
        records.append(_rec("minted", minted=2))
        report = evaluate_binding(records, program_id="nova")
        assert any("minted" in w for w in report.warnings)

    def test_no_minted_refs_no_warning(self) -> None:
        report = evaluate_binding(_batch_pass(5), program_id="nova")
        assert not any("minted" in w for w in report.warnings)


class TestZeroRefSlots:

    def test_zero_ref_slots_candidate_not_counted_against_coverage(self) -> None:
        records = [_rec(f"c{i}", total=0, attempted=0, resolved=0) for i in range(10)]
        report = evaluate_binding(records, program_id="nova")
        assert report.binding_coverage == 1.0

    def test_no_attempted_refs_not_counted_against_precision(self) -> None:
        records = [_rec(f"c{i}", total=0, attempted=0, resolved=0) for i in range(5)]
        report = evaluate_binding(records, program_id="nova")
        assert report.binding_precision == 1.0


class TestToDict:

    def test_to_dict_contains_required_keys(self) -> None:
        report = evaluate_binding(_batch_pass(5), program_id="nova")
        d = report.to_dict()
        for key in (
            "program_id", "n_candidates", "binding_precision", "binding_coverage",
            "binding_precision_ci", "binding_coverage_ci", "failures", "warnings",
        ):
            assert key in d, f"Missing key: {key}"

    def test_to_dict_ci_is_tuple(self) -> None:
        report = evaluate_binding(_batch_pass(5), program_id="nova")
        d = report.to_dict()
        assert len(d["binding_precision_ci"]) == 2
        assert d["binding_precision_ci"][0] <= d["binding_precision_ci"][1]


class TestBindingRecordFactory:

    def test_all_resolved(self) -> None:
        rec = binding_record_from_entity_refs(
            candidate_id="c1",
            entity_refs=("workitem:1", "workitem:2"),
        )
        assert rec.resolved_refs == 2
        assert rec.unresolved_refs == 0
        assert rec.precision == 1.0
        assert rec.coverage == 1.0

    def test_some_unresolved(self) -> None:
        rec = binding_record_from_entity_refs(
            candidate_id="c2",
            entity_refs=("workitem:1", "UNRESOLVED:mystery-person"),
        )
        assert rec.resolved_refs == 1
        assert rec.unresolved_refs == 1
        assert rec.precision == pytest.approx(0.5)

    def test_total_ref_slots_override(self) -> None:
        rec = binding_record_from_entity_refs(
            candidate_id="c3",
            entity_refs=("workitem:1",),
            total_ref_slots=3,
        )
        assert rec.total_ref_slots == 3
        assert rec.coverage == pytest.approx(1 / 3)

    def test_empty_refs(self) -> None:
        rec = binding_record_from_entity_refs(candidate_id="c4", entity_refs=())
        assert rec.resolved_refs == 0
        assert rec.attempted_refs == 0
        assert rec.precision == 1.0
        assert rec.coverage == 1.0
