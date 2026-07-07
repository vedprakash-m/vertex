"""S-5c: Clean-cycle flip gate + rollback contract tests.

Gate: "rollback contract test; thresholds read from validated sor_flip config"

Verifies that:
1. A family in shadow mode flips to primary after clean_cycles_to_flip consecutive
   clean cycles (zero divergence when critical_zero=True).
2. A family in primary mode rolls back to shadow if divergence exceeds the tolerance.
3. require_s0g_policy=True blocks the flip even when all other gates are met.
4. Thresholds are read from the validated sor_flip config (not hardcoded).
5. Counter resets to 0 on any dirty cycle.
6. The rollback checkpoint is written atomically (fact_store_sor.yaml updated).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.fact_sor_state import (
    FamilyFlipResult,
    evaluate_family_flip_gate,
    load_fact_sor_state,
    load_family_clean_cycles,
    save_fact_sor_state,
)
from src.core.truth_model import SorFlipFamilyConfig


_TS = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)

_POLICY_ADMITS = SorFlipFamilyConfig(
    clean_cycles_to_flip=3,
    divergence_tolerance=0.02,
    critical_zero=True,
    max_persistent_cycles=8,
    require_s0g_policy=False,  # S-0g accepted
)

_POLICY_BLOCKED = SorFlipFamilyConfig(
    clean_cycles_to_flip=3,
    divergence_tolerance=0.02,
    critical_zero=True,
    max_persistent_cycles=8,
    require_s0g_policy=True,  # Still blocked (e.g. judgment)
)


def _setup_shadow(prog: str, family: str, programs_root) -> None:
    """Put a program in shadow mode for the given family."""
    save_fact_sor_state(
        prog,
        mode="legacy",
        recorded_at=_TS,
        family_modes={family: "shadow"},
        programs_root=programs_root,
    )


def _setup_primary(prog: str, family: str, programs_root) -> None:
    """Put a program in primary mode for the given family."""
    save_fact_sor_state(
        prog,
        mode="legacy",
        recorded_at=_TS,
        family_modes={family: "primary"},
        programs_root=programs_root,
    )


class TestCleanCycleFlipGate:

    def test_flip_occurs_after_threshold_clean_cycles(self, tmp_path) -> None:
        """Shadow → primary after clean_cycles_to_flip=3 consecutive zero-divergence cycles."""
        programs_root = tmp_path / "programs"
        _setup_shadow("prog", "workitem.state", programs_root)

        results = []
        for _ in range(3):
            r = evaluate_family_flip_gate(
                "prog", "workitem.state",
                divergence_count=0,
                total_entities_in_family=0,
                sor_flip_config=_POLICY_ADMITS,
                recorded_at=_TS,
                programs_root=programs_root,
            )
            results.append(r)
            # Reload state between cycles (simulate separate cycle runs)

        assert results[-1].action == "flipped_to_primary"
        assert results[-1].new_mode == "primary"
        assert results[-1].previous_mode == "shadow"
        assert results[-1].clean_cycles == 3

        # Verify fact_store_sor.yaml was updated
        state = load_fact_sor_state("prog", programs_root=programs_root)
        assert state is not None
        assert state.family_modes.get("workitem.state") == "primary"

    def test_no_flip_before_threshold(self, tmp_path) -> None:
        """Only 2 clean cycles (threshold=3) → stays in shadow."""
        programs_root = tmp_path / "programs"
        _setup_shadow("prog", "workitem.state", programs_root)

        for _ in range(2):
            r = evaluate_family_flip_gate(
                "prog", "workitem.state",
                divergence_count=0,
                total_entities_in_family=0,
                sor_flip_config=_POLICY_ADMITS,
                recorded_at=_TS,
                programs_root=programs_root,
            )

        assert r.action == "no_change"
        state = load_fact_sor_state("prog", programs_root=programs_root)
        assert state.family_modes.get("workitem.state") == "shadow"

    def test_dirty_cycle_resets_counter(self, tmp_path) -> None:
        """2 clean + 1 dirty + 1 clean → counter=1 (not 3), no flip."""
        programs_root = tmp_path / "programs"
        _setup_shadow("prog", "workitem.state", programs_root)

        # 2 clean cycles
        for _ in range(2):
            evaluate_family_flip_gate(
                "prog", "workitem.state",
                divergence_count=0, total_entities_in_family=0,
                sor_flip_config=_POLICY_ADMITS, recorded_at=_TS, programs_root=programs_root,
            )
        # 1 dirty cycle
        dirty = evaluate_family_flip_gate(
            "prog", "workitem.state",
            divergence_count=5, total_entities_in_family=0,
            sor_flip_config=_POLICY_ADMITS, recorded_at=_TS, programs_root=programs_root,
        )
        assert dirty.action == "no_change"
        # Counter reset to 0
        cycles = load_family_clean_cycles("prog", programs_root=programs_root)
        assert cycles.get("workitem.state", 0) == 0

        # 1 more clean — still only 1 clean cycle, no flip
        r = evaluate_family_flip_gate(
            "prog", "workitem.state",
            divergence_count=0, total_entities_in_family=0,
            sor_flip_config=_POLICY_ADMITS, recorded_at=_TS, programs_root=programs_root,
        )
        assert r.action == "no_change"
        assert r.clean_cycles == 1

    def test_policy_gate_blocks_flip(self, tmp_path) -> None:
        """require_s0g_policy=True blocks flip even after threshold clean cycles."""
        programs_root = tmp_path / "programs"
        _setup_shadow("prog", "judgment", programs_root)

        for _ in range(5):  # more than threshold=3
            r = evaluate_family_flip_gate(
                "prog", "judgment",
                divergence_count=0, total_entities_in_family=0,
                sor_flip_config=_POLICY_BLOCKED, recorded_at=_TS, programs_root=programs_root,
            )

        assert r.action == "no_change"
        assert "require_s0g_policy" in r.reason
        state = load_fact_sor_state("prog", programs_root=programs_root)
        assert state.family_modes.get("judgment") == "shadow"


class TestRollbackCheckpoint:

    def test_primary_family_rolls_back_on_divergence(self, tmp_path) -> None:
        """S-5c rollback: primary → shadow when divergence > 0 (critical_zero=True)."""
        programs_root = tmp_path / "programs"
        _setup_primary("prog", "commitment", programs_root)

        r = evaluate_family_flip_gate(
            "prog", "commitment",
            divergence_count=1,  # any non-zero divergence
            total_entities_in_family=10,
            sor_flip_config=_POLICY_ADMITS,
            recorded_at=_TS,
            programs_root=programs_root,
        )

        assert r.action == "rolled_back_to_shadow"
        assert r.previous_mode == "primary"
        assert r.new_mode == "shadow"
        assert r.clean_cycles == 0

        # Verify YAML was updated
        state = load_fact_sor_state("prog", programs_root=programs_root)
        assert state.family_modes.get("commitment") == "shadow"

    def test_rollback_resets_counter(self, tmp_path) -> None:
        """Counter returns to 0 after a rollback."""
        programs_root = tmp_path / "programs"
        _setup_primary("prog", "commitment", programs_root)

        evaluate_family_flip_gate(
            "prog", "commitment",
            divergence_count=1, total_entities_in_family=0,
            sor_flip_config=_POLICY_ADMITS, recorded_at=_TS, programs_root=programs_root,
        )

        cycles = load_family_clean_cycles("prog", programs_root=programs_root)
        assert cycles.get("commitment", 0) == 0

    def test_primary_with_zero_divergence_no_rollback(self, tmp_path) -> None:
        """Primary mode with zero divergence stays primary — no spurious rollback."""
        programs_root = tmp_path / "programs"
        _setup_primary("prog", "workitem.state", programs_root)

        r = evaluate_family_flip_gate(
            "prog", "workitem.state",
            divergence_count=0, total_entities_in_family=0,
            sor_flip_config=_POLICY_ADMITS, recorded_at=_TS, programs_root=programs_root,
        )

        assert r.action == "no_change"
        state = load_fact_sor_state("prog", programs_root=programs_root)
        assert state.family_modes.get("workitem.state") == "primary"


class TestThresholdsFromConfig:

    def test_custom_threshold_is_respected(self, tmp_path) -> None:
        """clean_cycles_to_flip=7 requires 7 clean cycles before flip."""
        programs_root = tmp_path / "programs"
        _setup_shadow("prog", "workitem.state", programs_root)

        strict_cfg = SorFlipFamilyConfig(
            clean_cycles_to_flip=7,
            divergence_tolerance=0.02,
            critical_zero=True,
            max_persistent_cycles=8,
            require_s0g_policy=False,
        )

        for i in range(6):
            r = evaluate_family_flip_gate(
                "prog", "workitem.state",
                divergence_count=0, total_entities_in_family=0,
                sor_flip_config=strict_cfg, recorded_at=_TS, programs_root=programs_root,
            )
        # 6 cycles, threshold=7 → still no flip
        assert r.action == "no_change"
        assert r.clean_cycles == 6

        # Cycle 7 → flip
        r7 = evaluate_family_flip_gate(
            "prog", "workitem.state",
            divergence_count=0, total_entities_in_family=0,
            sor_flip_config=strict_cfg, recorded_at=_TS, programs_root=programs_root,
        )
        assert r7.action == "flipped_to_primary"

    def test_legacy_family_not_evaluated(self, tmp_path) -> None:
        """A family in legacy mode does not flip and returns no_change."""
        programs_root = tmp_path / "programs"
        # Legacy mode = no family_modes entry, program mode = legacy
        save_fact_sor_state(
            "prog", mode="legacy", recorded_at=_TS, programs_root=programs_root
        )

        r = evaluate_family_flip_gate(
            "prog", "workitem.state",
            divergence_count=0, total_entities_in_family=0,
            sor_flip_config=_POLICY_ADMITS, recorded_at=_TS, programs_root=programs_root,
        )
        # legacy mode → no_change (not in shadow or primary path)
        assert r.action == "no_change"
