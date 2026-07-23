from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.provider import DisabledStructuredProvider, LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.keyword_topic_router import KeywordM365TopicRouter, M365RoutingDecision
from src.core.m365_router_interface import IM365TopicRouter, M365ReassignCorrection
from src.core.models_v2 import Program, Workstream
from src.core.policy_loader import load_ai_feature_policy, load_m365_routing_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)


PROMPT_VERSION = "m365_topic_router.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "m365_topic_router"
_PROMPT_EXAMPLE_LIMIT = 5
_PROMPT_CORPUS_CANDIDATE_LIMIT = 50
_PROMPT_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9+._-]{1,}")


class M365TopicRouterError(Exception):
    """Raised when the AI-backed M365 topic router cannot be configured."""


@dataclass(frozen=True, slots=True)
class M365TopicRouter(IM365TopicRouter):
    client: LLMProvider
    fallback_router: IM365TopicRouter = field(default_factory=KeywordM365TopicRouter)
    # specs/backlog.md BL-C2: program_id/programs_root for the QG-29
    # release-audit trail -- default "" only exercised by direct-construction
    # tests that don't care about the audit trail; the real `from_program`
    # constructor always sets a real program_id.
    program_id: str = ""
    programs_root: Path = PROGRAMS_ROOT

    @classmethod
    def from_program(
        cls,
        program: Program,
        *,
        trace_context: AITraceContext | None = None,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> "M365TopicRouter":
        if get_ai_mode() == AIMode.DISABLED:
            return cls(client=DisabledStructuredProvider(feature_name="M365TopicRouter"), program_id=program.id, programs_root=programs_root)
        if program.ai is None or not program.ai.enabled:
            raise M365TopicRouterError("AI routing requires program.ai.enabled to be true.")

        deployments = resolve_ai_deployments_for_feature(
            feature_name=_FEATURE,
            primary_candidates=(program.ai.exec_summary_deployment, program.ai.blurb_deployment),
            backup_candidates=(program.ai.exec_summary_backup_deployment, program.ai.blurb_backup_deployment),
            primary_fallback_envs=("VERTEX_EXEC_DEPLOYMENT", "VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
            backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
        )
        if not deployments:
            raise M365TopicRouterError(
                "VERTEX_EXEC_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI to enable AI-backed M365 routing."
            )

        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=program.ai.temperature if program.ai.temperature is not None else load_ai_feature_policy(_FEATURE).temperature,
            budget_usd=program.ai.budget_usd_per_run,
            requests_per_minute=program.ai.requests_per_minute,
            trace_context=trace_context,
        )
        return cls(client=client, program_id=program.id, programs_root=programs_root)

    def route_artifact(
        self,
        *,
        display_name: str | None,
        subject_or_title: str | None,
        participant_aliases: tuple[str, ...],
        sample_text: str | None,
        workstream_profiles: tuple[Workstream, ...],
        recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
        recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
        recent_reassign_corrections: dict[str, tuple[M365ReassignCorrection, ...]] | None = None,
    ) -> M365RoutingDecision:
        fallback_decision = self.fallback_router.route_artifact(
            display_name=display_name,
            subject_or_title=subject_or_title,
            participant_aliases=participant_aliases,
            sample_text=sample_text,
            workstream_profiles=workstream_profiles,
            recent_confirmed_signals=recent_confirmed_signals,
            recent_rejected_signals=recent_rejected_signals,
            recent_reassign_corrections=recent_reassign_corrections,
        )
        if get_ai_mode() == AIMode.DISABLED:
            return fallback_decision
        if not workstream_profiles:
            return fallback_decision
        if _prefer_deterministic_routing(fallback_decision):
            return _with_deterministic_short_circuit_reasoning(fallback_decision)

        try:
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: _run_ai_route(
                    self.client,
                    system_prompt=_load_prompt(),
                    user_prompt=_build_user_prompt(
                        display_name=display_name,
                        subject_or_title=subject_or_title,
                        participant_aliases=participant_aliases,
                        sample_text=sample_text,
                        workstream_profiles=workstream_profiles,
                        recent_confirmed_signals=recent_confirmed_signals,
                        recent_rejected_signals=recent_rejected_signals,
                        recent_reassign_corrections=recent_reassign_corrections,
                        fallback_decision=fallback_decision,
                    ),
                    workstream_profiles=workstream_profiles,
                    program_id=self.program_id,
                    programs_root=self.programs_root,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
            if outcome.value is None:
                return fallback_decision
            return _combine_routing_decisions(fallback_decision=fallback_decision, ai_decision=outcome.value)
        except (AIClientError, M365TopicRouterError, TypeError, ValueError):
            return fallback_decision


def _run_ai_route(
    client: LLMProvider,
    *,
    system_prompt: str,
    user_prompt: str,
    workstream_profiles: tuple[Workstream, ...],
    program_id: str,
    programs_root: Path,
) -> M365RoutingDecision:
    """specs/backlog.md BL-C2: m365_topic_router is production-classified
    (its output feeds `promotion_candidates`/`promotion_blocked` artifact
    building during gather). Bounds-checks the raw response through
    AISchemaGateway, then reuses `_parse_routing_decision`'s existing
    workstream-membership/confidence/topics/reasoning validation as the
    semantic validator -- that check already exists and already raises the
    exact `M365TopicRouterError` `route_artifact`'s broad except clause
    depends on, so a separate validator class would just duplicate it.
    """
    ai_run_id = new_ai_run_id()

    def _lifecycle(state: AIRunState) -> None:
        record_ai_run_lifecycle(
            program_id=program_id,
            ai_run_id=ai_run_id,
            feature=_FEATURE,
            state=state,
            prompt_version=PROMPT_VERSION,
            policy_version=PROMPT_VERSION,
            programs_root=programs_root,
        )

    def _discard(terminal: ReleaseTerminal, reason: str) -> None:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=0,
            programs_root=programs_root,
        )

    _lifecycle(AIRunState.PLANNED)
    _lifecycle(AIRunState.REQUESTED)
    try:
        raw = client.structured(
            system_prompt,
            user_prompt,
            parser=lambda payload: payload,
            max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
            prompt_version=PROMPT_VERSION,
        )
    except Exception as error:
        _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")
        raise
    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")
        raise M365TopicRouterError("AI routing payload must be an object.")

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")
        raise M365TopicRouterError(f"AI routing response rejected by AISchemaGateway: {error}") from error
    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    try:
        decision = _parse_routing_decision(payload=raw, workstream_profiles=workstream_profiles)
    except M365TopicRouterError as error:
        _discard(ReleaseTerminal.REJECTED, f"routing semantic validation failed: {error}")
        raise
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and routing semantic validation",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    return decision


def _prefer_deterministic_routing(fallback_decision: M365RoutingDecision) -> bool:
    return (
        fallback_decision.workstream_id is not None
        and fallback_decision.confidence >= load_m365_routing_policy().deterministic_confidence_threshold
    )


def _with_deterministic_short_circuit_reasoning(fallback_decision: M365RoutingDecision) -> M365RoutingDecision:
    return M365RoutingDecision(
        workstream_id=fallback_decision.workstream_id,
        confidence=fallback_decision.confidence,
        topics=fallback_decision.topics,
        confidence_source=fallback_decision.confidence_source,
        reasoning=(
            f"{fallback_decision.reasoning} Deterministic fallback met the configured "
            f"{load_m365_routing_policy().deterministic_confidence_threshold:.2f} confidence threshold, so AI routing was skipped."
        ),
    )


def _combine_routing_decisions(
    *,
    fallback_decision: M365RoutingDecision,
    ai_decision: M365RoutingDecision,
) -> M365RoutingDecision:
    routing_policy = load_m365_routing_policy()
    if ai_decision.workstream_id == fallback_decision.workstream_id:
        combined_confidence = min(
            routing_policy.confidence_ceiling,
            round(max(ai_decision.confidence, fallback_decision.confidence) + routing_policy.agreement_boost, 2),
        )
        combined_topics = tuple(dict.fromkeys((*ai_decision.topics, *fallback_decision.topics)))
        return M365RoutingDecision(
            workstream_id=ai_decision.workstream_id,
            confidence=combined_confidence,
            topics=combined_topics,
            confidence_source=ai_decision.confidence_source,
            reasoning=(
                f"{ai_decision.reasoning} Agreement with deterministic fallback increased confidence from "
                f"{max(ai_decision.confidence, fallback_decision.confidence):.2f} to {combined_confidence:.2f}."
            ),
        )

    combined_topics = tuple(dict.fromkeys((*ai_decision.topics, *fallback_decision.topics)))
    capped_confidence = min(routing_policy.disagreement_cap, round(ai_decision.confidence, 2))
    return M365RoutingDecision(
        workstream_id=ai_decision.workstream_id,
        confidence=capped_confidence,
        topics=combined_topics,
        confidence_source=ai_decision.confidence_source,
        reasoning=(
            f"{ai_decision.reasoning} Deterministic fallback suggested {fallback_decision.workstream_id or 'no workstream'}, "
            f"so confidence was capped at {capped_confidence:.2f} for review."
        ),
    )


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=M365TopicRouterError)


