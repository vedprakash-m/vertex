from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, cast

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import NON_RETRYABLE_AI_ERRORS
from src.ai.prompt_registry import load_prompt
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
from src.core.ban_list_validator import find_ban_list_violations
from src.core.config_loader import EditorialRules, NarrativeProgramContext
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
from src.core.models import Confidence, DeltaKind, EditionType, ItemDelta, RiskLevel, WorkItem
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
    released_content_matches,
)
from src.core.verbosity_enforcer import enforce_verbosity
from src.core.voice_validator import build_writing_contract_prompt_lines


PROMPT_VERSION = "exec_summary_drafter.v1"
POLICY_VERSION = "exec_summary_drafter.v1"
_FEATURE = "exec_summary_drafter"
_OUTPUT_SCHEMA_VERSION = "1"
_CACHE_IDENTITY_VERSION = "exec_summary_drafter.released.v1"
_PILOT_CACHE_RELEASE_SCHEMA = "exec_summary_drafter.released_cache.v1"
_DEFAULT_EXEC_SUMMARY_MAX_WORDS = 150


class ExecSummaryDraftError(Exception):
    """Raised when a generated exec summary violates the editorial contract."""


@dataclass(frozen=True, slots=True)
class ExecSummaryDraft:
    text: str
    prompt_version: str
    cited_work_item_ids: tuple[int, ...]
    ai_confidence: Confidence


@dataclass(frozen=True, slots=True)
class _RankedChange:
    work_item_id: int
    priority: int
    label: str
    summary: str
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class _PreparedExecSummaryInputs:
    ranked_changes: tuple[_RankedChange, ...]
    allowed_items: tuple[WorkItem, ...]
    system_prompt: str
    user_prompt: str
    request_payload: dict[str, Any]


def _prepare_exec_summary_inputs(
    *,
    items: tuple[WorkItem, ...] | list[WorkItem],
    deltas: Any,
    editorial_rules: EditorialRules,
    program_id: str,
    edition_type: EditionType | None,
    program_context: NarrativeProgramContext | None,
    supplemental_context: tuple[str, ...],
) -> _PreparedExecSummaryInputs | None:
    ranked_changes = _rank_changes(tuple(items), deltas)
    if not ranked_changes:
        return None
    items_by_id = {item.id: item for item in items}
    missing_item_ids = tuple(
        change.work_item_id for change in ranked_changes if change.work_item_id not in items_by_id
    )
    if missing_item_ids:
        raise ExecSummaryDraftError(
            "Exec summary ranked changes missing work item context: "
            + ", ".join(str(item_id) for item_id in dict.fromkeys(missing_item_ids))
        )
    allowed_items = tuple(items_by_id[change.work_item_id] for change in ranked_changes)
    system_prompt = _load_prompt_template()
    max_words = editorial_rules.verbosity.exec_summary_max_words_for(
        edition_type,
        default=_DEFAULT_EXEC_SUMMARY_MAX_WORDS,
    ) or _DEFAULT_EXEC_SUMMARY_MAX_WORDS
    user_prompt = _build_user_prompt(
        ranked_changes=ranked_changes,
        editorial_rules=editorial_rules,
        program_context=program_context,
        supplemental_context=supplemental_context,
        max_words=max_words,
    )
    request_payload = {
        "program_id": program_id,
        "ranked_change_summaries": [change.summary for change in ranked_changes],
        "supplemental_context": list(supplemental_context),
        "max_words": max_words,
    }
    return _PreparedExecSummaryInputs(
        ranked_changes=ranked_changes,
        allowed_items=allowed_items,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        request_payload=request_payload,
    )


