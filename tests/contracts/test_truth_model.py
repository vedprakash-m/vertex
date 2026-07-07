"""WI-3.0: Contract tests for truth_model.py.

Covers:
- TruthContext construction
- derive_truth_level all 5 rules (rule-4 fixtures use suspended_sources=frozenset())
- Authority loader (family_map completeness, provenance_classes, authority entries)
- MATERIALITY_PREDICATES dict completeness
- Digest functions
- Join-table completeness (all management fact types → non-unknown family)
- Override TTL governance
- No demotion of management facts from HUMAN_CONFIRMED (INV-3.0)

Per spec WI-3.0 acceptance: derivation tests per rule; join-table completeness;
digest tests; override refusal; expiry-degradation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.core.truth_levels import TruthLevel
from src.core.truth_model import (
    MATERIALITY_PREDICATES,
    AuthorityEntry,
    SourceAuthorityPolicy,
    TruthContext,
    build_truth_context,
    compute_commitment_digest,
    compute_incident_digest,
    compute_metric_digest,
    compute_workitem_state_digest,
    derive_truth_level,
    get_authority_family,
    is_material_conflict,
    is_primary_authority,
    load_source_authority_policy,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_fact(
    fact_type: str = "signal.observation",
    natural_key: str = "test-key",
    review_state: str = "",
    accepted_by: str | None = None,
    write_authority: str = "human",
    entity_refs: tuple[str, ...] = ("entity-1",),
    source_signal_ids: tuple[str, ...] = (),
    payload: dict | None = None,
    lifecycle_state: str = "active",
) -> MagicMock:
    fact = MagicMock()
    fact.fact_type = fact_type
    fact.natural_key = natural_key
    fact.review_state = review_state
    fact.accepted_by = accepted_by
    fact.write_authority = write_authority
    fact.entity_refs = entity_refs
    fact.source_signal_ids = source_signal_ids
    fact.payload = payload or {}
    fact.lifecycle_state = lifecycle_state
    return fact


def _make_snapshot(facts=()) -> MagicMock:
    snap = MagicMock()
    snap.facts = facts
    return snap


def _empty_context() -> TruthContext:
    return TruthContext(
        baseline_locked_keys=frozenset(),
        suspended_sources=frozenset(),
        corroborated_keys=frozenset(),
    )


def _load_policy() -> SourceAuthorityPolicy:
    return load_source_authority_policy()


# ---------------------------------------------------------------------------
# TruthContext construction
# ---------------------------------------------------------------------------

class TestTruthContextConstruction:
    def test_build_truth_context_empty_snapshot(self):
        snap = _make_snapshot()
        ctx = build_truth_context("testprog", fact_snapshot=snap)
        assert isinstance(ctx, TruthContext)
        assert isinstance(ctx.baseline_locked_keys, frozenset)
        assert isinstance(ctx.suspended_sources, frozenset)
        assert isinstance(ctx.corroborated_keys, frozenset)

    def test_build_truth_context_governance_locked_facts(self):
        locked_fact = _make_fact(natural_key="locked-key", lifecycle_state="governance_locked")
        active_fact = _make_fact(natural_key="active-key", lifecycle_state="active")
        snap = _make_snapshot(facts=[locked_fact, active_fact])
        ctx = build_truth_context("testprog", fact_snapshot=snap)
        assert "locked-key" in ctx.baseline_locked_keys
        assert "active-key" not in ctx.baseline_locked_keys

    def test_build_truth_context_suspended_sources(self):
        trust_fact = _make_fact(
            fact_type="trust.source_score",
            payload={"source": "ado", "suspended": True, "score": 0.1, "breaker_verdict": "suspended"},
        )
        snap = _make_snapshot(facts=[trust_fact])
        ctx = build_truth_context("testprog", fact_snapshot=snap)
        assert "ado" in ctx.suspended_sources

    def test_build_truth_context_corroborated_keys(self):
        corr_fact = _make_fact(
            fact_type="fact.corroboration",
            payload={"entity_id": "entity-1", "family": "workitem.state"},
        )
        snap = _make_snapshot(facts=[corr_fact])
        ctx = build_truth_context("testprog", fact_snapshot=snap)
        assert ("entity-1", "workitem.state") in ctx.corroborated_keys

    def test_build_truth_context_ignores_closed_corroboration_facts(self):
        corr_fact = _make_fact(
            fact_type="fact.corroboration",
            payload={"entity_id": "entity-1", "family": "workitem.state"},
            lifecycle_state="closed",
        )
        snap = _make_snapshot(facts=[corr_fact])
        ctx = build_truth_context("testprog", fact_snapshot=snap)
        assert ("entity-1", "workitem.state") not in ctx.corroborated_keys


# ---------------------------------------------------------------------------
# derive_truth_level — all 5 rules
# ---------------------------------------------------------------------------

class TestDeriveTruthLevelRules:
    """Rule ordering tests. Rule-4 fixtures use suspended_sources=frozenset()."""

    def test_rule1_governance_locked(self):
        """Rule 1: natural_key ∈ baseline_locked_keys → GOVERNANCE_LOCKED."""
        ctx = TruthContext(
            baseline_locked_keys=frozenset({"the-key"}),
            suspended_sources=frozenset(),
            corroborated_keys=frozenset(),
        )
        fact = _make_fact(natural_key="the-key")
        assert derive_truth_level(fact, ctx) == TruthLevel.GOVERNANCE_LOCKED

    def test_rule1_priority_over_rule2(self):
        """Rule 1 takes priority even when review_state=accepted."""
        ctx = TruthContext(
            baseline_locked_keys=frozenset({"the-key"}),
            suspended_sources=frozenset(),
            corroborated_keys=frozenset(),
        )
        fact = _make_fact(natural_key="the-key", review_state="accepted")
        assert derive_truth_level(fact, ctx) == TruthLevel.GOVERNANCE_LOCKED

    def test_rule2_accepted_review_state(self):
        """Rule 2: confirmed review state → HUMAN_CONFIRMED."""
        ctx = _empty_context()
        for review_state in ("accepted", "confirmed", "approved", "human_confirmed"):
            fact = _make_fact(review_state=review_state)
            result = derive_truth_level(fact, ctx)
            assert result == TruthLevel.HUMAN_CONFIRMED, f"review_state={review_state!r} should be HUMAN_CONFIRMED"

    def test_rule2_accepted_by(self):
        """Rule 2: accepted_by set → HUMAN_CONFIRMED."""
        ctx = _empty_context()
        fact = _make_fact(accepted_by="pm@example.com")
        assert derive_truth_level(fact, ctx) == TruthLevel.HUMAN_CONFIRMED

    def test_rule2_bridge_accepted_review_state_does_not_over_promote(self):
        """Accepted bridge facts must not reach HUMAN_CONFIRMED via review_state alone."""
        policy = _load_policy()
        ctx = _empty_context()
        fact = _make_fact(
            fact_type="action.item",
            review_state="accepted",
            write_authority="bridge",
            payload={"source": "ado"},
        )
        assert derive_truth_level(fact, ctx, policy=policy) == TruthLevel.SOURCE_VALIDATED

    def test_rule2_bridge_accepted_by_does_not_over_promote(self):
        """Accepted bridge facts must not reach HUMAN_CONFIRMED via accepted_by alone."""
        policy = _load_policy()
        ctx = _empty_context()
        fact = _make_fact(
            fact_type="action.item",
            accepted_by="pm@example.com",
            write_authority="bridge",
            payload={"source": "ado"},
        )
        assert derive_truth_level(fact, ctx, policy=policy) == TruthLevel.SOURCE_VALIDATED

    def test_rule3_corroborated(self):
        """Rule 3: (entity_id, authority_family) ∈ corroborated_keys → CORROBORATED."""
        policy = _load_policy()
        ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            suspended_sources=frozenset(),
            corroborated_keys=frozenset({("entity-1", "workitem.state")}),
        )
        # action.item → workitem.state (from policy.family_map)
        fact = _make_fact(fact_type="action.item", entity_refs=("entity-1",))
        assert derive_truth_level(fact, ctx, policy=policy) == TruthLevel.CORROBORATED

    def test_rule4_source_validated_ado_workitem(self):
        """Rule 4: ADO is primary for workitem.state, not suspended → SOURCE_VALIDATED."""
        policy = _load_policy()
        ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            suspended_sources=frozenset(),  # empty per spec rule-4 fixtures
            corroborated_keys=frozenset(),
        )
        # action.item → workitem.state, ado is primary
        fact = _make_fact(
            fact_type="action.item",
            entity_refs=("entity-1",),
            payload={"source": "ado", "state": "active"},
        )
        assert derive_truth_level(fact, ctx, policy=policy) == TruthLevel.SOURCE_VALIDATED

    def test_rule4_suspended_source_does_not_validate(self):
        """Rule 4: suspended source → NOT SOURCE_VALIDATED."""
        policy = _load_policy()
        ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            suspended_sources=frozenset({"ado"}),
            corroborated_keys=frozenset(),
        )
        fact = _make_fact(
            fact_type="action.item",
            entity_refs=("entity-1",),
            payload={"source": "ado"},
        )
        result = derive_truth_level(fact, ctx, policy=policy)
        assert result != TruthLevel.SOURCE_VALIDATED

    def test_rule5_raw_observed_default(self):
        """Rule 5: no matching rule → RAW_OBSERVED."""
        ctx = _empty_context()
        fact = _make_fact(fact_type="signal.observation", payload={"source": "unknown_source"})
        assert derive_truth_level(fact, ctx) == TruthLevel.RAW_OBSERVED


# ---------------------------------------------------------------------------
# Management facts must NOT be demoted from HUMAN_CONFIRMED (INV-3.0)
# ---------------------------------------------------------------------------

_MANAGEMENT_FACT_TYPES = [
    "action.item", "risk.entry", "decision.entry", "dependency.link",
    "milestone.entry", "assumption.entry", "workstream.entry", "claim.entry",
    "commitment.entry",
]


def test_management_facts_stay_human_confirmed_with_review():
    """Management facts with accepted review state must reach HUMAN_CONFIRMED."""
    ctx = _empty_context()
    for ft in _MANAGEMENT_FACT_TYPES:
        fact = _make_fact(fact_type=ft, review_state="accepted")
        level = derive_truth_level(fact, ctx)
        assert level == TruthLevel.HUMAN_CONFIRMED, (
            f"Management fact {ft!r} with accepted review_state should be HUMAN_CONFIRMED, got {level}"
        )


# ---------------------------------------------------------------------------
# Authority loader — join-table completeness
# ---------------------------------------------------------------------------

class TestAuthorityLoader:
    def test_policy_loads_without_error(self):
        policy = _load_policy()
        assert policy.schema_version == "1"

    def test_all_management_fact_types_in_family_map(self):
        """Every management fact type must have a non-unknown family (join-table completeness)."""
        policy = _load_policy()
        for ft in _MANAGEMENT_FACT_TYPES:
            family = get_authority_family(ft, policy)
            assert family != "unknown", f"Fact type {ft!r} has no family in family_map"

    def test_provenance_classes_covers_main_sources(self):
        """Main data sources must have provenance class mappings."""
        policy = _load_policy()
        for source in ("ado", "kusto", "icm", "teams", "workiq", "ado_pr"):
            assert source in policy.provenance_classes, f"Source {source!r} missing from provenance_classes"

    def test_authority_entries_for_all_families(self):
        """Every family referenced in family_map must have an authority entry."""
        policy = _load_policy()
        for ft, family in policy.family_map.items():
            if family == "BY_SIGNAL_CLASS":
                continue  # special sentinel
            assert family in policy.authority, (
                f"Family {family!r} (from {ft!r}) has no authority entry"
            )

    def test_is_primary_authority_ado_workitem(self):
        policy = _load_policy()
        ctx = _empty_context()
        assert is_primary_authority("ado", "workitem.state", policy, ctx=ctx) is True

    def test_is_primary_authority_kusto_metric(self):
        policy = _load_policy()
        ctx = _empty_context()
        assert is_primary_authority("kusto", "metric", policy, ctx=ctx) is True

    def test_is_primary_authority_suspended_ado(self):
        policy = _load_policy()
        ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            suspended_sources=frozenset({"ado"}),
            corroborated_keys=frozenset(),
        )
        assert is_primary_authority("ado", "workitem.state", policy, ctx=ctx) is False

    def test_override_ttl_days_configured(self):
        policy = _load_policy()
        assert policy.override_ttl_days > 0

    def test_sor_flip_config_loads_with_validated_defaults(self):
        policy = _load_policy()
        defaults = policy.sor_flip.defaults
        assert defaults.clean_cycles_to_flip == 5
        assert defaults.divergence_tolerance == pytest.approx(0.02)
        assert defaults.critical_zero is True
        assert defaults.max_persistent_cycles == 8

        workitem = policy.sor_flip.for_family("workitem.state")
        assert workitem.require_s0g_policy is False  # ADR-0006 accepted: workitem.state gates open
        assert workitem.max_persistent_cycles == 8

        narrative = policy.sor_flip.for_family("narrative")
        assert narrative.clean_cycles_to_flip == 3
        assert narrative.divergence_tolerance == pytest.approx(0.10)

    def test_sor_flip_rejects_invalid_bounds(self, tmp_path):
        policy_path = tmp_path / "source_authority.yaml"
        policy_path.write_text(
            """