def _build_user_prompt(
    *,
    display_name: str | None,
    subject_or_title: str | None,
    participant_aliases: tuple[str, ...],
    sample_text: str | None,
    workstream_profiles: tuple[Workstream, ...],
    recent_confirmed_signals: dict[str, tuple[str, ...]] | None,
    recent_rejected_signals: dict[str, tuple[str, ...]] | None,
    recent_reassign_corrections: dict[str, tuple[M365ReassignCorrection, ...]] | None,
    fallback_decision: M365RoutingDecision,
) -> str:
    workstream_lines: list[str] = []
    confirmed_signals = recent_confirmed_signals or {}
    rejected_signals = recent_rejected_signals or {}
    reassign_corrections = recent_reassign_corrections or {}
    artifact_text = " ".join(part for part in (display_name, subject_or_title, sample_text) if part)
    for workstream in workstream_profiles:
        signal_sources = workstream.signal_sources
        keywords = signal_sources.workiq_keywords if signal_sources is not None else ()
        examples = _select_prompt_examples(
            artifact_text=artifact_text,
            confirmed_signals=confirmed_signals.get(workstream.id, ()),
        )
        rejected_examples = _select_prompt_examples(
            artifact_text=artifact_text,
            confirmed_signals=rejected_signals.get(workstream.id, ()),
        )
        correction_examples = _select_prompt_reassign_corrections(
            artifact_text=artifact_text,
            corrections=reassign_corrections.get(workstream.id, ()),
        )
        workstream_lines.append(
            "\n".join(
                (
                    f"- id: {workstream.id}",
                    f"  name: {workstream.name}",
                    f"  aliases: {', '.join(workstream.aliases) or '(none)'}",
                    f"  keywords: {', '.join(keywords) or '(none)'}",
                    f"  recent_confirmed_examples: {' | '.join(examples) if examples else '(none)'}",
                    f"  recent_rejected_examples: {' | '.join(rejected_examples) if rejected_examples else '(none)'}",
                    f"  recent_reassign_corrections: {' | '.join(correction_examples) if correction_examples else '(none)'}",
                )
            )
        )

    return "\n".join(
        (
            f"Display name: {display_name or '(none)'}",
            f"Subject or title: {subject_or_title or '(none)'}",
            f"Participant aliases: {', '.join(participant_aliases) or '(none)'}",
            "Sample text:",
            sample_text or "(none)",
            "",
            "Workstream profiles:",
            "\n\n".join(workstream_lines),
            "",
            f"Deterministic fallback: workstream_id={fallback_decision.workstream_id or '(none)'}, confidence={fallback_decision.confidence:.2f}, topics={', '.join(fallback_decision.topics) or '(none)'}, reasoning={fallback_decision.reasoning}",
        )
    )


