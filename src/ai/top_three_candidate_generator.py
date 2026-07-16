"""ADF-W4.7 (specs/arch-data-fix.md Section 8.10.6): the Zone B half of
top-three candidate generation.

Reuses ADF-W2.9's ``ProgramSynthesisRequest``/``SynthesisInputItem`` as the
candidate pool -- the same assembled evidence (verified workstream
syntheses, active milestones, strategic risks, open contradictions, Kusto
SLO breaches, source waivers) that already feeds ``program_synthesizer.py``,
rather than re-deriving a third evidence-assembly path. This module's own
job is narrower: select three of those items and write up Section 8.10.6's
required fields (reason, evidence, urgency, decision/action needed, owner,
confidence) for each, through the same AI safety lifecycle established by
``program_synthesizer.py``/``meeting_action_extractor.py``/
``risk_proposal_generator.py``.
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
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.policy_loader import load_ai_feature_policy
from src.core.program_synthesis import ProgramSynthesisRequest
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.top_three_candidates import TopThreeCandidateProposal

_OUTPUT_SCHEMA_VERSION = "1"

PROMPT_VERSION = "top_three_candidate_generator.v1"
POLICY_VERSION = "top_three_candidate_generator.v1"
_FEATURE = "top_three_candidate_generator"

_VALID_LEVELS = {"high", "medium", "low"}
_MAX_CANDIDATES = 3


def generate_top_three_candidates(
    request: ProgramSynthesisRequest,
    *,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[TopThreeCandidateProposal, ...]:
    """Returns up to three released candidates, or an empty tuple if the
    run was discarded/rejected. Never more than three -- a response
    proposing a fourth is rejected outright (fail closed on a schema
    violation, not silently truncated)."""
    if not request.items:
        return ()

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

    def _discard(terminal: ReleaseTerminal, reason: str, finding_count: int = 0) -> tuple[TopThreeCandidateProposal, ...]:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            programs_root=programs_root,
        )
        return ()

    _lifecycle(AIRunState.PLANNED)

    request_payload = {
        "items": [
            {"category": item.category, "item_id": item.item_id, "summary": item.summary, "severity": item.severity}
            for item in request.items
        ],
    }
    try:
        validate_bounded_payload(request_payload)
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
                max_tokens=policy.max_tokens or 900,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value
    except Exception as error:
        return _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")

    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        return _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        return _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    candidates = _parse_candidates(raw, request=request, ai_run_id=ai_run_id)
    if candidates is None:
        return _discard(ReleaseTerminal.REJECTED, "response missing/malformed candidates list.")

    findings = _validate_semantics(candidates, request=request)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    if findings:
        return _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and top-three semantic validation",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    return candidates


def _validate_semantics(
    candidates: tuple[TopThreeCandidateProposal, ...], *, request: ProgramSynthesisRequest
) -> tuple[str, ...]:
    findings: list[str] = []
    if not candidates:
        findings.append("no candidates produced.")
    if len(candidates) > _MAX_CANDIDATES:
        findings.append(f"{len(candidates)} candidates produced, exceeding the required top-{_MAX_CANDIDATES}.")
    known_ids = {item.item_id for item in request.items}
    seen_ids: set[str] = set()
    for index, candidate in enumerate(candidates):
        if candidate.item_id not in known_ids:
            findings.append(f"candidate[{index}] cites item_id {candidate.item_id!r} outside the assembled request.")
        if candidate.item_id in seen_ids:
            findings.append(f"candidate[{index}] duplicates item_id {candidate.item_id!r}.")
        seen_ids.add(candidate.item_id)
        if not candidate.reason.strip():
            findings.append(f"candidate[{index}] has an empty reason.")
        if not candidate.decision_or_action_needed.strip():
            findings.append(f"candidate[{index}] has an empty decision_or_action_needed.")
        if not candidate.evidence_refs:
            findings.append(f"candidate[{index}] has no evidence_refs.")
    return tuple(findings)


def _build_user_prompt(request: ProgramSynthesisRequest) -> str:
    parts = [f"PROGRAM: {request.program_id}", "", "CANDIDATE ITEMS:"]
    for item in request.items:
        severity = f" [{item.severity}]" if item.severity else ""
        parts.append(f"  - id={item.item_id} category={item.category}{severity}: {item.summary}")
    parts += [
        "",
        "Select the THREE most important items from the candidates above (never more, never fewer unless "
        "fewer than three candidates exist). Respond with JSON: "
        '{"candidates": [{"item_id": str, "reason": str, "evidence_refs": [item_id, ...], '
        '"urgency": "high"|"medium"|"low", "decision_or_action_needed": str, "owner": str|null, '
        '"confidence": "high"|"medium"|"low"}]}. '
        "item_id must be copied verbatim from the CANDIDATE ITEMS list. evidence_refs must only contain "
        "item_id values from that same list.",
    ]
    return "\n".join(parts)


def _parse_candidates(
    raw: dict[str, Any], *, request: ProgramSynthesisRequest, ai_run_id: str
) -> tuple[TopThreeCandidateProposal, ...] | None:
    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list):
        return None
    parsed: list[TopThreeCandidateProposal] = []
    for index, entry in enumerate(raw_candidates, start=1):
        candidate = _parse_one(entry, program_id=request.program_id, ai_run_id=ai_run_id, index=index)
        if candidate is None:
            return None
        parsed.append(candidate)
    return tuple(parsed)


def _parse_one(entry: Any, *, program_id: str, ai_run_id: str, index: int) -> TopThreeCandidateProposal | None:
    if not isinstance(entry, dict):
        return None
    item_id = entry.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        return None

    try:
        reason_raw = str(entry.get("reason", "")).strip()
        reason = process_generated_text(reason_raw).text if reason_raw else ""
        action_raw = str(entry.get("decision_or_action_needed", "")).strip()
        decision_or_action_needed = process_generated_text(action_raw).text if action_raw else ""
    except AIPipelineError:
        return None
    if not reason or not decision_or_action_needed:
        return None

    urgency = str(entry.get("urgency", "")).strip().lower()
    if urgency not in _VALID_LEVELS:
        return None
    confidence = str(entry.get("confidence", "")).strip().lower()
    if confidence not in _VALID_LEVELS:
        return None

    evidence_refs_raw = entry.get("evidence_refs")
    evidence_refs = (
        tuple(str(ref) for ref in evidence_refs_raw if isinstance(ref, str))
        if isinstance(evidence_refs_raw, list)
        else ()
    )

    owner = entry.get("owner")
    owner_alias = str(owner).strip().lower() or None if isinstance(owner, str) else None

    return TopThreeCandidateProposal(
        id=f"top3-candidate-{ai_run_id}-{index}",
        program_id=program_id,
        item_id=item_id,
        reason=reason,
        evidence_refs=evidence_refs,
        urgency=urgency,  # type: ignore[arg-type]
        decision_or_action_needed=decision_or_action_needed,
        owner_alias=owner_alias,
        confidence=confidence,  # type: ignore[arg-type]
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
    "You are Vertex's top-three prioritization engine (Section 8.10.6). Given a program's assembled "
    "candidate items, select exactly the three most important ones and write up why each matters, how "
    "urgent it is, what decision or action is needed, who owns it, and your confidence. Never cite an "
    "item_id or evidence_ref that was not in the candidate list you were given."
)


__all__ = ["POLICY_VERSION", "PROMPT_VERSION", "generate_top_three_candidates"]
