"""RELO-W6 qualification evidence contracts.

This module validates explicitly supplied evidence.  It does not collect live
data, authenticate owners, execute providers, or turn a mechanically complete
packet into deployment qualification.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from src.core.study_evaluator import (
    WeightedSavingsObservation,
    calculate_matched_cycle_savings,
)


SCHEMA_VERSION = "1"
W6_CARD_IDS = tuple(f"RELO-W6.{number}" for number in range(1, 13))
REQUIRED_QUALITY_DIMENSIONS = (
    "factual_correctness",
    "evidence_traceability",
    "risk_prioritization",
    "hidden_risk_usefulness",
    "dependency_correctness",
    "decision_action_specificity",
    "internal_consistency",
    "audience_fit",
    "concision",
    "human_correction",
)
LEARNING_PATHS = (
    "src/ai/edit_learner.py",
    "src/ai/learning_distiller.py",
    "src/commands/confirm_stages/learning_distiller.py",
)


@dataclass(frozen=True, slots=True)
class W6VerificationResult:
    card_id: str | None
    classification: str
    measurements: dict[str, Any]
    missing_fields: tuple[str, ...]
    invalid_fields: tuple[str, ...]
    failed_gates: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


_Validator = Callable[[dict[str, Any], list[str], list[str]], tuple[dict[str, Any], list[str]]]


def build_w6_evidence_template(card_id: str) -> dict[str, Any]:
    """Return a value-free enrollment template for one W6 card."""
    if card_id not in W6_CARD_IDS:
        raise ValueError(f"Unsupported W6 card {card_id!r}.")
    fields_by_card: dict[str, dict[str, Any]] = {
        "RELO-W6.1": {
            "protocol": {
                "frozen": False,
                "different_annotator_model_family": False,
                "human_adjudicates_all_disagreements": False,
                "disclosure": "",
                "human_audit_minimum": None,
            },
            "holdout": {
                "frozen": False,
                "dataset_hash": "",
                "post_relo_multi_source": False,
                "strata": [],
            },
        },
        "RELO-W6.2": {
            "protocol": {
                "frozen": False,
                "assignment": "alternating",
                "bootstrap_seed": None,
                "bootstrap_resamples": None,
                "applicable_workflow_classes": [],
            },
            "cycles": [],
        },
        "RELO-W6.3": {
            "window_start": None,
            "window_end": None,
            "eligible_routine_tasks": None,
            "completed_routine_tasks": None,
            "eligible_tier_decisions": None,
            "telemetry_records": None,
            "deterministic_local_tasks": None,
            "cache_reuse_tasks": None,
            "remote_provider_tasks": None,
            "certified_routing_frontier_calls": None,
            "ratified_local_coverage_floor": None,
            "ratified_routine_completion_floor": None,
        },
        "RELO-W6.4": {
            "protocol": {
                "frozen": False,
                "randomized_blind_order": False,
                "ratified_minimum_items": None,
                "noninferiority_margin": None,
            },
            "comparisons": [],
        },
        "RELO-W6.5": {
            "replacement_ready": False,
            "shadow_green": False,
            "tests_green": False,
            "candidates": [],
        },
        "RELO-W6.6": {
            "closed_cards": [],
            "canonical_mappings": [],
            "temporary_binding_only_refs": [],
        },
        "RELO-W6.7": {
            "transitions": [],
        },
        "RELO-W6.8": {
            "proposals": [],
        },
        "RELO-W6.9": {
            "corpus": {
                "frozen": False,
                "dataset_hash": "",
                "snapshot_hash": "",
                "rebuild_hash": "",
                "binding_precision_floor": None,
                "material_recall_floor": None,
                "overall_recall_floor": None,
            },
            "items": [],
            "continuity_runs": [],
        },
        "RELO-W6.10": {
            "profile": {},
            "ratified_budgets": {},
            "measurements": {},
            "restore": {},
            "onboarding": {},
        },
        "RELO-W6.11": {
            "paths": [],
        },
        "RELO-W6.12": {
            "requested_terminal_state": None,
            "evidence": {},
            "halt_reason": "",
            "profile_id": "",
        },
    }
    fields = fields_by_card[card_id]
    return {
        "schema_version": SCHEMA_VERSION,
        "card_id": card_id,
        "packet_id": "",
        "captured_at": None,
        "owner_disposition": {
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
            "approval_ref": None,
        },
        "artifacts": [],
        **fields,
    }


def verify_w6_packet(
    payload: object,
    *,
    repo_root: Path | None = None,
) -> W6VerificationResult:
    """Validate one W6 packet and report mechanics, never certification."""
    missing: list[str] = []
    invalid: list[str] = []
    failed: list[str] = []
    measurements: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return _result(None, "not_qualified", {}, ["input"], [], [], repo_root)

    for field in ("schema_version", "card_id", "packet_id", "captured_at", "owner_disposition", "artifacts"):
        if field not in payload:
            missing.append(field)
    if payload.get("schema_version") != SCHEMA_VERSION:
        invalid.append("schema_version")
    card_id = payload.get("card_id")
    if not isinstance(card_id, str) or card_id not in W6_CARD_IDS:
        invalid.append("card_id")
        return _result(None, "not_qualified", {}, missing, invalid, failed, repo_root)
    if _is_blank(payload.get("packet_id")):
        invalid.append("packet_id")
    captured_at = _aware_datetime(payload.get("captured_at"))
    if captured_at is None:
        invalid.append("captured_at")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        invalid.append("artifacts")
    else:
        _validate_artifacts(artifacts, repo_root=repo_root, missing=missing, invalid=invalid)
    owner_status = _validate_owner(payload.get("owner_disposition"), missing, invalid)

    validator = _VALIDATORS[card_id]
    card_measurements, card_failures = validator(payload, missing, invalid)
    measurements.update(card_measurements)
    failed.extend(card_failures)
    if repo_root is not None:
        _validate_repository_links(
            card_id,
            payload,
            repo_root=repo_root,
            missing=missing,
            invalid=invalid,
            failed=failed,
        )

    if card_id == "RELO-W6.12":
        classification = _classify_terminal(payload, missing=missing, invalid=invalid, failed=failed)
    elif missing or invalid or failed or captured_at is None:
        classification = "not_qualified"
    elif owner_status == "approved":
        classification = "owner_review"
    else:
        classification = "owner_review"
    return _result(card_id, classification, measurements, missing, invalid, failed, repo_root)


def build_learning_disposition_inventory(repo_root: Path) -> dict[str, Any]:
    """Return the current typed disposition inventory for legacy learning paths."""
    paths = []
    for relative in LEARNING_PATHS:
        disposition = {
            "path": relative,
            "exists": (repo_root / relative).is_file(),
            "disposition": (
                "retained_forensic"
                if relative.endswith("edit_learner.py")
                else "replaced_offline_proposal"
            ),
            "runtime_mutation_allowed": False,
            "runtime_prompt_consumer": False,
            "replacement": "src/core/governed_feedback_lifecycle.py",
            "evidence_refs": (
                "tests/unit/test_learning_paths_characterization.py",
                "governance/decisions/0020-relo-scope-and-execution-ratification.md",
            ),
        }
        paths.append(disposition)
    return {"schema_version": SCHEMA_VERSION, "paths": paths}


def build_w6_routing_evidence(
    records: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    eligible_routine_tasks: int,
    completed_routine_tasks: int,
    expected_tier_decisions: int,
    ratified_local_coverage_floor: float,
    ratified_routine_completion_floor: float,
) -> dict[str, Any]:
    """Project existing tier measurements into a W6.3 enrollment payload.

    This read-only reducer neither loads providers nor classifies absent rows as
    successful.  The caller supplies expected task/decision counts so telemetry
    gaps remain measurable rather than being inferred from the surviving rows.
    """
    if window_start.tzinfo is None or window_end.tzinfo is None or window_end <= window_start:
        raise ValueError("window_start and window_end must be timezone-aware and ordered.")
    for field, value in (
        ("eligible_routine_tasks", eligible_routine_tasks),
        ("completed_routine_tasks", completed_routine_tasks),
        ("expected_tier_decisions", expected_tier_decisions),
    ):
        if not _nonnegative_int(value):
            raise ValueError(f"{field} must be a non-negative integer.")
    if completed_routine_tasks > eligible_routine_tasks:
        raise ValueError("completed_routine_tasks cannot exceed eligible_routine_tasks.")
    if not 0 < ratified_local_coverage_floor <= 1:
        raise ValueError("ratified_local_coverage_floor must be greater than 0 and at most 1.")
    if not 0 < ratified_routine_completion_floor <= 1:
        raise ValueError("ratified_routine_completion_floor must be greater than 0 and at most 1.")

    selected: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("Routing measurement rows must be mappings.")
        recorded_at = _aware_datetime(row.get("recorded_at"))
        if recorded_at is None:
            raise ValueError("Routing measurement recorded_at must be timezone-aware.")
        if window_start <= recorded_at <= window_end:
            selected.append(row)
    deterministic = sum(
        1
        for row in selected
        if row.get("chosen_tier") in {"deterministic", "local", "local_semantic"}
        and row.get("cache_hit") is not True
    )
    cache = sum(1 for row in selected if row.get("cache_hit") is True or row.get("chosen_tier") == "cache")
    remote = sum(
        1
        for row in selected
        if row.get("frontier_called") is True
        or row.get("chosen_tier") in {"frontier", "remote"}
    )
    frontier_routing = sum(
        1
        for row in selected
        if row.get("frontier_called") is True
        and row.get("execution_class") in {"routing", "classification"}
    )
    reconciled = deterministic + cache + remote
    return {
        "window_start": window_start.astimezone(timezone.utc).isoformat(),
        "window_end": window_end.astimezone(timezone.utc).isoformat(),
        "eligible_routine_tasks": eligible_routine_tasks,
        "completed_routine_tasks": completed_routine_tasks,
        "eligible_tier_decisions": expected_tier_decisions,
        "telemetry_records": len(selected),
        "deterministic_local_tasks": deterministic,
        "cache_reuse_tasks": cache,
        "remote_provider_tasks": remote,
        "certified_routing_frontier_calls": frontier_routing,
        "ratified_local_coverage_floor": ratified_local_coverage_floor,
        "ratified_routine_completion_floor": ratified_routine_completion_floor,
        "unclassified_selected_records": len(selected) - reconciled,
        "expected_unobserved_decisions": max(0, expected_tier_decisions - len(selected)),
    }


def terminal_state_from_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate exact terminal semantics from an explicitly supplied packet."""
    result = verify_w6_packet(payload)
    return {
        "terminal_state": (
            payload.get("requested_terminal_state")
            if result.classification in {"PROPOSED", "CONTAINED", "ADVISORY_OPERATIONAL", "RELO_PROFILE_COMPLETE", "HALTED"}
            else None
        ),
        "assessment": result.classification,
        "measurement": result.measurements,
        "failed_gates": list(result.failed_gates),
        "missing_fields": list(result.missing_fields),
        "invalid_fields": list(result.invalid_fields),
        "limitations": list(result.limitations),
    }


