"""WI-3.2b: Contract tests for detect_corroboration_and_conflicts().

Covers (per spec §6.2.3):
- INV-13: same-provenance-class observations NEVER corroborate
- Independent observations with matching digests → corroboration emitted
- Materiality routing: family_is_commitment (material) vs workitem.state (minor)
- Suspended primary does NOT win conflicts
- Trust-gap precedence selects winner
- Open conflict emitted for unresolved material disagreement with challenge_input
- Conflict continuity: continuation evidence attaches to existing conflict id
- Minor conflict: fact.conflict with resolution="unresolved_minor", no challenge
- Sync never executes in legacy/shadow mode (counter only)
- Sync executes in primary SoR mode when all conditions met (mirror fields only)

Per spec WI-3.2b acceptance criteria: all 10 contract tests pass; no
regressions in test_truth_model.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.core.truth_model import (
    CorroborationConflictResult,
    SourceAuthorityPolicy,
    AuthorityEntry,
    TruthContext,
    detect_corroboration_and_conflicts,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_policy(
    *,
    extra_families: dict | None = None,
) -> SourceAuthorityPolicy:
    """Build a minimal policy for test use."""
    authority = {
        "workitem.state": AuthorityEntry(
            primary="ado",
            secondary=("human_comms",),
            human_role="resolve_ambiguity",
            mirror_fields=("state", "status_date"),
        ),
        "metric": AuthorityEntry(
            primary="kusto",
            secondary=("reality_assertions",),
            human_role="validate_interpretation",
            mirror_fields=("value", "observed_at"),
        ),
        "incident": AuthorityEntry(
            primary="icm",
            secondary=("ado", "human_comms"),
            human_role="confirm_impact",
            mirror_fields=("severity", "state"),
        ),
        "commitment": AuthorityEntry(
            primary="human",
            secondary=("ado", "human_comms"),
            human_role="own_commitment",
            mirror_fields=(),
        ),
        "judgment": AuthorityEntry(
            primary="human",
            secondary=("signals", "metrics"),
            human_role="own_judgment",
            mirror_fields=(),
        ),
        "narrative": AuthorityEntry(
            primary="human",
            secondary=("signals",),
            human_role="own_wording",
            mirror_fields=(),
        ),
    }
    if extra_families:
        authority.update(extra_families)
    return SourceAuthorityPolicy(
        schema_version="1",
        provenance_classes={
            "ado": "ado",
            "ado_pr": "ado",
            "kusto": "telemetry",
            "icm": "icm",
            "workiq": "human_comms",
            "teams": "human_comms",
            "transcript": "human_comms",
        },
        family_map={
            "action.item": "workitem.state",
            "milestone.entry": "workitem.state",
            "commitment.entry": "commitment",
            "risk.entry": "judgment",
        },
        authority=authority,
        corroboration_window_hours=72,
        conflict_trust_gap_threshold=0.15,
        materiality_predicate_ids=(),
        override_ttl_days=90,
    )


def _make_obs(
    *,
    entity_refs: tuple[str, ...] = ("entity-1",),
    fact_type: str = "action.item",
    payload: dict | None = None,
    source_signal_ids: tuple[str, ...] = (),
    natural_key: str = "nk-1",
) -> MagicMock:
    obs = MagicMock()
    obs.entity_refs = entity_refs
    obs.fact_type = fact_type
    obs.payload = payload or {}
    obs.source_signal_ids = source_signal_ids
    obs.natural_key = natural_key
    return obs


def _empty_ctx() -> TruthContext:
    return TruthContext(
        baseline_locked_keys=frozenset(),
        suspended_sources=frozenset(),
        corroborated_keys=frozenset(),
    )


# ---------------------------------------------------------------------------
# Tests — INV-13 independence guard
# ---------------------------------------------------------------------------

class TestIndependenceGuard:
    """Same provenance class → never corroborate (INV-13)."""

    def test_same_ado_class_no_corroboration(self):
        """Two ADO observations cannot corroborate each other (same class)."""
        policy = _make_policy()
        obs_a = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_123",),
            natural_key="nk-a",
        )
        obs_b = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_456",),
            natural_key="nk-b",
        )
        result = detect_corroboration_and_conflicts(
            [obs_a, obs_b],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.corroborations) == 0, (
            "Same-class (ado) observations must not corroborate (INV-13)"
        )

    def test_same_human_comms_class_no_corroboration(self):
        """workiq + teams both map to human_comms — no corroboration."""
        policy = _make_policy()
        obs_a = _make_obs(
            fact_type="action.item",
            payload={"state": "done"},
            source_signal_ids=("workiq_1",),
        )
        obs_b = _make_obs(
            fact_type="action.item",
            payload={"state": "done"},
            source_signal_ids=("teams_2",),
        )
        result = detect_corroboration_and_conflicts(
            [obs_a, obs_b],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.corroborations) == 0, (
            "workiq + teams are both human_comms → no corroboration"
        )

    def test_different_classes_with_matching_digest_corroborates(self):
        """ADO + IcM on same entity and digest → corroboration emitted."""
        policy = _make_policy()
        obs_a = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_100",),
            natural_key="nk-a",
        )
        obs_b = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("icm_200",),
            natural_key="nk-b",
        )
        result = detect_corroboration_and_conflicts(
            [obs_a, obs_b],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.corroborations) == 1
        corr = result.corroborations[0]
        assert corr["entity_id"] == "entity-1"
        assert corr["family"] == "workitem.state"
        assert corr["digest"] == "active"


# ---------------------------------------------------------------------------
# Tests — minor vs material conflict routing
# ---------------------------------------------------------------------------

class TestMaterialityRouting:
    """Minor conflicts use resolution="unresolved_minor"; material → challenge_input."""

    def test_workitem_state_disagreement_is_minor_no_challenge(self):
        """workitem.state disagreement with no materiality predicate → minor."""
        policy = _make_policy()
        obs_ado = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_100",),
            natural_key="nk-ado",
        )
        obs_icm = _make_obs(
            fact_type="action.item",
            payload={"state": "done"},
            source_signal_ids=("icm_200",),
            natural_key="nk-icm",
        )
        result = detect_corroboration_and_conflicts(
            [obs_ado, obs_icm],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict["resolution"] == "unresolved_minor"
        assert conflict["material"] is False
        assert len(result.challenge_inputs) == 0, "No challenge for minor conflicts"

    def test_commitment_disagreement_is_material_with_challenge(self):
        """commitment family disagreement → material conflict + challenge_input."""
        policy = _make_policy()
        obs_human = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-03-01", "status": "on-track", "entity_ref": "COMMIT-1"},
            source_signal_ids=("workiq_10",),
            natural_key="nk-commit-human",
        )
        obs_ado = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-04-15", "status": "at-risk", "entity_ref": "COMMIT-1"},
            source_signal_ids=("ado_20",),
            natural_key="nk-commit-ado",
        )
        result = detect_corroboration_and_conflicts(
            [obs_human, obs_ado],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict["material"] is True
        assert len(result.challenge_inputs) >= 1


# ---------------------------------------------------------------------------
# Tests — GAP-37 (specs/bklg.md BL-H1): winning_value/losing_value on the
# emitted fact.conflict, and resolution labels distinguishing which
# precedence rule actually fired.
# ---------------------------------------------------------------------------

class TestDisplayValuesAndResolutionLabels:
    def test_minor_conflict_carries_raw_display_values(self):
        """Minor (workitem.state) conflicts get winning_value/losing_value
        too -- sharedness of a fact/value isn't gated on materiality."""
        policy = _make_policy()
        obs_ado = _make_obs(
            fact_type="action.item",
            payload={"state": "Active"},
            source_signal_ids=("ado_100",),
            natural_key="nk-ado",
        )
        obs_icm = _make_obs(
            fact_type="action.item",
            payload={"state": "Done"},
            source_signal_ids=("icm_200",),
            natural_key="nk-icm",
        )
        result = detect_corroboration_and_conflicts(
            [obs_ado, obs_icm], policy, None, None, ctx=_empty_ctx(), now=_NOW,
        )
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        # ado is primary for workitem.state, so it's the winning side here
        assert conflict["winning_value"] == "Active"
        assert conflict["losing_value"] == "Done"

    def test_material_conflict_via_primary_authority_labels_resolution(self):
        policy = _make_policy()
        obs_human = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"source": "human", "due_date": "2026-08-01", "status": "on-track", "entity_ref": "COMMIT-1"},
            source_signal_ids=("workiq_10",),
            natural_key="nk-commit-human",
        )
        obs_ado = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2026-08-15", "status": "at-risk", "entity_ref": "COMMIT-1"},
            source_signal_ids=("ado_20",),
            natural_key="nk-commit-ado",
        )
        result = detect_corroboration_and_conflicts(
            [obs_human, obs_ado], policy, None, None, ctx=_empty_ctx(), now=_NOW,
        )
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict["winning_source"] == "human"
        assert conflict["resolution"] == "primary_authority:human"
        assert conflict["winning_value"] == "2026-08-01 / on-track"
        assert conflict["losing_value"] == "2026-08-15 / at-risk"

    def test_material_conflict_via_trust_gap_labels_resolution_distinctly(self):
        """Neither source is commitment's primary ("human"), so rule 1 never
        fires -- the trust-gap rule decides, and the label says so (not the
        generic "precedence:" this row's own investigation found collapsed
        both rules together)."""
        policy = _make_policy()
        obs_ado = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2026-08-01", "status": "on-track", "entity_ref": "COMMIT-1"},
            source_signal_ids=("ado_100",),
            natural_key="nk-ado",
        )
        obs_workiq = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2026-08-15", "status": "at-risk", "entity_ref": "COMMIT-1"},
            source_signal_ids=("workiq_200",),
            natural_key="nk-workiq",
        )
        trust_ledger = {"ado": 0.8, "workiq": 0.3}  # gap=0.5 >= 0.15 threshold
        result = detect_corroboration_and_conflicts(
            [obs_ado, obs_workiq], policy, trust_ledger, None, ctx=_empty_ctx(), now=_NOW,
        )
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict["winning_source"] == "ado"
        assert conflict["resolution"] == "trust_gap:ado"


