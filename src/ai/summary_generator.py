from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Protocol, cast

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.models_v2 import Program, Signal, Workstream
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.summary_store import summary_word_count
from src.core.trajectory_analyzer import DriftPattern
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "summary_generator.v1"
POLICY_VERSION = "summary_generator.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "summary_generator"
_OUTPUT_SCHEMA_VERSION = "1"
_MAX_WORDS = 500
_WORK_ITEM_REF_PATTERN = re.compile(r"\b(?:WI:|ADO#)(\d+)\b", re.IGNORECASE)


class SummaryGeneratorError(Exception):
    """Raised when rolling summary generation cannot complete."""


@dataclass(frozen=True, slots=True)
class RollingSummaryDraft:
    text: str
    prompt_version: str
    word_count: int


class _StructuredProvider(Protocol):
    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any: ...


class SummaryGenerator:
    """Generates compressed rolling workstream summaries from approved signals."""

    def __init__(self, *, client: _StructuredProvider | None) -> None:
        self._client = client

    @classmethod
    def from_program(
        cls,
        program: Program,
        *,
        trace_context: AITraceContext | None = None,
    ) -> "SummaryGenerator":
        if get_ai_mode() == AIMode.DISABLED:
            return cls(client=None)
        temperature = load_ai_feature_policy(_FEATURE).temperature
        budget_usd = 0.5
        if program.ai is not None:
            temperature = program.ai.temperature or temperature
            budget_usd = program.ai.budget_usd_per_run
        deployments = resolve_ai_deployments_for_feature(
            feature_name=_FEATURE,
            primary_candidates=(
                program.ai.blurb_deployment if program.ai is not None else None,
                program.ai.exec_summary_deployment if program.ai is not None else None,
            ),
            backup_candidates=(
                program.ai.blurb_backup_deployment if program.ai is not None else None,
                program.ai.exec_summary_backup_deployment if program.ai is not None else None,
            ),
            primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
            backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
        )
        if not deployments:
            raise SummaryGeneratorError(
                "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI to generate rolling summaries."
            )
        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=temperature,
            budget_usd=budget_usd,
            requests_per_minute=program.ai.requests_per_minute if program.ai is not None else None,
            trace_context=trace_context,
        )
        return cls(client=client)

    def generate(
        self,
        *,
        program: Program,
        workstream: Workstream,
        prior_summary: str | None,
        signals: tuple[Signal, ...],
        drift_patterns: tuple[DriftPattern, ...],
        programs_root: Path = PROGRAMS_ROOT,
    ) -> RollingSummaryDraft | None:
        """Runs the same AISchemaGateway/QG-29 safety lifecycle established
        by ``risk_proposal_generator.py`` (ADF-W5.1, P7): bounds-checked
        request/response payloads, the five ``AIRunState`` transitions, a
        semantic validator over the parsed summary, and a durable QG-29
        terminal release decision recorded before any caller may consume the
        result. Preserves this feature's existing raise-on-rejection
        contract (``SummaryGeneratorError``) rather than switching to the
        newer generators' return-``None``-on-rejection convention, since
        ``summarize.py``'s CLI already depends on a hard failure surfacing a
        clear operator-facing error for a genuinely malformed AI response."""
        if get_ai_mode() == AIMode.DISABLED or self._client is None:
            return None
        if not signals and not drift_patterns and not (prior_summary and prior_summary.strip()):
            return None

        ai_run_id = new_ai_run_id()
        program_id = program.id

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
            "program_id": program.id,
            "workstream_id": workstream.id,
            "prior_summary": prior_summary or "",
            "signal_texts": [signal.text for signal in signals],
            "signal_refs": [ref for signal in signals for ref in signal.entity_refs],
            "drift_pattern_ids": [pattern.work_item_id for pattern in drift_patterns],
            "drift_pattern_details": [pattern.detail for pattern in drift_patterns],
        }
        try:
            validate_bounded_payload(request_payload)
        except SchemaGatewayError as error:
            reason = f"AISchemaGateway rejected the outbound request: {error}"
            _discard(ReleaseTerminal.DISCARDED, reason)
            raise SummaryGeneratorError(reason) from error

        _lifecycle(AIRunState.REQUESTED)

        system_prompt = _load_prompt()
        user_prompt = _build_user_prompt(
            program=program,
            workstream=workstream,
            prior_summary=prior_summary,
            signals=signals,
            drift_patterns=drift_patterns,
        )
        policy = load_ai_feature_policy(_FEATURE)
        cache_key = AIResultCacheKey(
            program_id=program_id,
            feature=_FEATURE,
            canonical_input_hash=canonical_input_hash(user_prompt),
            prompt_version=PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            model_deployment=_resolve_model_deployment(self._client),
            context_manifest_hash=canonical_input_hash(
                "|".join(ref for signal in signals for ref in signal.entity_refs)
            ),
            output_schema_version=_OUTPUT_SCHEMA_VERSION,
        )
        try:
            client = self._client
            raw = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    system_prompt,
                    user_prompt,
                    parser=lambda payload: payload,
                    max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION,
                ),
                policy=policy,
                cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
                cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
            ).value
        except AIClientError as error:
            _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")
            raise SummaryGeneratorError(f"Rolling summary generation failed: {error}") from error

        _lifecycle(AIRunState.RESPONDED)

        try:
            validate_bounded_payload(raw)
        except SchemaGatewayError as error:
            reason = f"AISchemaGateway rejected the response: {error}"
            _discard(ReleaseTerminal.REJECTED, reason)
            raise SummaryGeneratorError(reason) from error

        _lifecycle(AIRunState.SCHEMA_VALIDATED)

        try:
            text = _parse_generated_summary_text(cast(dict[str, object], raw))
        except SummaryGeneratorError as error:
            _discard(ReleaseTerminal.REJECTED, str(error))
            raise

        try:
            processed = process_generated_text(text)
        except AIPipelineError as error:
            _discard(ReleaseTerminal.REJECTED, str(error))
            raise SummaryGeneratorError(str(error)) from error
        text = processed.text
        if not text:
            _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
            _discard(ReleaseTerminal.REJECTED, "processed text pipeline produced empty output.")
            return None

        findings = _validate_semantics(text, signals=signals, drift_patterns=drift_patterns)
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

        if findings:
            _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))
            raise SummaryGeneratorError("; ".join(findings))

        _discard(
            ReleaseTerminal.RELEASED,
            "passed AISchemaGateway bounds and rolling-summary semantic validation",
        )
        word_count = summary_word_count(text)
        return RollingSummaryDraft(text=text, prompt_version=PROMPT_VERSION, word_count=word_count)


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=SummaryGeneratorError)


