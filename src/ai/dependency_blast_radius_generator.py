"""ADF-W4.5 remainder (specs/arch-data-fix.md Section 8.10.2): the Zone B
half of dependency blast-radius proposal generation.

The fifth live feature this session to run the full AISchemaGateway +
five-`AIRunState` + QG-29 release lifecycle (after `program_synthesizer.py`,
`meeting_action_extractor.py`, `risk_proposal_generator.py`,
`top_three_candidate_generator.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.ai.provider import LLMProvider
from src.ai.tiered_router import route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.dependency_blast_radius import DependencyBlastRadiusProposal, DependencyBlastRadiusRequest
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)

_OUTPUT_SCHEMA_VERSION = "1"

PROMPT_VERSION = "dependency_blast_radius_generator.v1"
POLICY_VERSION = "dependency_blast_radius_generator.v1"
_FEATURE = "dependency_blast_radius_generator"


def generate_dependency_blast_radius_proposal(
    request: DependencyBlastRadiusRequest,
    *,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> DependencyBlastRadiusProposal | None:
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
        "from_summary": request.from_summary,
        "to_summary": request.to_summary,
        "risk_if_broken": request.risk_if_broken,
        "current_status": request.current_status,
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
                max_tokens=policy.max_tokens or 700,
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
        reason="passed AISchemaGateway bounds and dependency blast-radius semantic validation",
        validator_finding_count=0, programs_root=programs_root,
    )
    return proposal


def _validate_semantics(
    proposal: DependencyBlastRadiusProposal, *, request: DependencyBlastRadiusRequest
) -> tuple[str, ...]:
    findings: list[str] = []
    if not proposal.next_proving_event.strip():
        findings.append("next_proving_event is empty.")
    if not proposal.blast_radius_narrative.strip():
        findings.append("blast_radius_narrative is empty.")
    known_refs = set(request.evidence_refs)
    unknown_refs = tuple(ref for ref in proposal.evidence_refs if ref not in known_refs)
    if unknown_refs:
        findings.append(f"evidence_refs cites refs not present on the dependency: {unknown_refs}.")
    return tuple(findings)


def _build_user_prompt(request: DependencyBlastRadiusRequest) -> str:
    parts = [
        f"UPSTREAM (from): {request.from_summary}",
        f"DOWNSTREAM (to): {request.to_summary}",
        f"RISK IF BROKEN: {request.risk_if_broken}",
        f"CURRENT STATUS: {request.current_status}",
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
        'Respond with JSON: {"next_proving_event": str, "blast_radius_narrative": str, "evidence_refs": [ref, ...]}. '
        "evidence_refs must only contain refs copied verbatim from the EVIDENCE list above.",
    ]
    return "\n".join(parts)


def _parse_proposal(
    raw: dict[str, Any], *, request: DependencyBlastRadiusRequest, ai_run_id: str
) -> DependencyBlastRadiusProposal | None:
    try:
        next_event_raw = str(raw.get("next_proving_event", "")).strip()
        next_proving_event = process_generated_text(next_event_raw).text if next_event_raw else ""
        narrative_raw = str(raw.get("blast_radius_narrative", "")).strip()
        blast_radius_narrative = process_generated_text(narrative_raw).text if narrative_raw else ""
    except AIPipelineError:
        return None

    if not next_proving_event or not blast_radius_narrative:
        return None

    evidence_refs_raw = raw.get("evidence_refs")
    evidence_refs = (
        tuple(str(ref) for ref in evidence_refs_raw if isinstance(ref, str))
        if isinstance(evidence_refs_raw, list)
        else ()
    )

    return DependencyBlastRadiusProposal(
        id=f"blast-radius-{ai_run_id}",
        program_id=request.program_id,
        dependency_id=request.dependency_id,
        next_proving_event=next_proving_event,
        blast_radius_narrative=blast_radius_narrative,
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
    "You are Vertex's dependency blast-radius engine (Section 8.10.2). Given a dependency's upstream "
    "source, downstream impact, risk if broken, current status, and evidence, identify the next proving "
    "event (the concrete next signal that will confirm whether this dependency is actually on track) and "
    "write a blast-radius narrative (what breaks, and how far it spreads, if this dependency fails). "
    "Never state a causal claim without evidence. Only cite evidence_refs that were actually provided."
)


__all__ = ["POLICY_VERSION", "PROMPT_VERSION", "generate_dependency_blast_radius_proposal"]
