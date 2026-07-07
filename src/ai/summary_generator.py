from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Protocol

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.models_v2 import Program, Signal, Workstream
from src.core.summary_store import summary_word_count
from src.core.trajectory_analyzer import DriftPattern
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "summary_generator.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "summary_generator"
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
    ) -> RollingSummaryDraft | None:
        if get_ai_mode() == AIMode.DISABLED or self._client is None:
            return None
        if not signals and not drift_patterns and not (prior_summary and prior_summary.strip()):
            return None

        system_prompt = _load_prompt()
        user_prompt = _build_user_prompt(
            program=program,
            workstream=workstream,
            prior_summary=prior_summary,
            signals=signals,
            drift_patterns=drift_patterns,
        )
        try:
            client = self._client
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    system_prompt,
                    user_prompt,
                    parser=_parse_generated_summary_text,
                    max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
        except AIClientError as error:
            raise SummaryGeneratorError(f"Rolling summary generation failed: {error}") from error
        response_text = outcome.value

        text = _normalize_text(str(response_text))
        if not text:
            return None
        try:
            processed = process_generated_text(text)
        except AIPipelineError as error:
            raise SummaryGeneratorError(str(error)) from error
        text = processed.text
        if not text:
            return None
        _validate_work_item_refs(text, signals=signals, drift_patterns=drift_patterns)
        word_count = summary_word_count(text)
        if word_count > _MAX_WORDS:
            raise SummaryGeneratorError(
                f"Rolling summary exceeded {_MAX_WORDS} words ({word_count})."
            )
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
