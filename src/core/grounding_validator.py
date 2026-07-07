from __future__ import annotations

from dataclasses import dataclass, replace

from src.core.models import Confidence
from src.core.models_v2 import Signal, WorkstreamSynthesis


@dataclass(frozen=True, slots=True)
class GroundingValidationResult:
    synthesis: WorkstreamSynthesis
    invalid_evidence_refs: tuple[str, ...]
    flagged_for_review: bool


def validate_synthesis_grounding(
    synthesis: WorkstreamSynthesis,
    *,
    signals: tuple[Signal, ...] | list[Signal],
) -> GroundingValidationResult:
    valid_signal_ids = {signal.id for signal in signals}
    valid_refs: list[str] = []
    invalid_refs: list[str] = []

    for ref in synthesis.evidence_refs:
        if ref in valid_signal_ids:
            valid_refs.append(ref)
            continue
        invalid_refs.append(ref)

    invalid_ratio = (len(invalid_refs) / len(synthesis.evidence_refs)) if synthesis.evidence_refs else 0.0
    flagged_for_review = invalid_ratio > 0.5
    adjusted_confidence = Confidence.LOW if flagged_for_review else synthesis.confidence

    return GroundingValidationResult(
        synthesis=replace(
            synthesis,
            evidence_refs=tuple(valid_refs),
            confidence=adjusted_confidence,
        ),
        invalid_evidence_refs=tuple(invalid_refs),
        flagged_for_review=flagged_for_review,
    )