"""ADF-W2.9 (specs/arch-data-fix.md Section 8.10.5, 8.9): the Zone B half
of the shared program-synthesis contract -- the actual AI call.

Runs the full ADF-W2.8 AI safety lifecycle for real: AISchemaGateway bounds
check on both the outbound request and the inbound response, the five
``AIRunState`` transitions, a concrete ``SemanticValidator`` (the first one
built against a live feature -- ADF-W2.8 deliberately left this
uninvented), and a QG-29 terminal release decision before the result is
ever persisted where a Zone A reader (cockpit, report, ...) could pick it
up. "Human-authored executive summary remains authoritative; AI fills or
proposes; it does not silently overwrite" (Section 8.10.5) is enforced by
callers via ``src.core.human_precedence``, not by this module -- this
module only ever *proposes* a ``ProgramSynthesis``, released or not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.ai.provider import LLMProvider
from src.ai.tiered_router import route_through_tiers
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
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.policy_loader import load_ai_feature_policy
from src.core.program_synthesis import (
    ProgramSynthesis,
    ProgramSynthesisRecommendation,
    ProgramSynthesisRequest,
    content_hash_for_synthesis,
    persist_program_synthesis,
)
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)

_OUTPUT_SCHEMA_VERSION = "1"

PROMPT_VERSION = "program_synthesis.v1"
POLICY_VERSION = "program_synthesis.v1"
_FEATURE = "program_synthesizer"

# ADF-W2.9: the ContextCompiler candidate path (the pilot, mirroring
# decision_brief_advisor's `_via_context_gateway` discipline). The compiler
# token budget is generous because synthesis assembles many input items.
_CONTEXT_MAX_INPUT_TOKENS = 6000
_CONTEXT_OUTPUT_SCHEMA_TEXT = (
    'Respond with JSON: {"through_line": str, "long_poles": [str], "facts": [str], '
    '"inferences": [str], "recommendations": [{"text": str, "evidence_refs": [item_id, ...]}]}.'
)
_CONTEXT_GATEWAY_SYSTEM = "Respond only with the JSON object described below -- no prose, no markdown fences."


@dataclass(frozen=True, slots=True)
class ProgramSynthesisOutcome:
    ai_run_id: str
    released: bool
    synthesis: ProgramSynthesis | None
    findings: tuple[str, ...]


def generate_program_synthesis(
    request: ProgramSynthesisRequest,
    *,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> ProgramSynthesisOutcome:
    """Generate, validate, and (if it passes) release a ``ProgramSynthesis``
    for ``request``. Every call site consuming the result MUST still check
    ``outcome.released`` -- a rejected/discarded outcome carries no
    ``synthesis`` on purpose (Section 8.9.4: never render/propose/persist/
    apply without a durable released terminal)."""
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

    def _discard(terminal: ReleaseTerminal, reason: str, finding_count: int = 0) -> ProgramSynthesisOutcome:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            programs_root=programs_root,
        )
        return ProgramSynthesisOutcome(ai_run_id=ai_run_id, released=False, synthesis=None, findings=(reason,))

    _lifecycle(AIRunState.PLANNED)

    try:
        validate_bounded_payload(_request_to_payload(request))
    except SchemaGatewayError as error:
        return _discard(ReleaseTerminal.DISCARDED, f"AISchemaGateway rejected the outbound request: {error}")

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
        context_manifest_hash=canonical_input_hash("|".join(item.item_id for item in request.items)),
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
                max_tokens=policy.max_tokens or 1200,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value
    except Exception as error:  # provider/transport failure -- not a semantic finding.
        return _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")

    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        return _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        return _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    synthesis = _parse_synthesis(raw, request=request, ai_run_id=ai_run_id)
    if synthesis is None:
        return _discard(ReleaseTerminal.REJECTED, "response missing a non-empty through_line or failed the text-safety pipeline.")

    findings = _validate_program_synthesis_semantics(synthesis, request)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    if findings:
        return _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and program-synthesis semantic validation",
        validator_finding_count=0,
        released_content_hash=content_hash_for_synthesis(synthesis),
        programs_root=programs_root,
    )
    persist_program_synthesis(synthesis, programs_root=programs_root)
    return ProgramSynthesisOutcome(ai_run_id=ai_run_id, released=True, synthesis=synthesis, findings=())


def generate_program_synthesis_via_context_gateway(
    request: ProgramSynthesisRequest,
    *,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> ProgramSynthesisOutcome:
    """ADF-W2.9 (specs/arch-data-fix.md Sections 8.7/8.9/8.10.5): the
    ContextCompiler candidate path -- same input/output shape as
    ``generate_program_synthesis`` (a drop-in for blind A/B comparison), but
    evidence assembly runs through ``DeterministicContextCompiler``
    (ADF-W2.7) instead of the ad hoc ``_build_user_prompt`` above. The same
    full QG-29 lifecycle (AISchemaGateway bounds, five ``AIRunState``
    transitions, the program-synthesis ``SemanticValidator``, and the
    released/rejected/discarded terminal decision) still runs -- only the
    user-prompt construction differs.

    Like decision_brief_advisor's pilot, this is NOT a production swap: it
    exists so a blind comparison harness can gather real evidence on whether
    the compiler's deterministic salience/quota packing produces a better or
    worse synthesis than the ad-hoc path before either is promoted. The
    production-swap decision is calendar/human-bound and out of scope for
    code."""
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

    def _discard(terminal: ReleaseTerminal, reason: str, finding_count: int = 0) -> ProgramSynthesisOutcome:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            programs_root=programs_root,
        )
        return ProgramSynthesisOutcome(ai_run_id=ai_run_id, released=False, synthesis=None, findings=(reason,))

    _lifecycle(AIRunState.PLANNED)

    try:
        validate_bounded_payload(_request_to_payload(request))
    except SchemaGatewayError as error:
        return _discard(ReleaseTerminal.DISCARDED, f"AISchemaGateway rejected the outbound request: {error}")

    _lifecycle(AIRunState.REQUESTED)

    policy = load_ai_feature_policy(_FEATURE)
    compiled_prompt = _compile_synthesis_prompt(request, programs_root=programs_root)
    # When the compiler rejects the request (e.g. reserved tokens alone
    # exceed the budget), degrade to the proven ad-hoc prompt rather than
    # discarding -- the synthesis still has a valid path to completion, and
    # the comparison harness sees the same fallback the baseline uses.
    user_prompt = compiled_prompt if compiled_prompt is not None else _build_user_prompt(request)
    prompt_template = _load_prompt_template()
    cache_key = AIResultCacheKey(
        program_id=program_id,
        feature=_FEATURE,
        canonical_input_hash=canonical_input_hash(user_prompt),
        prompt_version=PROMPT_VERSION,
        policy_version=POLICY_VERSION,
        model_deployment=_resolve_model_deployment(client),
        context_manifest_hash=canonical_input_hash("|".join(item.item_id for item in request.items)),
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
    )
    try:
        raw = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                prompt_template,
                user_prompt,
                parser=lambda payload: payload,
                max_tokens=policy.max_tokens or 1200,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value
    except Exception as error:  # provider/transport failure -- not a semantic finding.
        return _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")

    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        return _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        return _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    synthesis = _parse_synthesis(raw, request=request, ai_run_id=ai_run_id)
    if synthesis is None:
        return _discard(ReleaseTerminal.REJECTED, "response missing a non-empty through_line or failed the text-safety pipeline.")

    findings = _validate_program_synthesis_semantics(synthesis, request)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    if findings:
        return _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and program-synthesis semantic validation (context-gateway path)",
        validator_finding_count=0,
        released_content_hash=content_hash_for_synthesis(synthesis),
        programs_root=programs_root,
    )
    persist_program_synthesis(synthesis, programs_root=programs_root)
    return ProgramSynthesisOutcome(ai_run_id=ai_run_id, released=True, synthesis=synthesis, findings=())


def _compile_synthesis_prompt(
    request: ProgramSynthesisRequest,
    *,
    programs_root: Path,
) -> str | None:
    """Build the user prompt through ``DeterministicContextCompiler`` (ADF-W2.7).
    Each ``SynthesisInputItem`` becomes one required ``EvidenceSpan`` (its
    summary), and coverage notes become optional spans (the compiler may drop
    them under quota/token pressure, which is the point -- bounded context).
    Returns ``None`` when the compiler rejects the request (QG-32), so the
    caller can degrade to the ad-hoc prompt. Mirrors
    decision_brief_advisor's ``_build_evidence_spans``/``_span`` helpers."""
    required = tuple(
        EvidenceSpan(
            evidence_id=f"item_{item.item_id}",
            source_family=item.category,
            text=item.summary,
            required=True,
            origin=ContentOrigin.AUTHORED,
            trust_level="high",
            verification_state="verified",
            injection_screen="pass",
            salience_inputs={},
            token_estimate=estimate_tokens(item.summary),
        )
        for item in request.items
        if item.summary
    )
    optional = tuple(
        EvidenceSpan(
            evidence_id=f"coverage_note_{index}",
            source_family="coverage_note",
            text=note,
            required=False,
            origin=ContentOrigin.SYSTEM,
            trust_level="medium",
            verification_state="unverified",
            injection_screen="pass",
            salience_inputs={"materiality": 0.4},
            token_estimate=estimate_tokens(note),
        )
        for index, note in enumerate(request.coverage_notes)
        if note
    )
    compile_request = ContextCompileRequest(
        program_id=request.program_id,
        edition_id=None,
        feature=_FEATURE,
        prompt_version=PROMPT_VERSION,
        system_instructions=_CONTEXT_GATEWAY_SYSTEM,
        output_schema_text=_CONTEXT_OUTPUT_SCHEMA_TEXT,
        required_evidence=required,
        optional_evidence=optional,
        max_input_tokens=_CONTEXT_MAX_INPUT_TOKENS,
        reserved_output_tokens=load_ai_feature_policy(_FEATURE).max_tokens or 1200,
    )
    validation_context = ValidationContext(
        program_id=request.program_id,
        run_id=new_ai_run_id(),
        execution_mode="advisory",
        classification="internal",
    )
    try:
        compiled = DeterministicContextCompiler(programs_root=programs_root).compile(
            compile_request, validation_context=validation_context
        )
    except ContextCompileRejected:
        return None
    return compiled.prompt_text


