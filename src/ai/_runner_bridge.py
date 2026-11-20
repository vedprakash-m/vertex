"""Shared StructuredFeatureRunner adapter for Wave-2 production AI migrations (RVX-D09).

Centralizes pinned RunnerContext construction, QG-29 lifecycle/release audit,
DeterministicContextCompiler assembly, ReleasedResultCache verification, and
error-type preservation so each migrated feature supplies only its prompt,
bounds payload, provider call site, and semantic parser/serializer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, TypeVar

from src.ai.client import NON_RETRYABLE_AI_ERRORS
from src.ai.structured_feature_runner import (
    CacheIdentity,
    CallCeilingLedger,
    ContextBuildResult,
    DeterministicResultCache,
    FeatureOutcome,
    FeatureOutcomeStatus,
    ReleasedResultCache,
    RunnerContext,
    StructuredFeatureRunner,
    StructuredFeatureSpec,
    ValidationResult,
    call_ceiling_ledger_path,
    deterministic_result_cache_path,
    released_result_cache_path,
)
from src.ai.tiered_router import Tier
from src.core.ai_result_cache import canonical_input_hash
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.context_compiler import (
    CompiledContext,
    ContentOrigin,
    ContextCompileRequest,
    DeterministicContextCompiler,
    EvidenceSpan,
    ValidationContext,
)
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import ConfigError, StateError
from src.core.ledger.event_log import canonical_json, read_events
from src.core.operator_identity import capture_operator_identity
from src.core.policy_loader import load_ai_feature_policy
from src.core.program_fact_store import ProgramFactStore, _resolve_fact_db_root
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
    released_content_matches,
)

ResultT = TypeVar("ResultT")


def build_pinned_runner_context(
    program_id: str,
    *,
    programs_root: Path,
    caller: str,
    feature: str,
    policy_version: str,
    classification: str = "confidential",
) -> RunnerContext:
    """Bind native OS operator identity and pinned ProgramFactStore snapshot."""
    try:
        identity = capture_operator_identity(caller, require_os_principal=True)
    except (OSError, ValueError):
        raise StateError(
            f"A native OS principal is required for {feature} runner execution."
        ) from None
    if identity.principal is None or not identity.principal.strip():
        raise StateError(
            f"A native OS principal is required for {feature} runner execution."
        )
    store = ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root))
    try:
        pin = store.pin_snapshot(metadata={"caller": caller, "feature": feature})
        drift = store.detect_drift(pin.snapshot_id)
    except (ConfigError, KeyError, OSError, sqlite3.DatabaseError, StateError, ValueError):
        raise StateError(
            f"The program fact snapshot could not be pinned for {feature} runner execution."
        ) from None
    digest = pin.content_sha256
    if (
        pin.program_id != program_id
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise StateError("The pinned program fact snapshot does not match the selected program.")
    if drift:
        raise StateError(f"The pinned program fact snapshot changed before {feature} execution.")
    return RunnerContext(
        program_id=program_id,
        run_id=new_ai_run_id(),
        policy_version=policy_version,
        snapshot_id=digest,
        principal=identity.principal,
        classification=classification,
    )


def run_structured_feature_via_runner(
    *,
    feature: str,
    task_class: str,
    context: RunnerContext,
    program_id: str | None,
    prompt_version: str,
    policy_version: str,
    schema_version: str = "1",
    programs_root: Path = PROGRAMS_ROOT,
    system_prompt: str,
    user_prompt: str,
    output_schema_text: str,
    request_payload: dict[str, Any],
    deployment_fingerprint: str,
    validator_version: str,
    invoke_provider: Callable[[CompiledContext], Any],
    parse_and_validate: Callable[[dict[str, Any]], ResultT | None],
    serialize_result: Callable[[dict[str, Any], ResultT], dict[str, Any]],
    error_factory: Callable[[str], Exception],
    raise_on_rejection: bool = True,
    raise_on_empty_result: bool = False,
    empty_value: ResultT | None = None,
    nonretryable_error_map: Callable[[BaseException], Exception] | None = None,
    provider_error_map: Callable[[Exception], Exception | None] | None = None,
    max_input_tokens: int = 6000,
    max_remote_calls_per_run: int | None = None,
    max_frontier_calls_per_run: int | None = None,
) -> ResultT | None:
    """Execute one structured AI feature call through StructuredFeatureRunner."""
    audit_program_id = program_id.strip() if isinstance(program_id, str) and program_id.strip() else None
    if audit_program_id is not None and context.program_id != audit_program_id:
        raise StateError(f"Runner context program must match the {feature} request.")
    if context.policy_version != policy_version:
        raise StateError(f"Runner context policy version must match {feature}.")

    if audit_program_id is not None:
        try:
            read_events(audit_program_id, programs_root=programs_root, repair_corruption=False)
        except (ConfigError, OSError, UnicodeError, ValueError, RecursionError):
            raise StateError(f"{feature} release audit could not be verified.") from None

    ai_run_id = new_ai_run_id()
    policy = load_ai_feature_policy(feature)
    release_schema = f"{feature}.released_cache.v{schema_version}"

    lifecycle_error: Exception | None = None
    nonretryable_error: BaseException | None = None
    provider_error: Exception | None = None
    schema_error: Exception | None = None
    semantic_error: Exception | None = None
    empty_result_returned = False
    release_write_pending = False
    release_written = False
    released_value: ResultT | None = None
    origin_read_pending = False
    verified_release_hash: str | None = None
    provider_called = False
    semantic_stage_recorded = False
    replayed_value: ResultT | None = None
    replay_refusal_reason: str | None = None

    def _lifecycle(state: AIRunState) -> None:
        nonlocal lifecycle_error
        if audit_program_id is None:
            return
        try:
            record_ai_run_lifecycle(
                program_id=audit_program_id,
                ai_run_id=ai_run_id,
                feature=feature,
                state=state,
                prompt_version=prompt_version,
                policy_version=policy_version,
                programs_root=programs_root,
            )
        except Exception as error:
            lifecycle_error = error
            raise

    def _decision(
        terminal: ReleaseTerminal,
        reason: str,
        *,
        finding_count: int = 0,
        released_content_hash: str | None = None,
    ) -> None:
        if audit_program_id is None:
            return
        record_ai_release_decision(
            program_id=audit_program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            released_content_hash=released_content_hash,
            programs_root=programs_root,
        )

    _lifecycle(AIRunState.PLANNED)
    try:
        validate_bounded_payload(request_payload)
    except SchemaGatewayError as error:
        reason = f"AISchemaGateway rejected the outbound request: {error}"
        _decision(ReleaseTerminal.DISCARDED, reason)
        if raise_on_rejection:
            raise error_factory(reason) from error
        return empty_value
    _lifecycle(AIRunState.REQUESTED)

    compiled_program_id = audit_program_id or context.program_id

    def build_context(_req: dict[str, Any]) -> ContextBuildResult:
        request_span = EvidenceSpan(
            evidence_id=f"{feature}-request:{compiled_program_id}",
            source_family=f"{feature}_request",
            text=user_prompt,
            required=True,
            origin=ContentOrigin.AUTHORED,
            trust_level="high",
            verification_state="operator_supplied",
            injection_screen="pass",
        )
        compiled = DeterministicContextCompiler(programs_root=programs_root).compile(
            ContextCompileRequest(
                program_id=compiled_program_id,
                edition_id=None,
                feature=feature,
                prompt_version=prompt_version,
                system_instructions=system_prompt,
                output_schema_text=output_schema_text,
                required_evidence=(request_span,),
                optional_evidence=(),
                max_input_tokens=max_input_tokens,
                reserved_output_tokens=policy.max_tokens or 700,
            ),
            validation_context=ValidationContext(
                program_id=compiled_program_id,
                run_id=context.run_id,
                execution_mode="observe",
                classification=context.classification,
            ),
        )
        return ContextBuildResult(compiled=compiled)

    def call_provider(_req: dict[str, Any], compiled: CompiledContext) -> dict[str, Any]:
        nonlocal nonretryable_error, provider_error, schema_error, provider_called
        provider_called = True
        try:
            raw = invoke_provider(compiled)
        except NON_RETRYABLE_AI_ERRORS as error:
            nonretryable_error = error
            raise
        except Exception as error:
            provider_error = error
            raise
        _lifecycle(AIRunState.RESPONDED)
        if not isinstance(raw, dict):
            schema_error = error_factory(f"{feature} response payload must be an object.")
            raise schema_error
        try:
            validate_bounded_payload(raw)
        except SchemaGatewayError as error:
            schema_error = error_factory(f"AISchemaGateway rejected the response: {error}")
            raise
        _lifecycle(AIRunState.SCHEMA_VALIDATED)
        return raw

    def semantic_validator(_req: dict[str, Any], raw: dict[str, Any]) -> ValidationResult:
        nonlocal semantic_stage_recorded, replayed_value, replay_refusal_reason
        nonlocal semantic_error, empty_result_returned
        cache_hit = (not provider_called) and (verified_release_hash is not None)
        revalidating_payload = semantic_stage_recorded
        if cache_hit:
            _lifecycle(AIRunState.RESPONDED)
            try:
                validate_bounded_payload(raw)
            except SchemaGatewayError:
                replay_refusal_reason = "released payload failed structural validation."
                raise StateError(f"Released {feature} payload failed structural validation.") from None
            _lifecycle(AIRunState.SCHEMA_VALIDATED)
        try:
            validated = parse_and_validate(raw)
        except Exception as error:
            if not semantic_stage_recorded:
                _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
                semantic_stage_recorded = True
            if cache_hit:
                replay_refusal_reason = str(error)
                raise StateError(f"Released {feature} payload failed semantic validation.") from error
            if revalidating_payload:
                raise StateError(f"Serialized {feature} payload failed release validation.") from error
            semantic_error = error
            return ValidationResult(valid=False, reason=str(error))
        if not semantic_stage_recorded:
            _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
            semantic_stage_recorded = True
        if validated is None:
            if raise_on_empty_result:
                if cache_hit or revalidating_payload:
                    raise StateError(f"Serialized {feature} produced empty output.")
            empty_result_returned = True
            if audit_program_id is not None and not raise_on_rejection:
                # For lenient features, an empty parsed list/result can still be legitimately released/empty.
                if revalidating_payload or cache_hit:
                    replayed_value = None
                return ValidationResult(valid=True, evidence_refs=())
            return ValidationResult(valid=False, reason=f"{feature} validation produced empty output.")
        if cache_hit or (audit_program_id is None and revalidating_payload):
            replayed_value = validated
        if audit_program_id is None and not release_written:
            replayed_value = validated
        return ValidationResult(valid=True, evidence_refs=())

    def deserialize_response(text: str) -> dict[str, Any]:
        response = json.loads(text)
        if not isinstance(response, dict):
            raise StateError(f"{feature} cached response must be an object.")
        return response

    def serialize_response(raw: dict[str, Any]) -> str:
        validated = parse_and_validate(raw)
        if validated is None:
            if raise_on_empty_result or raise_on_rejection:
                raise StateError(f"{feature} could not be serialized safely.")
            return json.dumps(raw, sort_keys=True)
        return json.dumps(serialize_result(raw, validated), sort_keys=True)

    def release_hash(identity: CacheIdentity, payload: str) -> str:
        return canonical_input_hash(
            canonical_json({
                "schema": release_schema,
                "cache_key": identity.cache_key(),
                "payload": payload,
            })
        )

    def record_release(identity: CacheIdentity, payload: str) -> str:
        nonlocal release_write_pending, release_written, released_value
        response = deserialize_response(payload)
        validate_bounded_payload(response)
        validated = parse_and_validate(response)
        content_hash = release_hash(identity, payload)
        release_write_pending = True
        _decision(
            ReleaseTerminal.RELEASED,
            f"passed AISchemaGateway bounds and {feature} semantic validation (via StructuredFeatureRunner)",
            released_content_hash=content_hash,
        )
        release_written = True
        release_write_pending = False
        released_value = validated
        return ai_run_id

    def verify_release(identity: CacheIdentity, payload: str, origin_run_id: str) -> bool:
        nonlocal origin_read_pending, verified_release_hash
        assert audit_program_id is not None
        origin_read_pending = True
        content_hash = release_hash(identity, payload)
        matches = released_content_matches(
            origin_run_id,
            program_id=audit_program_id,
            released_content_hash=content_hash,
            programs_root=programs_root,
        )
        origin_read_pending = False
        if matches is True:
            verified_release_hash = content_hash
        return matches

    def fallback_handler(_req: dict[str, Any]) -> FeatureOutcome[dict[str, Any]]:
        return FeatureOutcome(status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE)

    storage_program_id = context.program_id
    released_cache = (
        ReleasedResultCache(
            released_result_cache_path(storage_program_id, programs_root=programs_root),
            record_release=record_release,
            verify_release=verify_release,
        )
        if audit_program_id is not None
        else ReleasedResultCache(
            released_result_cache_path(storage_program_id, programs_root=programs_root),
        )
    )
    runner = StructuredFeatureRunner(
        context=context,
        frontier_eligible=policy.frontier_eligible,
        ceiling_ledger=CallCeilingLedger(
            call_ceiling_ledger_path(storage_program_id, programs_root=programs_root)
        ),
        released_cache=released_cache,
        deterministic_cache=DeterministicResultCache(
            deterministic_result_cache_path(storage_program_id, programs_root=programs_root)
        ),
    )
    spec = StructuredFeatureSpec(
        feature=feature,
        task_class=task_class,
        prompt_version=prompt_version,
        schema_version=schema_version,
        response_type=dict,
        deterministic_handler=None,
        local_router_name=None,
        fallback_handler=fallback_handler,
        semantic_validator=semantic_validator,
        build_context=build_context,
        call_provider=call_provider,
        request_hash=lambda _req: canonical_input_hash(user_prompt),
        deployment_fingerprint=deployment_fingerprint,
        validator_version=validator_version,
        serialize_response=serialize_response if audit_program_id is not None else None,
        deserialize_response=deserialize_response if audit_program_id is not None else None,
        max_remote_calls_per_run=max_remote_calls_per_run,
        max_frontier_calls_per_run=max_frontier_calls_per_run,
    )

    try:
        outcome = runner.run(spec, request_payload)
    except Exception:
        if lifecycle_error is not None:
            raise lifecycle_error
        if origin_read_pending:
            raise StateError(f"{feature} release audit could not be verified.") from None
        if release_write_pending:
            raise
        if release_written:
            raise StateError(f"{feature} cache write failed after release.") from None
        raise

    if lifecycle_error is not None:
        raise lifecycle_error
    if nonretryable_error is not None:
        _decision(ReleaseTerminal.DISCARDED, f"AI cost or preflight failure prevented {feature} execution.")
        if nonretryable_error_map is not None:
            raise nonretryable_error_map(nonretryable_error) from nonretryable_error
        raise nonretryable_error
    if schema_error is not None:
        _decision(
            ReleaseTerminal.REJECTED if "must be an object" not in str(schema_error) else ReleaseTerminal.DISCARDED,
            str(schema_error),
        )
        if raise_on_rejection:
            raise schema_error
        return empty_value
    if provider_error is not None:
        _decision(ReleaseTerminal.DISCARDED, f"provider call failed: {provider_error}")
        if provider_error_map is not None:
            mapped = provider_error_map(provider_error)
            if mapped is None:
                return empty_value
            raise mapped from provider_error
        raise provider_error
    if semantic_error is not None:
        _decision(ReleaseTerminal.REJECTED, str(semantic_error))
        if raise_on_rejection:
            raise semantic_error
        return empty_value
    if empty_result_returned and raise_on_rejection:
        _decision(ReleaseTerminal.REJECTED, f"{feature} produced empty output.")
        return empty_value
    if not outcome.released:
        _decision(
            ReleaseTerminal.REJECTED if outcome.status == FeatureOutcomeStatus.BLOCKED else ReleaseTerminal.DISCARDED,
            replay_refusal_reason or "; ".join(outcome.limitations) or outcome.status.value,
            finding_count=len(outcome.limitations),
        )
        return empty_value
    if audit_program_id is None:
        return replayed_value
    if outcome.tier == Tier.CACHE:
        if verified_release_hash is None:
            raise StateError(f"{feature} replay release state was not verified.")
        _decision(
            ReleaseTerminal.RELEASED,
            f"passed AISchemaGateway bounds and {feature} semantic validation (via StructuredFeatureRunner)",
            released_content_hash=verified_release_hash,
        )
        return replayed_value
    if not release_written:
        raise StateError(f"{feature} was not recorded as released.")
    return released_value
