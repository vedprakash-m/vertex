from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, cast

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.anticipation_detector import detect_anticipated_questions
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.models import Confidence
from src.core.models_v2 import Dependency, LeadershipReader, LegacyDependency, Signal
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.trajectory_analyzer import DriftPattern
from src.core.policy_loader import load_ai_feature_policy
from src.core.view_models import WorkstreamData


logger = logging.getLogger(__name__)

PROMPT_VERSION = "anticipation_question.v1"
from src.ai.prompt_registry import load_prompt
POLICY_VERSION = "anticipation_question.v1"
_FEATURE = "anticipation_engine"
_OUTPUT_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class AnticipatedQuestion:
    reader: str
    question: str
    evidence: tuple[str, ...]
    suggested_response: str
    confidence: Confidence


def anticipate_questions(
    *,
    readers: tuple[LeadershipReader, ...],
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    summaries: dict[str, str],
    workstreams: tuple[WorkstreamData, ...] = (),
    dependencies: tuple[Dependency | LegacyDependency, ...] = (),
    client: LLMProvider | None = None,
    max_questions: int = 5,
    program_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AnticipatedQuestion, ...]:
    """``program_id`` is optional because one of this feature's two call
    sites (``review_full.py``) can legitimately run without a resolved
    program. When it is provided, each AI-rendered question runs the same
    AISchemaGateway/QG-29 lifecycle established by
    ``risk_proposal_generator.py`` and ``meeting_action_extractor.py``
    (ADF-W5.1, P7); when it is absent, the AI call still runs with its
    existing graceful deterministic-fallback behavior, just without a
    durable audit trail (there is no program to attribute the trail to)."""
    valid_reader_names = {reader.name for reader in readers}
    findings = detect_anticipated_questions(
        readers=readers,
        workstreams=workstreams,
        drift_patterns=drift_patterns,
        approved_signals=signals,
        summaries=summaries,
        dependencies=dependencies,  # type: ignore[arg-type]
        max_questions=max_questions,
    )
    if not findings:
        return ()

    anticipated: list[AnticipatedQuestion] = []
    prompt_template = _load_prompt_template() if client is not None else None
    for finding in findings[:max_questions]:
        if finding.reader not in valid_reader_names:
            raise ValueError(f"Anticipation finding reader is not in provided readers: {finding.reader}")
        question_text = finding.question_seed
        response_text = finding.suggested_response_seed
        if client is not None and prompt_template is not None:
            rendered = _generate_with_ai(
                client=client,
                prompt_template=prompt_template,
                reader=finding.reader,
                question_seed=finding.question_seed,
                response_seed=finding.suggested_response_seed,
                evidence=finding.evidence,
                program_id=program_id,
                programs_root=programs_root,
            )
            if rendered is not None:
                question_text, response_text = rendered
        anticipated.append(
            AnticipatedQuestion(
                reader=finding.reader,
                question=question_text,
                evidence=finding.evidence,
                suggested_response=response_text,
                confidence=finding.confidence,
            )
        )
    return tuple(anticipated)