def _validate_program_synthesis_semantics(
    synthesis: ProgramSynthesis, request: ProgramSynthesisRequest
) -> tuple[str, ...]:
    """The first concrete ``SemanticValidator`` (ADF-W2.8's Protocol,
    previously uninstantiated) -- enforces Section 8.10.5's "no unsupported
    causal claim" and "source-backed recommendations" structurally: every
    recommendation must cite at least one ``item_id`` that was actually in
    the assembled request."""
    findings: list[str] = []
    if not synthesis.through_line.strip():
        findings.append("through_line is empty (Section 8.10.5 requires a coherent through-line).")
    if not synthesis.recommendations:
        findings.append("no recommendations produced (Section 8.10.5 requires decision/action specificity).")

    known_ids = {item.item_id for item in request.items}
    for index, rec in enumerate(synthesis.recommendations):
        if not rec.text.strip():
            findings.append(f"recommendation[{index}] has empty text.")
            continue
        if not rec.evidence_refs:
            findings.append(
                f"recommendation[{index}] ({rec.text[:60]!r}) has no evidence_refs -- "
                "Section 8.10.5 requires source-backed recommendations."
            )
            continue
        unknown = tuple(ref for ref in rec.evidence_refs if ref not in known_ids)
        if unknown:
            findings.append(
                f"recommendation[{index}] cites evidence_refs not present in the assembled request items "
                f"{unknown} -- possible unsupported causal claim."
            )
    return tuple(findings)


