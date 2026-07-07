"""CI-only DM gate registry anchors.

These gates are defined by contract tests rather than runtime GateEvaluation
calls, so the QG registry needs explicit file/function anchors for:

- QG-DM-2 Projection determinism
- QG-DM-3 Operator-correction coverage
- QG-DM-8 Source-ref completeness
- QG-DM-9 Backfill batch acceptance (entity-resolution ≥ 90%, spot-check sample, no lock conflicts)
- QG-DM-11 Knowledge-context determinism
- QG-DM-12 Self-containment
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _assert_file_contains(relative_path: str, *needles: str) -> None:
    path = REPO_ROOT / relative_path
    source = path.read_text(encoding="utf-8")
    for needle in needles:
        assert needle in source, f"{relative_path} is missing expected anchor {needle!r}"


def test_qg_dm_2_projection_determinism_contract_anchors_exist() -> None:
    _assert_file_contains(
        "tests/golden/test_ledger_projection.py",
        "test_qg_dm_2_projection_golden",
        "test_qg_dm_2_bitemporal_slices_golden",
        "program_golden_log.jsonl",
    )
    _assert_file_contains(
        "tests/unit/test_ledger_program_views.py",
        "test_projection_order_independence",
        "test_replay_idempotence",
        "canonical_projection_dump",
    )


def test_qg_dm_3_operator_correction_coverage_contract_anchors_exist() -> None:
    _assert_file_contains(
        "tests/unit/test_ledger_projection_logic.py",
        "test_choose_field_winner_respects_confidence_then_time_then_source_priority",
        "test_apply_supersession_handles_correction_chain",
    )
    _assert_file_contains(
        "tests/unit/test_ledger_field_locks.py",
        "test_field_lock_pins_supported_field",
        "test_field_lock_expires_by_as_of",
    )
    _assert_file_contains(
        "tests/contracts/test_baseline_immutability_contract.py",
        "test_write_confirmed_refuses_locked_and_trusted_snapshots",
        "test_hardlock_guard_is_wired_into_both_write_paths",
    )


def test_qg_dm_8_source_ref_completeness_contract_anchors_exist() -> None:
    _assert_file_contains(
        "tests/contracts/test_source_ref_policy_contract.py",
        "test_all_runtime_source_ref_variants_pass_shared_validator_when_valid",
        "test_all_runtime_source_ref_variants_cover_event_candidate_and_claim_write_paths",
    )


def test_qg_dm_9_backfill_batch_acceptance_contract_anchors_exist() -> None:
    """QG-DM-9: A backfill batch may be promoted only if entity-resolution ≥ 90%,
    spot-check sample approved, and no unresolved lock conflicts (S7 acceptance criterion)."""
    _assert_file_contains(
        "src/commands/ledger.py",
        "entity_resolution_gate",
        "entity_resolution_rate >= 0.9",
        "sample_gate",
        "required_sample_count",
        "lock_conflict_gate",
    )
    _assert_file_contains(
        "tests/unit/test_commands_ledger.py",
        "entity_resolution_rate",
        "test_ledger_quarantine_batch_rejects_pending_batch",
    )


def test_qg_dm_11_knowledge_context_determinism_contract_anchors_exist() -> None:
    _assert_file_contains(
        "tests/golden/test_knowledge_context.py",
        "test_qg_dm_11_knowledge_context_golden",
        "QG-DM-11",
    )
    _assert_file_contains(
        "tests/unit/test_knowledge_claim_store.py",
        "test_resolve_knowledge_context_prefers_confidence_before_scope",
        "test_resolve_knowledge_context_retains_wider_override_annotations_and_tombstones",
    )


def test_qg_dm_12_self_containment_contract_anchors_exist() -> None:
    _assert_file_contains(
        "tests/contracts/test_source_ref_policy_contract.py",
        "test_all_external_origin_runtime_variants_fail_shared_validator_without_vault_hash",
    )
    _assert_file_contains(
        "tests/unit/test_commands_doctor.py",
        "test_ledger_health_check_fails_on_external_origin_ref_without_vault_hash",
    )