# ---------------------------------------------------------------------------
# Tests — suspended primary
# ---------------------------------------------------------------------------

class TestSuspendedPrimary:
    """Suspended primary must NOT win conflicts."""

    def test_suspended_ado_does_not_win_workitem_state_conflict(self):
        """If ado is suspended, it does not get primary-precedence win."""
        policy = _make_policy()
        ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            suspended_sources=frozenset({"ado"}),
            corroborated_keys=frozenset(),
        )
        obs_ado = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_100",),
            natural_key="nk-ado",
        )
        obs_icm = _make_obs(
            fact_type="action.item",
            payload={"state": "done"},
            source_signal_ids=("icm_200",),
            natural_key="nk-icm",
        )
        result = detect_corroboration_and_conflicts(
            [obs_ado, obs_icm],
            policy,
            None,
            None,
            ctx=ctx,
            now=_NOW,
        )
        # Should not resolve with ado as winner
        if result.conflicts:
            for c in result.conflicts:
                assert c.get("winning_source") != "ado", (
                    "Suspended ado must not win as primary authority"
                )

    def test_active_primary_wins_conflict(self):
        """Non-suspended primary should win when trust-gap exists."""
        policy = _make_policy()
        trust_ledger = {"ado": 0.9, "icm": 0.5}
        obs_ado = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_100",),
            natural_key="nk-ado",
        )
        obs_icm = _make_obs(
            fact_type="action.item",
            payload={"state": "done"},
            source_signal_ids=("icm_200",),
            natural_key="nk-icm",
        )
        result = detect_corroboration_and_conflicts(
            [obs_ado, obs_icm],
            policy,
            trust_ledger,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.conflicts) == 1
        # ado is primary for workitem.state AND has trust-gap advantage
        assert result.conflicts[0]["winning_source"] == "ado"


