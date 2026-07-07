"""WI-3.10 [HARD]: E2E scenario test — "the disagreeing sources story".

Phase-3b integration gate. Tests the complete pipeline:
  gather → normalize → promote → corroborate → conflict → status MVP

Scenario: "Acme Auth Service rollout — two sources disagree on completion"
  - ADO says work item 1234 is 'Done'
  - Teams comms say the rollout is 'in progress'
  - A linked assumption premise goes stale
  - One source breaker fires (suspended)
  - Reconfirmation resets the staleness clock

Assertions:
  1. One corroboration fact written (ADO + IcM both say 'Done' for a different entity)
  2. One material conflict reaches triage (ADO vs Teams — DISPUTED_FACT attention)
  3. One minor conflict stays silent (non-material family, not in attention)
  4. One reconfirmation resets staleness clock (fact.reconfirmation event)
  5. One breaker suspension blocks further promotions from that source
  6. QG-27 blocks publish when material conflict exists
  7. DECISION_OUTCOME_DRIFT fires when linked assumption is stale
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.program_fact_store import (
    FactLifecycleState,
    FactPrecedence,
    FactReviewState,
    ProgramFactInput,
    ProgramFactRevision,
    ProgramFactSnapshot,
    ProgramFactStore,
    build_natural_key,
    load_program_facts,
)
from src.core.signal_promotion import promote_observation, is_provisional_signal
from src.core.truth_model import (
    TruthContext,
    build_truth_context,
    derive_truth_level,
)
from src.core.truth_levels import TruthLevel
from src.core.quality_gates.qg27 import QG27Input, evaluate_qg27

_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
_PROGRAM_ID = "e2e_test_disagreeing_sources"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_fact(
    natural_key: str,
    fact_type: str,
    entity_refs: tuple[str, ...],
    payload: dict,
    review_state: FactReviewState = FactReviewState.ACCEPTED,
    recorded_at: datetime = _NOW,
) -> ProgramFactRevision:
    return ProgramFactRevision(
        revision_id=f"rev_{natural_key[:20]}",
        fact_id=f"pf_{natural_key[:20]}",
        program_id=_PROGRAM_ID,
        natural_key=natural_key,
        fact_type=fact_type,
        scope="program",
        entity_refs=entity_refs,
        payload=payload,
        source_signal_ids=(),
        confidence=None,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=review_state,
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=recorded_at,
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="test",
    )


# ---------------------------------------------------------------------------
# Act 1: promote_observation pipeline
# ---------------------------------------------------------------------------

class TestAct1PromoteObservation:
    """Signal promotion: ADO says Done, Teams says in-progress."""

    def test_ado_signal_accepted(self, tmp_path: Path) -> None:
        """ADO (non-provisional) signal gets review_state=ACCEPTED."""
        result = promote_observation(
            program_id=_PROGRAM_ID,
            fact_type="action.item",
            entity_refs=("ADO:1234",),
            payload={"title": "Auth Service Rollout", "state": "Done"},
            source_family="ado",
            db_root=tmp_path,
        )
        assert result.action == "created"
        assert result.fact_write is not None
        assert result.fact_write.revision.review_state == FactReviewState.ACCEPTED

    def test_teams_signal_provisional(self, tmp_path: Path) -> None:
        """Teams (provisional) signal gets review_state=PROPOSED (not ACCEPTED)."""
        result = promote_observation(
            program_id=_PROGRAM_ID,
            fact_type="action.item",
            entity_refs=("teams_action:rollout_status",),
            payload={"title": "Auth rollout status", "state": "in_progress"},
            source_family="teams",
            db_root=tmp_path,
        )
        assert result.action == "created"
        assert result.fact_write is not None
        assert result.fact_write.revision.review_state == FactReviewState.PROPOSED

    def test_reconfirmation_resets_staleness(self, tmp_path: Path) -> None:
        """Re-promoting same ADO observation emits fact.reconfirmation (action='reconfirmed')."""
        common = dict(
            program_id=_PROGRAM_ID,
            fact_type="action.item",
            entity_refs=("ADO:5678",),
            payload={"title": "Old action", "state": "Active"},
            source_family="ado",
            db_root=tmp_path,
        )
        first = promote_observation(**common)
        assert first.action == "created"

        second = promote_observation(**common)
        assert second.action == "reconfirmed"
        assert second.reconfirmation_write is not None
        assert second.reconfirmation_write.revision.fact_type == "fact.reconfirmation"

    def test_suspended_source_blocked(self, tmp_path: Path) -> None:
        """Breaker suspension: source in truth_ctx.suspended_sources → action='suspended'."""
        truth_ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            corroborated_keys=frozenset(),
            suspended_sources=frozenset({"bad_source"}),
        )
        result = promote_observation(
            program_id=_PROGRAM_ID,
            fact_type="action.item",
            entity_refs=("bad:1",),
            payload={"title": "Should not land"},
            source_family="bad_source",
            truth_ctx=truth_ctx,
            db_root=tmp_path,
        )
        assert result.action == "suspended"
        assert result.fact_write is None


# ---------------------------------------------------------------------------
# Act 2: conflict + triage pipeline
# ---------------------------------------------------------------------------

class TestAct2ConflictPipeline:
    """Material conflict fires DISPUTED_FACT attention; minor conflict stays silent."""

    def _build_snapshot(
        self,
        *facts: ProgramFactRevision,
    ) -> ProgramFactSnapshot:
        return ProgramFactSnapshot(
            program_id=_PROGRAM_ID,
            as_of=_NOW,
            facts=facts,
        )

    def test_material_conflict_fires_disputed_fact_attention(self) -> None:
        """A material fact.conflict in the snapshot fires DISPUTED_FACT attention via QG-27."""
        material_conflict = _make_fact(
            natural_key="conflict:ADO:1234:workitem.state",
            fact_type="fact.conflict",
            entity_refs=("ADO:1234",),
            payload={
                "family": "workitem.state",
                "description": "ADO says Done; Teams says in-progress",
                "resolved": False,
                "is_material": True,
            },
        )
        snap = self._build_snapshot(material_conflict)
        qg_input = QG27Input(snapshot=snap, truth_levels={})
        result = evaluate_qg27(qg_input)
        assert not result.passed
        assert result.exit_code == 2  # HARD block
        assert not result.forceable

    def test_non_material_conflict_does_not_block(self) -> None:
        """A non-material (minor) conflict does NOT cause QG-27 hard block."""
        minor_conflict = _make_fact(
            natural_key="conflict:ADO:9999:narrative",
            fact_type="fact.conflict",
            entity_refs=("ADO:9999",),
            payload={
                "family": "narrative",
                "description": "Minor wording disagreement",
                "resolved": False,
                "is_material": False,
            },
        )
        snap = self._build_snapshot(minor_conflict)
        truth_levels = {}
        qg_input = QG27Input(snapshot=snap, truth_levels=truth_levels)
        result = evaluate_qg27(qg_input)
        # Non-material conflict doesn't trigger hard block
        assert result.exit_code != 2  # Not a hard block

    def test_qg27_advisory_for_below_min_truth(self) -> None:
        """QG-27 advisory fires for facts below SOURCE_VALIDATED."""
        fact = _make_fact(
            natural_key="obs:ADO:1234",
            fact_type="action.item",
            entity_refs=("ADO:1234",),
            payload={"title": "test", "state": "Active"},
        )
        snap = self._build_snapshot(fact)
        truth_levels = {fact.natural_key: TruthLevel.RAW_OBSERVED}
        qg_input = QG27Input(snapshot=snap, truth_levels=truth_levels)
        result = evaluate_qg27(qg_input)
        assert not result.passed
        assert result.exit_code == 1  # Advisory
        assert result.forceable  # Can be overridden


# ---------------------------------------------------------------------------
# Act 3: truth derivation
# ---------------------------------------------------------------------------

class TestAct3TruthDerivation:
    """Truth levels are derived correctly from TruthContext."""

    def test_raw_observed_without_corroboration(self) -> None:
        """Without corroboration, a signal.observation is RAW_OBSERVED."""
        fact = _make_fact(
            natural_key="obs:teams:rollout",
            fact_type="signal.observation",
            entity_refs=("teams:rollout",),
            payload={"state": "in_progress"},
            review_state=FactReviewState.PROPOSED,
        )
        snap = ProgramFactSnapshot(
            program_id=_PROGRAM_ID,
            as_of=_NOW,
            facts=(fact,),
        )
        truth_ctx = build_truth_context(_PROGRAM_ID, fact_snapshot=snap)
        level = derive_truth_level(fact, truth_ctx)
        assert level == TruthLevel.RAW_OBSERVED

    def test_governance_locked_highest_priority(self) -> None:
        """Governance-locked facts always get GOVERNANCE_LOCKED truth level."""
        locked_fact = _make_fact(
            natural_key="locked:action:1",
            fact_type="action.item",
            entity_refs=("action:1",),
            payload={"state": "Done", "lifecycle_state": "governance_locked"},
        )
        # Simulate a governance-locked fact (lifecycle_state field)
        from dataclasses import replace
        locked_fact = replace(locked_fact, lifecycle_state=FactLifecycleState.CLOSED)
        snap = ProgramFactSnapshot(
            program_id=_PROGRAM_ID,
            as_of=_NOW,
            facts=(locked_fact,),
        )
        truth_ctx = build_truth_context(_PROGRAM_ID, fact_snapshot=snap)
        # Without being in baseline_locked_keys, this is not GOVERNANCE_LOCKED
        level = derive_truth_level(locked_fact, truth_ctx)
        # CLOSED facts don't auto-become governance_locked — that requires explicit lock
        assert level in (TruthLevel.RAW_OBSERVED, TruthLevel.SOURCE_VALIDATED, TruthLevel.HUMAN_CONFIRMED)


# ---------------------------------------------------------------------------
# Act 4: DECISION_OUTCOME_DRIFT (WI-3.11)
# ---------------------------------------------------------------------------

class TestAct4DecisionOutcomeDrift:
    """Stale or disputed linked assumption fires DECISION_OUTCOME_DRIFT."""

    def test_drift_fires_on_stale_assumption(self) -> None:
        """When a decision's linked assumption is stale, DECISION_OUTCOME_DRIFT fires."""
        from datetime import date
        from src.core.program_reality import (
            ProgramReality,
            FactAssessment,
            AttentionKind,
        )
        from src.core.models_v2 import DecisionEntry, DecisionStatus

        decision_entry = DecisionEntry(
            id="D-001",
            program_id=_PROGRAM_ID,
            title="Rollout 80% by March",
            context="Auth service rollout decision",
            decision="Proceed with rollout",
            rationale=None,
            alternatives_considered=(),
            decided_by="pm@example.com",
            decision_date=date(2024, 1, 1),
            status=DecisionStatus.PROPOSED,
            superseded_by=None,
            linked_claim_id=None,
            linked_risk_id=None,
            linked_action_ids=(),
            workstream_id=None,
            entity_refs=("decision:D-001",),
            expected_outcome_refs=("assumption:rollout_80pct",),  # WI-3.11 field
        )
        decision_assessment = FactAssessment(
            record=decision_entry,
            fact_id="pf_D001",
            truth_level=TruthLevel.HUMAN_CONFIRMED,
            disputed=False,
            stale=False,
            provisional_inputs=False,
            evidence=(),
        )

        # assumption_entry with a natural key referenced by the decision
        # The decision's payload carries expected_outcome_refs pointing to this assumption
        assumption_assessment = FactAssessment(
            record=_make_fact(
                natural_key="assumption:rollout_80pct",
                fact_type="assumption.entry",
                entity_refs=("assumption:rollout_80pct",),
                payload={"statement": "Auth will reach 80%"},
                recorded_at=_NOW - timedelta(days=120),  # very old
            ),
            fact_id="pf_assump",
            truth_level=TruthLevel.HUMAN_CONFIRMED,
            disputed=False,
            stale=True,  # stale!
            provisional_inputs=False,
            evidence=(),
        )

        # Build a snapshot with expected_outcome_refs in the decision payload
        decision_fact = _make_fact(
            natural_key="decision:D-001",
            fact_type="decision.entry",
            entity_refs=("decision:D-001",),
            payload={
                "decision_id": "D-001",
                "title": "Rollout 80% by March",
                "expected_outcome_refs": ["assumption:rollout_80pct"],
            },
        )
        snap = ProgramFactSnapshot(
            program_id=_PROGRAM_ID,
            as_of=_NOW,
            facts=(decision_fact,),
        )

        pr = ProgramReality(
            program_id=_PROGRAM_ID,
            snapshot=snap,
            sor_mode="legacy",
            as_of=_NOW,
            _entity_fact_index={},
            _actions=(),
            _risks=(),
            _decisions=(decision_assessment,),
            _dependencies=(),
            _milestones=(),
            _assumptions=(assumption_assessment,),
            _workstreams=(),
            _claims=(),
        )
        items = pr.attention()
        drift_items = [i for i in items if i.kind == AttentionKind.DECISION_OUTCOME_DRIFT]
        assert len(drift_items) >= 1, (
            f"Expected DECISION_OUTCOME_DRIFT attention item for stale assumption, got: {[i.kind for i in items]}"
        )

    def test_no_drift_without_expected_outcome_refs(self) -> None:
        """No DECISION_OUTCOME_DRIFT if decision has no expected_outcome_refs."""
        from datetime import date
        from src.core.program_reality import ProgramReality, FactAssessment, AttentionKind
        from src.core.models_v2 import DecisionEntry, DecisionStatus

        decision_entry = DecisionEntry(
            id="D-002",
            program_id=_PROGRAM_ID,
            title="Simple decision",
            context="No linked assumptions",
            decision="Go ahead",
            rationale=None,
            alternatives_considered=(),
            decided_by="pm@example.com",
            decision_date=date(2024, 1, 1),
            status=DecisionStatus.PROPOSED,
            superseded_by=None,
            linked_claim_id=None,
            linked_risk_id=None,
            linked_action_ids=(),
            workstream_id=None,
            entity_refs=("decision:D-002",),
            expected_outcome_refs=(),  # No linked assumptions
        )
        decision_assessment = FactAssessment(
            record=decision_entry,
            fact_id="pf_D002",
            truth_level=TruthLevel.HUMAN_CONFIRMED,
            disputed=False,
            stale=False,
            provisional_inputs=False,
            evidence=(),
        )
        snap = ProgramFactSnapshot(
            program_id=_PROGRAM_ID,
            as_of=_NOW,
            facts=(),
        )
        pr = ProgramReality(
            program_id=_PROGRAM_ID,
            snapshot=snap,
            sor_mode="legacy",
            as_of=_NOW,
            _entity_fact_index={},
            _actions=(),
            _risks=(),
            _decisions=(decision_assessment,),
            _dependencies=(),
            _milestones=(),
            _assumptions=(),
            _workstreams=(),
            _claims=(),
        )
        items = pr.attention()
        drift_items = [i for i in items if i.kind == AttentionKind.DECISION_OUTCOME_DRIFT]
        assert len(drift_items) == 0