def _build_user_prompt(
    *,
    program: Program,
    workstream: Workstream,
    prior_summary: str | None,
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
) -> str:
    lines = [
        f"Program: {program.name}",
        f"Workstream: {workstream.name} ({workstream.id})",
    ]
    if program.objective:
        lines.append(f"Objective: {program.objective}")
    if workstream.description:
        lines.append(f"Description: {workstream.description}")
    if workstream.history_summary:
        lines.append(f"History summary: {workstream.history_summary}")
    if workstream.current_blocker:
        lines.append(f"Current blocker: {workstream.current_blocker}")

    lines.append("")
    lines.append("Prior summary:")
    lines.append(prior_summary.strip() if prior_summary and prior_summary.strip() else "(none)")
    lines.append("")
    lines.append("Approved signals:")
    if signals:
        for signal in signals:
            refs = ", ".join(signal.entity_refs) if signal.entity_refs else "none"
            lines.append(
                f"- {signal.timestamp.isoformat()} | {signal.source} | refs={refs} | {signal.text}"
            )
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("Trajectory patterns:")
    if drift_patterns:
        for pattern in drift_patterns:
            lines.append(
                f"- WI:{pattern.work_item_id} | {pattern.pattern} | {pattern.severity} | {pattern.detail}"
            )
    else:
        lines.append("(none)")

    lines.append("")
    lines.append("Write markdown only.")
    return "\n".join(lines)


def _normalize_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_generated_summary_text(payload: dict[str, object]) -> str:
    if not isinstance(payload, dict):
        raise SummaryGeneratorError("Rolling summary payload must be an object.")
    text = payload.get("text")
    if not isinstance(text, str):
        raise SummaryGeneratorError("Rolling summary payload must include text as a string.")
    normalized = _normalize_text(text)
    if not normalized:
        raise SummaryGeneratorError("Rolling summary payload text must be non-empty.")
    return normalized


def _validate_work_item_refs(
    text: str,
    *,
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
) -> None:
    allowed_work_item_ids: set[int] = set()
    for signal in signals:
        for entity_ref in signal.entity_refs:
            match = _WORK_ITEM_REF_PATTERN.fullmatch(entity_ref.strip())
            if match is not None:
                allowed_work_item_ids.add(int(match.group(1)))
    allowed_work_item_ids.update(pattern.work_item_id for pattern in drift_patterns)

    if not allowed_work_item_ids:
        return

    referenced_ids = {int(match.group(1)) for match in _WORK_ITEM_REF_PATTERN.finditer(text)}
    unknown_ids = sorted(referenced_ids - allowed_work_item_ids)
    if unknown_ids:
        raise SummaryGeneratorError(
            "Rolling summary referenced work items outside approved signals: "
            + ", ".join(str(work_item_id) for work_item_id in unknown_ids)
        )


def _validate_semantics(
    text: str,
    *,
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
) -> tuple[str, ...]:
    """AISchemaGateway's per-feature ``SemanticValidator`` (Section 8.9.3)
    for the rolling summary: a work-item reference outside the approved
    evidence set, and an over-length summary, are both findings the QG-29
    release decision must see rather than exceptions that short-circuit
    each other -- mirrors ``risk_proposal_generator._validate_semantics``'s
    accumulate-then-report shape."""
    findings: list[str] = []
    try:
        _validate_work_item_refs(text, signals=signals, drift_patterns=drift_patterns)
    except SummaryGeneratorError as error:
        findings.append(str(error))

    word_count = summary_word_count(text)
    if word_count > _MAX_WORDS:
        findings.append(f"Rolling summary exceeded {_MAX_WORDS} words ({word_count}).")

    return tuple(findings)


def _resolve_model_deployment(client: _StructuredProvider) -> str:
    """Best-effort model/deployment identifier for the cache key (Section
    8.8.3), mirroring ``risk_proposal_generator._resolve_model_deployment``.
    A wrong/missing id only under-shares the cache (never over-shares,
    since program_id/feature/input hash are still exact-matched), so this
    degrades safely rather than raising."""
    for attr in ("deployment", "primary_deployment", "model", "deployment_id"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None