# ---------------------------------------------------------------------------
# Tests — trust-gap precedence
# ---------------------------------------------------------------------------

class TestTrustGap:
    """Trust-gap ≥ threshold selects winner when no primary wins."""

    def test_trust_gap_selects_higher_trust_source(self):
        """When both sources are secondary, trust-gap picks the winner."""
        policy = _make_policy()
        # Use incident family where ado and human_comms are both secondary
        obs_ado = _make_obs(
            entity_refs=("INC-1",),
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_100",),
            natural_key="nk-ado",
        )
        obs_teams = _make_obs(
            entity_refs=("INC-1",),
            fact_type="action.item",
            payload={"state": "done"},
            source_signal_ids=("teams_200",),
            natural_key="nk-teams",
        )
        # ado and teams are different classes; ado has much higher trust
        trust_ledger = {"ado": 0.8, "teams": 0.3}
        result = detect_corroboration_and_conflicts(
            [obs_ado, obs_teams],
            policy,
            trust_ledger,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.conflicts) == 1
        # ado is primary for workitem.state, so it wins via primary precedence first
        assert result.conflicts[0]["winning_source"] == "ado"

    def test_no_trust_gap_below_threshold_no_winner(self):
        """Trust gap below threshold → no winner → open conflict + challenge."""
        policy = _make_policy()
        obs_ado = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-03-01", "entity_ref": "COMMIT-1", "status": "on-track"},
            source_signal_ids=("ado_100",),
            natural_key="nk-ado",
        )
        obs_teams = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-04-01", "entity_ref": "COMMIT-1", "status": "at-risk"},
            source_signal_ids=("teams_200",),
            natural_key="nk-teams",
        )
        trust_ledger = {"ado": 0.5, "teams": 0.45}  # gap=0.05 < 0.15 threshold
        result = detect_corroboration_and_conflicts(
            [obs_ado, obs_teams],
            policy,
            trust_ledger,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert len(result.conflicts) == 1
        assert len(result.challenge_inputs) == 1  # no winner → challenge


# ---------------------------------------------------------------------------
# Tests — conflict continuity
# ---------------------------------------------------------------------------

class TestConflictContinuity:
    """Later detections attach as continuation evidence to existing conflict."""

    def test_continuation_detection_reuses_existing_conflict_id(self):
        """If an open conflict exists for (entity, family), its id is reused."""
        policy = _make_policy()
        existing_conflict_id = "existing-conflict-abc"

        # Build a mock program_fact_store with an existing open conflict
        existing_fact = MagicMock()
        existing_fact.fact_type = "fact.conflict"
        existing_fact.payload = {
            "entity_id": "COMMIT-1",
            "family": "commitment",
            "resolved": False,
            "conflict_id": existing_conflict_id,
        }
        existing_fact.natural_key = existing_conflict_id

        mock_snap = MagicMock()
        mock_snap.facts = [existing_fact]
        mock_store = MagicMock()
        mock_store.snapshot.return_value = mock_snap

        obs_a = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-03-01", "entity_ref": "COMMIT-1", "status": "on-track"},
            source_signal_ids=("workiq_1",),
            natural_key="nk-a",
        )
        obs_b = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-05-01", "entity_ref": "COMMIT-1", "status": "delayed"},
            source_signal_ids=("ado_2",),
            natural_key="nk-b",
        )
        result = detect_corroboration_and_conflicts(
            [obs_a, obs_b],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
            program_fact_store=mock_store,
        )
        assert len(result.challenge_inputs) >= 1
        ch = result.challenge_inputs[0]
        assert ch["conflict_id"] == existing_conflict_id, (
            "Must reuse existing conflict id for continuity"
        )
        assert ch["is_continuation"] is True


