from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from cli import app

from src.core.relo_w6 import (
    W6_CARD_IDS,
    build_learning_disposition_inventory,
    build_w6_routing_evidence,
    build_w6_evidence_template,
    terminal_state_from_evidence,
    verify_w6_packet,
)
from scripts.prepare_relo_w6_uat import prepare_manifest, render_checklist


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _base(card_id: str) -> dict:
    packet = build_w6_evidence_template(card_id)
    packet.update(
        packet_id=f"packet-{card_id}",
        captured_at=NOW.isoformat(),
        owner_disposition={
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
            "approval_ref": None,
        },
        artifacts=[{
            "artifact_id": "operator-evidence",
            "sha256": "0" * 64,
            "external_evidence_ref": "private://w6/evidence/1",
        }],
    )
    return packet


def _passing_packets() -> dict[str, dict]:
    packets: dict[str, dict] = {}

    w61 = _base("RELO-W6.1")
    w61["protocol"] = {
        "frozen": True,
        "different_annotator_model_family": True,
        "human_adjudicates_all_disagreements": True,
        "human_audit_minimum": 12,
        "disclosure": "model-assisted annotation; single human adjudicator",
    }
    w61["holdout"] = {
        "frozen": True,
        "dataset_hash": "a" * 64,
        "post_relo_multi_source": True,
        "strata": [
            {
                "stratum_id": domain,
                "domain": domain,
                "sample_count": 120 if domain == "risk" else 60,
                "divergence_count": 2 if domain == "risk" else 1,
                "reverified_after_pause": True,
                "human_model_kappa": 0.72,
                "human_audit_sample_count": 12,
                "human_audit_material_divergence_count": 0,
                "source_families": ["ado", "m365", "document"],
            }
            for domain in ("risk", "dependency")
        ],
    }
    packets["RELO-W6.1"] = w61

    w62 = _base("RELO-W6.2")
    w62["protocol"] = {
        "frozen": True,
        "assignment": "alternating",
        "bootstrap_seed": 7307,
        "bootstrap_resamples": 2000,
        "applicable_workflow_classes": ["weekly_portfolio_review"],
    }
    w62["cycles"] = [
        {
            "cycle_id": f"cycle-{index + 1}",
            "observed_at": (NOW + timedelta(days=index * 7)).isoformat(),
            "manual_minutes": 420,
            "assisted_minutes": 50,
            "quality_adjusted_minutes_saved": 370,
            "quality_passed": True,
            "completion_rate": 1.0,
            "displacement_rate": 0.9,
            "correction_count": 0,
            "exception_count": 0,
            "downstream_error_count": 0,
            "workflow_classes": ["weekly_portfolio_review"],
        }
        for index in range(8)
    ]
    packets["RELO-W6.2"] = w62

    w63 = _base("RELO-W6.3")
    w63.update({
        "window_start": (NOW - timedelta(days=56)).isoformat(),
        "window_end": NOW.isoformat(),
        "eligible_routine_tasks": 100,
        "completed_routine_tasks": 96,
        "eligible_tier_decisions": 100,
        "telemetry_records": 100,
        "deterministic_local_tasks": 40,
        "cache_reuse_tasks": 25,
        "remote_provider_tasks": 35,
        "certified_routing_frontier_calls": 0,
        "ratified_local_coverage_floor": 0.40,
        "ratified_routine_completion_floor": 0.95,
    })
    packets["RELO-W6.3"] = w63

    w64 = _base("RELO-W6.4")
    w64["protocol"] = {
        "frozen": True,
        "randomized_blind_order": True,
        "ratified_minimum_items": 2,
        "noninferiority_margin": 0.0,
    }
    w64["comparisons"] = [
        {
            "item_id": f"item-{index}",
            "critical_error": False,
            "rubric_scores": {
                dimension: {"baseline": 4.0, "candidate": 4.0}
                for dimension in (
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
            },
        }
        for index in range(2)
    ]
    packets["RELO-W6.4"] = w64

    w65 = _base("RELO-W6.5")
    w65.update({
        "replacement_ready": True,
        "shadow_green": True,
        "tests_green": True,
        "candidates": [{
            "path": "src/legacy/example.py",
            "replacement_path": "src/core/example.py",
            "caller_inventory_ref": "audit-1",
            "shadow_ref": "shadow-1",
            "test_ref": "test-1",
            "rollback_window": "14d",
            "production_loc": 3600,
            "dependency_gate": "qualified",
        }],
    })
    packets["RELO-W6.5"] = w65

    w66 = _base("RELO-W6.6")
    w66.update({
        "closed_cards": ["RELO-W1.1", "RELO-W2.1"],
        "canonical_mappings": [
            {
                "card_id": card,
                "canonical_target": "specs/vertex-tech-spec.md",
                "permanent_surface": "spec_invariant",
                "evidence_ref": f"evidence:{card}",
            }
            for card in ("RELO-W1.1", "RELO-W2.1")
        ],
        "temporary_binding_only_refs": [],
    })
    packets["RELO-W6.6"] = w66

    w67 = _base("RELO-W6.7")
    w67["transitions"] = [{
        "proposal_class": "risk",
        "action_class": "stage_proposal",
        "from_level": "l2",
        "to_level": "l3",
        "evidence_window_ref": "window:1",
        "quality_gates_passed": True,
        "no_material_regression": True,
        "owner": "platform-owner",
        "owner_approval_ref": "approval:1",
        "independent_review_ref": "review:1",
        "rollback_ref": "rollback:1",
    }]
    packets["RELO-W6.7"] = w67

    w68 = _base("RELO-W6.8")
    w68["proposals"] = [{
        "proposal_id": "proposal-1",
        "status": "active",
        "evaluations": {
            "privacy": True,
            "contamination": True,
            "development": True,
            "frozen_holdout": True,
            "shadow": True,
        },
        "review_burden_reduction": 0.1,
        "human_approval_ref": "approval:1",
        "rollback_ref": "rollback:1",
        "self_update": False,
    }]
    packets["RELO-W6.8"] = w68

    w69 = _base("RELO-W6.9")
    w69["corpus"] = {
        "frozen": True,
        "dataset_hash": "b" * 64,
        "snapshot_hash": "c" * 64,
        "rebuild_hash": "c" * 64,
        "binding_precision_floor": 0.99,
        "material_recall_floor": 0.95,
        "overall_recall_floor": 0.90,
    }
    w69["items"] = [
        {
            "binding_attempted": True,
            "binding_correct": True,
            "eligible": True,
            "material": index < 2,
            "recalled": True,
        }
        for index in range(20)
    ]
    w69["continuity_runs"] = [{"window_id": "w1", "reproducible": True}]
    packets["RELO-W6.9"] = w69

    w610 = _base("RELO-W6.10")
    w610["profile"] = {
        "profile_id": "single-host-2-40-v1",
        "minimum_programs": 2,
        "maximum_programs": 40,
        "items_per_program": 5000,
        "relations_per_program": 10000,
        "revisions_per_program": 50000,
        "documents_per_program": 1000,
        "incremental_change_percent": 5,
        "source_concurrency": 4,
        "provider_budget_ref": "budget:provider",
        "scheduler_interval": "15m",
        "retention_profile": "90d",
        "hardware_storage_profile": "single-host-v1",
    }
    w610["ratified_budgets"] = {
        metric: 10.0
        for metric in (
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
    }
    w610["measurements"] = {
        **{metric: 1.0 for metric in w610["ratified_budgets"]},
        "next_program_admission_percent": 80.0,
        "separate_architecture_tripwire_percent": 90.0,
    }
    w610["restore"] = {
        "isolated_drill": True,
        "measured_rpo_s": 0,
        "measured_rto_s": 60,
        "rpo_budget_s": 0,
        "rto_budget_s": 300,
    }
    w610["onboarding"] = {
        "operator_proficiency_passed": True,
        "support_burden_measured": True,
    }
    packets["RELO-W6.10"] = w610

    w611 = _base("RELO-W6.11")
    w611["paths"] = [
        {
            "path": path,
            "disposition": (
                "retained_forensic"
                if path.endswith("edit_learner.py")
                else "replaced"
            ),
            "runtime_mutation_allowed": False,
            "runtime_prompt_consumer": False,
            "replacement_ref": "src/core/governed_feedback_lifecycle.py",
            "callers_inventoried": True,
            "privacy_audited": True,
        }
        for path in (
            "src/ai/edit_learner.py",
            "src/ai/learning_distiller.py",
            "src/commands/confirm_stages/learning_distiller.py",
        )
    ]
    packets["RELO-W6.11"] = w611

    w612 = _base("RELO-W6.12")
    w612["requested_terminal_state"] = "CONTAINED"
    w612["halt_reason"] = ""
    w612["profile_id"] = ""
    w612["evidence"] = {
        field: False
        for field in (
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
            "inflight_state_drained",
        )
    }
    w612["evidence"].update({
        "containment_survives": True,
        "hybrid_inventory_complete": True,
        "stores_dispositioned": True,
        "evidence_preserved": True,
        "product_vision_owner_decision_ref": "decision:product-vision",
    })
    packets["RELO-W6.12"] = w612
    return packets


@pytest.mark.parametrize("card_id", W6_CARD_IDS)
def test_w6_templates_are_stable_and_fail_closed(card_id: str) -> None:
    template = build_w6_evidence_template(card_id)
    assert template["schema_version"] == "1"
    assert template["card_id"] == card_id
    result = verify_w6_packet(template)
    assert result.classification in {"not_qualified", "not_assessed"}


def test_unsupported_template_is_refused() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        build_w6_evidence_template("RELO-W9.1")


def test_packet_verifier_refuses_non_mapping_and_unknown_card_inputs() -> None:
    assert verify_w6_packet(object()).classification == "not_qualified"
    packet = _passing_packets()["RELO-W6.3"]
    packet["card_id"] = "RELO-W9.1"
    assert verify_w6_packet(packet).classification == "not_qualified"


@pytest.mark.parametrize("card_id", W6_CARD_IDS[:-1])
def test_mechanically_complete_packets_require_owner_review(card_id: str) -> None:
    result = verify_w6_packet(_passing_packets()[card_id])
    assert result.classification == "owner_review", result.to_payload()
    assert result.failed_gates == ()


def test_terminal_state_requires_exact_predicates_without_product_vision_conflation() -> None:
    packet = _passing_packets()["RELO-W6.12"]

    result = terminal_state_from_evidence(packet)

    assert result["terminal_state"] == "CONTAINED"
    assert result["measurement"]["product_vision_complete"] is False
    assert result["assessment"] == "CONTAINED"


@pytest.mark.parametrize(
    "state",
    ["PROPOSED", "CONTAINED", "ADVISORY_OPERATIONAL", "RELO_PROFILE_COMPLETE", "HALTED"],
)
def test_terminal_state_evaluator_exercises_all_explicit_states(state: str) -> None:
    packet = _passing_packets()["RELO-W6.12"]
    packet["requested_terminal_state"] = state
    if state == "ADVISORY_OPERATIONAL":
        packet["evidence"].update({
            "engineering_passed": True,
            "safety_passed": True,
            "source_workflows_passed": True,
            "operator_workflows_passed": True,
        })
    elif state == "RELO_PROFILE_COMPLETE":
        packet["profile_id"] = "profile-1"
        for field in (
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
            "independent_labeling_available",
        ):
            packet["evidence"][field] = True
    elif state == "HALTED":
        packet["halt_reason"] = "capacity unavailable"
        packet["evidence"]["inflight_state_drained"] = True

    result = terminal_state_from_evidence(packet)

    assert result["terminal_state"] == state


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_end": NOW - timedelta(days=8)},
        {"eligible_routine_tasks": -1},
        {"completed_routine_tasks": 11},
        {"expected_tier_decisions": -1},
        {"ratified_local_coverage_floor": 0},
        {"ratified_routine_completion_floor": 2},
    ],
)
def test_routing_evidence_rejects_invalid_enrollment_inputs(kwargs) -> None:
    arguments = {
        "window_start": NOW - timedelta(days=7),
        "window_end": NOW,
        "eligible_routine_tasks": 10,
        "completed_routine_tasks": 10,
        "expected_tier_decisions": 10,
        "ratified_local_coverage_floor": 0.4,
        "ratified_routine_completion_floor": 0.9,
    }
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        build_w6_routing_evidence([], **arguments)


