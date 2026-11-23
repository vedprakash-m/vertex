"""Read-only verifier for synthetic RELO D7, D6d and D14 input packets.

The command consumes one explicit JSON path and writes one JSON result to
stdout. It never reads provider state, authenticates evidence, records owner
approval, or qualifies a production deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.study_evaluator import (  # noqa: E402
    WeightedSavingsObservation,
    calculate_matched_cycle_savings,
)


SCHEMA_VERSION = "1"
SUPPORTED_DECISIONS = frozenset({"RELO-D7", "RELO-D6d", "RELO-D14"})
MATCH_FIELDS = ("workflow_type", "program", "scope", "complexity")
VALID_STATUSES = frozenset({"completed", "failed", "unavailable", "abandoned"})
VALID_ASSIGNMENTS = frozenset({"alternating", "stepped_wedge"})
D7_THRESHOLD = Decimal("0.80")
MIN_BOOTSTRAP_RESAMPLES = 1000


def verify_packet(payload: object) -> dict[str, Any]:
    """Validate one explicitly supplied synthetic qualification packet."""
    limitations = _limitations()
    if not isinstance(payload, dict):
        return _result(
            decision=None,
            classification="not_qualified",
            missing_fields=(),
            invalid_fields=("input",),
            failed_gates=(),
            measurements={},
            limitations=limitations,
        )

    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    schema_version = payload.get("schema_version")
    if schema_version is None:
        missing_fields.append("schema_version")
    elif schema_version != SCHEMA_VERSION:
        invalid_fields.append("schema_version")

    decision_value = payload.get("decision")
    if decision_value is None:
        missing_fields.append("decision")
        decision: str | None = None
    elif not isinstance(decision_value, str) or decision_value not in SUPPORTED_DECISIONS:
        invalid_fields.append("decision")
        decision = str(decision_value)
    else:
        decision = decision_value

    if decision == "RELO-D7":
        outcome = _verify_d7(payload, missing_fields, invalid_fields)
    elif decision in {"RELO-D6d", "RELO-D14"}:
        outcome = _verify_economics(
            payload,
            decision=decision,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )
    else:
        outcome = ("not_qualified", {}, ())

    classification, measurements, failed_gates = outcome
    if missing_fields or invalid_fields:
        classification = "not_qualified"
    return _result(
        decision=decision,
        classification=classification,
        missing_fields=missing_fields,
        invalid_fields=invalid_fields,
        failed_gates=failed_gates,
        measurements=measurements,
        limitations=limitations,
    )


def _verify_d7(
    payload: dict[str, Any],
    missing_fields: list[str],
    invalid_fields: list[str],
) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    protocol_value = payload.get("protocol")
    protocol = protocol_value if isinstance(protocol_value, dict) else None
    if protocol is None:
        if protocol_value is None:
            missing_fields.append("protocol")
        else:
            invalid_fields.append("protocol")
        protocol = {}
        protocol_prefix = "protocol"
    else:
        protocol_prefix = "protocol"

    frozen = protocol.get("frozen")
    if frozen is None:
        missing_fields.append(f"{protocol_prefix}.frozen")
    elif frozen is not True:
        invalid_fields.append(f"{protocol_prefix}.frozen")

    assignment = protocol.get("assignment")
    if assignment is None:
        missing_fields.append(f"{protocol_prefix}.assignment")
    elif assignment not in VALID_ASSIGNMENTS:
        invalid_fields.append(f"{protocol_prefix}.assignment")

    baselines = protocol.get("historical_baselines")
    if baselines is None:
        missing_fields.append(f"{protocol_prefix}.historical_baselines")
    elif not isinstance(baselines, list):
        invalid_fields.append(f"{protocol_prefix}.historical_baselines")
    else:
        baseline_ids = [
            baseline.get("baseline_id")
            for baseline in baselines
            if isinstance(baseline, dict)
            and isinstance(baseline.get("baseline_id"), str)
            and baseline["baseline_id"].strip()
        ]
        if len(baselines) < 5 or len(set(baseline_ids)) < 5:
            invalid_fields.append(f"{protocol_prefix}.historical_baselines")
        for index, baseline in enumerate(baselines):
            path = f"{protocol_prefix}.historical_baselines[{index}]"
            if not isinstance(baseline, dict):
                invalid_fields.append(path)
            elif _is_blank(baseline.get("baseline_id")):
                missing_fields.append(f"{path}.baseline_id")

    workflow_classes = protocol.get("applicable_workflow_classes")
    workflow_class_set: set[str] = set()
    if workflow_classes is None:
        missing_fields.append(f"{protocol_prefix}.applicable_workflow_classes")
    elif not isinstance(workflow_classes, list) or not workflow_classes:
        invalid_fields.append(f"{protocol_prefix}.applicable_workflow_classes")
    else:
        for index, workflow_class in enumerate(workflow_classes):
            if not isinstance(workflow_class, str) or not workflow_class.strip():
                invalid_fields.append(
                    f"{protocol_prefix}.applicable_workflow_classes[{index}]"
                )
            else:
                workflow_class_set.add(workflow_class)

    bootstrap = protocol.get("bootstrap")
    bootstrap_parameters: dict[str, Any] = {}
    if bootstrap is None:
        missing_fields.append(f"{protocol_prefix}.bootstrap")
    elif not isinstance(bootstrap, dict):
        invalid_fields.append(f"{protocol_prefix}.bootstrap")
    else:
        method = bootstrap.get("method")
        if method is None:
            missing_fields.append(f"{protocol_prefix}.bootstrap.method")
        elif method != "matched_cycle_frequency_weighted":
            invalid_fields.append(f"{protocol_prefix}.bootstrap.method")

        resamples = bootstrap.get("resamples")
        if resamples is None:
            missing_fields.append(f"{protocol_prefix}.bootstrap.resamples")
        elif _is_positive_int(resamples) and resamples >= MIN_BOOTSTRAP_RESAMPLES:
            bootstrap_parameters["resamples"] = resamples
        else:
            invalid_fields.append(f"{protocol_prefix}.bootstrap.resamples")

        seed = bootstrap.get("seed")
        if seed is None:
            missing_fields.append(f"{protocol_prefix}.bootstrap.seed")
        elif _is_nonnegative_int(seed):
            bootstrap_parameters["seed"] = seed
        else:
            invalid_fields.append(f"{protocol_prefix}.bootstrap.seed")

        confidence = _decimal(bootstrap.get("confidence_level"))
        if "confidence_level" not in bootstrap:
            missing_fields.append(f"{protocol_prefix}.bootstrap.confidence_level")
        elif confidence != Decimal("0.95"):
            invalid_fields.append(f"{protocol_prefix}.bootstrap.confidence_level")
        else:
            bootstrap_parameters["confidence_level"] = confidence

    cycles_value = payload.get("cycles")
    cycles_path = "cycles"
    if cycles_value is None:
        cycles_value = payload.get("matched_cycles")
        cycles_path = "matched_cycles"
    if cycles_value is None:
        cycles_value = payload.get("pairs")
        cycles_path = "pairs"
        cycles = _flatten_pairs(cycles_value, invalid_fields)
    else:
        cycles = _validate_cycle_list(cycles_value, cycles_path, missing_fields, invalid_fields)

    valid_cycle_rows: list[dict[str, Any]] = []
    for index, cycle in enumerate(cycles):
        path = f"{cycles_path}[{index}]"
        row: dict[str, Any] = {"index": index}
        for field in (
            "pair_id",
            "cycle_id",
            "arm",
            "workflow_type",
            "program",
            "scope",
            "complexity",
            "status",
        ):
            value = cycle.get(field)
            if value is None:
                missing_fields.append(f"{path}.{field}")
            elif not isinstance(value, str) or not value.strip():
                invalid_fields.append(f"{path}.{field}")
            else:
                row[field] = value

        arm = row.get("arm")
        if isinstance(arm, str) and arm not in {"manual", "vertex"}:
            invalid_fields.append(f"{path}.arm")
        status = row.get("status")
        if isinstance(status, str) and status not in VALID_STATUSES:
            invalid_fields.append(f"{path}.status")

        workflow_type = row.get("workflow_type")
        if (
            isinstance(workflow_type, str)
            and workflow_class_set
            and workflow_type not in workflow_class_set
        ):
            invalid_fields.append(f"{path}.workflow_type")

        frequency_weight = _decimal(cycle.get("frequency_weight"))
        if "frequency_weight" not in cycle:
            missing_fields.append(f"{path}.frequency_weight")
        elif frequency_weight is None or frequency_weight <= 0:
            invalid_fields.append(f"{path}.frequency_weight")
        else:
            row["frequency_weight"] = frequency_weight

        active_seconds = _decimal(cycle.get("quality_adjusted_active_seconds"))
        if "quality_adjusted_active_seconds" not in cycle:
            missing_fields.append(f"{path}.quality_adjusted_active_seconds")
        elif active_seconds is None or active_seconds < 0:
            invalid_fields.append(f"{path}.quality_adjusted_active_seconds")
        else:
            row["active_seconds"] = active_seconds

        observed_at = cycle.get("observed_at")
        if observed_at is None:
            missing_fields.append(f"{path}.observed_at")
        else:
            parsed_time = _parse_datetime(observed_at)
            if parsed_time is None:
                invalid_fields.append(f"{path}.observed_at")
            else:
                row["observed_at"] = parsed_time
        valid_cycle_rows.append(row)

    matched_pairs = _matched_pairs(
        valid_cycle_rows,
        cycles_path=cycles_path,
        invalid_fields=invalid_fields,
    )
    matched_cycle_ids = {pair[0]["cycle_id"] for pair in matched_pairs}
    matched_count = len(matched_cycle_ids)
    elapsed_days: Decimal | None = None
    if matched_pairs:
        times = [
            record["observed_at"]
            for pair in matched_pairs
            for record in pair
        ]
        elapsed = max(times) - min(times)
        elapsed_days = Decimal(str(elapsed.total_seconds())) / Decimal("86400")

    failed_gates: list[str] = []
    if matched_count < 8:
        failed_gates.append("at_least_8_matched_operating_cycles")
    if elapsed_days is None or elapsed_days < Decimal("49"):
        failed_gates.append("first_to_last_elapsed_at_least_49_days")
    if workflow_class_set:
        classes_by_cycle: dict[str, set[str]] = {}
        for pair in matched_pairs:
            for record in pair:
                classes_by_cycle.setdefault(record["cycle_id"], set()).add(
                    record["workflow_type"]
                )
        incomplete_cycles = [
            cycle_id
            for cycle_id in matched_cycle_ids
            if classes_by_cycle[cycle_id] != workflow_class_set
        ]
        if incomplete_cycles:
            failed_gates.append("every_cycle_covers_applicable_workflow_classes")
    if any(
        record["status"] != "completed"
        for pair in matched_pairs
        for record in pair
    ):
        failed_gates.append("non_completed_pairs_need_owner_approved_denominator_treatment")

    measurements: dict[str, Any] = {
        "matched_operating_cycle_count": matched_count,
        "elapsed_days": _json_number(elapsed_days),
        "frequency_weighted_savings": None,
        "bootstrap_95_lower_bound": None,
        "bootstrap_resamples": bootstrap_parameters.get("resamples"),
        "bootstrap_seed": bootstrap_parameters.get("seed"),
    }
    if not missing_fields and not invalid_fields and not failed_gates:
        weighted_pairs_by_cycle: dict[
            str, list[tuple[Decimal, Decimal, Decimal]]
        ] = {}
        for pair in matched_pairs:
            by_arm = {record["arm"]: record for record in pair}
            weighted_pairs_by_cycle.setdefault(pair[0]["cycle_id"], []).append(
                (
                    by_arm["manual"]["active_seconds"],
                    by_arm["vertex"]["active_seconds"],
                    by_arm["manual"]["frequency_weight"],
                )
            )
        weighted_cycles = list(weighted_pairs_by_cycle.values())
        shared_cycles = tuple(
            tuple(
                WeightedSavingsObservation(
                    baseline_minutes=float(manual),
                    saved_minutes=float(manual - vertex),
                    frequency_weight=float(weight),
                )
                for manual, vertex, weight in cycle
            )
            for cycle in weighted_cycles
        )
        savings = calculate_matched_cycle_savings(
            shared_cycles,
            resamples=bootstrap_parameters["resamples"],
            seed=bootstrap_parameters["seed"],
            confidence_level=float(bootstrap_parameters["confidence_level"]),
        )
        lower_bound = savings.lcb_95_saved_fraction
        measurements["frequency_weighted_savings"] = _json_number(
            savings.saved_fraction
        )
        measurements["bootstrap_95_lower_bound"] = _json_number(lower_bound)
        if lower_bound is None or lower_bound <= float(D7_THRESHOLD):
            failed_gates.append("bootstrap_lower_bound_strictly_above_0.80")

    classification = "owner_review" if not failed_gates else "not_qualified"
    return classification, measurements, tuple(failed_gates)


def _validate_cycle_list(
    value: object,
    path: str,
    missing_fields: list[str],
    invalid_fields: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        invalid_fields.append(path)
        return []
    if not value:
        invalid_fields.append(path)
        return []
    cycles: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if isinstance(row, dict):
            cycles.append(row)
        else:
            invalid_fields.append(f"{path}[{index}]")
            cycles.append({})
    return cycles


def _flatten_pairs(
    value: object,
    invalid_fields: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        invalid_fields.append("pairs")
        return []
    flattened: list[dict[str, Any]] = []
    for pair_index, pair in enumerate(value):
        pair_path = f"pairs[{pair_index}]"
        if not isinstance(pair, dict):
            invalid_fields.append(pair_path)
            continue
        arms = pair.get("arms")
        if arms is None:
            flattened.append(pair)
            continue
        if not isinstance(arms, list):
            invalid_fields.append(f"{pair_path}.arms")
            continue
        shared = {
            field: pair.get(field)
            for field in (
                *MATCH_FIELDS,
                "pair_id",
                "cycle_id",
                "frequency_weight",
                "observed_at",
            )
        }
        for arm_index, arm in enumerate(arms):
            if not isinstance(arm, dict):
                invalid_fields.append(f"{pair_path}.arms[{arm_index}]")
                flattened.append({})
                continue
            flattened.append({**shared, **arm})
    return flattened


def _matched_pairs(
    rows: list[dict[str, Any]],
    *,
    cycles_path: str,
    invalid_fields: list[str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        pair_id = row.get("pair_id")
        if isinstance(pair_id, str):
            grouped.setdefault(pair_id, []).append(row)

    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pair_id, pair_rows in grouped.items():
        if len(pair_rows) != 2:
            invalid_fields.append(f"pair[{pair_id}].record_count")
            continue
        by_arm = {row.get("arm"): row for row in pair_rows}
        if set(by_arm) != {"manual", "vertex"} or len(by_arm) != 2:
            invalid_fields.append(f"pair[{pair_id}].arms")
            continue
        base, other = pair_rows
        matching = True
        for field in (*MATCH_FIELDS, "cycle_id", "observed_at"):
            if base.get(field) != other.get(field):
                matching = False
                invalid_fields.append(
                    f"{cycles_path}[{other['index']}].{field}"
                )
        if base.get("frequency_weight") != other.get("frequency_weight"):
            matching = False
            invalid_fields.append(f"pair[{pair_id}].frequency_weight")
        manual = by_arm["manual"]
        vertex = by_arm["vertex"]
        required = (
            "pair_id",
            "cycle_id",
            "arm",
            "workflow_type",
            "program",
            "scope",
            "complexity",
            "status",
            "frequency_weight",
            "active_seconds",
            "observed_at",
        )
        if matching and all(field in manual and field in vertex for field in required):
            if manual["active_seconds"] <= 0:
                invalid_fields.append(
                    f"{cycles_path}[{manual['index']}].quality_adjusted_active_seconds"
                )
            else:
                matched_pairs.append((manual, vertex))
    return matched_pairs


def _verify_economics(
    payload: dict[str, Any],
    *,
    decision: str,
    missing_fields: list[str],
    invalid_fields: list[str],
) -> tuple[str, dict[str, Any], tuple[str, ...]]:
    records_value = payload.get("records")
    records = _validate_cycle_list(records_value, "records", missing_fields, invalid_fields)
    required_refs = (
        "run_ref",
        "manifest_ref",
        "artifact_ref",
        "cycle_ref",
        "program_ref",
        "model_version",
        "pricing_version",
        "trace_evidence_ref",
        "manifest_evidence_ref",
        "billing_evidence_ref",
    )
    provider_total = Decimal(0)
    tco_total = Decimal(0)
    for index, record in enumerate(records):
        path = f"records[{index}]"
        for field in required_refs:
            value = record.get(field)
            if value is None:
                missing_fields.append(f"{path}.{field}")
            elif not isinstance(value, str) or not value.strip():
                invalid_fields.append(f"{path}.{field}")

        tokens = record.get("tokens")
        if tokens is None:
            missing_fields.append(f"{path}.tokens")
        elif not _is_nonnegative_int(tokens):
            invalid_fields.append(f"{path}.tokens")

        confidence = record.get("confidence")
        if "confidence" not in record:
            missing_fields.append(f"{path}.confidence")
        elif not _valid_confidence(confidence):
            invalid_fields.append(f"{path}.confidence")

        cost_values: dict[str, Decimal] = {}
        for field in ("cost", "provider_cost", "total_tco_cost"):
            value = _decimal(record.get(field))
            if field not in record:
                missing_fields.append(f"{path}.{field}")
            elif value is None or value < 0:
                invalid_fields.append(f"{path}.{field}")
            else:
                cost_values[field] = value
        if len(cost_values) == 3:
            provider_total += cost_values["provider_cost"]
            tco_total += cost_values["total_tco_cost"]
            if cost_values["cost"] != cost_values["provider_cost"]:
                invalid_fields.append(f"{path}.cost")
            if cost_values["total_tco_cost"] < cost_values["provider_cost"]:
                invalid_fields.append(f"{path}.total_tco_cost")

    denominators_value = payload.get("denominators")
    denominators = denominators_value if isinstance(denominators_value, dict) else {}
    if denominators_value is None:
        missing_fields.append("denominators")
    elif not isinstance(denominators_value, dict):
        invalid_fields.append("denominators")
    denominator_values: dict[str, Decimal] = {}
    for field in ("certified_artifact_count", "completed_cycle_count"):
        value = denominators.get(field)
        if value is None:
            missing_fields.append(f"denominators.{field}")
        elif not _is_positive_int(value):
            invalid_fields.append(f"denominators.{field}")
        else:
            denominator_values[field] = Decimal(value)
    active_program_months = _decimal(denominators.get("active_program_months"))
    if "active_program_months" not in denominators:
        missing_fields.append("denominators.active_program_months")
    elif active_program_months is None or active_program_months <= 0:
        invalid_fields.append("denominators.active_program_months")
    else:
        denominator_values["active_program_months"] = active_program_months

    unattributed_cost = _cost_field(
        payload, "unattributed_cost", missing_fields, invalid_fields
    )
    uncertain_cost = _cost_field(
        payload, "uncertain_cost", missing_fields, invalid_fields
    )
    if unattributed_cost is None:
        unattributed_cost = Decimal(0)
    if uncertain_cost is None:
        uncertain_cost = Decimal(0)

    if (
        records
        and not any(field.startswith("records[") for field in invalid_fields)
        and tco_total < provider_total + unattributed_cost + uncertain_cost
    ):
        invalid_fields.append("total_tco_cost_reconciliation")

    failed_gates: list[str] = []
    measurements: dict[str, Any] = {
        "provider_cost": _json_number(provider_total),
        "total_tco_cost": _json_number(tco_total),
        "cost_per_certified_artifact": None,
        "cost_per_completed_cycle": None,
        "cost_per_active_program_month": None,
        "unattributed_cost": _json_number(unattributed_cost),
        "uncertain_cost": _json_number(uncertain_cost),
        "unattributed_and_uncertain_cost": _json_number(
            unattributed_cost + uncertain_cost
        ),
        "record_count": len(records),
    }
    if len(denominator_values) == 3 and not missing_fields and not invalid_fields:
        per_artifact = tco_total / denominator_values["certified_artifact_count"]
        per_cycle = tco_total / denominator_values["completed_cycle_count"]
        per_program_month = tco_total / denominator_values["active_program_months"]
        measurements["cost_per_certified_artifact"] = _json_number(per_artifact)
        measurements["cost_per_completed_cycle"] = _json_number(per_cycle)
        measurements["cost_per_active_program_month"] = _json_number(
            per_program_month
        )

    if decision == "RELO-D6d":
        classification = "measurement_ready"
    else:
        thresholds = _private_budget_thresholds(
            payload, missing_fields, invalid_fields
        )
        if not missing_fields and not invalid_fields:
            comparisons = (
                (
                    "cost_per_certified_artifact_within_private_budget",
                    tco_total / denominator_values["certified_artifact_count"],
                    thresholds.get("max_cost_per_certified_artifact"),
                ),
                (
                    "cost_per_completed_cycle_within_private_budget",
                    tco_total / denominator_values["completed_cycle_count"],
                    thresholds.get("max_cost_per_completed_cycle"),
                ),
                (
                    "cost_per_active_program_month_within_private_budget",
                    tco_total / denominator_values["active_program_months"],
                    thresholds.get("max_cost_per_active_program_month"),
                ),
                (
                    "unattributed_and_uncertain_cost_within_private_budget",
                    unattributed_cost + uncertain_cost,
                    thresholds.get("max_unattributed_and_uncertain_cost"),
                ),
            )
            for gate, measured, threshold in comparisons:
                if threshold is None or measured > threshold:
                    failed_gates.append(gate)
        classification = "owner_review" if not failed_gates else "not_qualified"
    return classification, measurements, tuple(failed_gates)


def _private_budget_thresholds(
    payload: dict[str, Any],
    missing_fields: list[str],
    invalid_fields: list[str],
) -> dict[str, Decimal]:
    value = payload.get("private_budget_thresholds")
    if value is None:
        missing_fields.append("private_budget_thresholds")
        return {}
    if not isinstance(value, dict):
        invalid_fields.append("private_budget_thresholds")
        return {}
    result: dict[str, Decimal] = {}
    for field in (
        "max_cost_per_certified_artifact",
        "max_cost_per_completed_cycle",
        "max_cost_per_active_program_month",
        "max_unattributed_and_uncertain_cost",
    ):
        raw = value.get(field)
        path = f"private_budget_thresholds.{field}"
        if raw is None:
            missing_fields.append(path)
            continue
        threshold = _decimal(raw)
        if threshold is None or threshold < 0:
            invalid_fields.append(path)
        else:
            result[field] = threshold
    return result


def _cost_field(
    payload: dict[str, Any],
    field: str,
    missing_fields: list[str],
    invalid_fields: list[str],
) -> Decimal | None:
    if field not in payload:
        missing_fields.append(field)
        return None
    value = _decimal(payload.get(field))
    if value is None or value < 0:
        invalid_fields.append(field)
        return None
    return value


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _is_blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_confidence(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    numeric = _decimal(value)
    return numeric is not None and Decimal(0) <= numeric <= Decimal(1)


def _json_number(value: Decimal | float | int | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value == int(value) else float(value)
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _result(
    *,
    decision: str | None,
    classification: str,
    missing_fields: list[str] | tuple[str, ...],
    invalid_fields: list[str] | tuple[str, ...],
    failed_gates: tuple[str, ...],
    measurements: dict[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "classification": classification,
        "input_classification": "synthetic_only",
        "missing_fields": _ordered_unique(list(missing_fields)),
        "invalid_fields": _ordered_unique(list(invalid_fields)),
        "failed_gates": list(failed_gates),
        "measurements": measurements,
        "limitations": limitations,
    }


def _limitations() -> list[str]:
    return [
        "Synthetic input verification does not establish production or live-provider evidence.",
        "The verifier does not authenticate evidence, provider billing, pricing, tokens, or traces.",
        "The verifier does not infer owner approval, budget ratification, or certification.",
        "The verifier performs no provider access and makes no mutation.",
    ]


def verify_file(path: Path) -> dict[str, Any]:
    """Read and verify exactly one explicit JSON file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _result(
            decision=None,
            classification="not_qualified",
            missing_fields=(),
            invalid_fields=("input_path",),
            failed_gates=(),
            measurements={},
            limitations=_limitations(),
        )
    return verify_packet(payload)


def _require_input_argument(
    parser: argparse.ArgumentParser,
    option_path: Path | None,
    positional_path: Path | None,
) -> Path:
    if option_path is not None and positional_path is not None:
        parser.error("supply the JSON input path once, as --input or a positional path")
    selected = option_path or positional_path
    if selected is None:
        parser.error("an explicit JSON input path is required")
    return selected


def main(argv: list[str] | None = None) -> int:
    """Print one JSON qualification result without mutating the input."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_path", nargs="?", type=Path)
    parser.add_argument(
        "--input",
        dest="input_option",
        type=Path,
        help="Explicit synthetic qualification packet JSON path.",
    )
    args = parser.parse_args(argv)
    path = _require_input_argument(parser, args.input_option, args.input_path)
    json.dump(verify_file(path), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
