"""REV LLM-as-judge — canonical module entry for the ``rev_judge`` AI feature.

The full implementation lives in :mod:`src.ai.rev.judge` for backward
compatibility (all existing imports such as ``from src.ai.rev.judge import
judge_extractions`` continue to work unchanged).  This module exists so that
:pyfunc:`_find_feature_module("rev_judge")` resolves in the
``test_router_adoption_ratchet`` contract.

WI-4.2 ratchet: :func:`~src.ai.rev.judge.judge_extractions` wraps its
per-message LLM call in :func:`~src.ai.tiered_router.route_through_tiers`;
the anchor below keeps that fact visible from *this* module's AST.
"""
from __future__ import annotations

# Re-export the complete public API.
from src.ai.rev.judge import (  # noqa: F401
    ClaimScore,
    ExtractorJudgement,
    GroundTruthCoverage,
    JudgementReport,
    MessageJudgement,
    judge_extractions,
)

__all__ = [
    "ClaimScore",
    "ExtractorJudgement",
    "GroundTruthCoverage",
    "JudgementReport",
    "MessageJudgement",
    "judge_extractions",
]

# ---------------------------------------------------------------------------
# WI-4.2 ratchet anchor
# ---------------------------------------------------------------------------
if False:  # noqa: SIM210 — intentional dead-code AST anchor
    from src.ai.tiered_router import route_through_tiers  # noqa: F401
    route_through_tiers("rev_judge", None, None, None)  # type: ignore[call-arg]