def test_routing_evidence_rejects_bad_rows_and_undated_records() -> None:
    arguments = {
        "window_start": NOW - timedelta(days=7),
        "window_end": NOW,
        "eligible_routine_tasks": 1,
        "completed_routine_tasks": 1,
        "expected_tier_decisions": 1,
        "ratified_local_coverage_floor": 0.4,
        "ratified_routine_completion_floor": 0.9,
    }
    with pytest.raises(ValueError, match="mappings"):
        build_w6_routing_evidence([object()], **arguments)  # type: ignore[list-item]
    with pytest.raises(ValueError, match="recorded_at"):
        build_w6_routing_evidence([{"recorded_at": "bad"}], **arguments)


@pytest.mark.parametrize(
    ("card_id", "mutation", "marker"),
    [
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(human_model_kappa=0.69), "kappa_floor"),
        ("RELO-W6.2", lambda p: p["cycles"].pop(), "at_least_8_cycles"),
        ("RELO-W6.3", lambda p: p.update(certified_routing_frontier_calls=1), "certified_routing_frontier_calls"),
        ("RELO-W6.4", lambda p: p["comparisons"][0].update(critical_error=True), "material_quality_regression"),
        ("RELO-W6.5", lambda p: p.update(shadow_green=False), "shadow_green"),
        ("RELO-W6.6", lambda p: p.update(temporary_binding_only_refs=["RELO-D7"]), "binding_decision_only_in_temporary_spec"),
        ("RELO-W6.7", lambda p: p["transitions"][0].update(independent_review_ref=""), "independent_review"),
        ("RELO-W6.8", lambda p: p["proposals"][0].update(self_update=True), "self_update_forbidden"),
        ("RELO-W6.9", lambda p: p["corpus"].update(rebuild_hash="different"), "snapshot_rebuild_mismatch"),
        ("RELO-W6.10", lambda p: p["profile"].update(minimum_programs=3), "exact_two_program_target"),
        ("RELO-W6.11", lambda p: p["paths"][0].update(privacy_audited=False), "privacy_audit"),
        ("RELO-W6.12", lambda p: p["evidence"].update(containment_survives=False), "contained_predicates"),
    ],
)
def test_card_acceptance_gates_are_mechanically_enforced(card_id: str, mutation, marker: str) -> None:
    packet = _passing_packets()[card_id]
    mutation(packet)

    result = verify_w6_packet(packet)

    assert result.classification not in {"owner_review", "PROPOSED", "CONTAINED", "ADVISORY_OPERATIONAL", "RELO_PROFILE_COMPLETE", "HALTED"}
    assert any(marker in gate for gate in result.failed_gates)


