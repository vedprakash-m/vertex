from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.core.decision_brief_engine import DecisionBrief, DecisionItem
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "decision_brief_advisor.v1"
_FEATURE = "decision_brief_advisor"
_VALID_VERDICTS = frozenset({"ACCEPT", "REVISE", "REJECT", "DEFER"})


@dataclass(frozen=True, slots=True)
class DecisionAdvice:
    verdict: str  # ACCEPT | REVISE | REJECT | DEFER
    reasoning: str
    suggested_text: str | None


def advise_on_decision_brief(
    *,
    client: LLMProvider,
    brief: DecisionBrief,
) -> DecisionBrief:
    """Return a new brief with verdict/reasoning/suggested_text populated on each item."""
    prompt_template = _load_prompt_template()
    enriched_items: list[DecisionItem] = []
    for item in brief.items:
        advice = _advise_on_item(client=client, item=item, prompt_template=prompt_template)
        if advice is not None:
            enriched_items.append(
                replace(
                    item,
                    verdict=advice.verdict,
                    verdict_reasoning=advice.reasoning,
                    suggested_text=advice.suggested_text,
                )
            )
        else:
            enriched_items.append(item)
    return replace(brief, items=tuple(enriched_items), ai_enriched=True)


def _advise_on_item(
    *,
    client: LLMProvider,
    item: DecisionItem,
    prompt_template: str,
) -> DecisionAdvice | None:
    try:
        return route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                prompt_template,
                _build_user_prompt(item),
                parser=_parse_advice,
                max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                prompt_version=PROMPT_VERSION,
            ),
            policy=load_ai_feature_policy(_FEATURE),
        ).value
    except Exception:
        return None


def _build_user_prompt(item: DecisionItem) -> str:
    parts = [
        f"SECTION: {item.section_title}",
        "",
        "CURRENT TEXT:",
        item.current_text or "(empty)",
        "",
        "EVIDENCE DELTA (what changed this cycle):",
    ]
    for line in item.evidence_delta_lines:
        parts.append(f"  - {line}")
    if item.top_signals:
        parts += ["", "TOP SIGNALS:"]
        for sig in item.top_signals[:6]:
            ts = f" [{sig.timestamp}]" if sig.timestamp else ""
            src = f" ({sig.source})" if sig.source else ""
            parts.append(f"  - {sig.text}{ts}{src}")
    if item.kpi_summary:
        parts += ["", f"KPI SUMMARY: {item.kpi_summary}"]
    if item.vitality_summary:
        parts += ["", f"VITALITY: {item.vitality_summary}"]
    if item.stale_claims:
        parts += ["", "STALE CLAIMS (may need resolution):"]
        for claim in item.stale_claims[:3]:
            parts.append(f"  - {claim}")
    parts += [
        "",
        "PRIOR AI PROPOSAL (if any):",
        item.proposed_text or "(none — propose --ai was not run)",
    ]
    return "\n".join(parts)


def _parse_advice(raw: dict[str, Any]) -> DecisionAdvice | None:
    verdict = str(raw.get("verdict", "")).strip().upper()
    if verdict not in _VALID_VERDICTS:
        verdict = "DEFER"
    raw_reasoning = str(raw.get("reasoning", "")).strip()
    reasoning = ""
    if raw_reasoning:
        try:
            reasoning = (process_generated_text(raw_reasoning).text or "")[:600]
        except AIPipelineError:
            reasoning = ""
    raw_suggested = raw.get("suggested_text")
    suggested_text: str | None = None
    if verdict == "REVISE" and raw_suggested:
        candidate = str(raw_suggested).strip()
        if candidate and candidate.lower() not in {"null", "none", ""}:
            try:
                processed = process_generated_text(candidate)
                suggested_text = processed.text or None
            except AIPipelineError:
                suggested_text = None
    if verdict == "REVISE" and suggested_text is None:
        verdict = "DEFER"
    return DecisionAdvice(
        verdict=verdict,
        reasoning=reasoning,
        suggested_text=suggested_text,
    )


def _load_prompt_template() -> str:
    try:
        return load_prompt(PROMPT_VERSION)
    except PromptRegistryError:
        return _FALLBACK_PROMPT


_FALLBACK_PROMPT = (
    "You are a PM decision advisor. Given section evidence, respond with JSON: "
    '{"verdict": "ACCEPT|REVISE|REJECT|DEFER", "reasoning": "...", "suggested_text": null}.'
)
