from __future__ import annotations

from dataclasses import dataclass

from src.ai.grounding import GroundingError, ground_text
from src.ai.injection_detector import InjectionDetector
from src.ai.safety.causality_sanitizer import sanitize_text
from src.ai.safety.pii_scrubber import scan_text
from src.core.models import WorkItem


class AIPipelineError(Exception):
    """Raised when AI-generated text fails the shared safety pipeline."""


@dataclass(frozen=True, slots=True)
class ProcessedAIText:
    text: str
    cited_work_item_ids: tuple[int, ...] = ()


def process_generated_text(
    text: str,
    *,
    allowed_items: tuple[WorkItem, ...] = (),
) -> ProcessedAIText:
    normalized = text.strip()
    if not normalized:
        return ProcessedAIText(text="")

    pii_result = scan_text(normalized)
    injection_result = InjectionDetector().scan(pii_result.scrubbed_text)
    if injection_result.injection_detected:
        signal_types = ", ".join(injection_result.signal_types)
        raise AIPipelineError(f"Generated text rejected by injection detector: {signal_types}")

    sanitized = sanitize_text(pii_result.scrubbed_text)
    sanitized_text = sanitized.sanitized_text.strip()
    if not sanitized_text:
        return ProcessedAIText(text="")

    if not allowed_items:
        return ProcessedAIText(text=sanitized_text)

    try:
        grounded = ground_text(sanitized_text, allowed_items)
    except GroundingError as error:
        raise AIPipelineError(str(error)) from error
    return ProcessedAIText(
        text=grounded.grounded_text,
        cited_work_item_ids=grounded.cited_work_item_ids,
    )