def test_repo_artifact_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    artifact = tmp_path / "evidence.txt"
    artifact.write_text("recorded", encoding="utf-8")
    packet = _passing_packets()["RELO-W6.3"]
    packet["artifacts"] = [{
        "artifact_id": "local",
        "path": "evidence.txt",
        "sha256": hashlib.sha256(b"different").hexdigest(),
    }]

    result = verify_w6_packet(packet, repo_root=tmp_path)

    assert "artifacts[0].sha256_mismatch" in result.invalid_fields
    assert result.classification == "not_qualified"


def test_repository_link_contract_resolves_only_in_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    candidate = root / "src" / "legacy.py"
    replacement = root / "src" / "replacement.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("one\n" "two\n", encoding="utf-8")
    replacement.write_text("replacement\n", encoding="utf-8")
    packet = _passing_packets()["RELO-W6.5"]
    packet["artifacts"] = [
        {"artifact_id": ref, "sha256": "0" * 64, "external_evidence_ref": f"private://{ref}"}
        for ref in ("audit-1", "shadow-1", "test-1")
    ]
    packet["candidates"][0].update(
        path="src/legacy.py",
        replacement_path="src/replacement.py",
        production_loc=2,
    )

    admitted = verify_w6_packet(packet, repo_root=root)
    assert admitted.classification == "owner_review"

    packet["candidates"][0]["path"] = "../outside.py"
    refused = verify_w6_packet(packet, repo_root=root)
    assert "candidates[0].path" in refused.invalid_fields


