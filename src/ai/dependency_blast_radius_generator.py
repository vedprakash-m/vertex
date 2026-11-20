"""ADF-W4.5 remainder (specs/arch-data-fix.md Section 8.10.2): the Zone B
half of dependency blast-radius proposal generation.

The fifth live feature this session to run the full AISchemaGateway +
five-`AIRunState` + QG-29 release lifecycle (after `program_synthesizer.py`,
`meeting_action_extractor.py`, `risk_proposal_generator.py`,
`top_three_candidate_generator.py`).

RELO-W1.3/W1.6 migration: ``generate_dependency_blast_radius_proposal_via_runner``
routes the production proposal command through ``StructuredFeatureRunner``
(RELO-F1, Sec 8.1). The original direct implementation remains an explicitly
inventoried compatibility path for parity and historical cache coverage, but
the existing command now requires a caller-supplied bound ``RunnerContext``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.deployment_fallback import FallbackAIClient
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.ai.provider import LLMProvider
from src.ai.structured_feature_runner import (
    CacheIdentity,
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
from src.core.exceptions import ConfigError, StateError
from src.core.ledger.event_log import canonical_json, get_event_logs_dir, read_events
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
    released_content_matches,
)

_OUTPUT_SCHEMA_VERSION = "1"
_CACHE_IDENTITY_VERSION = "dependency_blast_radius.released.v1"
_PILOT_CACHE_RELEASE_SCHEMA = "dependency_blast_radius.released_cache.v1"

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
    program_id = request.program_id
    get_event_logs_dir(program_id, programs_root=programs_root)
    ai_run_id = new_ai_run_id()

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

    if len(request.evidence_refs) != len(request.evidence_texts):
        _lifecycle(AIRunState.PLANNED)
        _discard(ReleaseTerminal.DISCARDED, "Evidence references and texts must be paired.")
        return None

    request_payload = {
        "program_id": request.program_id,
        "dependency_id": request.dependency_id,
        "from_summary": request.from_summary,
        "to_summary": request.to_summary,
        "risk_if_broken": request.risk_if_broken,
        "current_status": request.current_status,
        "evidence_texts": list(request.evidence_texts),
        "evidence_refs": list(request.evidence_refs),
    }
    try:
        validate_bounded_payload(request_payload)
    except SchemaGatewayError:
        _lifecycle(AIRunState.PLANNED)
        _discard(ReleaseTerminal.DISCARDED, "AISchemaGateway rejected the outbound request.")
        return None

    cache_key = AIResultCacheKey(
        program_id=program_id,
        feature=_FEATURE,
        canonical_input_hash=canonical_input_hash(canonical_json({
            "identity_version": _CACHE_IDENTITY_VERSION,
            **request_payload,
        })),
        prompt_version=PROMPT_VERSION,
        policy_version=POLICY_VERSION,
        model_deployment=_resolve_dependency_cache_deployment(client),
        context_manifest_hash=canonical_input_hash("|".join(request.evidence_refs)),
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
    )
    try:
        cached_response = _cached_response(cache_key, request=request, programs_root=programs_root)
    except StateError:
        try:
            read_events(program_id, programs_root=programs_root, repair_corruption=False)
        except (ConfigError, OSError, UnicodeError, ValueError, RecursionError, StateError):
            raise StateError("Dependency proposal release audit could not be verified; no proposal released.") from None
        _lifecycle(AIRunState.PLANNED)
        _lifecycle(AIRunState.REQUESTED)
        _discard(ReleaseTerminal.DISCARDED, "Dependency proposal cache could not be verified; no proposal released.")
        raise StateError("Dependency proposal cache could not be verified; no proposal released.") from None
    except (ConfigError, OSError, UnicodeError, ValueError, RecursionError):
        raise StateError("Dependency proposal release audit could not be verified; no proposal released.") from None

    _lifecycle(AIRunState.PLANNED)
    _lifecycle(AIRunState.REQUESTED)
    prompt_template = _load_prompt_template()
    policy = load_ai_feature_policy(_FEATURE)
    try:
        route = route_through_tiers(
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
            cache_lookup_fn=lambda: cached_response,
        )
    except Exception:
        _discard(ReleaseTerminal.DISCARDED, "provider call failed.")
        return None

    _lifecycle(AIRunState.RESPONDED)

    raw = route.value
    if not isinstance(raw, dict):
        _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")
        return None

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError:
        _discard(ReleaseTerminal.REJECTED, "AISchemaGateway rejected the response.")
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

    content = {
        "program_id": request.program_id,
        "dependency_id": request.dependency_id,
        "request_hash": cache_key.canonical_input_hash,
        "next_proving_event": proposal.next_proving_event,
        "blast_radius_narrative": proposal.blast_radius_narrative,
        "evidence_refs": list(proposal.evidence_refs),
    }
    record_ai_release_decision(
        program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and dependency blast-radius semantic validation",
        validator_finding_count=0, released_content_hash=canonical_input_hash(canonical_json(content)),
        programs_root=programs_root,
    )
    if route.frontier_called:
        try:
            put_ai_result(cache_key, {"release_run_id": ai_run_id, "proposal": content}, programs_root=programs_root)
        except (OSError, ValueError, RecursionError, StateError):
            raise StateError("Dependency proposal cache write failed after release; no proposal staged.") from None
    return proposal


def generate_dependency_blast_radius_proposal_via_runner(
    request: DependencyBlastRadiusRequest,
    *,
    context: RunnerContext,
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> DependencyBlastRadiusProposal | None:
    """RELO-W1.3/W1.6 runner path: the same feature as
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
    lifecycle_error: Exception | None = None
    release_write_pending = False
    release_written = False
    released_proposal: DependencyBlastRadiusProposal | None = None
    origin_read_pending = False
    verified_release_hash: str | None = None
    semantic_stage_recorded = False
    replayed_proposal: DependencyBlastRadiusProposal | None = None
    replay_refusal_reason: str | None = None
    replay_finding_count = 0

    try:
        read_events(program_id, programs_root=programs_root, repair_corruption=False)
    except (ConfigError, OSError, UnicodeError, ValueError, RecursionError):
        raise StateError("Dependency proposal release audit could not be verified.") from None

    def _lifecycle(state: AIRunState) -> None:
        nonlocal lifecycle_error
        try:
            record_ai_run_lifecycle(
                program_id=program_id, ai_run_id=ai_run_id, feature=_FEATURE, state=state,
                prompt_version=PROMPT_VERSION, policy_version=POLICY_VERSION, programs_root=programs_root,
            )
        except Exception as error:
            # The runner catches provider callback exceptions; preserve audit-write failures.
            lifecycle_error = error
            raise

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
    except SchemaGatewayError:
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
            reason="AISchemaGateway rejected the outbound request.",
            validator_finding_count=0, programs_root=programs_root,
        )
        return None
    if len(request.evidence_refs) != len(request.evidence_texts):
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
            reason="Dependency evidence references and texts must have equal lengths.",
            validator_finding_count=0, programs_root=programs_root,
        )
        return None

    _lifecycle(AIRunState.REQUESTED)

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
            verification_state="operator_supplied", injection_screen="pass",
        )
        spans = (request_span,) + tuple(
            EvidenceSpan(
                evidence_id=ref, source_family="dependency_evidence", text=text, required=True,
                origin=ContentOrigin.AUTHORED, trust_level="operator_supplied",
                verification_state="unverified",
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
        _lifecycle(AIRunState.RESPONDED)
        if not isinstance(raw, dict):
            raise AIPipelineError("no structured response returned by the provider.")
        validate_bounded_payload(raw)
        _lifecycle(AIRunState.SCHEMA_VALIDATED)
        return raw

    def semantic_validator(req: DependencyBlastRadiusRequest, raw: dict[str, Any]) -> ValidationResult:
        nonlocal semantic_stage_recorded, replayed_proposal, replay_refusal_reason, replay_finding_count
        cache_hit = verified_release_hash is not None
        revalidating_payload = semantic_stage_recorded
        if cache_hit:
            _lifecycle(AIRunState.RESPONDED)
            try:
                validate_bounded_payload(raw)
            except SchemaGatewayError:
                replay_refusal_reason = "released payload failed structural validation."
                raise StateError("Released dependency proposal failed structural validation.") from None
            _lifecycle(AIRunState.SCHEMA_VALIDATED)
        proposal = _parse_proposal(raw, request=req, ai_run_id=ai_run_id)
        if proposal is None:
            if cache_hit:
                replay_refusal_reason = "released payload failed to re-parse into a proposal."
                raise StateError("Released dependency proposal failed to re-parse.")
            if revalidating_payload:
                raise StateError("Serialized dependency proposal failed release validation.")
            return ValidationResult(valid=False, reason="response missing required fields or failed the text-safety pipeline.")
        if (cache_hit or revalidating_payload) and proposal_fields(proposal) != raw:
            if cache_hit:
                replay_refusal_reason = "revalidated proposal did not match released payload."
            raise StateError("Revalidated dependency proposal did not match released payload.")
        findings = _validate_semantics(proposal, request=req)
        if not semantic_stage_recorded:
            _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
            semantic_stage_recorded = True
        if findings:
            if cache_hit:
                replay_refusal_reason = "released payload failed dependency semantic validation."
                replay_finding_count = len(findings)
            elif revalidating_payload:
                raise StateError("Serialized dependency proposal failed release validation.")
            return ValidationResult(valid=False, reason="; ".join(findings))
        if cache_hit:
            replayed_proposal = proposal
        return ValidationResult(valid=True, evidence_refs=proposal.evidence_refs)

    def deserialize_response(text: str) -> dict[str, Any]:
        response = json.loads(text)
        if not isinstance(response, dict):
            raise StateError("Dependency proposal cached response must be an object.")
        return response

    def proposal_fields(proposal: DependencyBlastRadiusProposal) -> dict[str, object]:
        return {
            "next_proving_event": proposal.next_proving_event,
            "blast_radius_narrative": proposal.blast_radius_narrative,
            "evidence_refs": list(proposal.evidence_refs),
        }

    def serialize_response(raw: dict[str, Any]) -> str:
        proposal = _parse_proposal(raw, request=request, ai_run_id=ai_run_id)
        if proposal is None:
            raise StateError("Dependency proposal could not be serialized safely.")
        return json.dumps(proposal_fields(proposal), sort_keys=True)

    def release_hash(identity: CacheIdentity, payload: str) -> str:
        return canonical_input_hash(canonical_json({
            "schema": _PILOT_CACHE_RELEASE_SCHEMA,
            "cache_key": identity.cache_key(),
            "payload": payload,
        }))

    def record_release(identity: CacheIdentity, payload: str) -> str:
        nonlocal release_write_pending, release_written, released_proposal
        response = deserialize_response(payload)
        validate_bounded_payload(response)
        proposal = _parse_proposal(response, request=request, ai_run_id=ai_run_id)
        if (
            proposal is None or proposal_fields(proposal) != response
            or _validate_semantics(proposal, request=request)
        ):
            raise StateError("Serialized dependency proposal failed release validation.")
        content_hash = release_hash(identity, payload)
        release_write_pending = True
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
            reason="passed AISchemaGateway bounds and dependency blast-radius semantic validation (via StructuredFeatureRunner)",
            validator_finding_count=0, released_content_hash=content_hash, programs_root=programs_root,
        )
        release_written = True
        release_write_pending = False
        released_proposal = proposal
        return ai_run_id

    def verify_release(identity: CacheIdentity, payload: str, origin_run_id: str) -> bool:
        nonlocal origin_read_pending, verified_release_hash
        origin_read_pending = True
        content_hash = release_hash(identity, payload)
        matches = released_content_matches(
            origin_run_id, program_id=program_id, released_content_hash=content_hash,
            programs_root=programs_root,
        )
        origin_read_pending = False
        if matches is True:
            verified_release_hash = content_hash
        return matches

    def fallback_handler(req: DependencyBlastRadiusRequest) -> FeatureOutcome[dict[str, Any]]:
        # No deterministic fallback exists for this feature (same as the
        # original implementation, which returns None on any failure path).
        return FeatureOutcome(status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE)

    runner = StructuredFeatureRunner(
        context=context,
        frontier_eligible=policy.frontier_eligible,
        released_cache=ReleasedResultCache(
            _released_cache_path(programs_root, program_id),
            record_release=record_release, verify_release=verify_release,
        ),
    )
    spec = StructuredFeatureSpec(
        feature=_FEATURE, task_class="synthesis", prompt_version=PROMPT_VERSION, schema_version=_OUTPUT_SCHEMA_VERSION,
        response_type=dict, deterministic_handler=None, local_router_name=None,
        fallback_handler=fallback_handler, semantic_validator=semantic_validator,
        build_context=build_context, call_provider=call_provider,
        request_hash=lambda req: canonical_input_hash(_build_user_prompt(req)),
        deployment_fingerprint=_resolve_dependency_cache_deployment(client),
        validator_version="dependency_blast_radius_generator.v1",
        serialize_response=serialize_response,
        deserialize_response=deserialize_response,
    )

    try:
        outcome = runner.run(spec, request)
    except Exception as error:
        if lifecycle_error is not None:
            raise lifecycle_error
        if origin_read_pending:
            raise StateError("Dependency proposal release audit could not be verified.") from None
        if release_write_pending:
            raise
        if release_written:
            raise StateError("Dependency proposal cache write failed after release; no proposal staged.") from None
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.DISCARDED,
            reason=(
                replay_refusal_reason
                or (
                    "Structured runner state failure; no proposal released."
                    if isinstance(error, StateError) else "Structured runner failed; no proposal released."
                )
            ),
            validator_finding_count=0, programs_root=programs_root,
        )
        return None

    if lifecycle_error is not None:
        raise lifecycle_error

    if not outcome.released:
        record_ai_release_decision(
            program_id=program_id, ai_run_id=ai_run_id,
            terminal=ReleaseTerminal.REJECTED if outcome.status == FeatureOutcomeStatus.BLOCKED else ReleaseTerminal.DISCARDED,
            reason=replay_refusal_reason or "; ".join(outcome.limitations) or outcome.status.value,
            validator_finding_count=replay_finding_count if replay_refusal_reason else len(outcome.limitations),
            programs_root=programs_root,
        )
        return None

    cache_hit = outcome.tier == Tier.CACHE
    if not cache_hit:
        if not release_written or released_proposal is None:
            raise StateError("Dependency proposal was not recorded as released.")
        return released_proposal
    if verified_release_hash is None:
        raise StateError("Dependency proposal originating release was not verified.")
    if replayed_proposal is None:
        raise StateError("Dependency proposal replay was not validated.")

    record_ai_release_decision(
        program_id=program_id, ai_run_id=ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and dependency blast-radius semantic validation (via StructuredFeatureRunner)",
        validator_finding_count=0, released_content_hash=verified_release_hash, programs_root=programs_root,
    )
    return replayed_proposal


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
        findings.append("evidence_refs cites refs not present on the dependency.")
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
    next_event_raw = raw.get("next_proving_event")
    narrative_raw = raw.get("blast_radius_narrative")
    evidence_refs_raw = raw.get("evidence_refs", [])
    if (
        not isinstance(next_event_raw, str)
        or not isinstance(narrative_raw, str)
        or not isinstance(evidence_refs_raw, list)
        or not all(isinstance(ref, str) for ref in evidence_refs_raw)
    ):
        return None

    try:
        next_event_raw = next_event_raw.strip()
        next_proving_event = process_generated_text(next_event_raw).text if next_event_raw else ""
        narrative_raw = narrative_raw.strip()
        blast_radius_narrative = process_generated_text(narrative_raw).text if narrative_raw else ""
    except AIPipelineError:
        return None

    if not next_proving_event or not blast_radius_narrative:
        return None

    return DependencyBlastRadiusProposal(
        id=f"blast-radius-{ai_run_id}",
        program_id=request.program_id,
        dependency_id=request.dependency_id,
        next_proving_event=next_proving_event,
        blast_radius_narrative=blast_radius_narrative,
        evidence_refs=tuple(evidence_refs_raw),
        ai_run_id=ai_run_id,
    )