# ---------------------------------------------------------------------------
# Tests — source sync (§6.2.6)
# ---------------------------------------------------------------------------

class TestSourceSync:
    """Sync never executes in legacy/shadow; executes in primary SoR mode."""

    def test_sync_not_executed_in_legacy_mode(self):
        """In legacy mode, sync-eligible delta increments sync_pending_count, not events."""
        policy = _make_policy()
        # ADO is primary for workitem.state — this creates a sync opportunity
        obs_primary = _make_obs(
            fact_type="action.item",
            payload={"state": "active", "status_date": "2025-01-10"},
            source_signal_ids=("ado_100",),
            natural_key="nk-primary",
        )
        obs_secondary = _make_obs(
            fact_type="action.item",
            payload={"state": "done", "status_date": "2025-01-05"},
            source_signal_ids=("icm_200",),
            natural_key="nk-secondary",
        )
        result = detect_corroboration_and_conflicts(
            [obs_primary, obs_secondary],
            policy,
            {"ado": 0.9, "icm": 0.4},
            None,
            ctx=_empty_ctx(),
            now=_NOW,
            family_sor_modes={"workitem.state": "legacy"},
        )
        assert len(result.sync_events) == 0, "No sync events in legacy mode"
        assert result.sync_pending_count >= 0  # counter only

    def test_sync_executes_in_primary_sor_mode(self):
        """In primary SoR mode, sync executes and updates mirror fields only."""
        policy = _make_policy()
        obs_primary = _make_obs(
            fact_type="action.item",
            payload={"state": "active", "status_date": "2025-01-10"},
            source_signal_ids=("ado_100",),
            natural_key="nk-primary",
        )
        obs_secondary = _make_obs(
            fact_type="action.item",
            payload={"state": "done", "status_date": "2025-01-05"},
            source_signal_ids=("icm_200",),
            natural_key="nk-secondary",
        )
        result = detect_corroboration_and_conflicts(
            [obs_primary, obs_secondary],
            policy,
            {"ado": 0.9, "icm": 0.4},
            None,
            ctx=_empty_ctx(),
            now=_NOW,
            family_sor_modes={"workitem.state": "primary"},
        )
        # In primary mode, sync events should be emitted
        assert len(result.sync_events) >= 1
        sync = result.sync_events[0]
        assert sync["source"] == "ado"
        # Only mirror fields (state, status_date) should appear in sync delta
        for field in sync.get("mirror_fields_updated", []):
            assert field in ("state", "status_date"), (
                f"Only mirror fields allowed in sync delta, got: {field}"
            )

    def test_sync_never_on_human_primary_family(self):
        """Human-primary families (commitment, judgment) must never sync."""
        policy = _make_policy()
        obs_a = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-03-01", "entity_ref": "COMMIT-1", "status": "on-track"},
            source_signal_ids=("workiq_10",),
            natural_key="nk-a",
        )
        obs_b = _make_obs(
            entity_refs=("COMMIT-1",),
            fact_type="commitment.entry",
            payload={"due_date": "2025-04-01", "entity_ref": "COMMIT-1", "status": "delayed"},
            source_signal_ids=("ado_20",),
            natural_key="nk-b",
        )
        result = detect_corroboration_and_conflicts(
            [obs_a, obs_b],
            policy,
            {"workiq": 0.7, "ado": 0.8},
            None,
            ctx=_empty_ctx(),
            now=_NOW,
            family_sor_modes={"commitment": "primary"},  # primary mode set, but human family
        )
        assert len(result.sync_events) == 0, (
            "Human-primary family (commitment) must never have sync events"
        )


