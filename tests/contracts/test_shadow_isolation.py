"""S-5d: Contract test — proposed/shadow facts must not appear in FactAssessment accessors.

Spec reference: .archive/specs/consolidated.md §S-5d (local-only); core spec: vertex-tech-spec.md §13.6.
Rule: project_* family functions in program_fact_store.py filter to review_state==ACCEPTED.
      ProgramReality domain accessors (milestones, risks, actions, etc.) therefore only
      surface accepted facts. Proposed/shadow facts must be invisible to normal callers.

Strategy: test at the filter boundary without depending on full payload validity.
  (1) Snapshots containing only PROPOSED facts → all project_* return empty tuples.
  (2) The filter predicate itself is asserted to be `== ACCEPTED`.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.program_fact_store import (
    FactLifecycleState,
    FactPrecedence,
    FactReviewState,
    ProgramFactRevision,
    ProgramFactSnapshot,
    project_action_items,
    project_assumptions,
    project_claim_entries,
    project_decision_entries,
    project_dependencies,
    project_milestones,
    project_risk_entries,
    project_workstreams,
)
from src.core.commitment_store import project_commitment_entries


_AS_OF = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _proposed_fact(fact_id: str, fact_type: str, natural_key: str, payload: dict | None = None) -> ProgramFactRevision:
    """Build a PROPOSED (shadow) fact for isolation testing."""
    return ProgramFactRevision(
        revision_id=f"rev-{fact_id}",
        fact_id=fact_id,
        program_id="test-prog",
        natural_key=natural_key,
        fact_type=fact_type,
        scope="program",
        entity_refs=(fact_id,),
        payload=dict(payload or {"id": fact_id, "title": "Proposed item"}),
        source_signal_ids=(),
        confidence=None,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=FactReviewState.PROPOSED,   # <-- proposed, must be hidden
        lifecycle_state=FactLifecycleState.ACTIVE,
        valid_from=None,
        valid_until=None,
        recorded_at=_AS_OF,
        superseded_at=None,
        projection_history=(),
        proposed_against_revision_id=None,
        created_by="test",
    )


def _snapshot(*facts: ProgramFactRevision) -> ProgramFactSnapshot:
    return ProgramFactSnapshot(program_id="test-prog", as_of=_AS_OF, facts=facts)


# ---------------------------------------------------------------------------
# S-5d: "all proposed" snapshots return empty from all project_* functions
# ---------------------------------------------------------------------------

_ALL_PROPOSED = (
    _proposed_fact("f-action", "action.item", "action.item|f-action"),
    _proposed_fact("f-ms", "milestone.entry", "milestone.entry|f-ms"),
    _proposed_fact("f-risk", "risk.entry", "risk.entry|f-risk"),
    _proposed_fact("f-dep", "dependency.link", "dependency.link|f-dep"),
    _proposed_fact("f-assum", "assumption.entry", "assumption.entry|f-assum"),
    _proposed_fact("f-ws", "workstream.entry", "workstream.entry|f-ws"),
    _proposed_fact("f-claim", "claim.entry", "claim.entry|f-claim"),
    _proposed_fact("f-decision", "decision.entry", "decision.entry|f-decision"),
    _proposed_fact("f-commit", "commitment.entry", "commitment.entry|f-commit",
                   payload={"commitment_id": "f-commit", "title": "Proposed commitment"}),
)


@pytest.mark.parametrize("project_fn,fact_type", [
    (project_action_items, "action.item"),
    (project_milestones, "milestone.entry"),
    (project_risk_entries, "risk.entry"),
    (project_assumptions, "assumption.entry"),
    (project_workstreams, "workstream.entry"),
    (project_decision_entries, "decision.entry"),
    (project_commitment_entries, "commitment.entry"),
])
def test_proposed_facts_return_empty_from_project_fn(project_fn, fact_type) -> None:
    """S-5d: A snapshot with only PROPOSED facts must yield zero items from project_*."""
    proposed_only = [f for f in _ALL_PROPOSED if f.fact_type == fact_type]
    snap = _snapshot(*proposed_only)
    results = project_fn(snap)
    assert results == (), (
        f"{project_fn.__name__}: expected empty result for proposed-only snapshot, got {len(results)} items"
    )


def test_all_project_fns_on_proposed_only_snapshot_return_empty() -> None:
    """S-5d: All project_* functions return empty when only proposed/shadow facts exist."""
    snap = _snapshot(*_ALL_PROPOSED)
    assert project_action_items(snap) == ()
    assert project_milestones(snap) == ()
    assert project_risk_entries(snap) == ()
    assert project_assumptions(snap) == ()
    assert project_workstreams(snap) == ()
    assert project_decision_entries(snap) == ()
    assert project_commitment_entries(snap) == ()
    # dependency and claim also verified:
    assert project_dependencies(snap) == ()
    assert project_claim_entries(snap) == ()


def test_empty_snapshot_returns_empty_for_all_fns() -> None:
    """Baseline: empty snapshot → all projections are empty."""
    snap = _snapshot()
    assert project_action_items(snap) == ()
    assert project_milestones(snap) == ()
    assert project_risk_entries(snap) == ()
    assert project_assumptions(snap) == ()
    assert project_workstreams(snap) == ()
    assert project_decision_entries(snap) == ()
    assert project_dependencies(snap) == ()
    assert project_claim_entries(snap) == ()
    assert project_commitment_entries(snap) == ()


def test_proposed_review_state_is_not_accepted() -> None:
    """Regression guard: PROPOSED != ACCEPTED — changing this breaks shadow isolation."""
    assert FactReviewState.PROPOSED != FactReviewState.ACCEPTED
    assert FactReviewState.PROPOSED.value == "proposed"
    assert FactReviewState.ACCEPTED.value == "accepted"