def _validate_and_build_draft(
    raw: Any,
    *,
    allowed_items: tuple[WorkItem, ...],
    ranked_changes: tuple[_RankedChange, ...],
    editorial_rules: EditorialRules,
    edition_type: EditionType | None,
) -> ExecSummaryDraft | None:
    raw_text = _parse_generated_exec_summary_text(cast(dict[str, object], raw))
    if not raw_text:
        return None
    try:
        grounded = process_generated_text(raw_text, allowed_items=allowed_items)
    except AIPipelineError as error:
        raise ExecSummaryDraftError(str(error)) from error
    if not grounded.text:
        return None
    _validate_editorial_rules(grounded.text, editorial_rules, edition_type=edition_type)
    return ExecSummaryDraft(
        text=grounded.text,
        prompt_version=PROMPT_VERSION,
        cited_work_item_ids=grounded.cited_work_item_ids,
        ai_confidence=_derive_ai_confidence(ranked_changes, grounded.cited_work_item_ids),
    )


def draft_exec_summary(
    *,
    client: LLMProvider,
    items: tuple[WorkItem, ...] | list[WorkItem],
    deltas: Any,
    editorial_rules: EditorialRules,
    program_id: str,
    edition_type: EditionType | None = None,
    program_context: NarrativeProgramContext | None = None,
    supplemental_context: tuple[str, ...] = (),
    programs_root: Path = PROGRAMS_ROOT,
) -> ExecSummaryDraft | None:
    """Runs the same AISchemaGateway/QG-29 safety lifecycle established by
    ``risk_proposal_generator.py`` (ADF-W5.1, P7): bounds-checked
    request/response payloads, the five ``AIRunState`` transitions, cache
    wiring via ``AIResultCacheKey``, and a durable QG-29 terminal release
    decision recorded before any caller may consume the result. Preserves
    this feature's existing raise-on-rejection contract
    (``ExecSummaryDraftError``) rather than switching to a
    return-``None``-on-rejection convention, since ``report_ai.py``'s
    deployment-fallback loop and ``propose.py`` both already depend on
    ``ExecSummaryDraftError`` propagating -- same reasoning as
    ``summary_generator.py``'s migration."""
    prepared = _prepare_exec_summary_inputs(
        items=items,
        deltas=deltas,
        editorial_rules=editorial_rules,
        program_id=program_id,
        edition_type=edition_type,
        program_context=program_context,
        supplemental_context=supplemental_context,
    )
    if prepared is None:
        return None

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

    def _discard(
        terminal: ReleaseTerminal,
        reason: str,
        finding_count: int = 0,
        *,
        released_content_hash: str | None = None,
    ) -> None:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            released_content_hash=released_content_hash,
            programs_root=programs_root,
        )

    _lifecycle(AIRunState.PLANNED)

    try:
        validate_bounded_payload(prepared.request_payload)
    except SchemaGatewayError as error:
        reason = f"AISchemaGateway rejected the outbound request: {error}"
        _discard(ReleaseTerminal.DISCARDED, reason)
        raise ExecSummaryDraftError(reason) from error

    _lifecycle(AIRunState.REQUESTED)

    policy = load_ai_feature_policy(_FEATURE)
    cache_key = AIResultCacheKey(
        program_id=program_id,
        feature=_FEATURE,
        canonical_input_hash=canonical_input_hash(prepared.user_prompt),
        prompt_version=PROMPT_VERSION,
        policy_version=POLICY_VERSION,
        model_deployment=_resolve_model_deployment(client),
        context_manifest_hash=canonical_input_hash(
            "|".join(str(change.work_item_id) for change in prepared.ranked_changes)
        ),
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
    )
    try:
        raw = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                prepared.system_prompt,
                prepared.user_prompt,
                parser=lambda payload: payload,
                max_tokens=policy.max_tokens,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value
    except NON_RETRYABLE_AI_ERRORS:
        _discard(ReleaseTerminal.DISCARDED, "AI cost or preflight failure prevented exec summary generation.")
        raise

    _lifecycle(AIRunState.RESPONDED)

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        reason = f"AISchemaGateway rejected the response: {error}"
        _discard(ReleaseTerminal.REJECTED, reason)
        raise ExecSummaryDraftError(reason) from error

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    try:
        draft = _validate_and_build_draft(
            raw,
            allowed_items=prepared.allowed_items,
            ranked_changes=prepared.ranked_changes,
            editorial_rules=editorial_rules,
            edition_type=edition_type,
        )
    except ExecSummaryDraftError as error:
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.REJECTED, str(error))
        raise
    if draft is None:
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.REJECTED, "processed text pipeline produced empty output.")
        return None

    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
    _discard(ReleaseTerminal.RELEASED, "passed AISchemaGateway bounds and editorial-rules validation")
    return draft