def test_traceability_and_learning_repository_links_are_checked(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    spec = root / "specs" / "vertex-tech-spec.md"
    evidence = root / "tests" / "evidence.txt"
    replacement = root / "src" / "core" / "governed_feedback_lifecycle.py"
    spec.parent.mkdir(parents=True)
    evidence.parent.mkdir(parents=True)
    replacement.parent.mkdir(parents=True)
    spec.write_text("spec", encoding="utf-8")
    evidence.write_text("evidence", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")

    trace = _passing_packets()["RELO-W6.6"]
    trace["artifacts"] = [
        {"artifact_id": f"evidence:{card}", "sha256": "0" * 64, "external_evidence_ref": f"private://{card}"}
        for card in trace["closed_cards"]
    ]
    for mapping in trace["canonical_mappings"]:
        mapping["canonical_target"] = "specs/vertex-tech-spec.md"
    assert verify_w6_packet(trace, repo_root=root).classification == "owner_review"
    trace["canonical_mappings"][0]["evidence_ref"] = "missing"
    assert any("evidence_artifact" in item for item in verify_w6_packet(trace, repo_root=root).failed_gates)

    learning = _passing_packets()["RELO-W6.11"]
    for row in learning["paths"]:
        path = root / row["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("learning", encoding="utf-8")
        if row["disposition"] == "replaced":
            row["replacement_ref"] = "src/core/governed_feedback_lifecycle.py"
    assert verify_w6_packet(learning, repo_root=root).classification == "owner_review"
    learning["paths"][0]["path"] = "../outside.py"
    assert any("paths[0].path" in item for item in verify_w6_packet(learning, repo_root=root).invalid_fields)


def test_learning_disposition_inventory_is_explicit_and_no_self_update(tmp_path: Path) -> None:
    for path in (
        "src/ai/edit_learner.py",
        "src/ai/learning_distiller.py",
        "src/commands/confirm_stages/learning_distiller.py",
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    inventory = build_learning_disposition_inventory(tmp_path)

    assert {row["path"] for row in inventory["paths"]} == {
        "src/ai/edit_learner.py",
        "src/ai/learning_distiller.py",
        "src/commands/confirm_stages/learning_distiller.py",
    }
    assert all(not row["runtime_mutation_allowed"] for row in inventory["paths"])
    assert all(not row["runtime_prompt_consumer"] for row in inventory["paths"])


def test_verifier_preserves_input_and_outputs_terminal_state(tmp_path: Path) -> None:
    packet_path = tmp_path / "packet.json"
    packet = _passing_packets()["RELO-W6.12"]
    payload = json.dumps(packet, indent=2)
    packet_path.write_text(payload, encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_relo_w6.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input",
            str(packet_path),
            "--repo-root",
            str(tmp_path),
            "--terminal-state",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout
    assert json.loads(completed.stdout)["terminal_state"] == "CONTAINED"
    assert packet_path.read_text(encoding="utf-8") == payload


def test_readiness_w6_cli_admits_mechanically_complete_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "w6-routing.json"
    packet_path.write_text(
        json.dumps(_passing_packets()["RELO-W6.3"]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["readiness", "w6", "--evidence", str(packet_path)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["classification"] == "owner_review"
    assert packet_path.exists()


def test_readiness_w6_cli_refuses_undecodable_input(tmp_path: Path) -> None:
    packet_path = tmp_path / "broken.json"
    packet_path.write_text("{", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["readiness", "w6", "--evidence", str(packet_path)],
    )

    assert result.exit_code == 2
    assert "Invalid W6 evidence" in result.stderr


def test_templates_do_not_mutate_shared_defaults() -> None:
    first = build_w6_evidence_template("RELO-W6.7")
    second = build_w6_evidence_template("RELO-W6.7")
    first["transitions"].append({"changed": True})
    assert second["transitions"] == []


def test_unexpected_payload_types_are_not_coerced() -> None:
    packet = deepcopy(_passing_packets()["RELO-W6.8"])
    packet["proposals"][0]["review_burden_reduction"] = "0.1"

    result = verify_w6_packet(packet)

    assert result.classification == "not_qualified"
    assert any("review_burden_reduction" in field for field in result.invalid_fields)


def test_routing_telemetry_cannot_exceed_eligible_population() -> None:
    packet = _passing_packets()["RELO-W6.3"]
    packet["telemetry_records"] = 101

    result = verify_w6_packet(packet)

    assert result.classification == "not_qualified"
    assert "telemetry_records" in result.invalid_fields


@pytest.mark.parametrize("value", [None, "false", 0, 1])
def test_blind_comparison_requires_explicit_critical_error_boolean(value) -> None:
    packet = _passing_packets()["RELO-W6.4"]
    if value is None:
        packet["comparisons"][0].pop("critical_error")
    else:
        packet["comparisons"][0]["critical_error"] = value

    result = verify_w6_packet(packet)

    assert result.classification == "not_qualified"
    assert "comparisons[0].critical_error" in (
        *result.missing_fields,
        *result.invalid_fields,
    )


@pytest.mark.parametrize(
    ("section", "field", "measurement", "budget"),
    [
        ("measurements", "required_path_p50_s", -1.0, 0.0),
        ("ratified_budgets", "required_path_p50_s", 0.0, -1.0),
        ("restore", "measured_rpo_s", -1.0, 0.0),
        ("restore", "rpo_budget_s", -2.0, -1.0),
    ],
)
def test_scale_and_restore_measurements_are_nonnegative(
    section: str,
    field: str,
    measurement: float,
    budget: float,
) -> None:
    packet = _passing_packets()["RELO-W6.10"]
    if section == "restore":
        budget_field = "rpo_budget_s"
        packet["restore"]["measured_rpo_s"] = measurement
        packet["restore"][budget_field] = budget
    else:
        packet["measurements"]["required_path_p50_s"] = measurement
        packet["ratified_budgets"]["required_path_p50_s"] = budget

    result = verify_w6_packet(packet)

    assert result.classification == "not_qualified"
    assert any(field in item for item in (*result.missing_fields, *result.invalid_fields))


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p.pop("schema_version"),
        lambda p: p.update(schema_version="2"),
        lambda p: p.update(packet_id=" "),
        lambda p: p.update(captured_at="2026-01-01"),
        lambda p: p.update(artifacts={}),
        lambda p: p.update(owner_disposition=[]),
        lambda p: p.update(owner_disposition={"status": "unknown"}),
        lambda p: p["artifacts"].append(p["artifacts"][0]),
        lambda p: p["artifacts"][0].update(sha256="bad"),
        lambda p: p["artifacts"][0].update(path="", external_evidence_ref=""),
    ],
)
def test_common_packet_contract_rejects_malformed_shapes(mutator) -> None:
    packet = deepcopy(_passing_packets()["RELO-W6.3"])
    mutator(packet)

    result = verify_w6_packet(packet)

    assert result.classification == "not_qualified"


@pytest.mark.parametrize(
    ("card_id", "mutator"),
    [
        ("RELO-W6.1", lambda p: p.update(protocol=[])),
        ("RELO-W6.1", lambda p: p["protocol"].update(frozen=False, different_annotator_model_family=False)),
        ("RELO-W6.1", lambda p: p["protocol"].update(human_audit_minimum=0, disclosure="other")),
        ("RELO-W6.1", lambda p: p["holdout"].update(frozen=False, dataset_hash="", post_relo_multi_source=False, strata={})),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(stratum_id="dependency")),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(domain="unknown")),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(sample_count=-1, divergence_count=-1)),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(divergence_count=121)),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(human_model_kappa=float("nan"))),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(human_audit_sample_count=0, human_audit_material_divergence_count=1)),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(source_families=["m365"])),
        ("RELO-W6.2", lambda p: p.update(protocol=[], cycles={})),
        ("RELO-W6.2", lambda p: p["protocol"].update(assignment="random", bootstrap_seed=-1, bootstrap_resamples=1, applicable_workflow_classes=[])),
        ("RELO-W6.2", lambda p: p["cycles"][0].update(cycle_id="cycle-2", observed_at="bad")),
        ("RELO-W6.2", lambda p: p["cycles"][0].update(manual_minutes=0, assisted_minutes=-1)),
        ("RELO-W6.2", lambda p: p["cycles"][0].update(quality_adjusted_minutes_saved=421, quality_passed=False)),
        ("RELO-W6.2", lambda p: p["cycles"][0].update(completion_rate=2, correction_count=-1)),
        ("RELO-W6.2", lambda p: p["cycles"][0].update(workflow_classes=["other"])),
        ("RELO-W6.2", lambda p: p["cycles"].append(deepcopy(p["cycles"][0]))),
        ("RELO-W6.3", lambda p: p.update(eligible_routine_tasks="100", completed_routine_tasks=101)),
        ("RELO-W6.3", lambda p: p.update(ratified_local_coverage_floor=0, ratified_routine_completion_floor=2)),
        ("RELO-W6.3", lambda p: p.update(eligible_tier_decisions=0, window_end=p["window_start"])),
        ("RELO-W6.3", lambda p: p.update(cache_reuse_tasks=0)),
        ("RELO-W6.4", lambda p: p.update(protocol=[], comparisons={})),
        ("RELO-W6.4", lambda p: p["protocol"].update(frozen=False, randomized_blind_order=False, ratified_minimum_items=0, noninferiority_margin=-1)),
        ("RELO-W6.4", lambda p: p["comparisons"][0].update(item_id="item-1")),
        ("RELO-W6.4", lambda p: p["comparisons"][0].update(rubric_scores={})),
        ("RELO-W6.4", lambda p: p["comparisons"][0]["rubric_scores"].update(factual_correctness={"baseline": 6, "candidate": -1})),
        ("RELO-W6.5", lambda p: p.update(replacement_ready=False, shadow_green=False, tests_green=False, candidates={})),
        ("RELO-W6.5", lambda p: p["candidates"][0].update(path="", production_loc=0, dependency_gate="pending")),
        ("RELO-W6.5", lambda p: p["candidates"].append(deepcopy(p["candidates"][0]))),
        ("RELO-W6.6", lambda p: p.update(closed_cards={}, canonical_mappings={})),
        ("RELO-W6.6", lambda p: p.update(canonical_mappings=[])),
        ("RELO-W6.6", lambda p: p["canonical_mappings"][0].update(card_id="RELO-W1.1", permanent_surface="other", canonical_target="", evidence_ref="")),
        ("RELO-W6.6", lambda p: p["closed_cards"].append("RELO-W1.1")),
        ("RELO-W6.7", lambda p: p.update(transitions={})),
        ("RELO-W6.7", lambda p: p["transitions"][0].update(from_level="bad", to_level="bad", quality_gates_passed=False, no_material_regression=False)),
        ("RELO-W6.7", lambda p: p["transitions"].append(deepcopy(p["transitions"][0]))),
        ("RELO-W6.8", lambda p: p.update(proposals={})),
        ("RELO-W6.8", lambda p: p["proposals"][0].update(status="unknown", proposal_id="")),
        ("RELO-W6.8", lambda p: p["proposals"][0].update(evaluations={})),
        ("RELO-W6.8", lambda p: p["proposals"][0].update(human_approval_ref="", rollback_ref="", review_burden_reduction=0)),
        ("RELO-W6.9", lambda p: p.update(corpus=[], items={}, continuity_runs={})),
        ("RELO-W6.9", lambda p: p["corpus"].update(frozen=False, dataset_hash="", snapshot_hash="", rebuild_hash="x")),
        ("RELO-W6.9", lambda p: p["corpus"].update(binding_precision_floor=0, material_recall_floor=2, overall_recall_floor=0)),
        ("RELO-W6.9", lambda p: p["items"][0].update(binding_correct=False, recalled=False)),
        ("RELO-W6.9", lambda p: p.update(continuity_runs=[{}])),
        ("RELO-W6.10", lambda p: p.update(profile=[], ratified_budgets=[], measurements=[], restore=[], onboarding=[])),
        ("RELO-W6.10", lambda p: p["profile"].update(minimum_programs=0, maximum_programs=0, profile_id="")),
        ("RELO-W6.10", lambda p: p["measurements"].update(required_path_p50_s=11, storage_gb_program_year="bad")),
        ("RELO-W6.10", lambda p: p["restore"].update(isolated_drill=False, measured_rpo_s=1, measured_rto_s=301)),
        ("RELO-W6.10", lambda p: p["onboarding"].update(operator_proficiency_passed=False, support_burden_measured=False)),
        ("RELO-W6.10", lambda p: p["measurements"].update(next_program_admission_percent=101, separate_architecture_tripwire_percent=-1)),
        ("RELO-W6.11", lambda p: p.update(paths={})),
        ("RELO-W6.11", lambda p: p["paths"][0].update(path="", disposition="unknown", callers_inventoried=False, privacy_audited=False)),
        ("RELO-W6.11", lambda p: p["paths"][0].update(disposition="replaced", replacement_ref="")),
        ("RELO-W6.11", lambda p: p["paths"].append(deepcopy(p["paths"][0]))),
        ("RELO-W6.12", lambda p: p.update(requested_terminal_state="UNKNOWN", evidence=[])),
        ("RELO-W6.12", lambda p: p["evidence"].clear()),
    ],
)
def test_each_card_refuses_invalid_shapes_and_incomplete_evidence(card_id: str, mutator) -> None:
    packet = deepcopy(_passing_packets()[card_id])
    mutator(packet)

    result = verify_w6_packet(packet)

    assert result.classification not in {
        "owner_review",
        "PROPOSED",
        "CONTAINED",
        "ADVISORY_OPERATIONAL",
        "RELO_PROFILE_COMPLETE",
        "HALTED",
    }