def _request_to_payload(request: ProgramSynthesisRequest) -> dict[str, Any]:
    return {
        "program_id": request.program_id,
        "as_of": request.as_of.isoformat(),
        "items": [
            {
                "category": item.category,
                "item_id": item.item_id,
                "summary": item.summary,
                "evidence_refs": list(item.evidence_refs),
                "severity": item.severity,
            }
            for item in request.items
        ],
    }


def _build_user_prompt(request: ProgramSynthesisRequest) -> str:
    parts = [f"PROGRAM: {request.program_id}", f"AS OF: {request.as_of.isoformat()}", "", "INPUT ITEMS:"]
    for item in request.items:
        severity = f" [{item.severity}]" if item.severity else ""
        parts.append(f"  - id={item.item_id} category={item.category}{severity}: {item.summary}")
    if not request.items:
        parts.append("  (none)")
    parts += ["", "COVERAGE NOTES (categories with no data this cycle -- do not invent content for them):"]
    for note in request.coverage_notes:
        parts.append(f"  - {note}")
    parts += [
        "",
        "Respond with JSON: {\"through_line\": str, \"long_poles\": [str], \"facts\": [str], "
        '"inferences": [str], "recommendations": [{"text": str, "evidence_refs": [item_id, ...]}]}. '
        "Every recommendation's evidence_refs must be item_id values copied verbatim from INPUT ITEMS above -- "
        "never invent an id, never make a causal claim without an evidence_ref.",
    ]
    return "\n".join(parts)