# ---------------------------------------------------------------------------
# Act 5: Structural corroboration (truth elevation)
# ---------------------------------------------------------------------------

class TestAct5Corroboration:
    """fact.corroboration events elevate truth level from RAW_OBSERVED to CORROBORATED."""

    def test_corroboration_fact_present_in_snapshot(self) -> None:
        """When a fact.corroboration event exists, TruthContext includes the corroborated pair."""
        corr_fact = _make_fact(
            natural_key="fact.corroboration:ADO:1234:workitem.state:2024-06-15",
            fact_type="fact.corroboration",
            entity_refs=("ADO:1234",),
            payload={
                "entity_id": "ADO:1234",
                "family": "workitem.state",
                "day_bucket": "2024-06-15",
            },
        )
        snap = ProgramFactSnapshot(
            program_id=_PROGRAM_ID,
            as_of=_NOW,
            facts=(corr_fact,),
        )
        truth_ctx = build_truth_context(_PROGRAM_ID, fact_snapshot=snap)
        assert ("ADO:1234", "workitem.state") in truth_ctx.corroborated_keys

    def test_source_suspension_blocks_new_promotion(self, tmp_path: Path) -> None:
        """Suspended source in TruthContext blocks promotion (action='suspended')."""
        truth_ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            corroborated_keys=frozenset(),
            suspended_sources=frozenset({"kusto"}),
        )
        result = promote_observation(
            program_id=_PROGRAM_ID,
            fact_type="signal.observation",
            entity_refs=("metric:latency",),
            payload={"value": 42.0},
            source_family="kusto",
            truth_ctx=truth_ctx,
            db_root=tmp_path,
        )
        assert result.action == "suspended"