def _select_prompt_examples(
    *,
    artifact_text: str,
    confirmed_signals: tuple[str, ...],
    max_examples: int = _PROMPT_EXAMPLE_LIMIT,
    candidate_limit: int = _PROMPT_CORPUS_CANDIDATE_LIMIT,
) -> tuple[str, ...]:
    if not confirmed_signals:
        return ()

    candidates = confirmed_signals[-candidate_limit:]
    artifact_tokens = _prompt_tokens(artifact_text)
    candidate_counts: dict[str, int] = {}
    candidate_last_index: dict[str, int] = {}
    for index, text in enumerate(candidates):
        if not text.strip():
            continue
        candidate_counts[text] = candidate_counts.get(text, 0) + 1
        candidate_last_index[text] = index
    ranked_candidates = sorted(
        candidate_counts,
        key=lambda text: (
            -_shared_prompt_token_count(artifact_tokens, text),
            -candidate_counts[text],
            -len(text),
            -candidate_last_index[text],
        ),
    )
    selected = list(ranked_candidates[:max_examples])
    return tuple(selected)


def _select_prompt_reassign_corrections(
    *,
    artifact_text: str,
    corrections: tuple[M365ReassignCorrection, ...],
    max_examples: int = 3,
) -> tuple[str, ...]:
    if not corrections:
        return ()

    artifact_tokens = _prompt_tokens(artifact_text)
    ranked = sorted(
        corrections,
        key=lambda correction: (
            -_shared_prompt_token_count(artifact_tokens, _format_reassign_correction(correction)),
            -len(_format_reassign_correction(correction)),
        ),
    )

    selected: list[str] = []
    seen: set[str] = set()
    for correction in ranked:
        text = _format_reassign_correction(correction)
        if text in seen:
            continue
        seen.add(text)
        selected.append(text)
        if len(selected) >= max_examples:
            break
    return tuple(selected)


