from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.context_budget import estimate_tokens
from src.core.context_compiler import (
    ContentOrigin,
    ContextCompileRejected,
    ContextCompileRequest,
    DeterministicContextCompiler,
    EvidenceSpan,
    ValidationContext,
)
from src.core.decision_brief_engine import DecisionBrief, DecisionItem, DecisionSignal
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)


PROMPT_VERSION = "decision_brief_advisor.v1"
POLICY_VERSION = "decision_brief_advisor.v1"
_FEATURE = "decision_brief_advisor"
_OUTPUT_SCHEMA_VERSION = "1"
_VALID_VERDICTS = frozenset({"ACCEPT", "REVISE", "REJECT", "DEFER"})

#: ADF-W2.9 P5: ContextCompiler needs a fixed input-token envelope
#: (Section 8.7.2 step 9's "token-budget packing"). `max_tokens` from the
#: feature policy already bounds the *output*; this is the separate input
#: budget the compiler packs required+optional evidence into.
_CONTEXT_MAX_INPUT_TOKENS = 6000
_CONTEXT_OUTPUT_SCHEMA_TEXT = (
    'Respond with JSON: {"verdict": "ACCEPT|REVISE|REJECT|DEFER", "reasoning": "...", "suggested_text": null}.'
)
_CONTEXT_GATEWAY_SYSTEM = "Respond only with the JSON object described below -- no prose, no markdown fences."


@dataclass(frozen=True, slots=True)
class DecisionAdvice:
    verdict: str  # ACCEPT | REVISE | REJECT | DEFER
    reasoning: str
    suggested_text: str | None


def advise_on_decision_brief(
    *,
    client: LLMProvider,
    brief: DecisionBrief,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> DecisionBrief:
    """Return a new brief with verdict/reasoning/suggested_text populated on each item.

    Runs the same AISchemaGateway/QG-29 safety lifecycle established by
    ``risk_proposal_generator.py`` (ADF-W5.1, P7): bounds-checked
    request/response payloads, the five ``AIRunState`` transitions, cache
    wiring via ``AIResultCacheKey``, and a durable QG-29 terminal release
    decision recorded per item before its advice may be consumed.
    ``_parse_advice`` already tolerates any malformed response by
    defaulting to a ``DEFER`` verdict rather than raising, so there is no
    separate semantic-rejection path here beyond AISchemaGateway's bounds
    checks -- unlike ``synthesizer.py``'s hard-fail contract, this
    feature's existing per-item graceful degrade (``_advise_on_item``
    catches any failure and leaves the item unchanged) is preserved
    unchanged, matching the same reasoning as ``anticipation_engine.py``'s
    migration."""
    prompt_template = _load_prompt_template()
    enriched_items: list[DecisionItem] = []
    for item in brief.items:
        advice = _advise_on_item(
            client=client,
            item=item,
            prompt_template=prompt_template,
            program_id=program_id,
            programs_root=programs_root,
        )
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
    program_id: str,
    programs_root: Path,
) -> DecisionAdvice | None:
    ai_run_id = new_ai_run_id()

    def _lifecycle(state: AIRunState) -> None:
        record_ai_run_lifecycle(
            program_id=program_id,
            ai_run_id=ai_run_id,
            feature=_FEATURE,
            state=state,
            prompt_version=PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            programs_root=programs_root,
        )

    def _discard(terminal: ReleaseTerminal, reason: str, finding_count: int = 0) -> None:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            programs_root=programs_root,
        )

    try:
        _lifecycle(AIRunState.PLANNED)

        request_payload = {
            "program_id": program_id,
            "section_id": item.section_id,
            "current_text": item.current_text,
            "evidence_delta_lines": list(item.evidence_delta_lines),
            "top_signal_texts": [signal.text for signal in item.top_signals],
            "kpi_summary": item.kpi_summary or "",
            "vitality_summary": item.vitality_summary,
            "stale_claims": list(item.stale_claims),
            "proposed_text": item.proposed_text or "",
        }
        try:
            validate_bounded_payload(request_payload)
        except SchemaGatewayError as error:
            _discard(ReleaseTerminal.DISCARDED, f"AISchemaGateway rejected the outbound request: {error}")
            return None

        _lifecycle(AIRunState.REQUESTED)

        user_prompt = _build_user_prompt(item)
        policy = load_ai_feature_policy(_FEATURE)
        cache_key = AIResultCacheKey(
            program_id=program_id,
            feature=_FEATURE,
            canonical_input_hash=canonical_input_hash(user_prompt),
            prompt_version=PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            model_deployment=_resolve_model_deployment(client),
            context_manifest_hash=canonical_input_hash(item.section_id),
            output_schema_version=_OUTPUT_SCHEMA_VERSION,
        )
        raw = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                prompt_template,
                user_prompt,
                parser=lambda payload: payload,
                max_tokens=policy.max_tokens,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value

        _lifecycle(AIRunState.RESPONDED)

        try:
            validate_bounded_payload(raw)
        except SchemaGatewayError as error:
            _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")
            return None

        _lifecycle(AIRunState.SCHEMA_VALIDATED)
        advice = _parse_advice(cast(dict[str, Any], raw))
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.RELEASED, "passed AISchemaGateway bounds and decision-advice parsing")
        return advice
    except Exception:
        # Preserves this feature's existing fully-graceful degrade: any
        # unexpected failure (provider error, cache I/O, etc.) yields no
        # advice for this item rather than dropping the whole brief.
        return None


