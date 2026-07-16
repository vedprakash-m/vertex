"""ADF-W4.7 remainder (specs/arch-data-fix.md Section 8.10.7): the Zone B
half of governance decision brief generation.

The sixth live feature this session to run the full AISchemaGateway +
five-`AIRunState` + QG-29 release lifecycle. See
``src/core/governance_decision_brief.py``'s module docstring for why this
is named ``GovernanceDecisionBriefProposal``, not "DecisionBrief" (that
name is already taken by ``decision_brief_engine.py`` for a different
purpose).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.ai.provider import LLMProvider
from src.ai.tiered_router import route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.governance_decision_brief import (
    GovernanceDecisionBriefProposal,
    GovernanceDecisionOption,
    GovernanceDecisionRequest,
)
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)

_OUTPUT_SCHEMA_VERSION = "1"

PROMPT_VERSION = "governance_decision_brief_generator.v1"
POLICY_VERSION = "governance_decision_brief_generator.v1"
_FEATURE = "governance_decision_brief_generator"
_MIN_OPTIONS = 2


def generate_governance_decision_brief(
    request: GovernanceDecisionRequest,
    *,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernanceDecisionBriefProposal | None:
    """Returns a released proposal, or ``None`` if discarded/rejected."""
    ai_run_id = new_ai_run_id()
    program_id = request.program_id

    def _lifecycle(state: AIRunState) -> None:
        record_ai_run_lifecycle(
            program_id=program_id, ai_run_id=ai_run_id, feature=_FEATURE, state=state,
            prompt_version=PROMPT_VERSION, policy_version=POLICY_VERSION, programs_root=programs_root,
        )

    def _discard(terminal: ReleaseTerminal, reason: str, finding_count: int = 0) -> None:
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=terminal, reason=reason,
            validator_finding_count=finding_count, programs_root=programs_root,
        )

    _lifecycle(AIRunState.PLANNED)

    request_payload = {
        "decision_text": request.decision_text,
        "evidence_texts": list(request.evidence_texts),
        "evidence_refs": list(request.evidence_refs),
    }
    try:
        validate_bounded_payload(request_payload)
    except SchemaGatewayError as error:
        _discard(ReleaseTerminal.DISCARDED, f"AISchemaGateway rejected the outbound request: {error}")
        return None

    _lifecycle(AIRunState.REQUESTED)

    prompt_template = _load_prompt_template()
    policy = load_ai_feature_policy(_FEATURE)
    cache_key = AIResultCacheKey(
        program_id=program_id,
        feature=_FEATURE,
        canonical_input_hash=canonical_input_hash(_build_user_prompt(request)),
        prompt_version=PROMPT_VERSION,
        policy_version=POLICY_VERSION,
        model_deployment=_resolve_model_deployment(client),
        context_manifest_hash=canonical_input_hash("|".join(request.evidence_refs)),
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
    )
    try:
        raw = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                prompt_template,
                _build_user_prompt(request),
                parser=lambda payload: payload,
                max_tokens=policy.max_tokens or 1000,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value
    except Exception as error:
        _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")
        return None

    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")
        return None

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")
        return None

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    proposal = _parse_proposal(raw, request=request, ai_run_id=ai_run_id)
    if proposal is None:
        _discard(ReleaseTerminal.REJECTED, "response missing required fields or failed the text-safety pipeline.")
        return None

    findings = _validate_semantics(proposal, request=request)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    if findings:
        _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))
        return None

    record_ai_release_decision(
        program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and governance-decision-brief semantic validation",
        validator_finding_count=0, programs_root=programs_root,
    )
    return proposal


def _validate_semantics(
    proposal: GovernanceDecisionBriefProposal, *, request: GovernanceDecisionRequest
) -> tuple[str, ...]:
    findings: list[str] = []
    if not proposal.decision.strip():
        findings.append("decision is empty.")
    if not proposal.context.strip():
        findings.append("context is empty.")
    if not proposal.recommendation.strip():
        findings.append("recommendation is empty.")
    if not proposal.consequences_of_delay.strip():
        findings.append("consequences_of_delay is empty.")
    if len(proposal.options) < _MIN_OPTIONS:
        findings.append(f"only {len(proposal.options)} option(s) proposed, need at least {_MIN_OPTIONS} for a real decision.")
    for index, option in enumerate(proposal.options):
        if not option.label.strip() or not option.tradeoffs.strip():
            findings.append(f"option[{index}] has an empty label or tradeoffs.")
    known_refs = set(request.evidence_refs)
    unknown_refs = tuple(ref for ref in proposal.evidence_refs if ref not in known_refs)
    if unknown_refs:
        findings.append(f"evidence_refs cites refs not present on the decision ask: {unknown_refs}.")
    return tuple(findings)


def _build_user_prompt(request: GovernanceDecisionRequest) -> str:
    parts = [f"DECISION ASK: {request.decision_text}", "", "EVIDENCE:"]
    if request.evidence_texts:
        for ref, text in zip(request.evidence_refs, request.evidence_texts):
            parts.append(f"  - [{ref}] {text}")
    else:
        parts.append("  (none)")
    parts += [
        "",
        'Respond with JSON: {"decision": str, "context": str, "options": [{"label": str, "tradeoffs": str}, ...], '
        '"recommendation": str, "consequences_of_delay": str, "owner": str|null, "due_date": "YYYY-MM-DD"|null, '
        '"evidence_refs": [ref, ...]}. Propose at least two distinct options. evidence_refs must only contain '
        "refs copied verbatim from the EVIDENCE list above.",
    ]
    return "\n".join(parts)


def _parse_proposal(
    raw: dict[str, Any], *, request: GovernanceDecisionRequest, ai_run_id: str
) -> GovernanceDecisionBriefProposal | None:
    try:
        decision_raw = str(raw.get("decision", "")).strip()
        decision = process_generated_text(decision_raw).text if decision_raw else ""
        context_raw = str(raw.get("context", "")).strip()
        context = process_generated_text(context_raw).text if context_raw else ""
        recommendation_raw = str(raw.get("recommendation", "")).strip()
        recommendation = process_generated_text(recommendation_raw).text if recommendation_raw else ""
        consequences_raw = str(raw.get("consequences_of_delay", "")).strip()
        consequences_of_delay = process_generated_text(consequences_raw).text if consequences_raw else ""
    except AIPipelineError:
        return None

    if not (decision and context and recommendation and consequences_of_delay):
        return None

    raw_options = raw.get("options")
    if not isinstance(raw_options, list):
        return None
    options: list[GovernanceDecisionOption] = []
    try:
        for entry in raw_options:
            if not isinstance(entry, dict):
                return None
            label_raw = str(entry.get("label", "")).strip()
            label = process_generated_text(label_raw).text if label_raw else ""
            tradeoffs_raw = str(entry.get("tradeoffs", "")).strip()
            tradeoffs = process_generated_text(tradeoffs_raw).text if tradeoffs_raw else ""
            if not label or not tradeoffs:
                return None
            options.append(GovernanceDecisionOption(label=label, tradeoffs=tradeoffs))
    except AIPipelineError:
        return None

    owner = raw.get("owner")
    owner_alias = str(owner).strip().lower() or None if isinstance(owner, str) else None

    due_date_raw = raw.get("due_date")
    due_date: date | None
    if due_date_raw is None:
        due_date = None
    elif isinstance(due_date_raw, str) and due_date_raw.strip():
        try:
            due_date = date.fromisoformat(due_date_raw.strip())
        except ValueError:
            return None
    else:
        return None

    evidence_refs_raw = raw.get("evidence_refs")
    evidence_refs = (
        tuple(str(ref) for ref in evidence_refs_raw if isinstance(ref, str))
        if isinstance(evidence_refs_raw, list)
        else ()
    )

    return GovernanceDecisionBriefProposal(
        id=f"governance-decision-{ai_run_id}",
        program_id=request.program_id,
        decision_ask_id=request.decision_ask_id,
        decision=decision,
        context=context,
        options=tuple(options),
        recommendation=recommendation,
        consequences_of_delay=consequences_of_delay,
        owner_alias=owner_alias,
        due_date=due_date,
        evidence_refs=evidence_refs,
        ai_run_id=ai_run_id,
    )


def _resolve_model_deployment(client: LLMProvider) -> str:
    """Best-effort model/deployment identifier for the cache key (Section
    8.8.3), same degrade-to-sentinel discipline as
    ``risk_proposal_generator.py``: a wrong/missing deployment id only
    under-shares the cache (never over-shares, since program_id/feature/
    input hash are still exact-matched), so this never raises."""
    for attr in ("deployment", "primary_deployment", "model", "deployment_id"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None


def _load_prompt_template() -> str:
    try:
        return load_prompt(PROMPT_VERSION)
    except PromptRegistryError:
        return _FALLBACK_PROMPT


_FALLBACK_PROMPT = (
    "You are Vertex's governance decision-brief engine (Section 8.10.7). Given an open decision ask and its "
    "evidence, write a real decision brief: the decision itself, context, at least two distinct options each "
    "with their tradeoffs, a recommendation, consequences of delay, an owner, and a due date. Never state a "
    "causal claim without evidence. Only cite evidence_refs that were actually provided."
)


__all__ = ["POLICY_VERSION", "PROMPT_VERSION", "generate_governance_decision_brief"]
