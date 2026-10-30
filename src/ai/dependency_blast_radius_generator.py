"""ADF-W4.5 remainder (specs/arch-data-fix.md Section 8.10.2): the Zone B
half of dependency blast-radius proposal generation.

The fifth live feature this session to run the full AISchemaGateway +
five-`AIRunState` + QG-29 release lifecycle (after `program_synthesizer.py`,
`meeting_action_extractor.py`, `risk_proposal_generator.py`,
`top_three_candidate_generator.py`).

RELO-W1.3 pilot migration (2026-07-30): ``generate_dependency_blast_radius_
proposal_via_runner`` is a second implementation of this feature routed
through ``StructuredFeatureRunner`` (RELO-F1, Sec 8.1) instead of a direct
``route_through_tiers``/``AIResultCache`` call. It is additive, alongside
the original ``generate_dependency_blast_radius_proposal`` — no existing
caller is changed. This is deliberately the first and, for now, only
pilot: a small, self-contained, single-purpose feature chosen specifically
to validate the runner's design against a real feature's actual evidence/
schema/validation/release-audit shape before any other call site is
touched (RELO-W1.6 remains separate, reviewed, one feature at a time).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.ai.provider import LLMProvider
from src.ai.structured_feature_runner import (
    ContextBuildResult,
    FeatureOutcome,
    FeatureOutcomeStatus,
    ReleasedResultCache,
    RunnerContext,
    StructuredFeatureRunner,
    StructuredFeatureSpec,
    ValidationResult,
)
from src.ai.tiered_router import Tier, route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.context_compiler import (
    CompiledContext,
    ContentOrigin,
    ContextCompileRejected,
    ContextCompileRequest,
    DeterministicContextCompiler,
    EvidenceSpan,
    ValidationContext,
)
from src.core.dependency_blast_radius import DependencyBlastRadiusProposal, DependencyBlastRadiusRequest
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import StateError
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


def generate_dependency_blast_radius_proposal_via_runner(
    request: DependencyBlastRadiusRequest,
    *,
    context: RunnerContext,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> DependencyBlastRadiusProposal | None:
    """RELO-W1.3 pilot: the same feature as
    ``generate_dependency_blast_radius_proposal``, routed through
    ``StructuredFeatureRunner`` instead of a direct
    ``route_through_tiers``/``AIResultCache`` call. Same external contract
    (returns a released proposal or ``None``; a fresh ``ai_run_id`` is
    minted per call even on a cache hit, matching the existing feature's
    established test contract), same release-audit lifecycle recorded,
    same AISchemaGateway bounds checks and semantic validation rules —
    only the dispatch/context/cache mechanics differ. The caller supplies
    the execution context; its identity is not authenticated here:

    - evidence is compiled through ``DeterministicContextCompiler``
      (RELO-W1.2's hardened required-span/token-accounting behavior)
      instead of raw string concatenation;
    - caching goes through the runner's ``ReleasedResultCache`` (RELO-W1.5)
      instead of ``AIResultCache`` directly;
    - the runner's Sec 8.1.5 call-count ceiling applies (unbounded here,
      matching today's uncapped behavior, but now structurally available).
    """
    if context.program_id != request.program_id:
        raise StateError("Runner context program must match the dependency request.")
    if context.policy_version != POLICY_VERSION:
        raise StateError("Runner context policy version must match the dependency feature.")
    ai_run_id = new_ai_run_id()
    program_id = request.program_id
    policy = load_ai_feature_policy(_FEATURE)

    record_ai_run_lifecycle(
        program_id=program_id, ai_run_id=ai_run_id, feature=_FEATURE, state=AIRunState.PLANNED,
        prompt_version=PROMPT_VERSION, policy_version=POLICY_VERSION, programs_root=programs_root,
    )

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
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
            reason=f"AISchemaGateway rejected the outbound request: {error}",
            validator_finding_count=0, programs_root=programs_root,
        )
        return None

    record_ai_run_lifecycle(
        program_id=program_id, ai_run_id=ai_run_id, feature=_FEATURE, state=AIRunState.REQUESTED,
        prompt_version=PROMPT_VERSION, policy_version=POLICY_VERSION, programs_root=programs_root,
    )

    def build_context(req: DependencyBlastRadiusRequest) -> ContextBuildResult:
        if len(req.evidence_refs) != len(req.evidence_texts):
            raise ContextCompileRejected("Dependency evidence references and texts must have equal lengths.")
        request_span = EvidenceSpan(
            evidence_id=f"dependency-request:{req.dependency_id}",
            source_family="dependency_context",
            text=json.dumps({
                "dependency_id": req.dependency_id,
                "from_summary": req.from_summary,
                "to_summary": req.to_summary,
                "risk_if_broken": req.risk_if_broken,
                "current_status": req.current_status,
            }, sort_keys=True),
            required=True, origin=ContentOrigin.AUTHORED, trust_level="high",
            verification_state="verified", injection_screen="pass",
        )
        spans = (request_span,) + tuple(
            EvidenceSpan(
                evidence_id=ref, source_family="dependency_evidence", text=text, required=True,
                origin=ContentOrigin.AUTHORED, trust_level="high", verification_state="verified",
                injection_screen="pass",
            )
            for ref, text in zip(req.evidence_refs, req.evidence_texts, strict=True)
        )
        compiled = DeterministicContextCompiler(programs_root=programs_root).compile(
            ContextCompileRequest(
                program_id=req.program_id, edition_id=None, feature=_FEATURE, prompt_version=PROMPT_VERSION,
                system_instructions=_load_prompt_template(),
                output_schema_text=(
                    'Respond with JSON: {"next_proving_event": str, "blast_radius_narrative": str, '
                    '"evidence_refs": [ref, ...]}. evidence_refs must only contain refs copied verbatim '
                    "from the EVIDENCE list above."
                ),
                required_evidence=spans, optional_evidence=(),
                max_input_tokens=6000, reserved_output_tokens=policy.max_tokens or 700,
            ),
            validation_context=ValidationContext(
                program_id=req.program_id, run_id=context.run_id, execution_mode="observe",
                classification=context.classification,
            ),
        )
        return ContextBuildResult(compiled=compiled)

    def call_provider(_req: DependencyBlastRadiusRequest, compiled: CompiledContext) -> dict[str, Any]:
        raw = client.structured(
            compiled.prompt_text, "", parser=lambda payload: payload,
            max_tokens=policy.max_tokens or 700, prompt_version=PROMPT_VERSION,
        )
        if not isinstance(raw, dict):
            raise AIPipelineError("no structured response returned by the provider.")
        validate_bounded_payload(raw)
        return raw

    def semantic_validator(req: DependencyBlastRadiusRequest, raw: dict[str, Any]) -> ValidationResult:
        proposal = _parse_proposal(raw, request=req, ai_run_id=ai_run_id)
        if proposal is None:
            return ValidationResult(valid=False, reason="response missing required fields or failed the text-safety pipeline.")
        findings = _validate_semantics(proposal, request=req)
        if findings:
            return ValidationResult(valid=False, reason="; ".join(findings))
        return ValidationResult(valid=True, evidence_refs=proposal.evidence_refs)

    def fallback_handler(req: DependencyBlastRadiusRequest) -> FeatureOutcome[dict[str, Any]]:
        # No deterministic fallback exists for this feature (same as the
        # original implementation, which returns None on any failure path).
        return FeatureOutcome(status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE)

    runner = StructuredFeatureRunner(
        context=context,
        frontier_eligible=policy.frontier_eligible,
        released_cache=ReleasedResultCache(_released_cache_path(programs_root, program_id)),
    )
    spec = StructuredFeatureSpec(
        feature=_FEATURE, task_class="synthesis", prompt_version=PROMPT_VERSION, schema_version=_OUTPUT_SCHEMA_VERSION,
        response_type=dict, deterministic_handler=None, local_router_name=None,
        fallback_handler=fallback_handler, semantic_validator=semantic_validator,
        build_context=build_context, call_provider=call_provider,
        request_hash=lambda req: canonical_input_hash(_build_user_prompt(req)),
        deployment_fingerprint=_resolve_model_deployment(client),
        validator_version="dependency_blast_radius_generator.v1",
        serialize_response=lambda payload: json.dumps(payload, sort_keys=True),
        deserialize_response=lambda text: json.loads(text),
    )

    try:
        outcome = runner.run(spec, request)
    except Exception as error:
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
            reason=f"provider call failed: {error}", validator_finding_count=0, programs_root=programs_root,
        )
        return None

    record_ai_run_lifecycle(
        program_id=program_id, ai_run_id=ai_run_id, feature=_FEATURE, state=AIRunState.RESPONDED,
        prompt_version=PROMPT_VERSION, policy_version=POLICY_VERSION, programs_root=programs_root,
    )
    record_ai_run_lifecycle(
        program_id=program_id, ai_run_id=ai_run_id, feature=_FEATURE, state=AIRunState.SEMANTICALLY_VALIDATED,
        prompt_version=PROMPT_VERSION, policy_version=POLICY_VERSION, programs_root=programs_root,
    )

    if not outcome.released:
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id,
            terminal=ReleaseTerminal.REJECTED if outcome.status == FeatureOutcomeStatus.BLOCKED else ReleaseTerminal.DISCARDED,
            reason="; ".join(outcome.limitations) or outcome.status.value,
            validator_finding_count=len(outcome.limitations), programs_root=programs_root,
        )
        return None

    assert outcome.value is not None
    proposal = _parse_proposal(outcome.value, request=request, ai_run_id=ai_run_id)
    if proposal is None:
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
            reason="released payload failed to re-parse into a proposal.",
            validator_finding_count=0, programs_root=programs_root,
        )
        return None

    record_ai_release_decision(
        program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and dependency blast-radius semantic validation (via StructuredFeatureRunner)",
        validator_finding_count=0, programs_root=programs_root,
    )
    return proposal


def _released_cache_path(programs_root: Path, program_id: str) -> Path:
    return programs_root / program_id / "runtime" / "ai_released_result_cache.sqlite3"


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


__all__ = [
    "POLICY_VERSION",
    "PROMPT_VERSION",
    "generate_dependency_blast_radius_proposal",
    "generate_dependency_blast_radius_proposal_via_runner",
]