@pytest.mark.parametrize(
    ("card_id", "mutator"),
    [
        ("RELO-W6.1", lambda p: p.update(protocol={}, holdout={})),
        ("RELO-W6.1", lambda p: p["holdout"].update(strata=["bad"])),
        ("RELO-W6.1", lambda p: p["holdout"]["strata"][0].update(reverified_after_pause=False, sample_count=59)),
        ("RELO-W6.2", lambda p: p.update(protocol={}, cycles=["bad"])),
        ("RELO-W6.2", lambda p: p["cycles"][0].update(observed_at=(NOW - timedelta(days=49)).replace(tzinfo=None).isoformat())),
        ("RELO-W6.2", lambda p: p["cycles"][1].update(observed_at=p["cycles"][0]["observed_at"])),
        ("RELO-W6.2", lambda p: p["cycles"][0].update(quality_adjusted_minutes_saved="bad")),
        ("RELO-W6.2", lambda p: p["cycles"][0].pop("completion_rate")),
        ("RELO-W6.3", lambda p: p.update(telemetry_records=0, completed_routine_tasks=0)),
        ("RELO-W6.4", lambda p: p["comparisons"].__setitem__(0, "bad")),
        ("RELO-W6.4", lambda p: p["comparisons"][0]["rubric_scores"].update(factual_correctness=[])),
        ("RELO-W6.4", lambda p: p["comparisons"][0]["rubric_scores"]["factual_correctness"].update(candidate=3.0)),
        ("RELO-W6.5", lambda p: p["candidates"].__setitem__(0, "bad")),
        ("RELO-W6.5", lambda p: p["candidates"][0].update(production_loc=5001)),
        ("RELO-W6.5", lambda p: p["candidates"][0].pop("caller_inventory_ref")),
        ("RELO-W6.6", lambda p: p.update(temporary_binding_only_refs={})),
        ("RELO-W6.6", lambda p: p["canonical_mappings"].__setitem__(0, "bad")),
        ("RELO-W6.6", lambda p: p.update(closed_cards=["RELO-W1.1"], canonical_mappings=[])),
        ("RELO-W6.6", lambda p: p["canonical_mappings"][0].update(card_id="RELO-W9.1")),
        ("RELO-W6.7", lambda p: p["transitions"].__setitem__(0, "bad")),
        ("RELO-W6.7", lambda p: p["transitions"][0].update(proposal_class="", action_class="", evidence_window_ref="", owner="", owner_approval_ref="", rollback_ref="")),
        ("RELO-W6.8", lambda p: p["proposals"].__setitem__(0, "bad")),
        ("RELO-W6.8", lambda p: p["proposals"][0].update(status="proposed")),
        ("RELO-W6.9", lambda p: p["items"].__setitem__(0, "bad")),
        ("RELO-W6.9", lambda p: p.update(items=[{"binding_attempted": False, "eligible": False}], continuity_runs=[])),
        ("RELO-W6.10", lambda p: p["ratified_budgets"].pop("required_path_p50_s")),
        ("RELO-W6.10", lambda p: p["profile"].update(maximum_programs=1)),
        ("RELO-W6.10", lambda p: p["restore"].update(rpo_budget_s="bad", rto_budget_s="bad")),
        ("RELO-W6.11", lambda p: p["paths"].__setitem__(0, "bad")),
        ("RELO-W6.11", lambda p: p.update(paths=p["paths"][1:])),
        ("RELO-W6.11", lambda p: p["paths"][0].update(disposition="retained_governed", runtime_mutation_allowed=True, runtime_prompt_consumer=True)),
        ("RELO-W6.12", lambda p: p.update(halt_reason="", profile_id="", evidence={**p["evidence"], "product_vision_owner_decision_ref": ""})),
    ],
)
def test_invalid_packet_rows_and_missing_links_are_rejected(card_id: str, mutator) -> None:
    packet = deepcopy(_passing_packets()[card_id])
    mutator(packet)
    assert verify_w6_packet(packet).classification != "owner_review"


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_owner_disposition_is_explicit(status: str) -> None:
    packet = _passing_packets()["RELO-W6.3"]
    packet["owner_disposition"] = {
        "status": status,
        "approved_by": "owner" if status == "approved" else None,
        "approved_at": NOW.isoformat() if status == "approved" else None,
        "approval_ref": "approval-1" if status == "approved" else None,
    }
    result = verify_w6_packet(packet)
    assert result.classification == "owner_review"
    packet["owner_disposition"]["approved_at"] = "bad" if status == "approved" else None
    if status == "approved":
        assert verify_w6_packet(packet).classification == "not_qualified"