def _format_reassign_correction(correction: M365ReassignCorrection) -> str:
    artifact_label = correction.artifact_display_name or "(unnamed artifact)"
    reason_label = correction.reason or "(no reason provided)"
    return (
        f"from={correction.prior_workstream_id} to={correction.corrected_workstream_id} "
        f"artifact={artifact_label} reason={reason_label}"
    )


def _shared_prompt_token_count(artifact_tokens: set[str], candidate_text: str) -> int:
    if not artifact_tokens:
        return 0
    return sum(1 for token in _prompt_tokens(candidate_text) if token in artifact_tokens)


def _prompt_tokens(value: str) -> set[str]:
    return {token for token in _PROMPT_TOKEN_PATTERN.findall(value.lower()) if len(token) >= 4}


def _parse_routing_decision(
    *,
    payload: dict[str, object],
    workstream_profiles: tuple[Workstream, ...],
) -> M365RoutingDecision:
    if not isinstance(payload, dict):
        raise M365TopicRouterError("AI routing payload must be an object.")

    allowed_workstream_ids = {workstream.id for workstream in workstream_profiles}
    raw_workstream_id = payload.get("workstream_id")
    if raw_workstream_id is None:
        workstream_id = None
    elif isinstance(raw_workstream_id, str) and raw_workstream_id.strip():
        workstream_id = _sanitize_generated_text(raw_workstream_id, field_name="workstream_id")
        if workstream_id not in allowed_workstream_ids:
            raise M365TopicRouterError("AI routing payload returned an unknown workstream_id.")
    else:
        raise M365TopicRouterError("AI routing payload must include workstream_id as a string or null.")

    raw_confidence = payload.get("confidence")
    if not isinstance(raw_confidence, (int, float)):
        raise M365TopicRouterError("AI routing payload must include confidence as a number.")
    confidence = max(0.0, min(load_m365_routing_policy().confidence_ceiling, round(float(raw_confidence), 2)))

    raw_topics = payload.get("topics")
    if not isinstance(raw_topics, list):
        raise M365TopicRouterError("AI routing payload must include topics as a list.")
    topics = tuple(
        dict.fromkeys(
            _sanitize_generated_text(topic, field_name="topics").lower()
            for topic in raw_topics
            if isinstance(topic, str) and topic.strip()
        )
    )

    raw_reasoning = payload.get("reasoning")
    if not isinstance(raw_reasoning, str) or not raw_reasoning.strip():
        raise M365TopicRouterError("AI routing payload must include non-empty reasoning.")

    return M365RoutingDecision(
        workstream_id=workstream_id,
        confidence=confidence,
        topics=topics,
        confidence_source="router",
        reasoning=_sanitize_generated_text(raw_reasoning, field_name="reasoning"),
    )


def _sanitize_generated_text(value: str, *, field_name: str) -> str:
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise M365TopicRouterError(f"AI routing payload must include non-empty {field_name}.")
    try:
        processed = process_generated_text(normalized)
    except AIPipelineError as error:
        raise M365TopicRouterError(f"AI routing payload {field_name} rejected by safety pipeline: {error}") from error
    if not processed.text:
        raise M365TopicRouterError(f"AI routing payload must include non-empty {field_name}.")
    return processed.text
