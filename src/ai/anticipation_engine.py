from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.anticipation_detector import detect_anticipated_questions
from src.core.models import Confidence
from src.core.models_v2 import Dependency, LeadershipReader, LegacyDependency, Signal
from src.core.trajectory_analyzer import DriftPattern
from src.core.policy_loader import load_ai_feature_policy
from src.core.view_models import WorkstreamData


logger = logging.getLogger(__name__)

PROMPT_VERSION = "anticipation_question.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "anticipation_engine"


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
) -> tuple[AnticipatedQuestion, ...]:
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
) -> tuple[str, str] | None:
    user_prompt = prompt_template.format(
        reader=reader,
        question_seed=question_seed,
        response_seed=response_seed,
        evidence="; ".join(evidence),
    )
    try:
        outcome = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                "You generate concise leadership anticipation questions as strict JSON.",
                user_prompt,
                parser=_parse_ai_rendered_question,
                max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                prompt_version=PROMPT_VERSION,
            ),
            policy=load_ai_feature_policy(_FEATURE),
        )
        rendered = outcome.value
        if rendered is None:
            return None
        question_text, response_text = rendered
        if not question_text or not response_text:
            return None
        return question_text, response_text
    except (AIPipelineError, ValueError, TypeError, AIClientError) as error:
        logger.warning("AI anticipation formatting failed; using deterministic fallback: %s", error)
        return None
    except Exception as error:
        logger.warning("AI anticipation call failed; using deterministic fallback: %s", error)
        return None


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