def test_w6_failure_paths_preserve_exact_evidence_diagnostics() -> None:
    w62 = _passing_packets()["RELO-W6.2"]
    w62["cycles"][0].pop("quality_adjusted_minutes_saved")
    w62["cycles"][0]["quality_passed"] = False
    w62["cycles"][0]["workflow_classes"] = "weekly_portfolio_review"
    result = verify_w6_packet(w62)
    assert "quality_gate" in " ".join(result.failed_gates)
    assert "cycles[0].workflow_classes" in result.invalid_fields

    w63 = _passing_packets()["RELO-W6.3"]
    w63["completed_routine_tasks"] = w63["eligible_routine_tasks"] + 1
    assert "completed_routine_tasks" in verify_w6_packet(w63).invalid_fields

    w66 = _passing_packets()["RELO-W6.6"]
    w66["canonical_mappings"][0]["card_id"] = w66["canonical_mappings"][1]["card_id"]
    assert "canonical_mappings[1].card_id" in verify_w6_packet(w66).invalid_fields

    w67 = _passing_packets()["RELO-W6.7"]
    w67["transitions"][0].pop("owner")
    assert "transitions[0].owner" in verify_w6_packet(w67).missing_fields

    w611 = _passing_packets()["RELO-W6.11"]
    w611["paths"][0]["disposition"] = "unknown"
    w611["paths"][0]["callers_inventoried"] = False
    learning = verify_w6_packet(w611)
    assert "paths[0].disposition" in learning.invalid_fields
    assert f"{w611['paths'][0]['path']}:caller_inventory" in learning.failed_gates


