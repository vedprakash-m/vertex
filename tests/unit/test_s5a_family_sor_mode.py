"""Unit tests for S-5a: per-family SoR mode resolution.

Gate: resolve_fact_mode_for_type(fact_type, state) + ProgramReality.family_sor_mode()
      correctly apply per-family overrides, falling back to program-level mode.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.fact_sor_state import FactSorState
from src.core.program_fact_store import resolve_fact_mode_for_type, _FACT_TYPE_TO_FAMILY


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_state(mode: str, family_modes: dict[str, str] | None = None) -> FactSorState:
    return FactSorState(
        mode=mode,
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        recorded_by="test",
        family_modes=family_modes or {},
    )


# ─── resolve_fact_mode_for_type ──────────────────────────────────────────────

class TestResolveFactModeForType:
    def test_none_state_returns_legacy(self) -> None:
        assert resolve_fact_mode_for_type("milestone.entry", None) == "legacy"

    def test_no_family_override_returns_program_mode(self) -> None:
        state = _make_state("shadow")
        assert resolve_fact_mode_for_type("milestone.entry", state) == "shadow"
        assert resolve_fact_mode_for_type("risk.entry", state) == "shadow"

    def test_family_override_applied_for_matching_fact_type(self) -> None:
        # Flip workitem.state family to primary while program is legacy
        state = _make_state("legacy", {"workitem.state": "primary"})
        assert resolve_fact_mode_for_type("milestone.entry", state) == "primary"
        assert resolve_fact_mode_for_type("workstream.entry", state) == "primary"
        assert resolve_fact_mode_for_type("action.item", state) == "primary"
        assert resolve_fact_mode_for_type("dependency.link", state) == "primary"

    def test_non_overridden_family_falls_back_to_program_mode(self) -> None:
        state = _make_state("legacy", {"workitem.state": "primary"})
        # commitment family not overridden → uses program-level "legacy"
        assert resolve_fact_mode_for_type("commitment.entry", state) == "legacy"
        assert resolve_fact_mode_for_type("risk.entry", state) == "legacy"

    def test_judgment_family_override(self) -> None:
        state = _make_state("shadow", {"judgment": "primary"})
        assert resolve_fact_mode_for_type("risk.entry", state) == "primary"
        assert resolve_fact_mode_for_type("decision.entry", state) == "primary"
        assert resolve_fact_mode_for_type("assumption.entry", state) == "primary"
        # other families still use shadow
        assert resolve_fact_mode_for_type("milestone.entry", state) == "shadow"

    def test_unknown_fact_type_falls_back_to_program_mode(self) -> None:
        state = _make_state("shadow", {"workitem.state": "primary"})
        # Unknown fact type has no family mapping → use program mode
        assert resolve_fact_mode_for_type("unknown.type", state) == "shadow"

    @pytest.mark.parametrize("fact_type", list(_FACT_TYPE_TO_FAMILY.keys()))
    def test_all_mapped_fact_types_are_resolvable(self, fact_type: str) -> None:
        state = _make_state("shadow")
        assert resolve_fact_mode_for_type(fact_type, state) == "shadow"


# ─── _FACT_TYPE_TO_FAMILY coverage ──────────────────────────────────────────

class TestFactTypeFamilyMapping:
    def test_milestone_is_workitem_state(self) -> None:
        assert _FACT_TYPE_TO_FAMILY["milestone.entry"] == "workitem.state"

    def test_risk_is_judgment(self) -> None:
        assert _FACT_TYPE_TO_FAMILY["risk.entry"] == "judgment"

    def test_commitment_is_commitment(self) -> None:
        assert _FACT_TYPE_TO_FAMILY["commitment.entry"] == "commitment"

    def test_claim_is_narrative(self) -> None:
        assert _FACT_TYPE_TO_FAMILY["claim.entry"] == "narrative"

    def test_mapping_keys_cover_known_storable_fact_types(self) -> None:
        expected = {
            "action.item", "dependency.link", "milestone.entry", "workstream.entry",
            "risk.entry", "decision.entry", "assumption.entry",
            "commitment.entry", "claim.entry",
        }
        assert expected.issubset(set(_FACT_TYPE_TO_FAMILY.keys()))


# ─── ProgramReality.family_sor_mode ─────────────────────────────────────────

class TestProgramRealityFamilySorMode:
    """Verify ProgramReality.family_sor_mode() resolves per-family modes correctly."""

    def _make_reality(self, sor_mode: str, family_modes: dict[str, str] | None = None):
        """Minimal ProgramReality instantiation to test family_sor_mode."""
        from datetime import datetime, timezone
        from src.core.program_reality import ProgramReality
        from src.core.program_fact_store import ProgramFactSnapshot

        snapshot = ProgramFactSnapshot(
            program_id="test",
            as_of=datetime.now(timezone.utc),
            facts=(),
        )
        return ProgramReality(
            program_id="test",
            snapshot=snapshot,
            sor_mode=sor_mode,
            as_of=snapshot.as_of,
            _entity_fact_index={},
            _actions=(),
            _risks=(),
            _decisions=(),
            _dependencies=(),
            _milestones=(),
            _assumptions=(),
            _workstreams=(),
            _claims=(),
            _family_sor_modes=family_modes or {},
        )

    def test_program_level_mode_returned_when_no_family_override(self) -> None:
        r = self._make_reality("shadow")
        assert r.family_sor_mode("workitem.state") == "shadow"
        assert r.family_sor_mode("judgment") == "shadow"

    def test_per_family_override_returned_when_set(self) -> None:
        r = self._make_reality("legacy", {"workitem.state": "primary"})
        assert r.family_sor_mode("workitem.state") == "primary"
        # Other families still use program-level legacy
        assert r.family_sor_mode("judgment") == "legacy"
        assert r.family_sor_mode("commitment") == "legacy"

    def test_unknown_family_falls_back_to_program_mode(self) -> None:
        r = self._make_reality("shadow", {"workitem.state": "primary"})
        assert r.family_sor_mode("unknown_family") == "shadow"

    def test_flip_family_changes_mode_independently(self) -> None:
        """Core S-5a gate: flip a family → that family's read source changes."""
        legacy_r = self._make_reality("legacy")
        primary_r = self._make_reality("legacy", {"workitem.state": "primary"})

        # Same program-level mode
        assert legacy_r.sor_mode == primary_r.sor_mode == "legacy"
        # But workitem.state family differs
        assert legacy_r.family_sor_mode("workitem.state") == "legacy"
        assert primary_r.family_sor_mode("workitem.state") == "primary"