def _process_text_list(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    processed: list[str] = []
    for entry in raw:
        text = str(entry).strip()
        if not text:
            continue
        result = process_generated_text(text)
        if result.text:
            processed.append(result.text)
    return tuple(processed)


def _parse_synthesis(
    raw: dict[str, Any], *, request: ProgramSynthesisRequest, ai_run_id: str
) -> ProgramSynthesis | None:
    try:
        through_line_raw = str(raw.get("through_line", "")).strip()
        through_line = process_generated_text(through_line_raw).text if through_line_raw else ""
        long_poles = _process_text_list(raw.get("long_poles", []))
        facts = _process_text_list(raw.get("facts", []))
        inferences = _process_text_list(raw.get("inferences", []))

        recommendations: list[ProgramSynthesisRecommendation] = []
        for entry in raw.get("recommendations", []) if isinstance(raw.get("recommendations"), list) else ():
            if not isinstance(entry, dict):
                continue
            text_raw = str(entry.get("text", "")).strip()
            text = process_generated_text(text_raw).text if text_raw else ""
            evidence_refs = tuple(str(ref) for ref in entry.get("evidence_refs", []) if isinstance(ref, (str, int)))
            recommendations.append(ProgramSynthesisRecommendation(text=text, evidence_refs=evidence_refs))
    except AIPipelineError:
        return None

    if not through_line:
        return None

    return ProgramSynthesis(
        program_id=request.program_id,
        ai_run_id=ai_run_id,
        through_line=through_line,
        long_poles=long_poles,
        facts=facts,
        inferences=inferences,
        recommendations=tuple(recommendations),
        generated_at=datetime.now(timezone.utc),
        prompt_version=PROMPT_VERSION,
        source_item_count=len(request.items),
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
    "You are Vertex's program-synthesis engine (Section 8.10.5). Given a program's verified workstream "
    "syntheses, active milestones, strategic risks, open contradictions, Kusto SLO breaches, and source "
    "waivers, produce one coherent through-line, long-pole identification, a strict fact/inference split, "
    "and source-backed recommendations. Never state a causal claim without an evidence_ref from the given "
    "input items. Distinguish observed fact from inference explicitly."
)


__all__ = [
    "POLICY_VERSION",
    "PROMPT_VERSION",
    "ProgramSynthesisOutcome",
    "generate_program_synthesis",
    "generate_program_synthesis_via_context_gateway",
]
