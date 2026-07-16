"""ADF-W0.15 (Section 8.15.3): the human holdout lane runner.

"Holdout re-scoring is required before enforce-mode promotion or deployment
change... LLM-as-judge may generate diagnostics but cannot be the sole
quality label or promotion authority. CI evaluates deterministic fields,
evidence references, schema, safety, and approved semantic rubric outputs;
human labels remain the certification ground truth."

This module is the refusal gate: it will not run an evaluation that would
silently masquerade as certification-grade when the underlying corpus
doesn't actually meet that bar. It is deliberately NOT wired to a live
corpus yet -- see governance/eval/corpus-schema.md's "Status" section for
why (no real independently-labeled holdout corpus exists today).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tests.eval.corpus_schema import CorpusItem


class HoldoutLaneRefused(Exception):
    """Raised when the holdout lane refuses to run -- never silently
    degrades to a weaker evaluation."""


@dataclass(frozen=True, slots=True)
class HoldoutResult:
    family: str
    total_items: int
    correct: int
    accuracy: float
    llm_judge_diagnostics: tuple[str, ...]  # advisory only, never scored


def run_holdout_evaluation(
    items: tuple[CorpusItem, ...],
    *,
    scorer_fn: Callable[[CorpusItem], bool],
    judge_fn: Callable[[CorpusItem], str] | None = None,
) -> HoldoutResult:
    """Scores ``items`` against ``scorer_fn`` (a deterministic or
    model-under-test prediction check) using only independently-labeled
    (human/adjudicated) items as ground truth. Refuses outright rather than
    silently proceeding when:

    - the corpus is empty;
    - every item's split is not ``holdout``;
    - zero items carry an independent label (Section 8.15.3's own
      requirement -- an all-``llm_judge`` corpus cannot certify anything).

    ``judge_fn``, if supplied, may generate advisory diagnostics for
    ``llm_judge``-sourced items -- these are recorded but never counted
    toward ``accuracy``, matching "cannot be the sole quality label or
    promotion authority" precisely.
    """
    if not items:
        raise HoldoutLaneRefused("Empty corpus: holdout lane refuses to run with zero items.")

    holdout_items = tuple(item for item in items if item.split == "holdout")
    if not holdout_items:
        raise HoldoutLaneRefused(
            "No items in the 'holdout' split: holdout lane refuses to score train/dev rows as certification evidence."
        )

    independent_items = tuple(item for item in holdout_items if item.is_independently_labeled)
    if not independent_items:
        raise HoldoutLaneRefused(
            "Zero independently-labeled (human/adjudicated) items in the holdout split: "
            "an all-llm_judge corpus cannot be certification ground truth (Section 8.15.3)."
        )

    families = {item.family for item in independent_items}
    if len(families) != 1:
        raise HoldoutLaneRefused(f"Mixed families in one holdout run: {sorted(families)}. Score one family at a time.")

    correct = sum(1 for item in independent_items if scorer_fn(item))
    total = len(independent_items)

    diagnostics: tuple[str, ...] = ()
    if judge_fn is not None:
        llm_judge_only = tuple(item for item in holdout_items if item.label_source == "llm_judge")
        diagnostics = tuple(judge_fn(item) for item in llm_judge_only)

    return HoldoutResult(
        family=families.pop(),
        total_items=total,
        correct=correct,
        accuracy=correct / total,
        llm_judge_diagnostics=diagnostics,
    )


__all__ = ["HoldoutLaneRefused", "HoldoutResult", "run_holdout_evaluation"]
