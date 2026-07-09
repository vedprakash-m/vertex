"""Track J (PR-11b) tests — reality-substrate context for AI narrative synthesis.

Verifies the FactAssessment → token-budgeted-prompt-line condensation that lets
the AI-generated executive summary / workstream blurbs honor the newsletter's
trust badges instead of contradicting them.

Key properties under test (each maps to a §6.10 item 6 / R18 requirement):
- Token-budget ceiling is enforced (no unbounded FactAssessment dump into prompt)
- Disputed/stale/low-truth facts are prioritised over the rest (the AI most
  needs warnings about the facts it is most likely to mis-narrate)
- The anti-contradiction directive appears in every non-empty payload
- Empty substrates produce a "do not assert confirmed" instruction
- No raw FactAssessment JSON / full evidence URI is emitted (condensation)
- Integration: _exec_ai_context_lines surfaces reality lines when a summary exists
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.commands.reality_context import (
    REALITY_CONTEXT_MAX_TOKENS,
    RealityAssessmentSummary,
    build_reality_assessment_summary,
    reality_context_lines,
)
from src.core.program_reality import FactAssessment
from src.core.truth_levels import TruthLevel


def _risk(
    *,
    id: str = "risk-1",
    title: str = "Sample risk",
    status_value: str = "open",
) -> object:
    """Minimal risk-shaped record for condensation (only title/status read)."""
    record = MagicMock()
    record.id = id
    record.title = title
    record.status = MagicMock()
    record.status.value = status_value
    record.probability = MagicMock()
    record.probability.value = "likely"
    record.impact = MagicMock()
    record.impact.value = "high"
    return record


def _decision(*, id: str = "dec-1", title: str = "Sample decision") -> object:
    record = MagicMock()
    record.id = id
    record.title = title
    record.status = MagicMock()
    record.status.value = "decided"
    record.decision = None
    record.name = None
    record.text = None
    return record


def _assessment(
    record,
    *,
    truth_level: TruthLevel = TruthLevel.HUMAN_CONFIRMED,
    disputed: bool = False,
    stale: bool = False,
    provisional: bool = False,
    evidence_count: int = 1,
) -> FactAssessment:
    return FactAssessment(
        record=record,
        fact_id=f"fact:{getattr(record, 'id', 'x')}",
        truth_level=truth_level,
        disputed=disputed,
        stale=stale,
        provisional_inputs=provisional,
        evidence=tuple(f"evidence-{i}" for i in range(evidence_count)),
        lineage=None,
    )


def _reality(
    *,
    risks=(),
    decisions=(),
    assumptions=(),
    milestones=(),
    dependencies=(),
) -> MagicMock:
    """Build a ProgramReality double returning controlled FactAssessment tuples."""
    reality = MagicMock()
    reality.risks = lambda: tuple(risks)
    reality.decisions = lambda: tuple(decisions)
    reality.assumptions = lambda: tuple(assumptions)
    reality.milestones = lambda: tuple(milestones)
    reality.dependencies = lambda: tuple(dependencies)
    return reality


# ---------------------------------------------------------------------------
# Token-budget enforcement (§6.10 item 6 / R18)
# ---------------------------------------------------------------------------


def test_token_budget_ceiling_never_exceeded() -> None:
    """The condensed payload must stay under the stated token ceiling."""
    # 500 risks, each with evidence — would be huge if naively serialized.
    risks = [
        _assessment(_risk(id=f"r{i}", title=f"Risk number {i} with a verbose description"),
                    truth_level=TruthLevel.RAW_OBSERVED, evidence_count=10)
        for i in range(500)
    ]
    summary = build_reality_assessment_summary(_reality(risks=risks))
    lines = reality_context_lines(summary)
    total_text = "\n".join(lines)
    from src.ai.context_budget import estimate_tokens
    assert estimate_tokens(total_text) <= REALITY_CONTEXT_MAX_TOKENS + 50  # +50 header slack


def test_no_raw_factassessment_json_dumped() -> None:
    """No full FactAssessment serialization (evidence URIs, lineage) reaches the prompt."""
    risk = _assessment(
        _risk(title="Sensitive title"),
        truth_level=TruthLevel.SOURCE_VALIDATED,
        disputed=True,
        evidence_count=5,
    )
    # Lineage carries provenance metadata that must NOT be dumped wholesale.
    summary = build_reality_assessment_summary(_reality(risks=(risk,)))
    lines = reality_context_lines(summary)
    joined = "\n".join(lines)
    # Evidence values are "evidence-0".."evidence-4"; none should appear verbatim.
    assert "evidence-0" not in joined
    assert "evidence-4" not in joined
    # The title IS surfaced (condensed to one line), but not as JSON.
    assert "Sensitive title" in joined
    assert "FactAssessment" not in joined


# ---------------------------------------------------------------------------
# Prioritisation (disputed > stale > low-truth > representative)
# ---------------------------------------------------------------------------


def test_disputed_facts_prioritised_over_representative() -> None:
    """Disputed facts win the token budget over undisputed ones."""
    disputed = _assessment(_risk(id="disputed-1", title="Disputed risk"), disputed=True)
    representative = _assessment(_risk(id="rep-1", title="Representative risk"),
                                 truth_level=TruthLevel.HUMAN_CONFIRMED)
    summary = build_reality_assessment_summary(_reality(risks=(representative, disputed)))
    attention = [ln for ln in summary.attention_lines if "risk:" in ln]
    # Disputed appears first (the AI most needs a warning about it).
    assert "Disputed risk" in attention[0]
    assert "DISPUTED" in attention[0]


def test_stale_facts_prioritised_over_low_truth() -> None:
    stale = _assessment(_risk(id="stale-1", title="Stale risk"), stale=True)
    low_truth = _assessment(_risk(id="low-1", title="Low truth risk"),
                            truth_level=TruthLevel.RAW_OBSERVED)
    summary = build_reality_assessment_summary(_reality(risks=(low_truth, stale)))
    attention_ids = [ln for ln in summary.attention_lines if "risk:" in ln]
    # Stale bucket comes before low-truth bucket.
    assert "Stale risk" in attention_ids[0]
    assert "STALE" in attention_ids[0]


def test_low_truth_facts_flagged_in_attention() -> None:
    low = _assessment(_risk(id="low-1", title="Unvalidated"), truth_level=TruthLevel.RAW_OBSERVED)
    summary = build_reality_assessment_summary(_reality(risks=(low,)))
    line = next(ln for ln in summary.attention_lines if "Unvalidated" in ln)
    assert "○" in line  # RAW_OBSERVED glyph


# ---------------------------------------------------------------------------
# Anti-contradiction directive (§6.10 item 4)
# ---------------------------------------------------------------------------


def test_anti_contradiction_directive_present() -> None:
    """Every non-empty payload carries the directive to honor trust badges."""
    risk = _assessment(_risk(title="Some risk"), truth_level=TruthLevel.CORROBORATED)
    summary = build_reality_assessment_summary(_reality(risks=(risk,)))
    lines = reality_context_lines(summary)
    directive = [ln for ln in lines if "Do not state" in ln or "honor" in ln.lower()]
    assert directive, "Anti-contradiction directive must appear in non-empty payloads"


def test_header_states_per_family_denominators() -> None:
    risk1 = _assessment(_risk(id="r1"), disputed=True)
    risk2 = _assessment(_risk(id="r2"), truth_level=TruthLevel.HUMAN_CONFIRMED)
    dec1 = _assessment(_decision(id="d1"), stale=True)
    summary = build_reality_assessment_summary(_reality(risks=(risk1, risk2), decisions=(dec1,)))
    lines = reality_context_lines(summary)
    header = next(ln for ln in lines if "Reality substrate confidence" in ln)
    assert "2 risk" in header
    assert "1 disputed" in header
    assert "1 decision" in header
    assert "1 stale" in header


# ---------------------------------------------------------------------------
# Empty substrate handling
# ---------------------------------------------------------------------------


def test_empty_substrate_produces_caution_instruction() -> None:
    summary = build_reality_assessment_summary(_reality())
    assert summary.empty is True
    lines = reality_context_lines(summary)
    joined = "\n".join(lines)
    assert "no reconciled facts" in joined.lower() or "unverified" in joined.lower()


def test_single_family_failure_does_not_poison_summary() -> None:
    """If one accessor raises, others still populate the summary."""
    reality = MagicMock()
    reality.risks = MagicMock(side_effect=RuntimeError("schema mismatch"))
    reality.decisions = lambda: (_assessment(_decision(id="d1")),)
    reality.assumptions = lambda: ()
    reality.milestones = lambda: ()
    reality.dependencies = lambda: ()
    summary = build_reality_assessment_summary(reality)
    assert summary.empty is False
    assert summary.family_totals.get("decision") == 1


# ---------------------------------------------------------------------------
# Integration: _exec_ai_context_lines surfaces reality lines
# ---------------------------------------------------------------------------


def test_exec_ai_context_lines_include_reality_lines() -> None:
    """The exec-summary context builder surfaces reality-substrate lines."""
    from src.commands import report_ai
    risk = _assessment(_risk(id="r1", title="Integration risk"), disputed=True)
    summary = build_reality_assessment_summary(_reality(risks=(risk,)))
    ai_context = report_ai._DraftAIContext(
        program_id="demo",
        programs_root=__import__("pathlib").Path("."),
        workstreams=(),
        rolling_summaries={},
        approved_signals=(),
        drift_patterns=(),
        dependency_cascades=(),
        reality_assessments=summary,
    )
    lines = report_ai._exec_ai_context_lines(None, ai_context)
    assert any("Integration risk" in ln for ln in lines)
    assert any("DISPUTED" in ln for ln in lines)


def test_exec_ai_context_lines_omit_reality_when_none() -> None:
    """When no summary is set (pre-Track-J / offline), no reality lines appear."""
    from src.commands import report_ai
    ai_context = report_ai._DraftAIContext(
        program_id="demo",
        programs_root=__import__("pathlib").Path("."),
        workstreams=(),
        rolling_summaries={},
        approved_signals=(),
        drift_patterns=(),
        dependency_cascades=(),
        reality_assessments=None,
    )
    lines = report_ai._exec_ai_context_lines(None, ai_context)
    assert not any("Reality substrate" in ln for ln in lines)


def test_reality_context_lines_none_summary_returns_empty() -> None:
    """The _reality_context_lines bridge returns () for None (byte-identical fallback)."""
    from src.commands.report_ai import _reality_context_lines
    assert _reality_context_lines(None) == ()
