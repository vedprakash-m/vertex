"""Focused tests for the read-only RELO qualification-input verifier."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core.relo_w6 import verify_w6_packet
from src.core.study_evaluator import (
    MatchedWeeklyCycle,
    StratumSample,
    WeightedSavingsObservation,
    calculate_matched_cycle_savings,
    evaluate_matched_cycles,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_relo_qualification_packet.py"
_SPEC = importlib.util.spec_from_file_location("verify_relo_qualification_packet", SCRIPT_PATH)
verifier = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["verify_relo_qualification_packet"] = verifier
_SPEC.loader.exec_module(verifier)


def _d7_packet(*, vertex_seconds: float = 10.0) -> dict[str, Any]:
    first_cycle = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cycles: list[dict[str, Any]] = []
    for index in range(8):
        observed_at = (first_cycle + timedelta(days=index * 7)).isoformat()
        pair_id = f"pair-{index + 1:02d}"
        for arm, active_seconds in (("manual", 100.0), ("vertex", vertex_seconds)):
            cycles.append(
                {
                    "pair_id": pair_id,
                    "cycle_id": f"cycle-{index + 1:02d}",
                    "arm": arm,
                    "workflow_type": "weekly_portfolio_review",
                    "program": "synthetic-program",
                    "scope": "one_workstream",
                    "complexity": "medium",
                    "frequency_weight": 1.0,
                    "observed_at": observed_at,
                    "status": "completed",
                    "quality_adjusted_active_seconds": active_seconds,
                }
            )
    return {
        "schema_version": "1",
        "decision": "RELO-D7",
        "protocol": {
            "frozen": True,
            "assignment": "alternating",
            "historical_baselines": [{"baseline_id": f"baseline-{index + 1}"} for index in range(5)],
            "applicable_workflow_classes": ["weekly_portfolio_review"],
            "bootstrap": {
                "method": "matched_cycle_frequency_weighted",
                "resamples": 2000,
                "seed": 7307,
                "confidence_level": 0.95,
            },
        },
        "cycles": cycles,
    }


def _economics_packet(decision: str = "RELO-D6d") -> dict[str, Any]:
    packet = {
        "schema_version": "1",
        "decision": decision,
        "records": [
            {
                "run_ref": "run-1",
                "manifest_ref": "manifest-1",
                "artifact_ref": "artifact-1",
                "cycle_ref": "cycle-1",
                "program_ref": "program-synthetic",
                "model_version": "synthetic-model-v1",
                "pricing_version": "synthetic-pricing-v1",
                "tokens": 1500,
                "cost": 1.25,
                "confidence": 0.9,
                "trace_evidence_ref": "trace-1",
                "manifest_evidence_ref": "manifest-evidence-1",
                "billing_evidence_ref": "billing-1",
                "provider_cost": 1.25,
                "total_tco_cost": 2.5,
            },
            {
                "run_ref": "run-2",
                "manifest_ref": "manifest-2",
                "artifact_ref": "artifact-2",
                "cycle_ref": "cycle-2",
                "program_ref": "program-synthetic",
                "model_version": "synthetic-model-v1",
                "pricing_version": "synthetic-pricing-v1",
                "tokens": 500,
                "cost": 0.75,
                "confidence": 0.8,
                "trace_evidence_ref": "trace-2",
                "manifest_evidence_ref": "manifest-evidence-2",
                "billing_evidence_ref": "billing-2",
                "provider_cost": 0.75,
                "total_tco_cost": 1.5,
            },
        ],
        "denominators": {
            "certified_artifact_count": 4,
            "completed_cycle_count": 2,
            "active_program_months": 0.5,
        },
        "unattributed_cost": 0.1,
        "uncertain_cost": 0.2,
    }
    if decision == "RELO-D14":
        packet["private_budget_thresholds"] = {
            "max_cost_per_certified_artifact": 1.0,
            "max_cost_per_completed_cycle": 2.0,
            "max_cost_per_active_program_month": 8.0,
            "max_unattributed_and_uncertain_cost": 0.3,
        }
    return packet


@pytest.mark.parametrize(
    ("mutate", "missing_field"),
    [
        (lambda payload: payload.pop("schema_version"), "schema_version"),
        (lambda payload: payload["protocol"].update(frozen=False), "protocol.frozen"),
        (
            lambda payload: payload["protocol"].update(assignment="random"),
            "protocol.assignment",
        ),
        (
            lambda payload: payload["cycles"][0].pop("quality_adjusted_active_seconds"),
            "cycles[0].quality_adjusted_active_seconds",
        ),
    ],
)
def test_d7_invalid_inputs_report_exact_fields(mutate, missing_field: str) -> None:
    payload = _d7_packet()
    mutate(payload)

    result = verifier.verify_packet(payload)

    assert result["classification"] == "not_qualified"
    assert missing_field in result["missing_fields"] + result["invalid_fields"]


def test_d7_exact_080_lower_bound_fails_owner_review_threshold() -> None:
    result = verifier.verify_packet(_d7_packet(vertex_seconds=20.0))

    assert result["classification"] == "not_qualified"
    assert result["measurements"]["bootstrap_95_lower_bound"] == 0.8
    assert "bootstrap_lower_bound_strictly_above_0.80" in result["failed_gates"]


@pytest.mark.parametrize(
    ("mutate", "failed_gate"),
    [
        (
            lambda payload: payload.update(
                cycles=[cycle for cycle in payload["cycles"] if cycle["pair_id"] != "pair-08"]
            ),
            "at_least_8_matched_operating_cycles",
        ),
        (
            lambda payload: [
                cycle.update(observed_at="2026-02-18T00:00:00+00:00")
                for cycle in payload["cycles"]
                if cycle["pair_id"] == "pair-08"
            ],
            "first_to_last_elapsed_at_least_49_days",
        ),
    ],
)
def test_d7_exact_cycle_and_elapsed_boundaries_are_enforced(
    mutate, failed_gate: str
) -> None:
    payload = _d7_packet()
    mutate(payload)

    result = verifier.verify_packet(payload)

    assert result["classification"] == "not_qualified"
    assert failed_gate in result["failed_gates"]


def test_d7_complete_positive_packet_is_owner_review_only() -> None:
    result = verifier.verify_packet(_d7_packet(vertex_seconds=10.0))

    assert result["classification"] == "owner_review"
    assert result["measurements"]["matched_operating_cycle_count"] == 8
    assert result["measurements"]["elapsed_days"] == 49.0
    assert result["measurements"]["bootstrap_95_lower_bound"] > 0.8
    assert result["measurements"]["bootstrap_seed"] == 7307
    assert "owner approval" in " ".join(result["limitations"]).lower()
    assert "certification" in " ".join(result["limitations"]).lower()


def test_w62_readiness_and_d7_share_one_weighted_savings_calculation() -> None:
    d7_payload = _d7_packet()
    for cycle_index in range(8):
        manual = 10.0 if cycle_index < 7 else 1000.0
        vertex = 0.0 if cycle_index < 7 else 400.0
        d7_payload["cycles"][cycle_index * 2][
            "quality_adjusted_active_seconds"
        ] = manual
        d7_payload["cycles"][cycle_index * 2 + 1][
            "quality_adjusted_active_seconds"
        ] = vertex

    d7_result = verifier.verify_packet(d7_payload)
    w62_payload = {
        "schema_version": "1",
        "card_id": "RELO-W6.2",
        "packet_id": "packet-w62",
        "captured_at": "2026-02-20T00:00:00+00:00",
        "owner_disposition": {
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
            "approval_ref": None,
        },
        "artifacts": [],
        "protocol": {
            "frozen": True,
            "assignment": "alternating",
            "bootstrap_seed": 7307,
            "bootstrap_resamples": 2000,
            "applicable_workflow_classes": ["weekly_portfolio_review"],
        },
        "cycles": [
            {
                "cycle_id": f"cycle-{index + 1}",
                "observed_at": (
                    datetime(2026, 1, 1, tzinfo=timezone.utc)
                    + timedelta(days=index * 7)
                ).isoformat(),
                "manual_minutes": manual,
                "assisted_minutes": vertex,
                "quality_passed": True,
                "completion_rate": 1.0,
                "displacement_rate": 0.0,
                "correction_count": 0,
                "exception_count": 0,
                "downstream_error_count": 0,
                "workflow_classes": ["weekly_portfolio_review"],
            }
            for index, (manual, vertex) in enumerate(
                (
                    (
                        d7_payload["cycles"][index * 2][
                            "quality_adjusted_active_seconds"
                        ],
                        d7_payload["cycles"][index * 2 + 1][
                            "quality_adjusted_active_seconds"
                        ],
                    )
                    for index in range(8)
                )
            )
        ],
    }
    w62_result = verify_w6_packet(w62_payload)
    readiness_result = evaluate_matched_cycles(
        tuple(
            MatchedWeeklyCycle(
                cycle_id=row["cycle_id"],
                week_start=datetime.fromisoformat(row["observed_at"]).date(),
                baseline_minutes=row["manual_minutes"],
                assisted_minutes=row["assisted_minutes"],
                quality_passed=True,
            )
            for row in w62_payload["cycles"]
        ),
        strata=(StratumSample("exec", 60, 0),),
        bootstrap_resamples=2000,
        bootstrap_seed=7307,
    )

    expected_savings = d7_result["measurements"]["frequency_weighted_savings"]
    assert w62_result.measurements[
        "quality_adjusted_saved_fraction"
    ] == pytest.approx(expected_savings)
    assert readiness_result.mean_saved_fraction == pytest.approx(expected_savings)
    assert w62_result.measurements["bootstrap_95_lower_bound"] == pytest.approx(
        d7_result["measurements"]["bootstrap_95_lower_bound"]
    )
    assert readiness_result.lcb_95_saved_fraction == pytest.approx(
        d7_result["measurements"]["bootstrap_95_lower_bound"]
    )
    assert d7_result["classification"] == "not_qualified"
    assert w62_result.classification == "not_qualified"
    assert w62_result.failed_gates
    assert readiness_result.accepted is False


def test_d7_frequency_weights_are_used_in_shared_cycle_bootstrap() -> None:
    payload = _d7_packet(vertex_seconds=40.0)
    weights = (1.0, 2.0, 3.0, 4.0)
    for cycle_index in range(8):
        for row in payload["cycles"][cycle_index * 2 : cycle_index * 2 + 2]:
            row["frequency_weight"] = weights[cycle_index % len(weights)]

    result = verifier.verify_packet(payload)
    shared = calculate_matched_cycle_savings(
        tuple(
            (
                WeightedSavingsObservation(
                    baseline_minutes=100.0,
                    saved_minutes=60.0,
                    frequency_weight=weights[index % len(weights)],
                ),
            )
            for index in range(8)
        ),
        resamples=2000,
        seed=7307,
    )

    assert result["measurements"]["frequency_weighted_savings"] == pytest.approx(
        shared.saved_fraction
    )
    assert result["measurements"]["bootstrap_95_lower_bound"] == pytest.approx(
        shared.lcb_95_saved_fraction
    )
    assert result["measurements"]["frequency_weighted_savings"] == pytest.approx(0.6)


def test_d7_requires_five_distinct_baselines_and_at_least_1000_resamples() -> None:
    duplicate = _d7_packet()
    duplicate["protocol"]["historical_baselines"] = [
        {"baseline_id": "one-baseline"}
    ] * 5
    sparse_bootstrap = _d7_packet()
    sparse_bootstrap["protocol"]["bootstrap"]["resamples"] = 1

    duplicate_result = verifier.verify_packet(duplicate)
    sparse_result = verifier.verify_packet(sparse_bootstrap)

    assert "protocol.historical_baselines" in duplicate_result["invalid_fields"]
    assert "protocol.bootstrap.resamples" in sparse_result["invalid_fields"]
    assert duplicate_result["classification"] == "not_qualified"
    assert sparse_result["classification"] == "not_qualified"


def test_d7_counts_cycles_but_preserves_each_cycle_cluster() -> None:
    payload = _d7_packet()
    payload["protocol"]["applicable_workflow_classes"] = [
        "weekly_portfolio_review",
        "dependency_review",
    ]
    additional: list[dict[str, Any]] = []
    for record in payload["cycles"]:
        extra = copy.deepcopy(record)
        extra["pair_id"] = f"dependency-{record['pair_id']}"
        extra["workflow_type"] = "dependency_review"
        additional.append(extra)
    payload["cycles"].extend(additional)

    result = verifier.verify_packet(payload)

    assert result["classification"] == "owner_review"
    assert result["measurements"]["matched_operating_cycle_count"] == 8


def test_d7_rejects_mismatched_pair_identity() -> None:
    payload = _d7_packet()
    payload["cycles"][1]["complexity"] = "high"
    payload["cycles"][3]["cycle_id"] = "different-cycle"

    result = verifier.verify_packet(payload)

    assert result["classification"] == "not_qualified"
    assert any(field.endswith(".complexity") for field in result["invalid_fields"])
    assert "cycles[3].cycle_id" in result["invalid_fields"]


def test_d7_requires_every_cycle_to_cover_applicable_workflow_classes() -> None:
    payload = _d7_packet()
    payload["protocol"]["applicable_workflow_classes"].append("dependency_review")

    result = verifier.verify_packet(payload)

    assert result["classification"] == "not_qualified"
    assert (
        "every_cycle_covers_applicable_workflow_classes"
        in result["failed_gates"]
    )


def test_d7_does_not_ignore_failed_or_unavailable_pairs_from_denominator() -> None:
    payload = _d7_packet()
    payload["cycles"][1]["status"] = "failed"

    result = verifier.verify_packet(payload)

    assert result["classification"] == "not_qualified"
    assert (
        "non_completed_pairs_need_owner_approved_denominator_treatment"
        in result["failed_gates"]
    )


def test_d6d_calculates_unit_economics_as_measurement_ready() -> None:
    result = verifier.verify_packet(_economics_packet())

    assert result["classification"] == "measurement_ready"
    assert result["measurements"] == {
        "provider_cost": 2.0,
        "total_tco_cost": 4.0,
        "cost_per_certified_artifact": 1.0,
        "cost_per_completed_cycle": 2.0,
        "cost_per_active_program_month": 8.0,
        "unattributed_cost": 0.1,
        "uncertain_cost": 0.2,
        "unattributed_and_uncertain_cost": 0.3,
        "record_count": 2,
    }
    assert "ratification" in " ".join(result["limitations"]).lower()


@pytest.mark.parametrize(
    "field",
    [
        "run_ref",
        "manifest_evidence_ref",
        "billing_evidence_ref",
        "total_tco_cost",
    ],
)
def test_economics_missing_record_evidence_is_not_qualified(field: str) -> None:
    payload = _economics_packet()
    payload["records"][0].pop(field)

    result = verifier.verify_packet(payload)

    assert result["classification"] == "not_qualified"
    assert f"records[0].{field}" in result["missing_fields"]


def test_economics_provider_cost_must_match_recorded_provider_cost() -> None:
    payload = _economics_packet()
    payload["records"][0]["cost"] = 1.0

    result = verifier.verify_packet(payload)

    assert result["classification"] == "not_qualified"
    assert "records[0].cost" in result["invalid_fields"]


def test_d14_thresholds_at_limits_are_owner_review() -> None:
    result = verifier.verify_packet(_economics_packet("RELO-D14"))

    assert result["classification"] == "owner_review"
    assert result["failed_gates"] == []


def test_d14_threshold_failure_and_missing_private_budget_are_not_qualified() -> None:
    failed = _economics_packet("RELO-D14")
    failed["private_budget_thresholds"]["max_cost_per_completed_cycle"] = 1.9
    missing = _economics_packet("RELO-D14")
    missing["private_budget_thresholds"].pop("max_unattributed_and_uncertain_cost")

    failed_result = verifier.verify_packet(failed)
    missing_result = verifier.verify_packet(missing)

    assert failed_result["classification"] == "not_qualified"
    assert "cost_per_completed_cycle_within_private_budget" in failed_result["failed_gates"]
    assert missing_result["classification"] == "not_qualified"
    assert (
        "private_budget_thresholds.max_unattributed_and_uncertain_cost"
        in missing_result["missing_fields"]
    )


def test_cli_outputs_json_and_does_not_mutate_input_tree(tmp_path: Path) -> None:
    input_path = tmp_path / "packet.json"
    input_path.write_text(json.dumps(_d7_packet(), indent=2), encoding="utf-8")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--input", str(input_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    result = json.loads(completed.stdout)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert completed.returncode == 0
    assert result["classification"] == "owner_review"
    assert after == before


def test_verify_packet_does_not_mutate_input() -> None:
    payload = _economics_packet("RELO-D14")
    before = copy.deepcopy(payload)

    verifier.verify_packet(payload)

    assert payload == before