def _resolve_dependency_cache_deployment(client: LLMProvider) -> str:
    if isinstance(client, FallbackAIClient):
        chain = canonical_json({
            "version": "fallback-chain.v1",
            "deployments": client.configured_deployments,
        })
        return f"fallback-chain.v1:{canonical_input_hash(chain)}"
    return _resolve_model_deployment(client)


def _resolve_model_deployment(client: LLMProvider) -> str:
    """Return a client-provided deployment label, or ``unknown`` if absent.

    An absent or inaccurate label can cause distinct clients to share a key.
    """
    for attr in ("deployment", "primary_deployment", "model", "deployment_id"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _cached_response(
    key: AIResultCacheKey, *, request: DependencyBlastRadiusRequest, programs_root: Path
) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root, strict_read=True)
    if hit is None:
        return None
    envelope = hit.value
    if not isinstance(envelope, dict) or set(envelope) != {"release_run_id", "proposal"}:
        raise StateError("Dependency proposal cache envelope is invalid.")
    release_run_id = envelope["release_run_id"]
    content = envelope["proposal"]
    if (
        not isinstance(release_run_id, str)
        or not release_run_id
        or not isinstance(content, dict)
        or set(content) != {
            "program_id", "dependency_id", "request_hash", "next_proving_event",
            "blast_radius_narrative", "evidence_refs",
        }
        or content["program_id"] != request.program_id
        or content["dependency_id"] != request.dependency_id
        or content["request_hash"] != key.canonical_input_hash
        or not isinstance(content["next_proving_event"], str)
        or not isinstance(content["blast_radius_narrative"], str)
        or not isinstance(content["evidence_refs"], list)
        or not all(isinstance(ref, str) for ref in content["evidence_refs"])
    ):
        raise StateError("Dependency proposal cache content is invalid.")
    try:
        validate_bounded_payload(content)
    except SchemaGatewayError:
        raise StateError("Dependency proposal cache content exceeds its bounds.") from None
    if not released_content_matches(
        release_run_id, program_id=request.program_id,
        released_content_hash=canonical_input_hash(canonical_json(content)), programs_root=programs_root,
    ):
        raise StateError("Dependency proposal cache lacks a matching release.")
    return {
        "next_proving_event": content["next_proving_event"],
        "blast_radius_narrative": content["blast_radius_narrative"],
        "evidence_refs": content["evidence_refs"],
    }


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
