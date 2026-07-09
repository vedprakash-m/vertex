"""Track J (PR-11b) — reality-substrate context for AI narrative synthesis.

The newsletter's AI-generated executive summary and workstream blurbs are the
most-read content in the publication. Before this module existed, the AI
drafters consumed ``bundle.program_context`` (a ``NarrativeProgramContext``
parsed from ``program.yaml``) as their *only* program-narrative input. That
representation carries no notion of truth level, dispute, staleness, evidence,
or lineage — so the AI could confidently narrate a risk as "settled" in prose
even when the structured table two inches below it carried a ``[DISPUTED]``
trust badge. A newsletter whose structured tables and AI narrative can visibly
contradict each other is not "a reconciled model of program truth" (PRD §1.1).

This module closes that gap by sourcing a compact, **token-budgeted** summary
of the program's reality substrate (``FactAssessment`` tuples from
``ProgramReality``) into the AI prompt's supplemental context lines. It is the
AI-narrative analogue of the trust badges Track B added to the structured
newsletter sections: the same truth/dispute/staleness signal, in prompt form.

Zone discipline (why this lives in ``src/commands/`` and not ``src/ai/``):
``src/ai/`` is Zone B (shared AI infrastructure) and is kept free of
``src.core.program_reality`` imports by the architecture-fitness contract. The
``FactAssessment`` → prompt-line condensation is inherently a bridge between the
reality substrate (Zone A core) and the AI drafters (Zone B), so it belongs in
the command layer (where ``report_ai.py`` already performs this bridging).

Token-budget strategy (§6.10 item 6, R18):
A raw serialization of ``FactAssessment`` tuples is explicitly disallowed — each
fact carries ``truth_level``/``disputed``/``stale``/``evidence``/``lineage``,
multiplied across every fact in a program, which would risk token-limit
exhaustion and uncontrolled per-issue LLM cost. Instead this module renders a
**minimalist per-fact summary line** (truth-level glyph + one-line title + a
short risk/status marker), prioritises the facts the AI is most likely to
mis-narrate (disputed, stale, low-truth), and enforces a hard token ceiling via
``src.ai.context_budget.estimate_tokens``. No ``FactAssessment`` JSON is ever
dumped into a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.ai.context_budget import estimate_tokens
from src.core.program_reality import FactAssessment, ProgramReality
from src.core.truth_levels import TruthLevel

# Token-budget ceiling for the entire reality-substrate context payload injected
# into one AI drafter prompt. Measured against the pre-Track-J baseline (zero
# reality payload) — chosen to be materially smaller than a typical workstream
# blurb's existing supplemental context so it cannot dominate the prompt, while
# still large enough to surface every disputed/stale/low-truth fact plus a
# representative sample of the rest. This is the hard ceiling mandated by §6.10
# item 6 / R18; the condensation logic below never exceeds it.
REALITY_CONTEXT_MAX_TOKENS = 900

# Within the token ceiling, how many facts from each "attention" bucket to show.
# Disputed/stale/low-truth facts are surfaced first (the AI is most likely to
# mis-narrate these); remaining budget covers a representative sample of the
# rest. These are caps, not targets — the token ceiling is the true bound.
_MAX_DISPUTED = 12
_MAX_STALE = 12
_MAX_LOW_TRUTH = 12
_MAX_REPRESENTATIVE = 8

# Truth levels below HUMAN_CONFIRMED. Facts at these levels have not been
# confirmed by a human/governance act — the AI should treat them as provisional
# and avoid stating them as settled facts. (Phase-1 note: levels are currently
# statically derived by category, not computed per-fact — see §1's phase-boundary
# honesty note. Surfacing them here is still correct: the AI must not contradict
# the structured table's badge, whatever level that badge shows.)
_LOW_TRUTH_LEVELS: frozenset[TruthLevel] = frozenset(
    {TruthLevel.RAW_OBSERVED, TruthLevel.SOURCE_VALIDATED}
)

# Compact glyph prefix per truth level, matching the newsletter's trust-badge
# vocabulary (§3.6b) so the AI sees the same signal the reader sees.
_TRUTH_GLYPH: dict[TruthLevel, str] = {
    TruthLevel.RAW_OBSERVED: "○",
    TruthLevel.SOURCE_VALIDATED: "●",
    TruthLevel.CORROBORATED: "◆",
    TruthLevel.HUMAN_CONFIRMED: "✔",
    TruthLevel.GOVERNANCE_LOCKED: "🔒",
}


@dataclass(frozen=True, slots=True)
class RealityAssessmentSummary:
    """A compact, token-bounded projection of a program's reality substrate.

    Built once per AI synthesis pass from ``ProgramReality`` (so a single load
    feeds both the exec-summary and every workstream blurb), then rendered to
    prompt lines by ``reality_context_lines``. Carries *summary counts* (which
    can be stated cheaply) plus *per-fact attention lines* (the facts the AI is
    most likely to mis-narrate). Never carries raw ``FactAssessment`` tuples or
    full evidence/lineage payloads — those are condensed to one line each.
    """

    # Per-family totals, so the AI knows the denominator ("of 14 risks, 2 are
    # disputed") without every fact needing its own line.
    family_totals: dict[str, int] = field(default_factory=dict)
    # Counts of facts the AI must be careful about, per family.
    disputed_counts: dict[str, int] = field(default_factory=dict)
    stale_counts: dict[str, int] = field(default_factory=dict)
    low_truth_counts: dict[str, int] = field(default_factory=dict)
    # One-line summaries of the specific facts the AI is most likely to
    # mis-narrate (disputed first, then stale, then low-truth, then a
    # representative sample). Already prioritised and token-capped at build time.
    attention_lines: tuple[str, ...] = ()
    # True when the reality substrate had no usable content (e.g. a program with
    # no bridged facts yet). Lets the prompt instruct the AI to avoid asserting
    # confirmation status it cannot verify, rather than silently omitting the
    # topic.
    empty: bool = False


def _fact_title(record: Any) -> str:
    """Best-effort single-line title for any domain record wrapped by a FactAssessment."""
    # Each domain model uses a different title-ish field; pick the most descriptive.
    for attr in ("title", "name", "text", "decision"):
        value = getattr(record, attr, None)
        if isinstance(value, str) and value.strip():
            return _single_line(value)
    # Fallbacks for families without a prose title.
    identifier = getattr(record, "id", None) or "<unknown>"
    return f"{type(record).__name__} {identifier}"


def _single_line(text: str) -> str:
    """Collapse a title to one trimmed line for compact prompt rendering."""
    return " ".join(text.strip().split())


def _risk_marker(record: Any) -> str:
    """A compact status/score marker for risk rows, empty for other families."""
    # RiskEntry carries probability+impact+status; surface the most decision-relevant bits.
    status = getattr(record, "status", None)
    parts: list[str] = []
    status_value = getattr(status, "value", status)
    if status_value:
        parts.append(str(status_value))
    prob = getattr(record, "probability", None)
    impact = getattr(record, "impact", None)
    prob_value = getattr(prob, "value", None)
    impact_value = getattr(impact, "value", None)
    if prob_value and impact_value:
        parts.append(f"{prob_value}/{impact_value}")
    return f" [{', '.join(parts)}]" if parts else ""


def _status_marker(record: Any) -> str:
    """A compact status marker for non-risk families (milestone/decision/assumption/...)."""
    status = getattr(record, "status", None)
    if status is None:
        return ""
    value = getattr(status, "value", status)
    if not value:
        return ""
    return f" [{value}]"


def _fact_attention_line(family: str, assessment: FactAssessment) -> str:
    """Render one FactAssessment as a single, compact, prompt-safe line.

    Format: ``{glyph} {family}: {title}{marker}{flags}`` — e.g.
    ``◆ risk: API migration timeline slips [open, likely/high] [DISPUTED]``.
    Deliberately omits full evidence URIs and lineage metadata (token budget).
    """
    glyph = _TRUTH_GLYPH.get(assessment.truth_level, "○")
    record = assessment.record
    title = _fact_title(record)
    marker = _risk_marker(record) if family == "risk" else _status_marker(record)
    flags: list[str] = []
    if assessment.disputed:
        flags.append("DISPUTED")
    if assessment.stale:
        flags.append("STALE")
    if assessment.provisional_inputs:
        flags.append("PROVISIONAL")
    flag_text = f" [{'/'.join(flags)}]" if flags else ""
    return f"{glyph} {family}: {title}{marker}{flag_text}"


def _prioritised_facts(
    assessments: tuple[FactAssessment, ...],
) -> tuple[tuple[FactAssessment, ...], tuple[FactAssessment, ...], tuple[FactAssessment, ...], tuple[FactAssessment, ...]]:
    """Split facts into attention buckets, most-attention first.

    Returns (disputed, stale, low_truth, representative). A fact appears in the
    highest-priority bucket it qualifies for (disputed > stale > low_truth > rest)
    so the token budget fills with the facts the AI most needs a warning about.
    """
    disputed: list[FactAssessment] = []
    stale: list[FactAssessment] = []
    low_truth: list[FactAssessment] = []
    representative: list[FactAssessment] = []
    for a in assessments:
        if a.disputed:
            disputed.append(a)
        elif a.stale:
            stale.append(a)
        elif a.truth_level in _LOW_TRUTH_LEVELS:
            low_truth.append(a)
        else:
            representative.append(a)
    return tuple(disputed), tuple(stale), tuple(low_truth), tuple(representative)


def _render_attention_lines(
    family: str,
    disputed: tuple[FactAssessment, ...],
    stale: tuple[FactAssessment, ...],
    low_truth: tuple[FactAssessment, ...],
    representative: tuple[FactAssessment, ...],
    *,
    max_tokens: int,
) -> tuple[str, ...]:
    """Render attention lines, disputed/stale/low-truth first, under the token ceiling."""
    lines: list[str] = []
    remaining = max_tokens
    buckets = (
        ("disputed", disputed, _MAX_DISPUTED),
        ("stale", stale, _MAX_STALE),
        ("low-truth", low_truth, _MAX_LOW_TRUTH),
        ("representative", representative, _MAX_REPRESENTATIVE),
    )
    for _label, bucket, cap in buckets:
        for assessment in bucket[:cap]:
            line = _fact_attention_line(family, assessment)
            cost = estimate_tokens(line)
            if cost > remaining and lines:
                # Token ceiling reached: stop adding lines rather than overflow.
                break
            lines.append(line)
            remaining -= cost
        else:
            continue
        # Inner break triggered (budget exhausted mid-bucket): stop all buckets.
        break
    return tuple(lines)


def _family_key(accessor_name: str) -> str:
    """Human-readable family label for prompt lines (singular, lowercase)."""
    mapping = {
        "risks": "risk",
        "decisions": "decision",
        "assumptions": "assumption",
        "milestones": "milestone",
        "dependencies": "dependency",
        "actions": "action",
        "workstreams": "workstream",
        "commitments": "commitment",
    }
    return mapping.get(accessor_name, accessor_name)


def build_reality_assessment_summary(
    reality: ProgramReality,
    *,
    max_tokens: int = REALITY_CONTEXT_MAX_TOKENS,
) -> RealityAssessmentSummary:
    """Condense a program's reality substrate into a token-bounded summary.

    Loads every newsletter-relevant family once, partitions facts into
    attention buckets (disputed > stale > low-truth > representative), and
    renders per-family attention lines until the token ceiling is hit. The
    result is safe to append to any AI drafter's supplemental context: it is
    bounded, prioritised, and carries no raw evidence/lineage payload.
    """
    # Family accessors in newsletter-relevance order. Disputed/stale facts in
    # the earlier-listed families win the shared token budget over later ones.
    accessors: tuple[tuple[str, Any], ...] = (
        ("risks", reality.risks),
        ("decisions", reality.decisions),
        ("assumptions", reality.assumptions),
        ("milestones", reality.milestones),
        ("dependencies", reality.dependencies),
    )
    family_totals: dict[str, int] = {}
    disputed_counts: dict[str, int] = {}
    stale_counts: dict[str, int] = {}
    low_truth_counts: dict[str, int] = {}
    all_attention: list[str] = []

    any_content = False
    remaining_tokens = max_tokens

    for accessor_name, accessor in accessors:
        try:
            assessments = accessor()
        except Exception:
            # A single family failing to load must not poison the whole summary.
            continue
        if not assessments:
            continue
        any_content = True
        family_label = _family_key(accessor_name)
        disputed, stale, low_truth, representative = _prioritised_facts(assessments)
        family_totals[family_label] = len(assessments)
        disputed_counts[family_label] = len(disputed)
        stale_counts[family_label] = len(stale)
        low_truth_counts[family_label] = len(low_truth)
        # Give each family a fair slice of the remaining budget rather than
        # letting the first family consume it all.
        family_budget = max(0, remaining_tokens // _families_remaining(accessors, accessor_name))
        lines = _render_attention_lines(
            family_label,
            disputed,
            stale,
            low_truth,
            representative,
            max_tokens=family_budget,
        )
        all_attention.extend(lines)
        remaining_tokens -= sum(estimate_tokens(line) for line in lines)
        if remaining_tokens <= 0:
            break

    return RealityAssessmentSummary(
        family_totals=family_totals,
        disputed_counts=disputed_counts,
        stale_counts=stale_counts,
        low_truth_counts=low_truth_counts,
        attention_lines=tuple(all_attention),
        empty=not any_content,
    )


def _families_remaining(accessors: tuple[tuple[str, Any], ...], current_name: str) -> int:
    """Count families from the current one onward, for fair budget slicing (min 1)."""
    for index, (name, _accessor) in enumerate(accessors):
        if name == current_name:
            return max(1, len(accessors) - index)
    return max(1, len(accessors))


def reality_context_lines(summary: RealityAssessmentSummary) -> tuple[str, ...]:
    """Render a RealityAssessmentSummary as supplemental-context prompt lines.

    Emits a header line stating the truth/dispute/staleness denominators per
    family (cheap, high-signal), followed by the prioritised per-fact attention
    lines. The output is a ``tuple[str, ...]`` matching the shape every existing
    ``_*_context_lines`` helper in ``report_ai.py`` already returns, so it drops
    cleanly into the existing ``supplemental_context`` plumbing.
    """
    if summary.empty:
        # No reality content: instruct the AI not to assert confirmation status
        # it cannot verify, rather than silently ignoring the topic.
        return (
            "Reality substrate: no reconciled facts available for this program yet. "
            "Do not assert that risks/decisions are confirmed or settled; state "
            "their status as unverified.",
        )

    lines: list[str] = []
    # Header: per-family denominators so the AI knows the shape of the truth it
    # is narrating ("of 14 risks, 2 disputed, 3 stale"). One compact line.
    family_parts: list[str] = []
    for family, total in summary.family_totals.items():
        flags: list[str] = []
        disputed = summary.disputed_counts.get(family, 0)
        stale = summary.stale_counts.get(family, 0)
        low_truth = summary.low_truth_counts.get(family, 0)
        if disputed:
            flags.append(f"{disputed} disputed")
        if stale:
            flags.append(f"{stale} stale")
        if low_truth:
            flags.append(f"{low_truth} low-truth")
        flag_text = f" ({'; '.join(flags)})" if flags else ""
        family_parts.append(f"{total} {family}{flag_text}")
    lines.append("Reality substrate confidence: " + "; ".join(family_parts) + ".")

    # Attention lines: the specific facts the AI must not mis-narrate.
    if summary.attention_lines:
        lines.append("Facts requiring care in narration (truth-level, flags):")
        lines.extend(summary.attention_lines)
    else:
        lines.append(
            "All reconciled facts are human-confirmed or above with no disputes or "
            "staleness; narrate normally but do not overstate corroboration."
        )

    # Closing instruction: the core anti-contradiction directive (closes the
    # §6.10 item 4 gap — structured tables and AI prose must not contradict).
    lines.append(
        "Do not state any flagged fact as settled/confirmed in prose; honor the "
        "truth-level and DISPUTED/STALE/PROVISIONAL markers above consistently "
        "with the newsletter's structured tables."
    )
    return tuple(lines)