def _generate_with_ai(
    *,
    client: LLMProvider,
    prompt_template: str,
    reader: str,
    question_seed: str,
    response_seed: str,
    evidence: tuple[str, ...],
    program_id: str | None,
    programs_root: Path,
) -> tuple[str, str] | None:
    """Renders one AI question/response pair. Never raises -- any failure
    (bounds rejection, provider error, malformed/ungrounded response) logs
    a warning and returns ``None`` so the caller falls back to the
    deterministic seed text. When ``program_id`` is available, every
    attempt is recorded through the same AISchemaGateway/QG-29 lifecycle
    as this codebase's other migrated AI features; a durable ``RELEASED``
    decision is the only path a caller may treat the AI-rendered pair as
    trustworthy, matching Section 8.9.4."""
    user_prompt = prompt_template.format(
        reader=reader,
        question_seed=question_seed,
        response_seed=response_seed,
        evidence="; ".join(evidence),
    )

    if program_id is None:
        # specs/backlog.md BL-C2: no program to attribute a durable
        # ai_release_audit trail to (review_full.py can legitimately run
        # without a resolved program), so QG-29's lifecycle/terminal
        # recording genuinely does not apply here -- but AISchemaGateway's
        # bounds check and the same semantic validator the program_id
        # branch below uses are both program-independent (Zone-A pure
        # functions) and were previously skipped in this branch only,
        # not because they didn't apply. Now applied here too, so the two
        # call sites differ only in audit-trail durability, not in
        # validation rigor.
        request_payload = {
            "reader": reader,
            "question_seed": question_seed,
            "response_seed": response_seed,
            "evidence": list(evidence),
        }
        try:
            validate_bounded_payload(request_payload)
        except SchemaGatewayError as error:
            logger.warning("AI anticipation request rejected by AISchemaGateway; using deterministic fallback: %s", error)
            return None

        try:
            raw = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    "You generate concise leadership anticipation questions as strict JSON.",
                    user_prompt,
                    parser=lambda payload: payload,
                    max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            ).value
        except (AIPipelineError, ValueError, TypeError, AIClientError) as error:
            logger.warning("AI anticipation formatting failed; using deterministic fallback: %s", error)
            return None
        except Exception as error:
            logger.warning("AI anticipation call failed; using deterministic fallback: %s", error)
            return None

        if raw is None:
            return None
        if not isinstance(raw, dict):
            logger.warning("AI anticipation response was not a structured object; using deterministic fallback.")
            return None
        try:
            validate_bounded_payload(raw)
        except SchemaGatewayError as error:
            logger.warning("AI anticipation response rejected by AISchemaGateway; using deterministic fallback: %s", error)
            return None
        try:
            question_text, response_text = _parse_ai_rendered_question(raw)
        except ValueError as error:
            logger.warning("AI anticipation response malformed; using deterministic fallback: %s", error)
            return None
        findings = _validate_semantics(question_text, response_text)
        if findings:
            logger.warning(
                "AI anticipation response failed semantic validation; using deterministic fallback: %s",
                "; ".join(findings),
            )
            return None
        return _validated_rendered_pair((question_text, response_text))

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

    _lifecycle(AIRunState.PLANNED)

    request_payload = {
        "reader": reader,
        "question_seed": question_seed,
        "response_seed": response_seed,
        "evidence": list(evidence),
    }
    try:
        validate_bounded_payload(request_payload)
    except SchemaGatewayError as error:
        _discard(ReleaseTerminal.DISCARDED, f"AISchemaGateway rejected the outbound request: {error}")
        logger.warning("AI anticipation request rejected by AISchemaGateway; using deterministic fallback: %s", error)
        return None

    _lifecycle(AIRunState.REQUESTED)

    policy = load_ai_feature_policy(_FEATURE)
    cache_key = AIResultCacheKey(
        program_id=program_id,
        feature=_FEATURE,
        canonical_input_hash=canonical_input_hash(user_prompt),
        prompt_version=PROMPT_VERSION,
        policy_version=POLICY_VERSION,
        model_deployment=_resolve_model_deployment(client),
        context_manifest_hash=canonical_input_hash("|".join(evidence)),
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
    )
    try:
        raw = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                "You generate concise leadership anticipation questions as strict JSON.",
                user_prompt,
                parser=lambda payload: payload,
                max_tokens=policy.max_tokens,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value
    except (AIPipelineError, ValueError, TypeError, AIClientError) as error:
        _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")
        logger.warning("AI anticipation formatting failed; using deterministic fallback: %s", error)
        return None
    except Exception as error:
        _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")
        logger.warning("AI anticipation call failed; using deterministic fallback: %s", error)
        return None

    _lifecycle(AIRunState.RESPONDED)

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")
        logger.warning("AI anticipation response rejected by AISchemaGateway; using deterministic fallback: %s", error)
        return None

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    try:
        question_text, response_text = _parse_ai_rendered_question(cast(dict[str, object], raw))
    except ValueError as error:
        _discard(ReleaseTerminal.REJECTED, str(error))
        logger.warning("AI anticipation response malformed; using deterministic fallback: %s", error)
        return None

    findings = _validate_semantics(question_text, response_text)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    if findings:
        _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))
        logger.warning("AI anticipation response failed semantic validation; using deterministic fallback: %s", "; ".join(findings))
        return None

    _discard(
        ReleaseTerminal.RELEASED,
        "passed AISchemaGateway bounds and anticipation-question semantic validation",
    )
    return question_text, response_text


def _validated_rendered_pair(rendered: tuple[str, str] | None) -> tuple[str, str] | None:
    if rendered is None:
        return None
    question_text, response_text = rendered
    if not question_text or not response_text:
        return None
    return question_text, response_text


def _validate_semantics(question_text: str, response_text: str) -> tuple[str, ...]:
    """AISchemaGateway's per-feature ``SemanticValidator`` (Section 8.9.3)
    for anticipation questions: a rendered "question" that lost its
    question-mark, or an empty response after sanitization, is a finding
    the QG-29 release decision must see rather than a silent pass-through."""
    findings: list[str] = []
    if not question_text.strip():
        findings.append("Anticipation question text must be non-empty.")
    elif not question_text.strip().endswith("?"):
        findings.append("Anticipation question text does not read as a question.")
    if not response_text.strip():
        findings.append("Anticipation suggested_response text must be non-empty.")
    return tuple(findings)


def _resolve_model_deployment(client: LLMProvider) -> str:
    """Best-effort model/deployment identifier for the cache key (Section
    8.8.3), same degrade-to-sentinel discipline as
    ``risk_proposal_generator.py``/``meeting_action_extractor.py``: a
    wrong/missing deployment id only under-shares the cache (never
    over-shares, since program_id/feature/input hash are still
    exact-matched), so this never raises."""
    for attr in ("deployment", "primary_deployment", "model", "deployment_id"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None


def _parse_ai_rendered_question(payload: dict[str, object]) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Anticipation payload must be an object.")
    question_value = payload.get("question")
    if not isinstance(question_value, str):
        raise ValueError("Anticipation payload must include question as a string.")
    response_value = payload.get("suggested_response")
    if not isinstance(response_value, str):
        raise ValueError("Anticipation payload must include suggested_response as a string.")
    question_text = _sanitize(question_value)
    response_text = _sanitize(response_value)
    if not question_text:
        raise ValueError("Anticipation payload question must be non-empty.")
    if not response_text:
        raise ValueError("Anticipation payload suggested_response must be non-empty.")
    return question_text, response_text


def _sanitize(text: str) -> str:
    processed = process_generated_text(text)
    return processed.text.strip()


def _load_prompt_template() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=ValueError)