def draft_exec_summary_via_runner(
    *,
    context: RunnerContext,
    client: LLMProvider,
    items: tuple[WorkItem, ...] | list[WorkItem],
    deltas: Any,
    editorial_rules: EditorialRules,
    program_id: str,
    edition_type: EditionType | None = None,
    program_context: NarrativeProgramContext | None = None,
    supplemental_context: tuple[str, ...] = (),
    programs_root: Path = PROGRAMS_ROOT,
) -> ExecSummaryDraft | None:
    """RVX-D09 runner path: routes ``exec_summary_drafter`` through
    ``StructuredFeatureRunner`` with deterministic bounded-context compilation,
    release-verified SQLite result caching, and durable QG-29 lifecycle/release
    auditing while preserving ``draft_exec_summary``'s external contract."""
    if context.program_id != program_id:
        raise StateError("Runner context program must match the exec summary program.")
    if context.policy_version != POLICY_VERSION:
        raise StateError("Runner context policy version must match the exec summary feature.")

    prepared = _prepare_exec_summary_inputs(
        items=items,
        deltas=deltas,
        editorial_rules=editorial_rules,
        program_id=program_id,
        edition_type=edition_type,
        program_context=program_context,
        supplemental_context=supplemental_context,
    )
    if prepared is None:
        return None

    try:
        read_events(program_id, programs_root=programs_root, repair_corruption=False)
    except (ConfigError, OSError, UnicodeError, ValueError, RecursionError):
        raise StateError("Exec summary release audit could not be verified.") from None

    ai_run_id = new_ai_run_id()
    policy = load_ai_feature_policy(_FEATURE)
    lifecycle_error: Exception | None = None
    nonretryable_error: BaseException | None = None
    provider_error: Exception | None = None
    schema_error: ExecSummaryDraftError | None = None
    semantic_error: ExecSummaryDraftError | None = None
    empty_draft_returned = False
    release_write_pending = False
    release_written = False
    released_draft: ExecSummaryDraft | None = None
    origin_read_pending = False
    verified_release_hash: str | None = None
    provider_called = False
    semantic_stage_recorded = False
    replayed_draft: ExecSummaryDraft | None = None
    replay_refusal_reason: str | None = None

    def _lifecycle(state: AIRunState) -> None:
        nonlocal lifecycle_error
        try:
            record_ai_run_lifecycle(
                program_id=program_id,
                ai_run_id=ai_run_id,
                feature=_FEATURE,
                state=state,
                prompt_version=PROMPT_VERSION,
                policy_version=POLICY_VERSION,
                programs_root=programs_root,
            )
        except Exception as error:
            lifecycle_error = error
            raise

    def _discard(
        terminal: ReleaseTerminal,
        reason: str,
        finding_count: int = 0,
        *,
        released_content_hash: str | None = None,
    ) -> None:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            released_content_hash=released_content_hash,
            programs_root=programs_root,
        )

    _lifecycle(AIRunState.PLANNED)
    try:
        validate_bounded_payload(prepared.request_payload)
    except SchemaGatewayError as error:
        reason = f"AISchemaGateway rejected the outbound request: {error}"
        _discard(ReleaseTerminal.DISCARDED, reason)
        raise ExecSummaryDraftError(reason) from error
    _lifecycle(AIRunState.REQUESTED)

    def build_context(_req: _PreparedExecSummaryInputs) -> ContextBuildResult:
        request_span = EvidenceSpan(
            evidence_id=f"exec-summary-request:{program_id}",
            source_family="exec_summary_request",
            text=prepared.user_prompt,
            required=True,
            origin=ContentOrigin.AUTHORED,
            trust_level="high",
            verification_state="operator_supplied",
            injection_screen="pass",
        )
        compiled = DeterministicContextCompiler(programs_root=programs_root).compile(
            ContextCompileRequest(
                program_id=program_id,
                edition_id=None,
                feature=_FEATURE,
                prompt_version=PROMPT_VERSION,
                system_instructions=prepared.system_prompt,
                output_schema_text='Respond with JSON: {"text": str}. Cite every claim with [#WI].',
                required_evidence=(request_span,),
                optional_evidence=(),
                max_input_tokens=6000,
                reserved_output_tokens=policy.max_tokens or 700,
            ),
            validation_context=ValidationContext(
                program_id=program_id,
                run_id=context.run_id,
                execution_mode="observe",
                classification=context.classification,
            ),
        )
        return ContextBuildResult(compiled=compiled)

    def call_provider(_req: _PreparedExecSummaryInputs, compiled: CompiledContext) -> dict[str, Any]:
        nonlocal nonretryable_error, provider_error, schema_error, provider_called
        provider_called = True
        try:
            raw = client.structured(
                compiled.prompt_text,
                "",
                parser=lambda payload: payload,
                max_tokens=policy.max_tokens,
                prompt_version=PROMPT_VERSION,
            )
        except NON_RETRYABLE_AI_ERRORS as error:
            nonretryable_error = error
            raise
        except Exception as error:
            provider_error = error
            raise
        _lifecycle(AIRunState.RESPONDED)
        if not isinstance(raw, dict):
            schema_error = ExecSummaryDraftError("Generated exec summary payload must be an object.")
            raise schema_error
        try:
            validate_bounded_payload(raw)
        except SchemaGatewayError as error:
            schema_error = ExecSummaryDraftError(f"AISchemaGateway rejected the response: {error}")
            raise
        _lifecycle(AIRunState.SCHEMA_VALIDATED)
        return raw

    def semantic_validator(_req: _PreparedExecSummaryInputs, raw: dict[str, Any]) -> ValidationResult:
        nonlocal semantic_stage_recorded, replayed_draft, replay_refusal_reason, semantic_error, empty_draft_returned
        cache_hit = (not provider_called) and (verified_release_hash is not None)
        revalidating_payload = semantic_stage_recorded
        if cache_hit:
            _lifecycle(AIRunState.RESPONDED)
            try:
                validate_bounded_payload(raw)
            except SchemaGatewayError:
                replay_refusal_reason = "released payload failed structural validation."
                raise StateError("Released exec summary failed structural validation.") from None
            _lifecycle(AIRunState.SCHEMA_VALIDATED)
        try:
            draft = _validate_and_build_draft(
                raw,
                allowed_items=prepared.allowed_items,
                ranked_changes=prepared.ranked_changes,
                editorial_rules=editorial_rules,
                edition_type=edition_type,
            )
        except ExecSummaryDraftError as error:
            if not semantic_stage_recorded:
                _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
                semantic_stage_recorded = True
            if cache_hit:
                replay_refusal_reason = str(error)
                raise StateError("Released exec summary failed semantic validation.") from error
            if revalidating_payload:
                raise StateError("Serialized exec summary failed release validation.") from error
            semantic_error = error
            return ValidationResult(valid=False, reason=str(error))
        if not semantic_stage_recorded:
            _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
            semantic_stage_recorded = True
        if draft is None:
            if cache_hit or revalidating_payload:
                raise StateError("Serialized exec summary produced empty output.")
            empty_draft_returned = True
            return ValidationResult(valid=False, reason="processed text pipeline produced empty output.")
        if cache_hit:
            replayed_draft = draft
        refs = tuple(f"#{item_id}" for item_id in draft.cited_work_item_ids)
        return ValidationResult(valid=True, evidence_refs=refs)

    def deserialize_response(text: str) -> dict[str, Any]:
        response = json.loads(text)
        if not isinstance(response, dict):
            raise StateError("Exec summary cached response must be an object.")
        return response

    def serialize_response(raw: dict[str, Any]) -> str:
        text = _parse_generated_exec_summary_text(cast(dict[str, object], raw))
        draft = _validate_and_build_draft(
            {"text": text},
            allowed_items=prepared.allowed_items,
            ranked_changes=prepared.ranked_changes,
            editorial_rules=editorial_rules,
            edition_type=edition_type,
        )
        if draft is None:
            raise StateError("Exec summary could not be serialized safely.")
        return json.dumps({"text": text}, sort_keys=True)

    def release_hash(identity: CacheIdentity, payload: str) -> str:
        return canonical_input_hash(canonical_json({
            "schema": _PILOT_CACHE_RELEASE_SCHEMA,
            "cache_key": identity.cache_key(),
            "payload": payload,
        }))

    def record_release(identity: CacheIdentity, payload: str) -> str:
        nonlocal release_write_pending, release_written, released_draft
        response = deserialize_response(payload)
        validate_bounded_payload(response)
        draft = _validate_and_build_draft(
            response,
            allowed_items=prepared.allowed_items,
            ranked_changes=prepared.ranked_changes,
            editorial_rules=editorial_rules,
            edition_type=edition_type,
        )
        assert draft is not None
        content_hash = release_hash(identity, payload)
        release_write_pending = True
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=ReleaseTerminal.RELEASED,
            reason="passed AISchemaGateway bounds and editorial-rules validation (via StructuredFeatureRunner)",
            validator_finding_count=0,
            released_content_hash=content_hash,
            programs_root=programs_root,
        )
        release_written = True
        release_write_pending = False
        released_draft = draft
        return ai_run_id

    def verify_release(identity: CacheIdentity, payload: str, origin_run_id: str) -> bool:
        nonlocal origin_read_pending, verified_release_hash
        origin_read_pending = True
        content_hash = release_hash(identity, payload)
        matches = released_content_matches(
            origin_run_id,
            program_id=program_id,
            released_content_hash=content_hash,
            programs_root=programs_root,
        )
        origin_read_pending = False
        if matches is True:
            verified_release_hash = content_hash
        return matches

    def fallback_handler(_req: _PreparedExecSummaryInputs) -> FeatureOutcome[dict[str, Any]]:
        return FeatureOutcome(status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE)

    runner = StructuredFeatureRunner(
        context=context,
        frontier_eligible=policy.frontier_eligible,
        released_cache=ReleasedResultCache(
            _released_cache_path(programs_root, program_id),
            record_release=record_release,
            verify_release=verify_release,
        ),
    )
    spec = StructuredFeatureSpec(
        feature=_FEATURE,
        task_class="synthesis",
        prompt_version=PROMPT_VERSION,
        schema_version=_OUTPUT_SCHEMA_VERSION,
        response_type=dict,
        deterministic_handler=None,
        local_router_name=None,
        fallback_handler=fallback_handler,
        semantic_validator=semantic_validator,
        build_context=build_context,
        call_provider=call_provider,
        request_hash=lambda req: canonical_input_hash(req.user_prompt),
        deployment_fingerprint=_resolve_model_deployment(client),
        validator_version="exec_summary_drafter.v1",
        serialize_response=serialize_response,
        deserialize_response=deserialize_response,
    )

    try:
        outcome = runner.run(spec, prepared)
    except Exception:
        if lifecycle_error is not None:
            raise lifecycle_error
        if origin_read_pending:
            raise StateError("Exec summary release audit could not be verified.") from None
        if release_write_pending:
            raise
        if release_written:
            raise StateError("Exec summary cache write failed after release.") from None
        raise

    if lifecycle_error is not None:
        raise lifecycle_error
    if nonretryable_error is not None:
        _discard(ReleaseTerminal.DISCARDED, "AI cost or preflight failure prevented exec summary generation.")
        raise nonretryable_error
    if schema_error is not None:
        _discard(ReleaseTerminal.REJECTED, str(schema_error))
        raise schema_error
    if provider_error is not None:
        raise provider_error
    if semantic_error is not None:
        _discard(ReleaseTerminal.REJECTED, str(semantic_error))
        raise semantic_error
    if empty_draft_returned:
        _discard(ReleaseTerminal.REJECTED, "processed text pipeline produced empty output.")
        return None
    if not outcome.released:
        _discard(
            ReleaseTerminal.REJECTED if outcome.status == FeatureOutcomeStatus.BLOCKED else ReleaseTerminal.DISCARDED,
            replay_refusal_reason or "; ".join(outcome.limitations) or outcome.status.value,
            len(outcome.limitations),
        )
        return None
    if outcome.tier == Tier.CACHE:
        if verified_release_hash is None or replayed_draft is None:
            raise StateError("Exec summary replay release state was not verified.")
        _discard(
            ReleaseTerminal.RELEASED,
            "passed AISchemaGateway bounds and editorial-rules validation (via StructuredFeatureRunner)",
            released_content_hash=verified_release_hash,
        )
        return replayed_draft
    assert release_written and released_draft is not None
    return released_draft