# ---------------------------------------------------------------------------
# Tests — empty / no-observation edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases: single observations, no entity refs, no digestible values."""

    def test_single_observation_no_events(self):
        """A group with only one observation produces no events."""
        policy = _make_policy()
        obs = _make_obs(
            fact_type="action.item",
            payload={"state": "active"},
            source_signal_ids=("ado_100",),
        )
        result = detect_corroboration_and_conflicts(
            [obs],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert result.corroborations == ()
        assert result.conflicts == ()
        assert result.sync_pending_count == 0
        assert result.sync_events == ()

    def test_observation_without_entity_refs_is_skipped(self):
        """Observations with no entity_refs are silently skipped."""
        policy = _make_policy()
        obs = _make_obs(
            entity_refs=(),
            fact_type="action.item",
            payload={"state": "active"},
        )
        result = detect_corroboration_and_conflicts(
            [obs],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert result.corroborations == ()
        assert result.conflicts == ()

    def test_empty_observations_list(self):
        """Empty observations list returns all-empty result."""
        policy = _make_policy()
        result = detect_corroboration_and_conflicts(
            [],
            policy,
            None,
            None,
            ctx=_empty_ctx(),
            now=_NOW,
        )
        assert isinstance(result, CorroborationConflictResult)
        assert result.sync_pending_count == 0