policy_schema_version: "1"
sor_flip:
  defaults:
    clean_cycles_to_flip: 0
    divergence_tolerance: 0.02
    critical_zero: true
    max_persistent_cycles: 8
""".strip(),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="clean_cycles_to_flip"):
            load_source_authority_policy(override_path=policy_path)

    def test_sor_flip_rejects_non_boolean_critical_zero(self, tmp_path):
        policy_path = tmp_path / "source_authority.yaml"
        policy_path.write_text(
            """
policy_schema_version: "1"
sor_flip:
  defaults:
    clean_cycles_to_flip: 5
    divergence_tolerance: 0.02
    critical_zero: "yes"
    max_persistent_cycles: 8
""".strip(),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="critical_zero"):
            load_source_authority_policy(override_path=policy_path)


# ---------------------------------------------------------------------------
# Digest functions
# ---------------------------------------------------------------------------

class TestDigestFunctions:
    def test_workitem_state_digest_lowercase(self):
        d = compute_workitem_state_digest({"state": "Active"})
        assert d == "active"

    def test_workitem_state_digest_status_fallback(self):
        d = compute_workitem_state_digest({"status": "Closed"})
        assert d == "closed"

    def test_workitem_state_digest_none_when_missing(self):
        assert compute_workitem_state_digest({}) is None

    def test_metric_digest_buckets_values(self):
        d1 = compute_metric_digest({"value": 0.51})
        d2 = compute_metric_digest({"value": 0.53})
        # Both should bucket to the same value at 5% tolerance
        assert d1 == d2

    def test_metric_digest_none_when_missing(self):
        assert compute_metric_digest({}) is None

    def test_incident_digest_format(self):
        d = compute_incident_digest({"severity": "High", "state": "Active"})
        assert d == "high:active"

    def test_incident_digest_none_when_missing(self):
        assert compute_incident_digest({}) is None

    def test_commitment_digest_includes_fields(self):
        d = compute_commitment_digest({
            "entity_ref": "e1",
            "due_date": "2024-06-30",
            "status": "active",
        })
        assert d is not None
        assert "e1" in d
        assert "2024-06-30" in d

    def test_text_human_digest_requires_resolved_entity(self):
        from src.core.truth_model import compute_text_human_digest
        # Without resolved entity → None
        assert compute_text_human_digest({"text": "some text"}, resolved_entity_id=None) is None
        # With resolved entity and metadata → not None
        d = compute_text_human_digest({"url": "https://example.com"}, resolved_entity_id="e1")
        assert d is not None


# ---------------------------------------------------------------------------
# MATERIALITY_PREDICATES completeness
# ---------------------------------------------------------------------------

def test_materiality_predicates_keys_match_yaml():
    """Predicates registered in the code must match the YAML's material_if ids."""
    policy = _load_policy()
    yaml_ids = set(policy.materiality_predicate_ids)
    code_ids = set(MATERIALITY_PREDICATES.keys())
    missing = yaml_ids - code_ids
    assert not missing, f"Materiality predicates in YAML not implemented in code: {missing}"