def _validate_w61(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    protocol = _mapping(payload, "protocol", missing, invalid)
    holdout = _mapping(payload, "holdout", missing, invalid)
    for field in (
        "frozen",
        "different_annotator_model_family",
        "human_adjudicates_all_disagreements",
        "human_audit_minimum",
        "disclosure",
    ):
        if field not in protocol:
            missing.append(f"protocol.{field}")
    if protocol.get("frozen") is not True:
        failed = ["annotation_protocol_not_frozen"]
    else:
        failed = []
    if protocol.get("different_annotator_model_family") is not True:
        failed.append("annotator_model_family_independence_not_established")
    if protocol.get("human_adjudicates_all_disagreements") is not True:
        failed.append("human_adjudication_not_complete")
    disclosure = str(protocol.get("disclosure") or "").strip().lower()
    if "model-assisted annotation" not in disclosure or "single human adjudicator" not in disclosure:
        failed.append("mandatory_model_assisted_disclosure_missing")
    minimum_audit = protocol.get("human_audit_minimum")
    if not _positive_int(minimum_audit):
        invalid.append("protocol.human_audit_minimum")

    for field in ("frozen", "dataset_hash", "post_relo_multi_source", "strata"):
        if field not in holdout:
            missing.append(f"holdout.{field}")
    if holdout.get("frozen") is not True:
        failed.append("holdout_not_frozen")
    if _is_blank(holdout.get("dataset_hash")):
        invalid.append("holdout.dataset_hash")
    if holdout.get("post_relo_multi_source") is not True:
        failed.append("post_relo_multi_source_evidence_missing")
    strata = holdout.get("strata")
    strata_results = []
    domains: set[str] = set()
    if not isinstance(strata, list) or not strata:
        invalid.append("holdout.strata")
    else:
        stratum_ids: set[str] = set()
        for index, row in enumerate(strata):
            prefix = f"holdout.strata[{index}]"
            if not isinstance(row, dict):
                invalid.append(prefix)
                continue
            stratum_id = row.get("stratum_id")
            domain = row.get("domain")
            if _is_blank(stratum_id) or not isinstance(stratum_id, str) or stratum_id in stratum_ids:
                invalid.append(f"{prefix}.stratum_id")
            else:
                stratum_ids.add(stratum_id)
            if domain in {"risk", "dependency"}:
                domains.add(str(domain))
            else:
                invalid.append(f"{prefix}.domain")
            sample_count = row.get("sample_count")
            divergence_count = row.get("divergence_count")
            kappa = row.get("human_model_kappa")
            audit_count = row.get("human_audit_sample_count")
            audit_divergence = row.get("human_audit_material_divergence_count")
            if not _nonnegative_int(sample_count) or not _nonnegative_int(divergence_count):
                invalid.append(f"{prefix}.sample_count_or_divergence_count")
                continue
            assert isinstance(sample_count, int) and not isinstance(sample_count, bool)
            assert isinstance(divergence_count, int) and not isinstance(divergence_count, bool)
            sample_total = int(sample_count)
            divergence_total = int(divergence_count)
            if divergence_total > sample_total:
                invalid.append(f"{prefix}.divergence_count")
            required = 120 if divergence_total >= 2 else 60
            if sample_total < required:
                failed.append(f"{stratum_id}:sample_floor")
            if divergence_total >= 2 and row.get("reverified_after_pause") is not True:
                failed.append(f"{stratum_id}:pause_reverification")
            if not isinstance(kappa, (int, float)) or isinstance(kappa, bool) or not math.isfinite(float(kappa)):
                invalid.append(f"{prefix}.human_model_kappa")
            elif float(kappa) < 0.70:
                failed.append(f"{stratum_id}:kappa_floor")
            if (
                not _positive_int(minimum_audit)
                or not _nonnegative_int(audit_count)
                or not isinstance(minimum_audit, int)
                or not isinstance(audit_count, int)
                or isinstance(minimum_audit, bool)
                or isinstance(audit_count, bool)
                or audit_count < minimum_audit
            ):
                failed.append(f"{stratum_id}:human_audit_sample")
            if (
                not _nonnegative_int(audit_divergence)
                or not isinstance(audit_divergence, int)
                or isinstance(audit_divergence, bool)
                or audit_divergence != 0
            ):
                failed.append(f"{stratum_id}:human_audit_material_divergence")
            sources = row.get("source_families")
            if (
                not isinstance(sources, list)
                or not all(isinstance(item, str) and item.strip() for item in sources)
                or len(set(sources)) < 2
                or "ado" not in set(sources)
            ):
                failed.append(f"{stratum_id}:source_diversity")
            strata_results.append(
                {
                    "stratum_id": stratum_id,
                    "required_samples": required,
                    "sample_count": sample_total,
                    "divergence_count": divergence_total,
                }
            )
    if domains != {"risk", "dependency"}:
        failed.append("risk_and_dependency_strata_required")
    return {"strata": strata_results, "disclosure": disclosure}, failed


def _validate_w62(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    protocol = _mapping(payload, "protocol", missing, invalid)
    cycles_value = payload.get("cycles")
    for field in (
        "frozen",
        "assignment",
        "bootstrap_seed",
        "bootstrap_resamples",
        "applicable_workflow_classes",
    ):
        if field not in protocol:
            missing.append(f"protocol.{field}")
    if protocol.get("frozen") is not True:
        failed = ["study_protocol_not_frozen"]
    else:
        failed = []
    if protocol.get("assignment") not in {"alternating", "stepped_wedge"}:
        invalid.append("protocol.assignment")
    seed = protocol.get("bootstrap_seed")
    resamples = protocol.get("bootstrap_resamples")
    if not _nonnegative_int(seed):
        invalid.append("protocol.bootstrap_seed")
    if not _positive_int(resamples) or not isinstance(resamples, int) or isinstance(resamples, bool) or resamples < 1000:
        invalid.append("protocol.bootstrap_resamples")
    workflow_classes = protocol.get("applicable_workflow_classes")
    if (
        not isinstance(workflow_classes, list)
        or not workflow_classes
        or not all(isinstance(item, str) and item.strip() for item in workflow_classes)
    ):
        invalid.append("protocol.applicable_workflow_classes")
        required_classes: set[str] = set()
    else:
        required_classes = set(workflow_classes)

    if not isinstance(cycles_value, list):
        invalid.append("cycles")
        return {}, failed
    valid_cycles: list[tuple[datetime, WeightedSavingsObservation]] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(cycles_value):
        prefix = f"cycles[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        cycle_id = row.get("cycle_id")
        if _is_blank(cycle_id) or cycle_id in seen_ids:
            invalid.append(f"{prefix}.cycle_id")
            continue
        seen_ids.add(str(cycle_id))
        observed_at = _aware_datetime(row.get("observed_at"))
        if observed_at is None:
            invalid.append(f"{prefix}.observed_at")
            continue
        manual = _finite_number(row.get("manual_minutes"))
        assisted = _finite_number(row.get("assisted_minutes"))
        raw_saved = row.get("quality_adjusted_minutes_saved")
        saved = _finite_number(raw_saved)
        if manual is None or manual <= 0 or assisted is None or assisted < 0:
            invalid.append(f"{prefix}.minutes")
            continue
        if saved is None and raw_saved is None:
            saved = manual - assisted
        elif saved is None:
            invalid.append(f"{prefix}.quality_adjusted_minutes_saved")
            continue
        elif saved > manual:
            invalid.append(f"{prefix}.quality_adjusted_minutes_saved")
            continue
        if row.get("quality_passed") is not True:
            saved = 0.0
            failed.append(f"{cycle_id}:quality_gate")
        for metric in (
            "completion_rate",
            "displacement_rate",
            "correction_count",
            "exception_count",
            "downstream_error_count",
        ):
            value = row.get(metric)
            if value is None:
                missing.append(f"{prefix}.{metric}")
            elif metric.endswith("_rate") and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                invalid.append(f"{prefix}.{metric}")
            elif metric.endswith("_count") and not _nonnegative_int(value):
                invalid.append(f"{prefix}.{metric}")
        row_classes = row.get("workflow_classes")
        if not isinstance(row_classes, list):
            invalid.append(f"{prefix}.workflow_classes")
        elif set(row_classes) != required_classes:
            failed.append(f"{cycle_id}:workflow_class_coverage")
        valid_cycles.append(
            (observed_at, WeightedSavingsObservation(manual, saved))
        )

    ordered = sorted(valid_cycles)
    if len({int(item[0].timestamp()) for item in ordered}) != len(ordered):
        failed.append("duplicate_cycle_times")
    if any((later[0] - earlier[0]).days < 7 for earlier, later in zip(ordered, ordered[1:])):
        failed.append("overlapping_weekly_cycles")
    elapsed = (ordered[-1][0] - ordered[0][0]).days if ordered else 0
    if len(ordered) < 8:
        failed.append("at_least_8_cycles")
    if elapsed < 49:
        failed.append("at_least_49_days")
    bootstrap_valid = (
        _positive_int(resamples)
        and isinstance(resamples, int)
        and not isinstance(resamples, bool)
        and resamples >= 1000
        and _nonnegative_int(seed)
        and isinstance(seed, int)
        and not isinstance(seed, bool)
    )
    shared_resamples = 1
    shared_seed = 0
    if bootstrap_valid:
        assert isinstance(resamples, int) and not isinstance(resamples, bool)
        assert isinstance(seed, int) and not isinstance(seed, bool)
        shared_resamples = resamples
        shared_seed = seed
    shared_savings = (
        calculate_matched_cycle_savings(
            tuple((observation,) for _, observation in ordered),
            resamples=shared_resamples,
            seed=shared_seed,
        )
        if ordered
        else None
    )
    lower_bound: float | None = None
    if len(ordered) >= 2 and bootstrap_valid and shared_savings is not None:
        lower_bound = shared_savings.lcb_95_saved_fraction
        assert lower_bound is not None
        if lower_bound <= 0.80:
            failed.append("lcb_must_exceed_0.80")
    return {
        "cycle_count": len(ordered),
        "elapsed_days": elapsed,
        "quality_adjusted_saved_fraction": (
            shared_savings.saved_fraction if shared_savings is not None else None
        ),
        "bootstrap_95_lower_bound": lower_bound,
    }, failed


def _validate_w63(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    failed: list[str] = []
    integer_fields = (
        "eligible_routine_tasks",
        "completed_routine_tasks",
        "eligible_tier_decisions",
        "telemetry_records",
        "deterministic_local_tasks",
        "cache_reuse_tasks",
        "remote_provider_tasks",
        "certified_routing_frontier_calls",
    )
    values: dict[str, int] = {}
    for field in integer_fields:
        value = payload.get(field)
        if value is None:
            missing.append(field)
        elif not _nonnegative_int(value):
            invalid.append(field)
        else:
            values[field] = int(value)
    ratios: dict[str, float] = {}
    for field in ("ratified_local_coverage_floor", "ratified_routine_completion_floor"):
        value = _finite_number(payload.get(field))
        if value is None:
            missing.append(field)
        elif not 0.0 < value <= 1.0:
            invalid.append(field)
        else:
            ratios[field] = value
    start = _aware_datetime(payload.get("window_start"))
    end = _aware_datetime(payload.get("window_end"))
    if start is None or end is None or end <= start:
        invalid.append("measurement_window")
    if set(values) != set(integer_fields):
        return {}, failed

    tier_total = values["eligible_tier_decisions"]
    tasks_total = values["eligible_routine_tasks"]
    if values["telemetry_records"] > tier_total:
        invalid.append("telemetry_records")
    if tier_total != (
        values["deterministic_local_tasks"]
        + values["cache_reuse_tasks"]
        + values["remote_provider_tasks"]
    ):
        failed.append("routing_population_does_not_reconcile")
    if values["completed_routine_tasks"] > tasks_total:
        invalid.append("completed_routine_tasks")
    avoidance = (
        (tier_total - values["remote_provider_tasks"]) / tier_total
        if tier_total
        else None
    )
    local_coverage = values["deterministic_local_tasks"] / tier_total if tier_total else None
    telemetry = values["telemetry_records"] / tier_total if tier_total else None
    completion = values["completed_routine_tasks"] / tasks_total if tasks_total else None
    if avoidance is None or avoidance < 0.60:
        failed.append("remote_provider_avoidance_floor")
    if values["certified_routing_frontier_calls"] != 0:
        failed.append("certified_routing_frontier_calls")
    if local_coverage is None or local_coverage < ratios.get("ratified_local_coverage_floor", math.inf):
        failed.append("deterministic_local_coverage_floor")
    if completion is None or completion < ratios.get("ratified_routine_completion_floor", math.inf):
        failed.append("routine_completion_floor")
    if telemetry is None or telemetry < 0.99:
        failed.append("tier_telemetry_coverage_floor")
    return {
        "remote_provider_avoidance": avoidance,
        "deterministic_local_coverage_excluding_cache": local_coverage,
        "routine_completion_rate": completion,
        "telemetry_coverage": telemetry,
        "certified_routing_frontier_calls": values["certified_routing_frontier_calls"],
    }, failed


def _validate_w64(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    protocol = _mapping(payload, "protocol", missing, invalid)
    comparisons = payload.get("comparisons")
    failed: list[str] = []
    if protocol.get("frozen") is not True:
        failed.append("blind_protocol_not_frozen")
    if protocol.get("randomized_blind_order") is not True:
        failed.append("blind_order_not_randomized")
    minimum = protocol.get("ratified_minimum_items")
    margin = _finite_number(protocol.get("noninferiority_margin"))
    if not _positive_int(minimum):
        invalid.append("protocol.ratified_minimum_items")
    if margin is None or margin < 0:
        invalid.append("protocol.noninferiority_margin")
    if not isinstance(comparisons, list) or not comparisons:
        invalid.append("comparisons")
        return {}, failed
    item_ids: set[str] = set()
    material_regressions = 0
    dimensions_checked = 0
    for index, row in enumerate(comparisons):
        prefix = f"comparisons[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        item_id = row.get("item_id")
        if _is_blank(item_id) or item_id in item_ids:
            invalid.append(f"{prefix}.item_id")
        else:
            item_ids.add(str(item_id))
        if "critical_error" not in row:
            missing.append(f"{prefix}.critical_error")
        elif not isinstance(row["critical_error"], bool):
            invalid.append(f"{prefix}.critical_error")
        elif row["critical_error"] is True:
            material_regressions += 1
        scores = row.get("rubric_scores")
        if not isinstance(scores, dict) or set(scores) != set(REQUIRED_QUALITY_DIMENSIONS):
            invalid.append(f"{prefix}.rubric_scores")
            continue
        for dimension, pair in scores.items():
            if not isinstance(pair, dict):
                invalid.append(f"{prefix}.rubric_scores.{dimension}")
                continue
            baseline = _finite_number(pair.get("baseline"))
            candidate = _finite_number(pair.get("candidate"))
            if baseline is None or candidate is None or not 0 <= baseline <= 5 or not 0 <= candidate <= 5:
                invalid.append(f"{prefix}.rubric_scores.{dimension}")
                continue
            dimensions_checked += 1
            if dimension not in {"concision", "human_correction"} and margin is not None and candidate < baseline - margin:
                material_regressions += 1
    minimum_items = minimum if isinstance(minimum, int) and not isinstance(minimum, bool) and minimum > 0 else 0
    if minimum_items and len(item_ids) < minimum_items:
        failed.append("blind_comparison_sample_floor")
    if material_regressions:
        failed.append("material_quality_regression")
    return {
        "item_count": len(item_ids),
        "dimensions_checked": dimensions_checked,
        "material_regression_count": material_regressions,
    }, failed


def _validate_w65(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    failed: list[str] = []
    for field in ("replacement_ready", "shadow_green", "tests_green"):
        if payload.get(field) is not True:
            failed.append(field)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        invalid.append("candidates")
        return {}, failed
    total_loc = 0
    paths: set[str] = set()
    for index, row in enumerate(candidates):
        prefix = f"candidates[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        path = row.get("path")
        if _is_blank(path) or path in paths:
            invalid.append(f"{prefix}.path")
        else:
            paths.add(str(path))
        for field in ("replacement_path", "caller_inventory_ref", "shadow_ref", "test_ref", "rollback_window"):
            if _is_blank(row.get(field)):
                missing.append(f"{prefix}.{field}")
        loc = row.get("production_loc")
        if not _positive_int(loc):
            invalid.append(f"{prefix}.production_loc")
        else:
            assert isinstance(loc, int) and not isinstance(loc, bool)
            total_loc += int(loc)
        if row.get("dependency_gate") != "qualified":
            failed.append(f"{path}:dependency_gate")
    if total_loc > 5000:
        failed.append("bounded_deletion_ceiling")
    return {"candidate_count": len(paths), "production_loc_target": total_loc}, failed


def _validate_w66(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    mappings = payload.get("canonical_mappings")
    closed = payload.get("closed_cards")
    failed: list[str] = []
    temporary = payload.get("temporary_binding_only_refs")
    if not isinstance(temporary, list) or any(temporary):
        failed.append("binding_decision_only_in_temporary_spec")
    if not isinstance(closed, list) or not isinstance(mappings, list) or not closed:
        invalid.append("closed_cards_or_canonical_mappings")
        return {}, failed
    card_ids: set[str] = set()
    mapped_ids: set[str] = set()
    for index, row in enumerate(mappings):
        prefix = f"canonical_mappings[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        card_id = row.get("card_id")
        if _is_blank(card_id) or card_id in mapped_ids:
            invalid.append(f"{prefix}.card_id")
        else:
            mapped_ids.add(str(card_id))
        target = row.get("canonical_target")
        kind = row.get("permanent_surface")
        if _is_blank(target):
            missing.append(f"{prefix}.canonical_target")
        if kind not in {"spec_invariant", "governance_decision", "budget", "test", "runbook", "evidence_ledger"}:
            invalid.append(f"{prefix}.permanent_surface")
        if _is_blank(row.get("evidence_ref")):
            missing.append(f"{prefix}.evidence_ref")
    for index, card_id in enumerate(closed):
        if not isinstance(card_id, str) or not card_id or card_id in card_ids:
            invalid.append(f"closed_cards[{index}]")
        else:
            card_ids.add(card_id)
    if card_ids - mapped_ids:
        failed.append("closed_cards_without_canonical_mapping")
    if mapped_ids - card_ids:
        failed.append("orphan_canonical_mapping")
    return {"closed_card_count": len(card_ids), "mapped_card_count": len(mapped_ids)}, failed


def _validate_w67(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    transitions = payload.get("transitions")
    if not isinstance(transitions, list) or not transitions:
        invalid.append("transitions")
        return {}, []
    failed: list[str] = []
    keys: set[tuple[str, str]] = set()
    for index, row in enumerate(transitions):
        prefix = f"transitions[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        proposal_class = row.get("proposal_class")
        action_class = row.get("action_class")
        if _is_blank(proposal_class) or _is_blank(action_class):
            invalid.append(f"{prefix}.class")
            continue
        key = (str(proposal_class), str(action_class))
        if key in keys:
            invalid.append(f"{prefix}.duplicate_class")
        keys.add(key)
        current = row.get("from_level")
        target = row.get("to_level")
        if current not in {"l0", "l1", "l2", "l3", "l4"} or target not in {"l0", "l1", "l2", "l3", "l4"}:
            invalid.append(f"{prefix}.level")
        for field in ("evidence_window_ref", "owner", "owner_approval_ref", "rollback_ref"):
            if _is_blank(row.get(field)):
                missing.append(f"{prefix}.{field}")
        if row.get("quality_gates_passed") is not True or row.get("no_material_regression") is not True:
            failed.append(f"{prefix}:maturity_evidence")
        if target in {"l3", "l4"} and row.get("independent_review_ref") in {None, ""}:
            failed.append(f"{prefix}:independent_review")
    return {"transition_count": len(keys)}, failed


def _validate_w68(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    proposals = payload.get("proposals")
    if not isinstance(proposals, list) or not proposals:
        invalid.append("proposals")
        return {}, []
    failed: list[str] = []
    proposal_ids: set[str] = set()
    promoted = 0
    for index, row in enumerate(proposals):
        prefix = f"proposals[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        proposal_id = row.get("proposal_id")
        if _is_blank(proposal_id) or proposal_id in proposal_ids:
            invalid.append(f"{prefix}.proposal_id")
        else:
            proposal_ids.add(str(proposal_id))
        status = row.get("status")
        if status not in {"proposed", "evaluating", "awaiting_approval", "approved", "shadow", "active", "rejected", "expired", "superseded", "rolled_back"}:
            invalid.append(f"{prefix}.status")
        if status == "active":
            promoted += 1
            for field in ("privacy", "contamination", "development", "frozen_holdout", "shadow"):
                if row.get("evaluations", {}).get(field) is not True:
                    failed.append(f"{prefix}:{field}")
            reduction = _finite_number(row.get("review_burden_reduction"))
            if reduction is None:
                invalid.append(f"{prefix}.review_burden_reduction")
            elif reduction <= 0:
                failed.append(f"{prefix}:review_burden_reduction")
            if row.get("human_approval_ref") in {None, ""}:
                failed.append(f"{prefix}:human_approval")
            if row.get("rollback_ref") in {None, ""}:
                failed.append(f"{prefix}:rollback_ref")
        if row.get("self_update") is True:
            failed.append(f"{prefix}:self_update_forbidden")
    if promoted == 0:
        failed.append("no_active_governed_feedback")
    return {"proposal_count": len(proposal_ids), "promoted_count": promoted}, failed


def _validate_w69(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    corpus = _mapping(payload, "corpus", missing, invalid)
    items = payload.get("items")
    continuity = payload.get("continuity_runs")
    failed: list[str] = []
    if corpus.get("frozen") is not True:
        failed.append("holdout_not_frozen")
    for field in ("dataset_hash", "snapshot_hash", "rebuild_hash"):
        if _is_blank(corpus.get(field)):
            invalid.append(f"corpus.{field}")
    if corpus.get("snapshot_hash") != corpus.get("rebuild_hash"):
        failed.append("snapshot_rebuild_mismatch")
    floors: dict[str, float] = {}
    for field in ("binding_precision_floor", "material_recall_floor", "overall_recall_floor"):
        value = _finite_number(corpus.get(field))
        if value is None or not 0 < value <= 1:
            invalid.append(f"corpus.{field}")
        else:
            floors[field] = value
    if not isinstance(items, list) or not items:
        invalid.append("items")
        return {}, failed
    predicted = bound_correct = eligible = material = material_recalled = overall_recalled = 0
    for index, row in enumerate(items):
        prefix = f"items[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        if row.get("binding_attempted") is True:
            predicted += 1
            if row.get("binding_correct") is True:
                bound_correct += 1
        if row.get("eligible") is True:
            eligible += 1
            if row.get("recalled") is True:
                overall_recalled += 1
            if row.get("material") is True:
                material += 1
                if row.get("recalled") is True:
                    material_recalled += 1
    binding_precision = bound_correct / predicted if predicted else 0.0
    material_recall = material_recalled / material if material else 0.0
    overall_recall = overall_recalled / eligible if eligible else 0.0
    if binding_precision < floors.get("binding_precision_floor", math.inf):
        failed.append("binding_precision_floor")
    if material_recall < floors.get("material_recall_floor", math.inf):
        failed.append("material_recall_floor")
    if overall_recall < floors.get("overall_recall_floor", math.inf):
        failed.append("overall_recall_floor")
    if not isinstance(continuity, list) or not continuity:
        invalid.append("continuity_runs")
    else:
        if any(not isinstance(row, dict) or row.get("reproducible") is not True for row in continuity):
            failed.append("long_horizon_reproducibility")
    return {
        "binding_precision": binding_precision,
        "material_recall": material_recall,
        "overall_recall": overall_recall,
        "eligible_count": eligible,
        "material_count": material,
    }, failed


def _validate_w610(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    sections = {}
    for section in ("profile", "ratified_budgets", "measurements", "restore", "onboarding"):
        sections[section] = _mapping(payload, section, missing, invalid)
    profile = sections["profile"]
    required_dimensions = (
        "profile_id",
        "minimum_programs",
        "maximum_programs",
        "items_per_program",
        "relations_per_program",
        "revisions_per_program",
        "documents_per_program",
        "incremental_change_percent",
        "source_concurrency",
        "provider_budget_ref",
        "scheduler_interval",
        "retention_profile",
        "hardware_storage_profile",
    )
    for field in required_dimensions:
        if _is_blank(profile.get(field)) and field not in {
            "minimum_programs",
            "maximum_programs",
            "items_per_program",
            "relations_per_program",
            "revisions_per_program",
            "documents_per_program",
            "incremental_change_percent",
            "source_concurrency",
        }:
            missing.append(f"profile.{field}")
    for field in (
        "minimum_programs",
        "maximum_programs",
        "items_per_program",
        "relations_per_program",
        "revisions_per_program",
        "documents_per_program",
        "source_concurrency",
    ):
        if not _positive_int(profile.get(field)):
            invalid.append(f"profile.{field}")
    if profile.get("minimum_programs") != 2:
        failed: list[str] = ["exact_two_program_target"]
    else:
        failed = []
    maximum = profile.get("maximum_programs")
    if _positive_int(maximum):
        assert isinstance(maximum, int) and not isinstance(maximum, bool)
        if maximum < 2:
            failed.append("maximum_profile")
    budgets = sections["ratified_budgets"]
    measurements = sections["measurements"]
    required_metrics = (
        "required_path_p50_s",
        "required_path_p95_s",
        "required_path_p99_s",
        "queue_wait_s",
        "compiler_span_count",
        "compiler_token_count",
        "storage_gb_program_year",
        "cost_per_certified_artifact",
        "cost_per_completed_cycle",
        "cost_per_active_program_month",
        "unattributed_cost",
        "uncertain_cost",
    )
    for metric in required_metrics:
        value = measurements.get(metric)
        budget = budgets.get(metric)
        if value is None or budget is None:
            missing.append(f"measurements_or_budgets.{metric}")
            continue
        numeric_value = _finite_number(value)
        numeric_budget = _finite_number(budget)
        if numeric_value is None or numeric_budget is None:
            invalid.append(metric)
        elif numeric_value < 0:
            invalid.append(f"measurements.{metric}")
        elif numeric_budget < 0:
            invalid.append(f"ratified_budgets.{metric}")
        elif numeric_value > numeric_budget:
            failed.append(f"{metric}:budget")
    restore = sections["restore"]
    if restore.get("isolated_drill") is not True:
        failed.append("isolated_restore_drill")
    for field in ("measured_rpo_s", "measured_rto_s", "rpo_budget_s", "rto_budget_s"):
        value = _finite_number(restore.get(field))
        if value is None:
            missing.append(f"restore.{field}")
        elif value < 0:
            invalid.append(f"restore.{field}")
    measured_rpo = _finite_number(restore.get("measured_rpo_s"))
    rpo_budget = _finite_number(restore.get("rpo_budget_s"))
    if (
        measured_rpo is not None
        and rpo_budget is not None
        and measured_rpo >= 0
        and rpo_budget >= 0
        and measured_rpo > rpo_budget
    ):
        failed.append("restore:rpo")
    measured_rto = _finite_number(restore.get("measured_rto_s"))
    rto_budget = _finite_number(restore.get("rto_budget_s"))
    if (
        measured_rto is not None
        and rto_budget is not None
        and measured_rto >= 0
        and rto_budget >= 0
        and measured_rto > rto_budget
    ):
        failed.append("restore:rto")
    onboarding = sections["onboarding"]
    if onboarding.get("operator_proficiency_passed") is not True:
        failed.append("operator_proficiency")
    if onboarding.get("support_burden_measured") is not True:
        failed.append("support_burden")
    admission = _finite_number(measurements.get("next_program_admission_percent"))
    separate = _finite_number(measurements.get("separate_architecture_tripwire_percent"))
    if admission is None or separate is None or not 0 <= admission <= 100 or not 0 <= separate <= 100:
        invalid.append("admission_tripwires")
    return {
        "profile_id": profile.get("profile_id"),
        "minimum_programs": profile.get("minimum_programs"),
        "maximum_programs": profile.get("maximum_programs"),
        "required_metrics_checked": len(required_metrics),
    }, failed


def _validate_w611(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        invalid.append("paths")
        return {}, []
    failed: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(paths):
        prefix = f"paths[{index}]"
        if not isinstance(row, dict):
            invalid.append(prefix)
            continue
        path = row.get("path")
        if _is_blank(path) or path in by_path:
            invalid.append(f"{prefix}.path")
            continue
        by_path[str(path)] = row
        if row.get("disposition") not in {"retained_governed", "retained_forensic", "replaced", "deleted"}:
            invalid.append(f"{prefix}.disposition")
        if row.get("disposition") == "retained_governed":
            if row.get("runtime_mutation_allowed") is True or row.get("runtime_prompt_consumer") is True:
                failed.append(f"{path}:no_self_update")
        if row.get("disposition") in {"replaced", "deleted"} and _is_blank(row.get("replacement_ref")):
            missing.append(f"{prefix}.replacement_ref")
        if row.get("callers_inventoried") is not True:
            failed.append(f"{path}:caller_inventory")
        if row.get("privacy_audited") is not True:
            failed.append(f"{path}:privacy_audit")
    for required in LEARNING_PATHS:
        if required not in by_path:
            failed.append(f"missing_disposition:{required}")
    return {"disposition_count": len(by_path)}, failed


def _validate_w612(payload: dict[str, Any], missing: list[str], invalid: list[str]) -> tuple[dict[str, Any], list[str]]:
    state = payload.get("requested_terminal_state")
    evidence = _mapping(payload, "evidence", missing, invalid)
    if state not in {"PROPOSED", "CONTAINED", "ADVISORY_OPERATIONAL", "RELO_PROFILE_COMPLETE", "HALTED"}:
        invalid.append("requested_terminal_state")
    required = (
        "engineering_passed",
        "safety_passed",
        "source_workflows_passed",
        "operator_workflows_passed",
        "independent_labeling_available",
        "mandatory_vision_sources_available",
        "scheduled_identity_available",
        "autonomy_evidence_available",
        "fixed_targets_passed",
        "ratified_budgets_passed",
        "non_forceable_gates_passed",
        "mandatory_sources_no_waiver",
        "scheduled_recoverable_operation",
        "operators_proficient",
        "closed_loop_functions_certified",
        "quality_adjusted_human_impact_lcb_above_080",
        "containment_survives",
        "hybrid_inventory_complete",
        "inflight_state_drained",
        "stores_dispositioned",
        "evidence_preserved",
        "product_vision_owner_decision_ref",
    )
    for field in required:
        if field not in evidence:
            missing.append(f"evidence.{field}")
    failed: list[str] = []
    if state == "PROPOSED":
        pass
    elif state == "CONTAINED":
        if any(evidence.get(field) is not True for field in (
            "containment_survives",
            "hybrid_inventory_complete",
            "stores_dispositioned",
            "evidence_preserved",
        )):
            failed.append("contained_predicates")
    elif state == "ADVISORY_OPERATIONAL":
        base = (
            "engineering_passed",
            "safety_passed",
            "source_workflows_passed",
            "operator_workflows_passed",
            "containment_survives",
        )
        if any(evidence.get(field) is not True for field in base):
            failed.append("advisory_operational_base")
        unavailable = (
            "independent_labeling_available",
            "mandatory_vision_sources_available",
            "scheduled_identity_available",
            "autonomy_evidence_available",
        )
        if all(evidence.get(field) is True for field in unavailable):
            failed.append("advisory_requires_unavailable_full_authority_gate")
    elif state == "RELO_PROFILE_COMPLETE":
        complete_fields = (
            "fixed_targets_passed",
            "ratified_budgets_passed",
            "non_forceable_gates_passed",
            "mandatory_sources_no_waiver",
            "scheduled_recoverable_operation",
            "operators_proficient",
            "closed_loop_functions_certified",
            "quality_adjusted_human_impact_lcb_above_080",
            "engineering_passed",
            "safety_passed",
            "source_workflows_passed",
            "operator_workflows_passed",
            "containment_survives",
            "independent_labeling_available",
        )
        if any(evidence.get(field) is not True for field in complete_fields):
            failed.append("profile_complete_predicates")
        if _is_blank(payload.get("profile_id")):
            missing.append("profile_id")
        if _is_blank(evidence.get("product_vision_owner_decision_ref")):
            missing.append("evidence.product_vision_owner_decision_ref")
    elif state == "HALTED":
        if _is_blank(payload.get("halt_reason")):
            missing.append("halt_reason")
        if any(evidence.get(field) is not True for field in (
            "containment_survives",
            "hybrid_inventory_complete",
            "inflight_state_drained",
            "stores_dispositioned",
            "evidence_preserved",
        )):
            failed.append("halt_disposition_predicates")
    return {
        "requested_terminal_state": state,
        "product_vision_complete": False,
        "profile_id": payload.get("profile_id"),
    }, failed


_VALIDATORS: dict[str, _Validator] = {
    "RELO-W6.1": _validate_w61,
    "RELO-W6.2": _validate_w62,
    "RELO-W6.3": _validate_w63,
    "RELO-W6.4": _validate_w64,
    "RELO-W6.5": _validate_w65,
    "RELO-W6.6": _validate_w66,
    "RELO-W6.7": _validate_w67,
    "RELO-W6.8": _validate_w68,
    "RELO-W6.9": _validate_w69,
    "RELO-W6.10": _validate_w610,
    "RELO-W6.11": _validate_w611,
    "RELO-W6.12": _validate_w612,
}


def _classify_terminal(
    payload: dict[str, Any],
    *,
    missing: list[str],
    invalid: list[str],
    failed: list[str],
) -> str:
    if missing or invalid or failed:
        return "not_assessed"
    state = payload.get("requested_terminal_state")
    return str(state) if state in {"PROPOSED", "CONTAINED", "ADVISORY_OPERATIONAL", "RELO_PROFILE_COMPLETE", "HALTED"} else "not_assessed"


def _validate_artifacts(
    artifacts: list[object],
    *,
    repo_root: Path | None,
    missing: list[str],
    invalid: list[str],
) -> None:
    seen: set[str] = set()
    for index, value in enumerate(artifacts):
        prefix = f"artifacts[{index}]"
        if not isinstance(value, dict):
            invalid.append(prefix)
            continue
        artifact_id = value.get("artifact_id")
        if _is_blank(artifact_id) or artifact_id in seen:
            invalid.append(f"{prefix}.artifact_id")
        else:
            seen.add(str(artifact_id))
        digest = value.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            invalid.append(f"{prefix}.sha256")
        path = value.get("path")
        external_ref = value.get("external_evidence_ref")
        if _is_blank(path) and _is_blank(external_ref):
            missing.append(f"{prefix}.path_or_external_evidence_ref")
            continue
        if path and repo_root is not None:
            candidate = (repo_root / str(path)).resolve()
            try:
                candidate.relative_to(repo_root.resolve())
            except ValueError:
                invalid.append(f"{prefix}.path")
                continue
            if not candidate.is_file():
                invalid.append(f"{prefix}.path")
            elif digest and hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
                invalid.append(f"{prefix}.sha256_mismatch")


def _validate_repository_links(
    card_id: str,
    payload: dict[str, Any],
    *,
    repo_root: Path,
    missing: list[str],
    invalid: list[str],
    failed: list[str],
) -> None:
    root = repo_root.resolve()
    artifact_ids = {
        row.get("artifact_id")
        for row in payload.get("artifacts", ())
        if isinstance(row, dict) and isinstance(row.get("artifact_id"), str)
    }

    def admitted_path(prefix: str, value: object, *, required_file: bool = True) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            missing.append(prefix)
            return None
        candidate = (root / value).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            invalid.append(prefix)
            return None
        if required_file and not candidate.is_file():
            invalid.append(prefix)
            return None
        return candidate

    if card_id == "RELO-W6.5":
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            return
        for index, row in enumerate(candidates):
            if not isinstance(row, dict):
                continue
            prefix = f"candidates[{index}]"
            candidate = admitted_path(f"{prefix}.path", row.get("path"))
            admitted_path(f"{prefix}.replacement_path", row.get("replacement_path"))
            for field in ("caller_inventory_ref", "shadow_ref", "test_ref"):
                if row.get(field) not in artifact_ids:
                    failed.append(f"{prefix}:{field}_artifact")
            declared = row.get("production_loc")
            if candidate is not None and _positive_int(declared):
                observed = len(candidate.read_text(encoding="utf-8").splitlines())
                if observed != declared and observed > 0:
                    invalid.append(f"{prefix}.production_loc")
    elif card_id == "RELO-W6.6":
        mappings = payload.get("canonical_mappings")
        if not isinstance(mappings, list):
            return
        for index, row in enumerate(mappings):
            if not isinstance(row, dict):
                continue
            prefix = f"canonical_mappings[{index}]"
            admitted_path(f"{prefix}.canonical_target", row.get("canonical_target"))
            if row.get("evidence_ref") not in artifact_ids:
                failed.append(f"{prefix}:evidence_artifact")
    elif card_id == "RELO-W6.11":
        paths = payload.get("paths")
        if not isinstance(paths, list):
            return
        for index, row in enumerate(paths):
            if not isinstance(row, dict):
                continue
            prefix = f"paths[{index}]"
            disposition = row.get("disposition")
            if disposition != "deleted":
                admitted_path(f"{prefix}.path", row.get("path"))
            if disposition in {"retained_governed", "replaced"}:
                admitted_path(f"{prefix}.replacement_ref", row.get("replacement_ref"))


def _validate_owner(value: object, missing: list[str], invalid: list[str]) -> str:
    if not isinstance(value, dict):
        invalid.append("owner_disposition")
        return "pending"
    status = value.get("status")
    if status not in {"pending", "approved", "rejected"}:
        invalid.append("owner_disposition.status")
        return "pending"
    if status == "approved":
        for field in ("approved_by", "approved_at", "approval_ref"):
            if _is_blank(value.get(field)):
                missing.append(f"owner_disposition.{field}")
        if _aware_datetime(value.get("approved_at")) is None:
            invalid.append("owner_disposition.approved_at")
    return str(status)


def _result(
    card_id: str | None,
    classification: str,
    measurements: dict[str, Any],
    missing: list[str],
    invalid: list[str],
    failed: list[str],
    repo_root: Path | None,
) -> W6VerificationResult:
    return W6VerificationResult(
        card_id=card_id,
        classification=classification,
        measurements=measurements,
        missing_fields=tuple(sorted(set(missing))),
        invalid_fields=tuple(sorted(set(invalid))),
        failed_gates=tuple(sorted(set(failed))),
        limitations=(
            "Evidence is explicitly supplied and mechanically checked; no provider or owner state is queried.",
            "Owner approval, private evidence authenticity, corpus independence and operational qualification are not authenticated.",
            "Synthetic fixtures and mechanically complete packets never establish production qualification.",
            (
                "Repository-local artifacts are hash-checked."
                if repo_root is not None
                else "Repository-local artifact hashes are not checked without an explicit repository root."
            ),
        ),
    )


def _mapping(
    payload: dict[str, Any],
    field: str,
    missing: list[str],
    invalid: list[str],
) -> dict[str, Any]:
    value = payload.get(field)
    if value is None:
        missing.append(field)
        return {}
    if not isinstance(value, dict):
        invalid.append(field)
        return {}
    return value


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


__all__ = [
    "LEARNING_PATHS",
    "SCHEMA_VERSION",
    "W6_CARD_IDS",
    "W6VerificationResult",
    "build_learning_disposition_inventory",
    "build_w6_routing_evidence",
    "build_w6_evidence_template",
    "terminal_state_from_evidence",
    "verify_w6_packet",
]
