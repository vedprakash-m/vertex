"""REV LLM-as-judge harness — Zone B (specs/gaps.md §5.1).

Uses Claude (or any ``LLMProvider``) to score extraction quality on a fixture
corpus, comparing two extractors on recall, precision, grounding, and
materiality classification.

Usage:
    from src.ai.rev.judge import judge_extractions, JudgementReport

    report = judge_extractions(
        messages=fixture_messages,
        extractor_a_name="deterministic",
        extractor_a_claims=det_claims_by_message,
        extractor_b_name="llm",
        extractor_b_claims=llm_claims_by_message,
        canonical_texts=canonical_texts_by_message,
        client=claude_client,
    )
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.ai.provider import LLMProvider
from src.ai.prompt_registry import load_prompt
from src.ai.tiered_router import RouteResult, route_through_tiers
from src.core.policy_loader import load_ai_feature_policy
from src.ai.rev.extractor import ExtractedClaim

_JUDGE_FEATURE = "rev_judge"
LLM_PROMPT_VERSION = "rev_judge.v1"


@dataclass
class ClaimScore:
    event_type: str
    verdict: str  # CORRECT | PARTIAL | HALLUCINATED
    reason: str


@dataclass
class ExtractorJudgement:
    extractor_name: str
    scores: list[ClaimScore] = field(default_factory=list)
    precision: float = 0.0
    recall: float = 0.0

    @property
    def correct_count(self) -> int:
        return sum(1 for s in self.scores if s.verdict == "CORRECT")

    @property
    def partial_count(self) -> int:
        return sum(1 for s in self.scores if s.verdict == "PARTIAL")

    @property
    def hallucinated_count(self) -> int:
        return sum(1 for s in self.scores if s.verdict == "HALLUCINATED")


@dataclass
class GroundTruthCoverage:
    fact: str
    captured_by: str  # A | B | both | neither


@dataclass
class MessageJudgement:
    message_id: str
    subject: str
    extractor_a: ExtractorJudgement
    extractor_b: ExtractorJudgement
    ground_truth_coverage: list[GroundTruthCoverage] = field(default_factory=list)
    summary: str = ""


@dataclass
class JudgementReport:
    """Aggregate comparison of two extractors across all judged messages."""

    extractor_a_name: str
    extractor_b_name: str
    message_judgements: list[MessageJudgement] = field(default_factory=list)
    overall_precision_a: float = 0.0
    overall_recall_a: float = 0.0
    overall_precision_b: float = 0.0
    overall_recall_b: float = 0.0
    recommendation: str = ""
    quiet_lane_suppressed_count: int = 0  # KI-2: messages where both extractors returned 0 claims
    cache_hits: int = 0  # P2-8: judge-cache hits on this run (0 when caching disabled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extractor_a_name": self.extractor_a_name,
            "extractor_b_name": self.extractor_b_name,
            "overall_precision_a": round(self.overall_precision_a, 3),
            "overall_recall_a": round(self.overall_recall_a, 3),
            "overall_precision_b": round(self.overall_precision_b, 3),
            "overall_recall_b": round(self.overall_recall_b, 3),
            "quiet_lane_suppressed_count": self.quiet_lane_suppressed_count,
            "cache_hits": self.cache_hits,
            "recommendation": self.recommendation,
            "messages": [
                {
                    "message_id": mj.message_id,
                    "subject": mj.subject,
                    "summary": mj.summary,
                    "extractor_a": {
                        "precision": round(mj.extractor_a.precision, 3),
                        "recall": round(mj.extractor_a.recall, 3),
                        "correct": mj.extractor_a.correct_count,
                        "partial": mj.extractor_a.partial_count,
                        "hallucinated": mj.extractor_a.hallucinated_count,
                        "scores": [{"event_type": s.event_type, "verdict": s.verdict, "reason": s.reason} for s in mj.extractor_a.scores],
                    },
                    "extractor_b": {
                        "precision": round(mj.extractor_b.precision, 3),
                        "recall": round(mj.extractor_b.recall, 3),
                        "correct": mj.extractor_b.correct_count,
                        "partial": mj.extractor_b.partial_count,
                        "hallucinated": mj.extractor_b.hallucinated_count,
                        "scores": [{"event_type": s.event_type, "verdict": s.verdict, "reason": s.reason} for s in mj.extractor_b.scores],
                    },
                    "ground_truth_coverage": [
                        {"fact": g.fact, "captured_by": g.captured_by}
                        for g in mj.ground_truth_coverage
                    ],
                }
                for mj in self.message_judgements
            ],
        }

    def render_human(self) -> str:
        lines: list[str] = []
        lines.append("=" * 70)
        lines.append("REV Extractor Comparison — LLM-as-Judge Report")
        lines.append("=" * 70)
        lines.append(f"  Extractor A: {self.extractor_a_name}")
        lines.append(f"  Extractor B: {self.extractor_b_name}")
        lines.append("")
        lines.append("AGGREGATE METRICS")
        lines.append(f"  Extractor A: precision={self.overall_precision_a:.1%}  recall={self.overall_recall_a:.1%}")
        lines.append(f"  Extractor B: precision={self.overall_precision_b:.1%}  recall={self.overall_recall_b:.1%}")
        lines.append("")
        lines.append("RECOMMENDATION")
        lines.append(f"  {self.recommendation}")
        lines.append("")
        lines.append("PER-MESSAGE BREAKDOWN")
        for mj in self.message_judgements:
            lines.append(f"  [{mj.message_id}] {mj.subject[:60]}")
            lines.append(f"    A: prec={mj.extractor_a.precision:.1%} recall={mj.extractor_a.recall:.1%} "
                         f"(✓{mj.extractor_a.correct_count} ~{mj.extractor_a.partial_count} ✗{mj.extractor_a.hallucinated_count})")
            lines.append(f"    B: prec={mj.extractor_b.precision:.1%} recall={mj.extractor_b.recall:.1%} "
                         f"(✓{mj.extractor_b.correct_count} ~{mj.extractor_b.partial_count} ✗{mj.extractor_b.hallucinated_count})")
            if mj.ground_truth_coverage:
                lines.append("    Ground truth coverage:")
                for g in mj.ground_truth_coverage:
                    lines.append(f"      [{g.captured_by:6}] {g.fact[:80]}")
            if mj.summary:
                lines.append(f"    Summary: {mj.summary}")
            lines.append("")
        return "\n".join(lines)


def judge_extractions(
    *,
    messages: list[dict[str, str]],
    extractor_a_name: str,
    extractor_a_claims: dict[str, tuple[ExtractedClaim, ...]],
    extractor_b_name: str,
    extractor_b_claims: dict[str, tuple[ExtractedClaim, ...]],
    canonical_texts: dict[str, str],
    ground_truth: dict[str, list[str]] | None = None,
    client: LLMProvider,
    use_cache: bool = False,
    cache_program_id: str | None = None,
    cache_programs_root: Path | None = None,
) -> JudgementReport:
    """Run LLM-as-judge over all messages, comparing extractor_a vs extractor_b.

    Args:
        messages: List of message dicts with 'message_id', 'subject'.
        extractor_a_claims: message_id → tuple of claims from extractor A.
        extractor_b_claims: message_id → tuple of claims from extractor B.
        canonical_texts: message_id → canonical text used for extraction.
        ground_truth: Optional pre-labeled facts per message (list of fact strings).
        client: Any LLMProvider (use Claude for best judge quality).
        use_cache: P2-8 — when True (with cache_program_id + cache_programs_root),
            cache judge verdicts keyed by (source_document_key, prompt_version,
            ground_truth_hash) so re-running the judge over an unchanged corpus
            skips the LLM call. Best-effort: a cache error degrades to a live call.
    """
    cache_enabled = bool(use_cache) and cache_program_id is not None and cache_programs_root is not None
    if cache_enabled:
        from src.core.rev.rev_cache_store import (
            get_judge_result,
            hash_ground_truth,
            put_judge_result,
        )

    message_judgements: list[MessageJudgement] = []
    all_prec_a: list[float] = []
    all_recall_a: list[float] = []
    all_prec_b: list[float] = []
    all_recall_b: list[float] = []
    quiet_lane_suppressed = 0
    cache_hits = 0

    for msg in messages:
        mid = msg.get("message_id", "")
        subject = msg.get("subject", "")
        canonical = canonical_texts.get(mid, "")
        claims_a = extractor_a_claims.get(mid, ())
        claims_b = extractor_b_claims.get(mid, ())
        gt_facts = ground_truth.get(mid, []) if ground_truth else []

        if not claims_a and not claims_b:
            # KI-2: still include ground-truth facts from silent messages in recall.
            # Count each silent message toward the quiet_lane suppression metric.
            quiet_lane_suppressed += 1
            if gt_facts:
                # Neither extractor captured these facts — penalise recall for both.
                all_recall_a.append(0.0)
                all_recall_b.append(0.0)
            continue  # no claims to precision-score

        user_prompt = _build_judge_user_prompt(
            message_id=mid,
            subject=subject,
            canonical_text=canonical,
            claims_a=claims_a,
            claims_b=claims_b,
            ground_truth_facts=gt_facts,
            extractor_a_name=extractor_a_name,
            extractor_b_name=extractor_b_name,
        )

        # P2-8: judge cache. Keyed by (source_document_key=message_id,
        # prompt_version, ground_truth_hash). A hit skips the LLM call.
        raw: dict[str, Any] = {}
        gt_hash = ""
        if cache_enabled:
            # cache_enabled ⇒ cache_program_id/cache_programs_root are non-None.
            assert cache_program_id is not None and cache_programs_root is not None
            prog_id = cache_program_id
            prog_root = cache_programs_root
            gt_hash = hash_ground_truth(gt_facts)
            try:
                cached = get_judge_result(
                    program_id=prog_id,
                    source_document_key=mid,
                    prompt_version=LLM_PROMPT_VERSION,
                    ground_truth_hash=gt_hash,
                    programs_root=prog_root,
                )
            except Exception:
                cached = None
            if isinstance(cached, dict):
                raw = cached
                cache_hits += 1

        if not raw:
            try:
                _policy = load_ai_feature_policy(_JUDGE_FEATURE)
                raw_route: RouteResult[dict[str, Any]] = route_through_tiers(
                    _JUDGE_FEATURE,
                    deterministic_fn=None,
                    local_fn=None,
                    frontier_fn=lambda: client.structured(
                        load_prompt(LLM_PROMPT_VERSION),
                        user_prompt,
                        parser=lambda p: p if isinstance(p, dict) else {},
                        max_tokens=_policy.max_tokens,
                        prompt_version=LLM_PROMPT_VERSION,
                    ),
                )
                raw = raw_route.value if raw_route.value is not None else {}
            except Exception:
                raw = {}
            # Persist the verdict for reuse (best-effort).
            if cache_enabled and raw:
                try:
                    put_judge_result(
                        program_id=prog_id,
                        source_document_key=mid,
                        prompt_version=LLM_PROMPT_VERSION,
                        ground_truth_hash=gt_hash,
                        verdict=raw,
                        programs_root=prog_root,
                    )
                except Exception:
                    pass

        mj = _parse_judge_response(
            raw,
            message_id=mid,
            subject=subject,
            extractor_a_name=extractor_a_name,
            extractor_b_name=extractor_b_name,
        )
        message_judgements.append(mj)
        all_prec_a.append(mj.extractor_a.precision)
        all_recall_a.append(mj.extractor_a.recall)
        all_prec_b.append(mj.extractor_b.precision)
        all_recall_b.append(mj.extractor_b.recall)

    overall_prec_a = sum(all_prec_a) / len(all_prec_a) if all_prec_a else 0.0
    overall_recall_a = sum(all_recall_a) / len(all_recall_a) if all_recall_a else 0.0
    overall_prec_b = sum(all_prec_b) / len(all_prec_b) if all_prec_b else 0.0
    overall_recall_b = sum(all_recall_b) / len(all_recall_b) if all_recall_b else 0.0

    recommendation = _derive_recommendation(
        extractor_a_name=extractor_a_name,
        extractor_b_name=extractor_b_name,
        prec_a=overall_prec_a,
        recall_a=overall_recall_a,
        prec_b=overall_prec_b,
        recall_b=overall_recall_b,
    )

    return JudgementReport(
        extractor_a_name=extractor_a_name,
        extractor_b_name=extractor_b_name,
        message_judgements=message_judgements,
        overall_precision_a=overall_prec_a,
        overall_recall_a=overall_recall_a,
        overall_precision_b=overall_prec_b,
        overall_recall_b=overall_recall_b,
        recommendation=recommendation,
        quiet_lane_suppressed_count=quiet_lane_suppressed,
        cache_hits=cache_hits,
    )


def _build_judge_user_prompt(
    *,
    message_id: str,
    subject: str,
    canonical_text: str,
    claims_a: tuple[ExtractedClaim, ...],
    claims_b: tuple[ExtractedClaim, ...],
    ground_truth_facts: list[str],
    extractor_a_name: str,
    extractor_b_name: str,
) -> str:
    lines: list[str] = []
    lines.append(f"Message ID: {message_id}")
    lines.append(f"Subject: {subject}")
    lines.append("")
    lines.append("[CANONICAL TEXT]")
    lines.append(canonical_text.strip())
    lines.append("")

    if ground_truth_facts:
        lines.append("[GROUND TRUTH FACTS]")
        for i, fact in enumerate(ground_truth_facts, 1):
            lines.append(f"  GT{i}: {fact}")
        lines.append("")

    lines.append(f"[EXTRACTOR A — {extractor_a_name}]")
    if claims_a:
        for i, claim in enumerate(claims_a, 1):
            span_text = claim.evidence_spans[0].excerpt_text if claim.evidence_spans else "(no span)"
            lines.append(f"  A{i}: event_type={claim.event_type}  confidence={claim.extraction_confidence:.2f}")
            lines.append(f"       payload={json.dumps(claim.payload)}")
            lines.append(f"       excerpt={repr(span_text[:120])}")
    else:
        lines.append("  (no claims extracted)")
    lines.append("")

    lines.append(f"[EXTRACTOR B — {extractor_b_name}]")
    if claims_b:
        for i, claim in enumerate(claims_b, 1):
            span_text = claim.evidence_spans[0].excerpt_text if claim.evidence_spans else "(no span)"
            lines.append(f"  B{i}: event_type={claim.event_type}  confidence={claim.extraction_confidence:.2f}")
            lines.append(f"       payload={json.dumps(claim.payload)}")
            lines.append(f"       excerpt={repr(span_text[:120])}")
    else:
        lines.append("  (no claims extracted)")

    return "\n".join(lines)


def _parse_judge_response(
    raw: dict[str, Any],
    *,
    message_id: str,
    subject: str,
    extractor_a_name: str,
    extractor_b_name: str,
) -> MessageJudgement:
    def _parse_extractor_judgement(name: str, data: object) -> ExtractorJudgement:
        ej = ExtractorJudgement(extractor_name=name)
        if not isinstance(data, dict):
            return ej
        ej.precision = float(data.get("precision", 0.0))
        ej.recall = float(data.get("recall", 0.0))
        for s in data.get("scores", []):
            if isinstance(s, dict):
                ej.scores.append(ClaimScore(
                    event_type=str(s.get("event_type", "")),
                    verdict=str(s.get("verdict", "HALLUCINATED")),
                    reason=str(s.get("reason", "")),
                ))
        return ej

    ej_a = _parse_extractor_judgement(extractor_a_name, raw.get("extractor_a"))
    ej_b = _parse_extractor_judgement(extractor_b_name, raw.get("extractor_b"))

    coverage: list[GroundTruthCoverage] = []
    for g in raw.get("ground_truth_coverage", []):
        if isinstance(g, dict):
            coverage.append(GroundTruthCoverage(
                fact=str(g.get("fact", "")),
                captured_by=str(g.get("captured_by", "neither")),
            ))

    return MessageJudgement(
        message_id=message_id,
        subject=subject,
        extractor_a=ej_a,
        extractor_b=ej_b,
        ground_truth_coverage=coverage,
        summary=str(raw.get("summary", "")),
    )


def _derive_recommendation(
    *,
    extractor_a_name: str,
    extractor_b_name: str,
    prec_a: float,
    recall_a: float,
    prec_b: float,
    recall_b: float,
) -> str:
    if prec_b >= prec_a and recall_b > recall_a + 0.1:
        return (
            f"Use {extractor_b_name}: higher recall ({recall_b:.0%} vs {recall_a:.0%}) "
            f"with maintained precision ({prec_b:.0%} vs {prec_a:.0%}). "
            "LLM extractor is recommended for production once quality floor corpus confirms G-xtract-prec >= 80%."
        )
    if prec_b < prec_a - 0.1:
        return (
            f"Keep {extractor_a_name}: {extractor_b_name} shows lower precision ({prec_b:.0%} vs {prec_a:.0%}). "
            "Tune extractor prompt before switching default."
        )
    return (
        f"Results inconclusive (A: prec={prec_a:.0%}/recall={recall_a:.0%}; "
        f"B: prec={prec_b:.0%}/recall={recall_b:.0%}). "
        "Expand corpus to >= 50 labeled candidates before concluding."
    )


__all__ = [
    "JudgementReport",
    "MessageJudgement",
    "ExtractorJudgement",
    "ClaimScore",
    "GroundTruthCoverage",
    "judge_extractions",
]