def advise_on_decision_brief_via_context_gateway(
    *,
    client: LLMProvider,
    brief: DecisionBrief,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> DecisionBrief:
    """ADF-W2.9 P5 (specs/arch-data-fix.md Sections 8.7/8.9): the pilot
    candidate path -- same input/output shape as ``advise_on_decision_brief``
    (a drop-in for blind comparison), but evidence assembly runs through
    ``DeterministicContextCompiler`` (ADF-W2.7) instead of the ad hoc
    ``_build_user_prompt``/``[:6]`` truncation above, and the raw response
    is bounds-checked through ``validate_bounded_payload`` (ADF-W2.8)
    before ``_parse_advice`` runs. Not wired into any production command --
    only the blind A/B comparison harness (``src/commands/decision_brief_pilot.py``)
    calls this, pending real evidence before any production swap."""
    prompt_template = _load_prompt_template()
    enriched_items: list[DecisionItem] = []
    for item in brief.items:
        advice = _advise_on_item_via_context_gateway(
            client=client, item=item, prompt_template=prompt_template,
            program_id=program_id, edition_id=brief.edition_name, programs_root=programs_root,
        )
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


def _advise_on_item_via_context_gateway(
    *,
    client: LLMProvider,
    item: DecisionItem,
    prompt_template: str,
    program_id: str,
    edition_id: str | None,
    programs_root: Path,
) -> DecisionAdvice | None:
    required, optional = _build_evidence_spans(item)
    request = ContextCompileRequest(
        program_id=program_id,
        edition_id=edition_id,
        feature=_FEATURE,
        prompt_version=PROMPT_VERSION,
        system_instructions=prompt_template,
        output_schema_text=_CONTEXT_OUTPUT_SCHEMA_TEXT,
        required_evidence=required,
        optional_evidence=optional,
        max_input_tokens=_CONTEXT_MAX_INPUT_TOKENS,
        reserved_output_tokens=load_ai_feature_policy(_FEATURE).max_tokens or 800,
    )
    validation_context = ValidationContext(
        program_id=program_id,
        run_id=new_ai_run_id(),
        execution_mode="advisory",
        classification="internal",
    )
    try:
        compiled = DeterministicContextCompiler(programs_root=programs_root).compile(
            request, validation_context=validation_context
        )
    except ContextCompileRejected:
        return None

    try:
        return route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                _CONTEXT_GATEWAY_SYSTEM,
                compiled.prompt_text,
                parser=_parse_advice_with_gateway,
                max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                prompt_version=PROMPT_VERSION,
            ),
            policy=load_ai_feature_policy(_FEATURE),
        ).value
    except Exception:
        return None