def test_materiality_predicates_callable():
    """All predicate values must be callable."""
    for pred_id, fn in MATERIALITY_PREDICATES.items():
        assert callable(fn), f"Predicate {pred_id!r} is not callable"


def test_predicate_family_is_commitment_fires():
    fact = _make_fact(fact_type="commitment.entry")
    assert MATERIALITY_PREDICATES["family_is_commitment"](fact, None) is True


def test_predicate_family_is_commitment_no_fire():
    fact = _make_fact(fact_type="risk.entry")
    assert MATERIALITY_PREDICATES["family_is_commitment"](fact, None) is False


def test_predicate_severity_critical_or_high_fires():
    fact = _make_fact(fact_type="risk.entry", payload={"risk_impact": "high"})
    assert MATERIALITY_PREDICATES["severity_critical_or_high"](fact, None) is True


def test_predicate_severity_critical_or_high_no_fire_for_non_risk():
    fact = _make_fact(fact_type="decision.entry", payload={"risk_impact": "high"})
    assert MATERIALITY_PREDICATES["severity_critical_or_high"](fact, None) is False


def test_is_material_conflict_fires_on_matching_predicate():
    fact = _make_fact(fact_type="commitment.entry")
    assert is_material_conflict(fact, None, predicate_ids=("family_is_commitment",)) is True


def test_is_material_conflict_no_fire():
    fact = _make_fact(fact_type="signal.observation")
    assert is_material_conflict(fact, None) is False
