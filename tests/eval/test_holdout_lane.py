"""ADF-W0.15: tests/eval/holdout_lane.py -- the holdout lane refusal gate."""
from __future__ import annotations

import pytest

from tests.eval.corpus_schema import CorpusItem
from tests.eval.holdout_lane import HoldoutLaneRefused, run_holdout_evaluation


def _item(**overrides: object) -> CorpusItem:
    defaults: dict[str, object] = dict(
        item_id="i1", family="risk", split="holdout",
        input_excerpt="text", label={"kind": "candidate"}, label_source="human",
    )
    defaults.update(overrides)
    return CorpusItem(**defaults)  # type: ignore[arg-type]


def test_refuses_on_empty_corpus() -> None:
    with pytest.raises(HoldoutLaneRefused, match="Empty corpus"):
        run_holdout_evaluation((), scorer_fn=lambda item: True)


def test_refuses_when_no_items_in_holdout_split() -> None:
    items = (_item(split="dev"),)
    with pytest.raises(HoldoutLaneRefused, match="holdout"):
        run_holdout_evaluation(items, scorer_fn=lambda item: True)


def test_refuses_when_all_items_are_llm_judge_only() -> None:
    items = (_item(label_source="llm_judge"),)
    with pytest.raises(HoldoutLaneRefused, match="llm_judge"):
        run_holdout_evaluation(items, scorer_fn=lambda item: True)


def test_refuses_on_mixed_families() -> None:
    items = (_item(family="risk"), _item(item_id="i2", family="dependency"))
    with pytest.raises(HoldoutLaneRefused, match="Mixed families"):
        run_holdout_evaluation(items, scorer_fn=lambda item: True)


def test_scores_only_independently_labeled_items() -> None:
    items = (
        _item(item_id="i1", label_source="human"),
        _item(item_id="i2", label_source="llm_judge"),
    )
    result = run_holdout_evaluation(items, scorer_fn=lambda item: True)
    assert result.total_items == 1  # only the human-labeled item counted


def test_llm_judge_diagnostics_are_advisory_and_never_scored() -> None:
    items = (
        _item(item_id="i1", label_source="human"),
        _item(item_id="i2", label_source="llm_judge"),
    )
    result = run_holdout_evaluation(
        items, scorer_fn=lambda item: True, judge_fn=lambda item: f"diagnostic for {item.item_id}"
    )
    assert result.llm_judge_diagnostics == ("diagnostic for i2",)
    assert result.total_items == 1  # diagnostics never inflate the scored total


def test_accuracy_reflects_scorer_results() -> None:
    items = (
        _item(item_id="i1", label_source="human"),
        _item(item_id="i2", label_source="adjudicated"),
    )
    result = run_holdout_evaluation(items, scorer_fn=lambda item: item.item_id == "i1")
    assert result.correct == 1
    assert result.total_items == 2
    assert result.accuracy == 0.5