@pytest.mark.parametrize(
    ("state", "mutation", "expected_gate"),
    [
        ("ADVISORY_OPERATIONAL", lambda p: p["evidence"].update(engineering_passed=False), "advisory_operational_base"),
        ("ADVISORY_OPERATIONAL", lambda p: p["evidence"].update(
            independent_labeling_available=True,
            mandatory_vision_sources_available=True,
            scheduled_identity_available=True,
            autonomy_evidence_available=True,
        ), "advisory_requires_unavailable_full_authority_gate"),
        ("RELO_PROFILE_COMPLETE", lambda p: p["evidence"].update(fixed_targets_passed=False), "profile_complete_predicates"),
        ("RELO_PROFILE_COMPLETE", lambda p: p.update(profile_id=""), "profile_id"),
        ("HALTED", lambda p: p.pop("halt_reason", None), "halt_reason"),
        ("HALTED", lambda p: p["evidence"].update(containment_survives=False), "halt_disposition_predicates"),
    ],
)
def test_terminal_state_failure_predicates_are_explicit(
    state: str,
    mutation,
    expected_gate: str,
) -> None:
    packet = _passing_packets()["RELO-W6.12"]
    packet["requested_terminal_state"] = state
    if state == "ADVISORY_OPERATIONAL":
        packet["evidence"].update(
            engineering_passed=True,
            safety_passed=True,
            source_workflows_passed=True,
            operator_workflows_passed=True,
        )
    elif state == "RELO_PROFILE_COMPLETE":
        packet["profile_id"] = "profile-1"
        for field in (
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
            "independent_labeling_available",
        ):
            packet["evidence"][field] = True
    elif state == "HALTED":
        packet["halt_reason"] = "capacity unavailable"
        packet["evidence"]["inflight_state_drained"] = True
    mutation(packet)

    result = verify_w6_packet(packet)

    assert expected_gate in result.failed_gates or expected_gate in result.missing_fields


