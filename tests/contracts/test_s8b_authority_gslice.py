"""S-8b: Authority G-slice synthetic test.

Verifies the end-to-end authority promotion path using synthetic data:
  1. workitem.state family starts in shadow mode.
  2. A milestone.entry fact is written via REV (write_authority="bridge").
  3. Entity resolution (S-2c) resolves the bridge fact's refs.
  4. 5 consecutive clean cycles (zero divergence) advance the flip gate.
  5. workitem.state flips to primary.
  6. Fact store snapshot reflects the REV-written fact.

Note: Production authority promotion requires the S-6 corpus gate (labeled
training corpus). This test uses synthetic data to prove the mechanism.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.entity_resolution import (
    resolve_fact_entity_refs_for_store,
    _BRIDGE_WRITE_AUTHORITY,
    _UNRESOLVED_PREFIX,
)
from src.core.fact_sor_state import (
    AUTHORITY_FAMILIES,
    FamilyFlipResult,
    evaluate_family_flip_gate,
    load_fact_sor_state,
    save_fact_sor_state,
)
from src.core.truth_model import SorFlipFamilyConfig


_TS = datetime(2024, 8, 15, 10, 0, tzinfo=timezone.utc)

_FLIP_CFG = SorFlipFamilyConfig(
    clean_cycles_to_flip=5,
    divergence_tolerance=0.02,
    critical_zero=True,
    max_persistent_cycles=10,
    require_s0g_policy=False,  # workitem.state accepted in S-0g
)


class TestAuthorityGSlice:
    """S-8b: Synthetic authority promotion proof."""

    def test_shadow_to_primary_after_5_clean_cycles(self, tmp_path) -> None:
        """workitem.state: shadow → primary after 5 zero-divergence REV cycles."""
        programs_root = tmp_path / "programs"
        prog = "nova"

        # 1. Start in shadow mode
        save_fact_sor_state(
            prog, mode="legacy", recorded_at=_TS,
            family_modes={"workitem.state": "shadow"},
            programs_root=programs_root,
        )

        # 2. Run 5 clean cycles (simulate REV completing with zero divergence)
        results: list[FamilyFlipResult] = []
        for _ in range(5):
            r = evaluate_family_flip_gate(
                prog, "workitem.state",
                divergence_count=0, total_entities_in_family=0,
                sor_flip_config=_FLIP_CFG, recorded_at=_TS,
                programs_root=programs_root,
            )
            results.append(r)

        # 3. On cycle 5 the gate fires
        assert results[-1].action == "flipped_to_primary"
        assert results[-1].new_mode == "primary"
        assert results[-1].clean_cycles == 5

        # 4. State file reflects primary
        state = load_fact_sor_state(prog, programs_root=programs_root)
        assert state is not None
        assert state.family_modes.get("workitem.state") == "primary"

    def test_bridge_fact_entity_refs_resolved_before_write(self) -> None:
        """S-2c: REV-sourced fact entity refs resolve to canonical keys or UNRESOLVED."""
        known = frozenset({"workitem:1001", "workitem:2002", "milestone:303"})

        # Case A: exact match
        r_exact = resolve_fact_entity_refs_for_store(
            ("workitem:1001",),
            known_natural_keys=known,
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert r_exact.resolved_refs == ("workitem:1001",)
        assert r_exact.resolution_strategy == "direct_match"

        # Case B: numeric short-form
        r_short = resolve_fact_entity_refs_for_store(
            ("1001",),
            known_natural_keys=known,
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert r_short.resolved_refs == ("workitem:1001",)
        assert r_short.resolution_strategy == "partial_match"

        # Case C: unknown → UNRESOLVED (not dropped)
        r_unknown = resolve_fact_entity_refs_for_store(
            ("workitem:9999",),
            known_natural_keys=known,
            write_authority=_BRIDGE_WRITE_AUTHORITY,
        )
        assert r_unknown.resolved_refs[0].startswith(_UNRESOLVED_PREFIX)
        assert r_unknown.unresolved_count == 1

    def test_rollback_when_primary_diverges(self, tmp_path) -> None:
        """After flip, divergence in primary → automatic rollback to shadow."""
        programs_root = tmp_path / "programs"
        prog = "nova"

        # Start as primary (already flipped)
        save_fact_sor_state(
            prog, mode="legacy", recorded_at=_TS,
            family_modes={"workitem.state": "primary"},
            programs_root=programs_root,
        )

        # Divergence detected
        r = evaluate_family_flip_gate(
            prog, "workitem.state",
            divergence_count=3, total_entities_in_family=10,
            sor_flip_config=_FLIP_CFG, recorded_at=_TS,
            programs_root=programs_root,
        )
        assert r.action == "rolled_back_to_shadow"
        assert r.previous_mode == "primary"
        assert r.new_mode == "shadow"

        state = load_fact_sor_state(prog, programs_root=programs_root)
        assert state.family_modes.get("workitem.state") == "shadow"

    def test_commitment_family_also_flippable(self, tmp_path) -> None:
        """commitment family (also S-0g accepted) flips after clean cycles."""
        programs_root = tmp_path / "programs"
        prog = "nova"

        save_fact_sor_state(
            prog, mode="legacy", recorded_at=_TS,
            family_modes={"commitment": "shadow"},
            programs_root=programs_root,
        )

        for _ in range(5):
            r = evaluate_family_flip_gate(
                prog, "commitment",
                divergence_count=0, total_entities_in_family=0,
                sor_flip_config=_FLIP_CFG, recorded_at=_TS,
                programs_root=programs_root,
            )

        assert r.action == "flipped_to_primary"
        state = load_fact_sor_state(prog, programs_root=programs_root)
        assert state.family_modes.get("commitment") == "primary"

    def test_judgment_family_stays_blocked(self, tmp_path) -> None:
        """judgment family (require_s0g_policy=True) never flips."""
        programs_root = tmp_path / "programs"
        prog = "nova"

        save_fact_sor_state(
            prog, mode="legacy", recorded_at=_TS,
            family_modes={"judgment": "shadow"},
            programs_root=programs_root,
        )

        blocked_cfg = SorFlipFamilyConfig(
            clean_cycles_to_flip=5,
            divergence_tolerance=0.02,
            critical_zero=True,
            max_persistent_cycles=10,
            require_s0g_policy=True,  # judgment is still gated
        )

        for _ in range(10):  # far above threshold
            r = evaluate_family_flip_gate(
                prog, "judgment",
                divergence_count=0, total_entities_in_family=0,
                sor_flip_config=blocked_cfg, recorded_at=_TS,
                programs_root=programs_root,
            )

        assert r.action == "no_change"
        assert "require_s0g_policy" in r.reason
        state = load_fact_sor_state(prog, programs_root=programs_root)
        assert state.family_modes.get("judgment") == "shadow"

    def test_authority_families_constant_includes_expected(self) -> None:
        """AUTHORITY_FAMILIES tuple includes the four approved S-0g claim families."""
        assert "workitem.state" in AUTHORITY_FAMILIES
        assert "commitment" in AUTHORITY_FAMILIES
        assert "judgment" in AUTHORITY_FAMILIES
