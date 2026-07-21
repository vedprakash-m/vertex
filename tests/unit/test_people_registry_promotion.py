"""PPL-W2B.6 per-program promotion and rollback coverage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_registry_identity import (
    bootstrap_registry_identity,
    load_registry_config,
    load_registry_manifest,
    registry_manifest_path,
    write_registry_manifest,
)
from src.core.people_registry_modes import rollback_program_mode, set_program_mode
from src.core.people_registry_promotion import (
    PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED,
    PROGRAM_PROMOTION_REQUIRED_CONSUMERS,
    ProgramPromotionConsumerEvidence,
    ProgramPromotionCycleEvidence,
    load_program_promotion_state,
    program_promotion_state_path,
    program_promotion_status,
    record_program_promotion_cycle,
    record_program_rollback_restore_drill,
)
from src.core.people_registry_storage_class import RegistryStorageQualification

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _bootstrap(knowledge_root: Path) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="synthetic", apply=True, as_of=_NOW)


def _generation(knowledge_root: Path) -> str:
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    return manifest.generation_id


def _evidence(
    generation_id: str,
    *,
    parity_divergence_count: int = 0,
    critical_conflicts: int = 0,
    consumer: str | None = None,
    consumer_succeeded: bool = True,
) -> ProgramPromotionCycleEvidence:
    return ProgramPromotionCycleEvidence(
        generation_id=generation_id,
        load_succeeded=True,
        load_generation_id=generation_id,
        consumers=tuple(
            ProgramPromotionConsumerEvidence(
                consumer=consumer_name,
                generation_id=generation_id,
                succeeded=consumer_succeeded if consumer_name == consumer else True,
            )
            for consumer_name in PROGRAM_PROMOTION_REQUIRED_CONSUMERS
        ),
        parity_divergence_count=parity_divergence_count,
        unresolved_critical_identity_conflicts=critical_conflicts,
        nfr_compliant=True,
    )


def _prepare_shadow_program(knowledge_root: Path, program_id: str) -> str:
    set_program_mode(knowledge_root, program_id, "shadow", actor="operator")
    generation_id = _generation(knowledge_root)
    record_program_rollback_restore_drill(
        knowledge_root,
        program_id,
        generation_id=generation_id,
        restore_verified=True,
        recorded_at=_NOW,
    )
    return generation_id


def _record_clean_cycles(knowledge_root: Path, program_id: str, generation_id: str, count: int) -> None:
    for cycle in range(count):
        result = record_program_promotion_cycle(
            knowledge_root,
            program_id,
            _evidence(generation_id),
            recorded_at=_NOW.replace(minute=cycle),
        )
    assert result.clean_cycles == count


def test_five_clean_cycles_make_only_that_shadow_program_eligible_for_primary(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    generation_id = _prepare_shadow_program(knowledge_root, "acme")

    _record_clean_cycles(knowledge_root, "acme", generation_id, PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED)

    status = program_promotion_status(knowledge_root, "acme")
    assert status.ready_to_promote is True
    updated = set_program_mode(knowledge_root, "acme", "primary", actor="operator")
    assert updated.program_mode("acme") == "primary"
    assert updated.program_mode("fabrikam") == "legacy"
    assert program_promotion_state_path(knowledge_root, "acme").exists()
    assert not program_promotion_state_path(knowledge_root, "acme").with_suffix(".json.tmp").exists()


def test_parity_consumer_and_conflict_failures_reset_the_clean_cycle_counter(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    generation_id = _prepare_shadow_program(knowledge_root, "acme")
    _record_clean_cycles(knowledge_root, "acme", generation_id, 2)

    parity = record_program_promotion_cycle(
        knowledge_root,
        "acme",
        _evidence(generation_id, parity_divergence_count=1),
        recorded_at=_NOW,
    )
    assert parity.action == "reset"
    assert parity.clean_cycles == 0
    assert "parity" in parity.reason

    _record_clean_cycles(knowledge_root, "acme", generation_id, 1)
    consumer = record_program_promotion_cycle(
        knowledge_root,
        "acme",
        _evidence(generation_id, consumer="nudge", consumer_succeeded=False),
        recorded_at=_NOW,
    )
    assert consumer.clean_cycles == 0
    assert "nudge consumer failed" in consumer.reason

    _record_clean_cycles(knowledge_root, "acme", generation_id, 1)
    conflict = record_program_promotion_cycle(
        knowledge_root,
        "acme",
        _evidence(generation_id, critical_conflicts=1),
        recorded_at=_NOW,
    )
    assert conflict.clean_cycles == 0
    assert "critical identity conflict" in conflict.reason


def test_generation_change_resets_evidence_and_invalidates_rollback_drill(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    generation_id = _prepare_shadow_program(knowledge_root, "acme")
    _record_clean_cycles(knowledge_root, "acme", generation_id, 2)

    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    updated_manifest = replace(
        manifest,
        generation_id="registry-generation-new",
        prior_generation=manifest.generation_id,
        committed_at=_NOW,
    )
    write_registry_manifest(registry_manifest_path(knowledge_root), updated_manifest)

    result = record_program_promotion_cycle(
        knowledge_root,
        "acme",
        _evidence(updated_manifest.generation_id),
        recorded_at=_NOW,
    )

    assert result.action == "reset"
    assert result.clean_cycles == 0
    assert "generation changed" in result.reason
    assert "rollback/restore drill" in result.reason


def test_primary_cannot_be_a_direct_jump_and_requires_a_current_restore_drill(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    with pytest.raises(ConfigError, match="must be in 'shadow'"):
        set_program_mode(knowledge_root, "acme", "primary", actor="operator")

    set_program_mode(knowledge_root, "acme", "shadow", actor="operator")
    generation_id = _generation(knowledge_root)
    for _ in range(PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED):
        result = record_program_promotion_cycle(knowledge_root, "acme", _evidence(generation_id), recorded_at=_NOW)

    assert result.clean_cycles == 0
    assert "rollback/restore drill" in result.reason
    with pytest.raises(ConfigError, match="five-clean-cycle gate"):
        set_program_mode(knowledge_root, "acme", "primary", actor="operator")


def test_nfr_storage_and_manifest_requirements_reset_the_gate(monkeypatch, tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    generation_id = _prepare_shadow_program(knowledge_root, "acme")

    nfr_evidence = replace(_evidence(generation_id), nfr_compliant=False)
    nfr_result = record_program_promotion_cycle(knowledge_root, "acme", nfr_evidence, recorded_at=_NOW)
    assert nfr_result.clean_cycles == 0
    assert "NFR" in nfr_result.reason

    monkeypatch.setattr(
        "src.core.people_registry_promotion.qualify_registry_storage",
        lambda _root: RegistryStorageQualification(
            storage_class="unsupported_sync",
            qualified_for_primary=False,
            detail="synthetic unsupported storage",
            checked_at=_NOW,
        ),
    )
    storage_result = record_program_promotion_cycle(knowledge_root, "acme", _evidence(generation_id), recorded_at=_NOW)
    assert storage_result.clean_cycles == 0
    assert "storage is not qualified" in storage_result.reason

    monkeypatch.undo()
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    write_registry_manifest(
        registry_manifest_path(knowledge_root),
        replace(manifest, source_hashes=(("people_directory.yaml", "sha256:missing"),)),
    )
    manifest_result = record_program_promotion_cycle(knowledge_root, "acme", _evidence(generation_id), recorded_at=_NOW)
    assert manifest_result.clean_cycles == 0
    assert "manifest integrity" in manifest_result.reason


def test_program_promotion_state_is_isolated_per_program(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    acme_generation = _prepare_shadow_program(knowledge_root, "acme")
    fabrikam_generation = _prepare_shadow_program(knowledge_root, "fabrikam")

    _record_clean_cycles(knowledge_root, "acme", acme_generation, PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED)
    _record_clean_cycles(knowledge_root, "fabrikam", fabrikam_generation, 1)

    set_program_mode(knowledge_root, "acme", "primary", actor="operator")
    assert load_registry_config(knowledge_root).program_mode("fabrikam") == "shadow"  # type: ignore[union-attr]
    assert load_program_promotion_state(knowledge_root, "fabrikam").clean_cycles == 1  # type: ignore[union-attr]
    with pytest.raises(ConfigError, match="five-clean-cycle gate"):
        set_program_mode(knowledge_root, "fabrikam", "primary", actor="operator")


def test_independent_rollback_resets_only_mode_metadata_and_cycles(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    generation_id = _prepare_shadow_program(knowledge_root, "acme")
    _record_clean_cycles(knowledge_root, "acme", generation_id, PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED)
    set_program_mode(knowledge_root, "acme", "primary", actor="operator")

    updated = rollback_program_mode(knowledge_root, "acme", target_mode="legacy", actor="operator")

    assert updated.program_mode("acme") == "legacy"
    assert load_program_promotion_state(knowledge_root, "acme").clean_cycles == 0  # type: ignore[union-attr]
    assert not (knowledge_root / "registry_synthetic_records.yaml").exists()
