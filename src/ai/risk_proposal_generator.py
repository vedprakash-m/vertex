"""ADF-W4.5 (specs/arch-data-fix.md Section 8.10.1): the Zone B half of
risk proposal generation -- the actual AI call.

Runs the same AI safety lifecycle established twice already this session
(``program_synthesizer.py`` for ADF-W2.9, ``meeting_action_extractor.py``
for ADF-W3.3): AISchemaGateway bounds checks, the five ``AIRunState``
transitions, a concrete ``SemanticValidator``, and a QG-29 terminal release
decision before any caller may consume the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.ai.provider import LLMProvider
from src.ai.tiered_router import route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, SemanticValidator, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.models_v2 import RiskCategory, RiskImpact, RiskProbability
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.risk_proposal import RiskProposal, RiskProposalRequest

_OUTPUT_SCHEMA_VERSION = "1"

PROMPT_VERSION = "risk_proposal_generator.v1"
POLICY_VERSION = "risk_proposal_generator.v1"
_FEATURE = "risk_proposal_generator"

_VALID_PROBABILITIES = {member.value for member in RiskProbability if member is not RiskProbability.UNASSESSED}
_VALID_IMPACTS = {member.value for member in RiskImpact if member is not RiskImpact.UNASSESSED}
_VALID_CATEGORIES = {member.value for member in RiskCategory}


@dataclass(frozen=True, slots=True)
class RiskProposalSemanticValidator:
    """ADF-W2.8 pilot: the first concrete ``SemanticValidator`` (Section
    8.9.3), conforming to ``ai_schema_gateway.SemanticValidator`` the same
    way a ``ContextCompiler`` caller injects a ``TokenEstimator``. Runs on
    the RAW response payload -- one step in the pipeline before typed
    parsing (``AITransport -> AISchemaGateway -> SemanticValidator ->
    typed parse``) -- so a rejection carries itemized findings in the QG-29
    release-audit ledger (``validator_finding_count``, per-finding
    ``reason`` text) instead of collapsing every structural miss into one
    generic "failed to parse" message.

    Checks the two things Section 8.10.1's "AI may propose" list actually
    constrains: every narrative field is present/non-empty, the three
    closed-vocabulary fields (probability/impact/category) name a
    recognized value, ``by_when`` is a valid ISO date or null, and
    ``evidence_refs`` is a subset of the evidence the candidate actually
    carried (a proposal cannot cite evidence it was never given -- "evidence
    existence" from Section 8.9.3's own example list)."""

    known_evidence_refs: frozenset[str] = field(default_factory=frozenset)
    validator_id: str = "risk_proposal_generator.v1"

    def validate(self, payload: dict[str, Any]) -> tuple[str, ...]:
        findings: list[str] = []
        for field_name in ("causal_title", "why_it_matters", "mitigation", "fallback"):
            value = payload.get(field_name)
            if not isinstance(value, str) or not value.strip():
                findings.append(f"{field_name} is missing or empty.")

        probability = str(payload.get("probability", "")).strip().lower()
        if probability not in _VALID_PROBABILITIES:
            findings.append(f"probability {probability!r} is not a recognized value.")
        impact = str(payload.get("impact", "")).strip().lower()
        if impact not in _VALID_IMPACTS:
            findings.append(f"impact {impact!r} is not a recognized value.")
        category = str(payload.get("category", "")).strip().lower()
        if category not in _VALID_CATEGORIES:
            findings.append(f"category {category!r} is not a recognized value.")

        by_when_raw = payload.get("by_when")
        if by_when_raw is not None:
            if not isinstance(by_when_raw, str):
                findings.append("by_when must be an ISO date string or null.")
            else:
                try:
                    date.fromisoformat(by_when_raw.strip())
                except ValueError:
                    findings.append(f"by_when {by_when_raw!r} is not a valid ISO date.")

        evidence_refs_raw = payload.get("evidence_refs")
        if isinstance(evidence_refs_raw, list):
            unknown_refs = tuple(
                str(ref) for ref in evidence_refs_raw if isinstance(ref, str) and ref not in self.known_evidence_refs
            )
            if unknown_refs:
                findings.append(f"evidence_refs cites refs not present on the candidate: {unknown_refs}.")

        return tuple(findings)


# Type-checked (mypy), not just conventional: a SemanticValidator
# implementation must actually satisfy the Protocol it claims to pilot.
_conforms_to_semantic_validator_protocol: SemanticValidator = RiskProposalSemanticValidator()


def generate_risk_proposal(
    request: RiskProposalRequest,
    *,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> RiskProposal | None:
    """Returns a released ``RiskProposal``, or ``None`` if the run was
    discarded/rejected (Section 8.9.4: never consume an unreleased AI
    output)."""
    ai_run_id = new_ai_run_id()
    program_id = request.program_id

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

    _lifecycle(AIRunState.PLANNED)

    request_payload = {
        "candidate_title": request.candidate_title,
        "candidate_description": request.candidate_description,
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
                max_tokens=policy.max_tokens or 800,
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

    # ADF-W2.8 pilot: semantic validation runs on the RAW payload, one step
    # before typed parsing, so a rejection's ledger reason is itemized
    # findings, not a generic "failed to parse" message.
    semantic_validator = RiskProposalSemanticValidator(known_evidence_refs=frozenset(request.evidence_refs))
    findings = semantic_validator.validate(raw)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    if findings:
        _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))
        return None

    proposal = _parse_proposal(raw, request=request, ai_run_id=ai_run_id)
    if proposal is None:
        _discard(ReleaseTerminal.REJECTED, "response passed semantic validation but failed the text-safety pipeline.")
        return None

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and risk-proposal semantic validation",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    return proposal


def _build_user_prompt(request: RiskProposalRequest) -> str:
    parts = [
        f"CANDIDATE TITLE: {request.candidate_title}",
        f"CANDIDATE DESCRIPTION: {request.candidate_description}",
        "",
        "EVIDENCE:",
    ]
    if request.evidence_texts:
        for ref, text in zip(request.evidence_refs, request.evidence_texts):
            parts.append(f"  - [{ref}] {text}")
    else:
        parts.append("  (none)")
    parts += [
        "",
        'Respond with JSON: {"causal_title": str, "why_it_matters": str, "probability": '
        '"very_likely"|"likely"|"possible"|"unlikely", "impact": "critical"|"high"|"medium"|"low", '
        '"category": "technical"|"schedule"|"resource"|"dependency"|"external", "mitigation": str, '
        '"owner": str|null, "by_when": "YYYY-MM-DD"|null, "fallback": str, "evidence_refs": [ref, ...]}. '
        "evidence_refs must only contain refs copied verbatim from the EVIDENCE list above.",
    ]
    return "\n".join(parts)


def _parse_proposal(
    raw: dict[str, Any], *, request: RiskProposalRequest, ai_run_id: str
) -> RiskProposal | None:
    try:
        causal_title_raw = str(raw.get("causal_title", "")).strip()
        causal_title = process_generated_text(causal_title_raw).text if causal_title_raw else ""
        why_it_matters_raw = str(raw.get("why_it_matters", "")).strip()
        why_it_matters = process_generated_text(why_it_matters_raw).text if why_it_matters_raw else ""
        mitigation_raw = str(raw.get("mitigation", "")).strip()
        mitigation = process_generated_text(mitigation_raw).text if mitigation_raw else ""
        fallback_raw = str(raw.get("fallback", "")).strip()
        fallback = process_generated_text(fallback_raw).text if fallback_raw else ""
    except AIPipelineError:
        return None

    if not (causal_title and why_it_matters and mitigation and fallback):
        return None

    probability_raw = str(raw.get("probability", "")).strip().lower()
    if probability_raw not in _VALID_PROBABILITIES:
        return None
    impact_raw = str(raw.get("impact", "")).strip().lower()
    if impact_raw not in _VALID_IMPACTS:
        return None
    category_raw = str(raw.get("category", "")).strip().lower()
    if category_raw not in _VALID_CATEGORIES:
        return None

    owner = raw.get("owner")
    owner_alias = str(owner).strip().lower() or None if isinstance(owner, str) else None

    by_when_raw = raw.get("by_when")
    by_when: date | None
    if by_when_raw is None:
        by_when = None
    elif isinstance(by_when_raw, str) and by_when_raw.strip():
        try:
            by_when = date.fromisoformat(by_when_raw.strip())
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

    return RiskProposal(
        id=f"risk-proposal-{ai_run_id}",
        program_id=request.program_id,
        candidate_risk_id=request.candidate_risk_id,
        causal_title=causal_title,
        why_it_matters=why_it_matters,
        probability=RiskProbability(probability_raw),
        impact=RiskImpact(impact_raw),
        category=RiskCategory(category_raw),
        mitigation=mitigation,
        owner_alias=owner_alias,
        by_when=by_when,
        fallback=fallback,
        evidence_refs=evidence_refs,
        ai_run_id=ai_run_id,
    )


def _resolve_model_deployment(client: LLMProvider) -> str:
    """Best-effort model/deployment identifier for the cache key (Section
    8.8.3). ``LLMProvider`` is a minimal Protocol with no required
    deployment attribute; concrete clients (``FallbackStructuredClient`` and
    similar) commonly expose one of these. Falls back to a stable sentinel
    rather than raising -- a wrong/missing deployment id only means the
    cache under-shares (never over-shares, since program_id/feature/input
    hash are still exact-matched), so this is a safe degrade."""
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
    "You are Vertex's risk assessment engine (Section 8.10.1). Given a machine-detected risk candidate "
    "and its evidence, propose a causal title, why it matters, probability, impact, category, mitigation, "
    "owner, by-when date, and fallback plan. Never state a causal claim without evidence. Only cite "
    "evidence_refs that were actually provided."
)


__all__ = ["POLICY_VERSION", "PROMPT_VERSION", "RiskProposalSemanticValidator", "generate_risk_proposal"]