def _parse_advice_with_gateway(raw: dict[str, Any]) -> DecisionAdvice | None:
    """AISchemaGateway bounds check (ADF-W2.8) ahead of the same
    ``_parse_advice`` semantic parsing the baseline path uses."""
    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError:
        return None
    return _parse_advice(raw)


def _build_evidence_spans(item: DecisionItem) -> tuple[tuple[EvidenceSpan, ...], tuple[EvidenceSpan, ...]]:
    """Section 8.7.3's required-evidence list -- "evidence referenced by an
    authoritative proposed claim" and "conflict/counter-evidence for a
    disputed fact" -- maps onto this item's own text/delta/prior-proposal/
    stale-claims; everything else (ranked signals, KPI/vitality summaries)
    is optional and subject to the compiler's salience ranking, quotas, and
    token-budget packing rather than the old hardcoded ``[:6]`` truncation."""
    required: list[EvidenceSpan] = []
    if item.current_text:
        required.append(_span("current_text", item.current_text, required=True))
    if item.evidence_delta_lines:
        text = "\n".join(f"- {line}" for line in item.evidence_delta_lines)
        required.append(_span("evidence_delta", text, required=True))
    if item.proposed_text:
        required.append(_span("prior_ai_proposal", item.proposed_text, required=True))
    if item.stale_claims:
        text = "\n".join(f"- {claim}" for claim in item.stale_claims)
        required.append(_span("stale_claims", text, required=True))

    optional: list[EvidenceSpan] = []
    for index, signal in enumerate(item.top_signals):
        optional.append(_signal_span(index, signal))
    if item.kpi_summary:
        optional.append(_span("kpi_summary", item.kpi_summary, salience_inputs={"materiality": 0.6}))
    if item.vitality_summary:
        optional.append(_span("vitality_summary", item.vitality_summary, salience_inputs={"materiality": 0.5}))
    return tuple(required), tuple(optional)


def _span(
    evidence_id: str,
    text: str,
    *,
    required: bool = False,
    salience_inputs: dict[str, float] | None = None,
) -> EvidenceSpan:
    return EvidenceSpan(
        evidence_id=evidence_id,
        source_family="decision_brief",
        text=text,
        required=required,
        origin=ContentOrigin.AUTHORED,
        trust_level="high",
        verification_state="verified",
        injection_screen="pass",
        salience_inputs=salience_inputs or {},
        token_estimate=estimate_tokens(text),
    )


def _signal_span(index: int, signal: DecisionSignal) -> EvidenceSpan:
    # Signals arrive pre-ordered by the brief builder's own relevance
    # ranking; a decaying recency input lets the compiler's salience
    # ranking approximate that original order rather than discard it,
    # while still letting genuinely low-value trailing signals lose out
    # under quota/token pressure.
    return EvidenceSpan(
        evidence_id=f"signal_{signal.signal_id}",
        source_family="signal",
        text=signal.text,
        required=False,
        origin=ContentOrigin.SYSTEM,
        trust_level="medium",
        verification_state="unverified",
        injection_screen="pass",
        salience_inputs={"recency": max(0.1, 1.0 - index * 0.15)},
        token_estimate=estimate_tokens(signal.text),
    )


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


def _resolve_model_deployment(client: LLMProvider) -> str:
    """Best-effort model/deployment identifier for the cache key (Section
    8.8.3), mirroring ``risk_proposal_generator._resolve_model_deployment``.
    A wrong/missing id only under-shares the cache (never over-shares,
    since program_id/feature/input hash are still exact-matched), so this
    degrades safely rather than raising."""
    for attr in ("deployment", "primary_deployment", "model", "deployment_id"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None