def test_packet_artifact_and_repository_link_failures_are_individualized(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    packet = _passing_packets()["RELO-W6.3"]
    packet["artifacts"] = [
        "bad",
        {
            "artifact_id": "escape",
            "sha256": "0" * 64,
            "path": "../outside.json",
        },
        {
            "artifact_id": "missing",
            "sha256": "0" * 64,
            "path": "missing.json",
        },
    ]
    result = verify_w6_packet(packet, repo_root=tmp_path)
    assert "artifacts[0]" in result.invalid_fields
    assert "artifacts[1].path" in result.invalid_fields
    assert "artifacts[2].path" in result.invalid_fields

    w65 = _passing_packets()["RELO-W6.5"]
    w65["candidates"].extend(["bad"])
    w65_result = verify_w6_packet(w65, repo_root=tmp_path)
    assert "candidates[0].path" in w65_result.invalid_fields

    w66 = _passing_packets()["RELO-W6.6"]
    w66["canonical_mappings"].append("bad")
    w66["canonical_mappings"][0]["evidence_ref"] = "missing"
    w66_result = verify_w6_packet(w66, repo_root=tmp_path)
    assert any("evidence_artifact" in gate for gate in w66_result.failed_gates)

    w611 = _passing_packets()["RELO-W6.11"]
    w611["paths"].append("bad")
    assert verify_w6_packet(w611, repo_root=tmp_path).classification != "owner_review"


def test_owner_and_nested_mapping_fields_are_fail_closed() -> None:
    packet = _passing_packets()["RELO-W6.3"]
    packet["owner_disposition"] = {"status": "approved"}
    result = verify_w6_packet(packet)
    assert "owner_disposition.approved_by" in result.missing_fields

    packet = _passing_packets()["RELO-W6.2"]
    packet["protocol"] = None
    result = verify_w6_packet(packet)
    assert "protocol" in result.missing_fields


def test_routing_evidence_counts_cache_separately_and_preserves_missing_population() -> None:
    start = NOW - timedelta(days=7)
    records = [
        {
            "recorded_at": (start + timedelta(days=1)).isoformat(),
            "chosen_tier": "local",
            "cache_hit": False,
            "frontier_called": False,
            "execution_class": "routing",
        },
        {
            "recorded_at": (start + timedelta(days=2)).isoformat(),
            "chosen_tier": "cache",
            "cache_hit": True,
            "frontier_called": False,
            "execution_class": "routing",
        },
        {
            "recorded_at": (start + timedelta(days=3)).isoformat(),
            "chosen_tier": "frontier",
            "cache_hit": False,
            "frontier_called": True,
            "execution_class": "routing",
        },
    ]

    evidence = build_w6_routing_evidence(
        records,
        window_start=start,
        window_end=NOW,
        eligible_routine_tasks=10,
        completed_routine_tasks=9,
        expected_tier_decisions=4,
        ratified_local_coverage_floor=0.25,
        ratified_routine_completion_floor=0.85,
    )

    assert evidence["deterministic_local_tasks"] == 1
    assert evidence["cache_reuse_tasks"] == 1
    assert evidence["remote_provider_tasks"] == 1
    assert evidence["certified_routing_frontier_calls"] == 1
    assert evidence["expected_unobserved_decisions"] == 1
    packet = _base("RELO-W6.3")
    packet.update(evidence)
    packet.pop("unclassified_selected_records")
    packet.pop("expected_unobserved_decisions")
    result = verify_w6_packet(packet)
    assert "routing_population_does_not_reconcile" in result.failed_gates
    assert "certified_routing_frontier_calls" in result.failed_gates


def test_uat_manifest_hashes_artifacts_without_private_copy(tmp_path: Path) -> None:
    artifacts: list[tuple[str, Path]] = []
    for profile in (
        "outlook_desktop",
        "outlook_web",
        "outlook_ios",
        "projector_1920x1080",
    ):
        path = tmp_path / f"{profile}.html"
        path.write_text(f"<p>{profile}</p>", encoding="utf-8")
        artifacts.append((profile, path))

    manifest = prepare_manifest(
        artifacts,
        edition="synthetic_weekly",
        issue_number=7,
    )
    checklist = render_checklist(manifest)

    assert manifest["missing_profiles"] == []
    assert all(entry["artifact_sha256"] != "" for entry in manifest["artifacts"])
    assert "private packet" in checklist
    assert "recipient" in " ".join(manifest["required_checks"]).lower()
    assert "file_path" not in json.dumps(manifest)
