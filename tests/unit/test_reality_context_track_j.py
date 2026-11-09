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

import logging
import sys
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
    total_text = "\n".join(f"- {line}" for line in lines)
    from src.ai.context_budget import estimate_tokens
    assert estimate_tokens(total_text) <= REALITY_CONTEXT_MAX_TOKENS


def test_oversized_first_fact_does_not_hide_smaller_or_later_families(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    oversized = _assessment(_risk(id="large", title="A" * 1200), disputed=True)
    smaller = _assessment(_risk(id="small", title="Small disputed risk"), disputed=True)
    decision = _assessment(_decision(title="Later decision"), stale=True)

    summary = build_reality_assessment_summary(
        _reality(risks=(oversized, smaller), decisions=(decision,)),
        max_tokens=900,
    )
    rendered = "\n".join(f"- {line}" for line in reality_context_lines(summary))

    assert len(rendered) <= 900
    assert summary.family_totals == {"risk": 2, "decision": 1}
    assert any("Small disputed risk" in line for line in summary.attention_lines)
    assert "A" * 1200 not in rendered
    assert "omitted" in rendered.lower()
    assert "all reconciled facts are human-confirmed" not in rendered.lower()


def test_oversized_later_fact_does_not_hide_smaller_facts_in_family(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    facts = (
        _assessment(_risk(id="first", title="First small risk"), disputed=True),
        _assessment(_risk(id="large", title="A" * 250), disputed=True),
        _assessment(_risk(id="last", title="Later small risk"), disputed=True),
    )

    summary = build_reality_assessment_summary(_reality(risks=facts), max_tokens=900)
    rendered = "\n".join(f"- {line}" for line in reality_context_lines(summary))

    assert len(rendered) <= 900
    assert "First small risk" in rendered
    assert "Later small risk" in rendered
    assert "A" * 250 not in rendered
    assert "omitted" in rendered.lower()


def test_summary_ceiling_includes_bullet_headers_caution_and_separators(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    facts = tuple(
        _assessment(_risk(id=f"r{i}", title=f"Risk {i} " + "x" * 38), disputed=True)
        for i in range(4)
    )
    summary = build_reality_assessment_summary(
        _reality(risks=facts, decisions=facts, assumptions=facts, milestones=facts, dependencies=facts),
        max_tokens=600,
    )
    rendered = "\n".join(f"- {line}" for line in reality_context_lines(summary))

    assert len(rendered) <= 600
    assert summary.family_totals == {
        "risk": 4,
        "decision": 4,
        "assumption": 4,
        "milestone": 4,
        "dependency": 4,
    }
    assert "Do not state" in rendered
    assert "omitted" in rendered.lower()


def test_empty_detail_selection_does_not_claim_facts_are_confirmed() -> None:
    summary = RealityAssessmentSummary(
        family_totals={"risk": 1},
        disputed_counts={"risk": 1},
    )

    rendered = "\n".join(reality_context_lines(summary)).lower()
    assert "1 disputed" in rendered
    assert "omitted" in rendered or "not included" in rendered
    assert "all reconciled facts are human-confirmed" not in rendered


@pytest.mark.parametrize("budget", [0, -1])
def test_nonpositive_budget_fails_before_reading_facts(monkeypatch, budget: int) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    reality = _reality()
    reality.risks = MagicMock()

    with pytest.raises(ValueError, match="max_tokens"):
        build_reality_assessment_summary(reality, max_tokens=budget)
    reality.risks.assert_not_called()


@pytest.mark.parametrize("has_fact", [False, True])
def test_budget_too_small_for_mandatory_summary_fails(monkeypatch, has_fact: bool) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    facts = (_assessment(_risk()),) if has_fact else ()

    with pytest.raises(ValueError, match="max_tokens"):
        build_reality_assessment_summary(_reality(risks=facts), max_tokens=20)


def test_untruncated_summary_keeps_fact_flags_and_caution(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    fact = _assessment(_risk(title="Short risk"), disputed=True, stale=True, provisional=True)
    summary = build_reality_assessment_summary(_reality(risks=(fact,)), max_tokens=900)
    lines = reality_context_lines(summary)
    detail = next(line for line in lines if "Short risk" in line)

    assert summary.family_totals == {"risk": 1}
    assert detail.index("DISPUTED") < detail.index("Short risk")
    assert "STALE" in detail and "PROVISIONAL" in detail
    assert "omitted" not in "\n".join(lines).lower()
    assert any("Do not state" in line for line in lines)
    assert len("\n".join(f"- {line}" for line in lines)) <= 900


def test_summary_exact_budget_boundary_and_one_below(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    reality = _reality(risks=(_assessment(_risk(title="Boundary risk"), disputed=True),))
    unrestricted = build_reality_assessment_summary(reality, max_tokens=900)
    exact_budget = len("\n".join(f"- {line}" for line in reality_context_lines(unrestricted)))

    exact = build_reality_assessment_summary(reality, max_tokens=exact_budget)
    below = build_reality_assessment_summary(reality, max_tokens=exact_budget - 1)

    assert exact == unrestricted
    assert len("\n".join(f"- {line}" for line in reality_context_lines(exact))) == exact_budget
    assert len("\n".join(f"- {line}" for line in reality_context_lines(below))) <= exact_budget - 1
    assert not below.attention_lines
    assert "not included" in "\n".join(reality_context_lines(below))


def test_summary_ceiling_with_local_tiktoken_encoding(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "tiktoken", raising=False)
    tiktoken = pytest.importorskip("tiktoken")
    encoding = tiktoken.Encoding(
        name="synthetic_bytes",
        pat_str=r"(?s).+",
        mergeable_ranks={bytes([value]): value for value in range(256)},
        special_tokens={},
    )
    monkeypatch.setattr(tiktoken, "encoding_for_model", lambda _model: encoding)
    reality = _reality(
        risks=(
            _assessment(_risk(id="large", title="合" * 500), disputed=True),
            _assessment(_risk(id="small", title="Small risk"), stale=True),
        ),
    )

    summary = build_reality_assessment_summary(reality)
    rendered = "\n".join(f"- {line}" for line in reality_context_lines(summary))

    assert len(encoding.encode(rendered)) <= REALITY_CONTEXT_MAX_TOKENS
    assert "Small risk" in rendered
    assert "合" * 500 not in rendered
    assert "omitted" in rendered.lower()


@pytest.mark.parametrize("rolling_count", [0, 10])
def test_trust_caution_and_flags_survive_real_prompt_clipping(monkeypatch, tmp_path, rolling_count: int) -> None:
    from src.ai import blurb_generator, exec_summary_drafter
    from src.commands import report_ai

    monkeypatch.setattr(
        "src.commands.reality_context.estimate_tokens",
        lambda text: max(1, len(text) // 4) if text else 0,
    )
    fact = _assessment(
        _risk(title="Long risk " + "x" * 3000),
        disputed=True,
        stale=True,
        provisional=True,
    )
    summary = build_reality_assessment_summary(_reality(risks=(fact,)))
    workstream_ids = tuple(f"ws-{index}" for index in range(rolling_count))
    ai_context = report_ai._DraftAIContext(
        program_id="synthetic",
        programs_root=tmp_path,
        workstreams=(),
        rolling_summaries={workstream_id: "rolling context" for workstream_id in workstream_ids},
        approved_signals=(),
        drift_patterns=(),
        dependency_cascades=(),
        reality_assessments=summary,
    )
    monkeypatch.setattr(report_ai, "_matching_program_workstream_ids", lambda *args: workstream_ids)
    for name in (
        "_edit_pattern_context_lines",
        "summarize_recent_calibration",
        "_confidence_band_context_lines",
        "_prompt_version_confidence_context_lines",
        "_prompt_version_context_lines",
        "_prompt_version_model_context_lines",
        "_model_context_lines",
        "_lt_deck_context_lines",
    ):
        monkeypatch.setattr(report_ai, name, lambda *args, **kwargs: ())

    section_context = report_ai._section_ai_context_lines((), None, ai_context)
    exec_context = report_ai._exec_ai_context_lines(None, ai_context)
    section_prompt = blurb_generator._build_user_prompt(
        workstream_name="Synthetic",
        eligible_items=(),
        evidence_by_item={},
        deltas=(),
        editorial_rules=MagicMock(),
        program_context=None,
        supplemental_context=section_context,
        max_words=None,
    )
    exec_prompt = exec_summary_drafter._build_user_prompt(
        ranked_changes=(),
        editorial_rules=MagicMock(),
        program_context=None,
        supplemental_context=exec_context,
        max_words=100,
    )

    assert "Do not state" in section_prompt
    assert "Do not state" in exec_prompt
    if rolling_count:
        assert "Rolling summary [ws-0]" in section_prompt
        assert "supplemental_context_omitted:" in section_prompt
    else:
        section_detail = next(line for line in section_prompt.splitlines() if "Long risk" in line)
        assert "DISPUTED" in section_detail and "STALE" in section_detail and "PROVISIONAL" in section_detail
        assert section_detail.index("DISPUTED") < section_detail.index("Long risk")
        assert section_detail.endswith("...")


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


def test_header_counts_overlapping_trust_properties_independently(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    facts = (
        _assessment(_risk(id="overlap"), truth_level=TruthLevel.RAW_OBSERVED, disputed=True, stale=True),
        _assessment(_risk(id="stale"), stale=True),
        _assessment(_risk(id="low"), truth_level=TruthLevel.SOURCE_VALIDATED),
    )

    summary = build_reality_assessment_summary(_reality(risks=facts))
    header = reality_context_lines(summary)[0]

    assert summary.family_totals == {"risk": 3}
    assert summary.disputed_counts == {"risk": 1}
    assert summary.stale_counts == {"risk": 2}
    assert summary.low_truth_counts == {"risk": 2}
    assert "1 disputed" in header
    assert "2 stale" in header
    assert "2 low-truth" in header


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


@pytest.mark.parametrize(
    ("accessor", "family"),
    [
        ("risks", "risk"),
        ("decisions", "decision"),
        ("assumptions", "assumption"),
        ("milestones", "milestone"),
        ("dependencies", "dependency"),
    ],
)
def test_failed_reality_family_is_visible_without_sensitive_error_text(
    monkeypatch, caplog, accessor: str, family: str,
) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    reality = _reality(risks=(_assessment(_risk(title="Known synthetic risk")),))
    setattr(reality, accessor, MagicMock(side_effect=RuntimeError("private-path-and-fact")))

    with caplog.at_level(logging.WARNING, logger="src.commands.reality_context"):
        summary = build_reality_assessment_summary(reality)

    rendered = "\n".join(reality_context_lines(summary))
    assert "unavailable" in rendered.lower()
    assert family in rendered.splitlines()[0]
    assert family in caplog.text and "RuntimeError" in caplog.text
    assert "private-path-and-fact" not in rendered
    assert "private-path-and-fact" not in caplog.text
    assert len("\n".join(f"- {line}" for line in reality_context_lines(summary))) <= REALITY_CONTEXT_MAX_TOKENS
    assert summary.unavailable_families == (family,)


def test_fully_unavailable_reality_is_not_described_as_successfully_empty(monkeypatch, caplog) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    reality = _reality()
    for accessor in ("risks", "decisions", "assumptions", "milestones", "dependencies"):
        setattr(reality, accessor, MagicMock(side_effect=RuntimeError("private failure")))

    with caplog.at_level(logging.WARNING, logger="src.commands.reality_context"):
        summary = build_reality_assessment_summary(reality)
    rendered = "\n".join(reality_context_lines(summary)).lower()

    assert summary.empty
    assert "unavailable" in rendered
    assert "no reconciled facts available" not in rendered
    assert "private failure" not in rendered and "private failure" not in caplog.text
    assert summary.unavailable_families == ("risk", "decision", "assumption", "milestone", "dependency")


def test_successfully_empty_reality_has_no_unavailable_warning(monkeypatch, caplog) -> None:
    monkeypatch.setattr("src.commands.reality_context.estimate_tokens", len)
    with caplog.at_level(logging.WARNING, logger="src.commands.reality_context"):
        summary = build_reality_assessment_summary(_reality())
    rendered = "\n".join(reality_context_lines(summary)).lower()
    assert summary.empty
    assert "no reconciled facts" in rendered
    assert "unavailable" not in rendered
    assert not caplog.records


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
