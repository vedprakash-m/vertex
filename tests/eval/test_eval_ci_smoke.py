"""ADF-W0.15 (Section 8.15.3): the CI regression lane -- synthetic fixtures
+ the Issue-079 regression corpus, deterministic assertions only, no LLM
calls. Run with ``python -m pytest -m eval_ci -q`` (the brief's done-check).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.core.milestone_engine import assess_milestone_health
from src.core.models_v2 import Milestone, MilestoneStatus
from tests.eval.corpus_schema import CorpusItem, load_corpus_family
from tests.eval.holdout_lane import HoldoutLaneRefused, run_holdout_evaluation

pytestmark = pytest.mark.eval_ci


def test_synthetic_risk_corpus_loads_and_round_trips() -> None:
    items = load_corpus_family("risk_synthetic")
    assert len(items) == 3
    assert all(isinstance(item, CorpusItem) for item in items)
    assert {item.split for item in items} == {"holdout", "dev"}


def test_synthetic_risk_corpus_holdout_split_is_independently_labeled() -> None:
    holdout_items = load_corpus_family("risk_synthetic", split="holdout")
    assert len(holdout_items) == 2
    assert all(item.is_independently_labeled for item in holdout_items)


def test_unknown_corpus_family_returns_empty_not_error() -> None:
    assert load_corpus_family("does_not_exist_yet") == ()


def test_holdout_lane_scores_synthetic_fixture_deterministically() -> None:
    items = load_corpus_family("risk_synthetic", split="holdout")

    def _scorer(item: CorpusItem) -> bool:
        # Deterministic stand-in "model": a candidate risk mentions "milestone" or "slipped".
        predicted_candidate = "milestone" in item.input_excerpt.lower() or "slipped" in item.input_excerpt.lower()
        actual_candidate = item.label.get("kind") == "candidate"
        return predicted_candidate == actual_candidate

    result = run_holdout_evaluation(items, scorer_fn=_scorer)
    assert result.family == "risk"
    assert result.total_items == 2
    assert result.accuracy == 1.0


def test_issue_079_regression_still_holds() -> None:
    """The Issue-079 regression corpus reference from the brief: a past-due,
    zero-evidence, declared-ON_TRACK milestone must never render ON_TRACK
    (Section 8.10.3 / INV-ADF-12). Full coverage lives in
    tests/unit/test_milestone_no_evidence.py; this is the CI-lane's own
    deterministic assertion against the same invariant."""
    milestone = Milestone(
        id="eval-ci-issue-079",
        program_id="xpf",
        name="Issue-079-style milestone (eval_ci lane)",
        target_date=date(2026, 6, 1),
        owner_alias="maintainer",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=("Ship it",),
        linked_workstream_ids=("xpf",),
        linked_work_item_ids=(),
    )
    assessment = assess_milestone_health(milestone, (), {}, datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc))
    assert assessment.computed_health == MilestoneStatus.MISSED
    assert assessment.coverage_gap is True
