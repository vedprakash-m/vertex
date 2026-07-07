"""GAP-5: Verify that _make_assessment uses the real derive_truth_level engine
when a fact and TrustContext are available.

The Phase-1 stub (_derive_truth_level_phase1) only checks fact_type membership;
the real engine (derive_truth_level) checks governance lock, review_state, and
source authority. These tests verify the wiring introduced in GAP-5.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.program_reality import _make_assessment
from src.core.truth_levels import TruthLevel
from src.core.truth_model import TruthContext


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fake_fact(
    *,
    fact_type: str = "action.item",
    natural_key: str = "nk-1",
    review_state: str = "accepted",
    write_authority: str = "human",
    accepted_by: str | None = "test-user",
):
    """Build a minimal stand-in for ProgramFactRevision."""
    class _FakeFact:
        def build_lineage(self):
            return None
    f = _FakeFact()
    f.fact_type = fact_type
    f.natural_key = natural_key
    f.review_state = review_state
    f.write_authority = write_authority
    f.accepted_by = accepted_by
    f.fact_id = "fact-001"
    f.source_signal_ids = ()
    f.entity_refs = ("ref-1",)
    f.recorded_at = _utcnow()
    return f


def _empty_truth_ctx() -> TruthContext:
    return TruthContext(
        baseline_locked_keys=frozenset(),
        suspended_sources=frozenset(),
        corroborated_keys=frozenset(),
    )


def _locked_truth_ctx(natural_key: str) -> TruthContext:
    return TruthContext(
        baseline_locked_keys=frozenset({natural_key}),
        suspended_sources=frozenset(),
        corroborated_keys=frozenset(),
    )


# ---------------------------------------------------------------------------
# Phase-1 stub fallback: truth_ctx=None → static table used
# ---------------------------------------------------------------------------

def test_make_assessment_falls_back_to_stub_when_no_truth_ctx() -> None:
    """Without a TrustContext, _make_assessment must use the Phase-1 stub."""
    fact = _fake_fact(fact_type="action.item", review_state="")
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="action.item",
        as_of=_utcnow(),
        truth_ctx=None,
    )
    # Phase-1 stub: management fact → HUMAN_CONFIRMED
    assert assessment.truth_level == TruthLevel.HUMAN_CONFIRMED


def test_make_assessment_fallback_raw_for_non_management_fact() -> None:
    fact = _fake_fact(fact_type="signal.observation", review_state="")
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="signal.observation",
        as_of=_utcnow(),
        truth_ctx=None,
    )
    assert assessment.truth_level == TruthLevel.RAW_OBSERVED


# ---------------------------------------------------------------------------
# Real engine wiring: truth_ctx provided → derive_truth_level called
# ---------------------------------------------------------------------------

def test_make_assessment_uses_real_engine_governance_locked() -> None:
    """Governance-locked natural key → GOVERNANCE_LOCKED regardless of fact_type."""
    fact = _fake_fact(fact_type="action.item", natural_key="locked-nk", review_state="")
    ctx = _locked_truth_ctx("locked-nk")
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="action.item",
        as_of=_utcnow(),
        truth_ctx=ctx,
    )
    assert assessment.truth_level == TruthLevel.GOVERNANCE_LOCKED


def test_make_assessment_uses_real_engine_human_confirmed_on_accepted_review() -> None:
    """accepted review_state + human write_authority → HUMAN_CONFIRMED via real engine."""
    fact = _fake_fact(fact_type="signal.observation", review_state="accepted", write_authority="human")
    ctx = _empty_truth_ctx()
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="signal.observation",
        as_of=_utcnow(),
        truth_ctx=ctx,
    )
    # Phase-1 stub would return RAW_OBSERVED for signal.observation;
    # the real engine returns HUMAN_CONFIRMED because review_state=accepted + write_authority=human.
    assert assessment.truth_level == TruthLevel.HUMAN_CONFIRMED


def test_make_assessment_no_fact_uses_stub() -> None:
    """When fact is None, truth_ctx is irrelevant — stub provides the default."""
    ctx = _locked_truth_ctx("any-nk")
    assessment = _make_assessment(
        object(),
        fact=None,
        fact_type="risk.entry",
        as_of=_utcnow(),
        truth_ctx=ctx,
    )
    # risk.entry is in _MANAGEMENT_FACT_TYPES → stub returns HUMAN_CONFIRMED
    assert assessment.truth_level == TruthLevel.HUMAN_CONFIRMED


# ---------------------------------------------------------------------------
# GAP-5 remaining: disputed + provisional_inputs wiring
# ---------------------------------------------------------------------------

def test_make_assessment_disputed_flag_set_when_natural_key_conflicts() -> None:
    """disputed=True when fact.natural_key in disputed_natural_keys (WI-3.2b)."""
    fact = _fake_fact(natural_key="nk-conflict")
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="action.item",
        as_of=_utcnow(),
        disputed_natural_keys=frozenset({"nk-conflict", "nk-other"}),
    )
    assert assessment.disputed is True


def test_make_assessment_disputed_flag_false_when_no_match() -> None:
    """disputed=False when natural_key not in disputed set."""
    fact = _fake_fact(natural_key="nk-clean")
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="action.item",
        as_of=_utcnow(),
        disputed_natural_keys=frozenset({"nk-conflict"}),
    )
    assert assessment.disputed is False


def test_make_assessment_disputed_flag_false_when_no_set_provided() -> None:
    """disputed=False when disputed_natural_keys is None (default)."""
    fact = _fake_fact(natural_key="nk-1")
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="action.item",
        as_of=_utcnow(),
    )
    assert assessment.disputed is False


def test_make_assessment_provisional_flag_set_when_source_signal_pending() -> None:
    """provisional_inputs=True when fact.source_signal_ids intersects pending signals (WI-3.2a)."""
    class _FactWithSignals:
        fact_type = "action.item"
        natural_key = "nk-1"
        review_state = "accepted"
        write_authority = "human"
        accepted_by = "user"
        fact_id = "fact-001"
        source_signal_ids = ("sig-pending", "sig-accepted")
        entity_refs = ("ref-1",)
        recorded_at = datetime.now(timezone.utc)
        def build_lineage(self): return None

    assessment = _make_assessment(
        object(),
        fact=_FactWithSignals(),
        fact_type="action.item",
        as_of=datetime.now(timezone.utc),
        provisional_signal_ids=frozenset({"sig-pending"}),
    )
    assert assessment.provisional_inputs is True


def test_make_assessment_provisional_flag_false_when_all_signals_accepted() -> None:
    """provisional_inputs=False when none of source_signal_ids are pending."""
    class _FactWithSignals:
        fact_type = "action.item"
        natural_key = "nk-1"
        review_state = "accepted"
        write_authority = "human"
        accepted_by = "user"
        fact_id = "fact-001"
        source_signal_ids = ("sig-accepted-1", "sig-accepted-2")
        entity_refs = ("ref-1",)
        recorded_at = datetime.now(timezone.utc)
        def build_lineage(self): return None

    assessment = _make_assessment(
        object(),
        fact=_FactWithSignals(),
        fact_type="action.item",
        as_of=datetime.now(timezone.utc),
        provisional_signal_ids=frozenset({"sig-other-pending"}),
    )
    assert assessment.provisional_inputs is False


def test_make_assessment_provisional_flag_false_when_no_set_provided() -> None:
    """provisional_inputs=False when provisional_signal_ids is None (default)."""
    fact = _fake_fact()
    assessment = _make_assessment(
        object(),
        fact=fact,
        fact_type="action.item",
        as_of=datetime.now(timezone.utc),
    )
    assert assessment.provisional_inputs is False