def _released_cache_path(programs_root: Path, program_id: str) -> Path:
    return programs_root / program_id / "runtime" / "ai_released_result_cache.sqlite3"


def _load_prompt_template() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=ExecSummaryDraftError)


def _build_user_prompt(
    *,
    ranked_changes: tuple[_RankedChange, ...],
    editorial_rules: EditorialRules,
    program_context: NarrativeProgramContext | None,
    supplemental_context: tuple[str, ...],
    max_words: int,
) -> str:
    lines = ["Top ranked candidate changes:"]
    if program_context is not None:
        lines.insert(0, f"Program: {program_context.program_name}")
        objective = getattr(program_context, "objective", None)
        if objective:
            lines.insert(1, f"Objective: {objective}")
        lines[2:2] = list(
            build_writing_contract_prompt_lines(
                program_context,
                editorial_rules=editorial_rules,
            )
        )
    for change in ranked_changes:
        lines.append(f"- priority={change.priority} | {change.label} | {change.summary}")
    if supplemental_context:
        lines.append("Supplemental context:")
        lines.extend(f"- {entry}" for entry in supplemental_context if entry.strip())
    lines.append(f"Write one prose paragraph of at most {max_words} words. Cite every claim with [#WI].")
    return "\n".join(lines)


def _rank_changes(items: tuple[WorkItem, ...], deltas: Any) -> tuple[_RankedChange, ...]:
    items_by_id = {item.id: item for item in items}
    owner_changes = tuple(getattr(deltas, "owner_changes", ()))

    candidates: list[_RankedChange] = []
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=0,
            label="RISK_UP",
            summary=_describe_risk_up(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.risk_changes
        if delta.kind == DeltaKind.RISK_UP
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=1,
            label="NEW_HIGH",
            summary=_describe_new_high(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.new_items
        if delta.new_risk == RiskLevel.HIGH
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=2,
            label="CLOSED_HIGH",
            summary=_describe_closed_high(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.closed_items
        if RiskLevel.HIGH in {delta.old_risk, delta.new_risk}
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=3,
            label="ETA_SLIP",
            summary=_describe_eta_change(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.eta_changes
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=4,
            label="OWNER_CHANGE",
            summary=_describe_owner_change(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in owner_changes
        if delta.kind == DeltaKind.OWNER_CHANGED
    )

    deduped: dict[int, _RankedChange] = {}
    for candidate in sorted(candidates, key=lambda change: (change.priority, change.work_item_id)):
        deduped.setdefault(candidate.work_item_id, candidate)
    return tuple(list(deduped.values())[:3])


def _derive_ai_confidence(ranked_changes: tuple[_RankedChange, ...], cited_work_item_ids: tuple[int, ...]) -> Confidence:
    confidence_by_item = {change.work_item_id: change.confidence for change in ranked_changes}
    missing_item_ids = tuple(item_id for item_id in cited_work_item_ids if item_id not in confidence_by_item)
    if missing_item_ids:
        raise ExecSummaryDraftError(
            "Exec summary cited work items missing ranked-change confidence: "
            + ", ".join(str(item_id) for item_id in missing_item_ids)
        )
    confidences = [confidence_by_item[item_id] for item_id in cited_work_item_ids]
    if not confidences:
        return Confidence.NONE
    return min(confidences, key=_confidence_rank)


def _confidence_rank(confidence: Confidence) -> int:
    return {
        Confidence.NONE: 0,
        Confidence.LOW: 1,
        Confidence.MEDIUM: 2,
        Confidence.HIGH: 3,
    }[confidence]


def _describe_risk_up(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"RISK_UP #{delta.work_item_id} {title} from {delta.old_risk.value if delta.old_risk is not None else 'unknown'} to {delta.new_risk.value if delta.new_risk is not None else 'unknown'}."


def _describe_new_high(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"NEW_HIGH #{delta.work_item_id} {title} entered scope at high risk."


def _describe_closed_high(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"CLOSED_HIGH #{delta.work_item_id} {title} closed from high risk."


def _describe_eta_change(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"ETA_SLIP #{delta.work_item_id} {title} moved from {delta.old_eta.isoformat() if delta.old_eta is not None else 'none'} to {delta.new_eta.isoformat() if delta.new_eta is not None else 'none'}."


def _describe_owner_change(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    previous_owner, current_owner = delta.field_changes.get("assigned_to", (None, None))
    return f"OWNER_CHANGE #{delta.work_item_id} {title} moved from {previous_owner or 'unassigned'} to {current_owner or 'unassigned'}."


def _validate_editorial_rules(
    text: str,
    editorial_rules: EditorialRules,
    *,
    edition_type: EditionType | None,
) -> None:
    if len([paragraph for paragraph in text.strip().splitlines() if paragraph.strip()]) > 1:
        raise ExecSummaryDraftError("Generated exec summary must stay within a single paragraph.")

    violations = find_ban_list_violations({"exec_summary": text}, editorial_rules)
    if violations:
        phrases = ", ".join(sorted({violation.phrase for violation in violations}))
        raise ExecSummaryDraftError(f"Generated exec summary violates the ban-list: {phrases}")

    verbosity_violations = enforce_verbosity(
        workstream_blurbs={},
        exec_summary_text=text,
        scorecard_summaries={},
        subject_line=None,
        verbosity=editorial_rules.verbosity,
        edition_type=edition_type,
    )
    if verbosity_violations:
        messages = "; ".join(violation.message for violation in verbosity_violations)
        raise ExecSummaryDraftError(f"Generated exec summary violates verbosity rules: {messages}")


def _parse_generated_exec_summary_text(payload: dict[str, object]) -> str:
    if not isinstance(payload, dict):
        raise ExecSummaryDraftError("Generated exec summary payload must be an object.")
    text = payload.get("text")
    if not isinstance(text, str):
        raise ExecSummaryDraftError("Generated exec summary payload must include text as a string.")
    normalized = text.strip()
    if not normalized:
        raise ExecSummaryDraftError("Generated exec summary payload text must be non-empty.")
    return normalized


def _resolve_model_deployment(client: LLMProvider) -> str:
    return getattr(client, "deployment", None) or getattr(client, "model", None) or type(client).__name__


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None
