from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.ai.grounding import GroundingResult
from src.core.models import Confidence


class ConfidenceTaggingError(Exception):
    """Raised when confidence cannot be derived for grounded AI output."""


@dataclass(frozen=True, slots=True)
class TaggedSentence:
    text: str
    confidence: Confidence
    cited_work_item_ids: tuple[int, ...]
    match_type: str


@dataclass(frozen=True, slots=True)
class ConfidenceTaggingResult:
    tagged_sentences: tuple[TaggedSentence, ...]

    @property
    def tagged_text(self) -> str:
        return " ".join(sentence.text for sentence in self.tagged_sentences)

    @property
    def overall_confidence(self) -> Confidence:
        if not self.tagged_sentences:
            return Confidence.NONE
        return min((sentence.confidence for sentence in self.tagged_sentences), key=_confidence_rank)


def tag_grounded_text(
    grounded: GroundingResult,
    confidence_by_work_item: Mapping[int, Confidence],
) -> ConfidenceTaggingResult:
    tagged_sentences: list[TaggedSentence] = []
    for sentence in grounded.grounded_sentences:
        tagged_sentences.append(
            TaggedSentence(
                text=sentence.text,
                confidence=_derive_sentence_confidence(sentence.cited_work_item_ids, sentence.match_type, confidence_by_work_item),
                cited_work_item_ids=sentence.cited_work_item_ids,
                match_type=sentence.match_type,
            )
        )
    return ConfidenceTaggingResult(tagged_sentences=tuple(tagged_sentences))


def _derive_sentence_confidence(
    cited_work_item_ids: tuple[int, ...],
    match_type: str,
    confidence_by_work_item: Mapping[int, Confidence],
) -> Confidence:
    if not cited_work_item_ids:
        return Confidence.NONE

    cited_confidences: list[Confidence] = []
    for work_item_id in cited_work_item_ids:
        confidence = confidence_by_work_item.get(work_item_id)
        if confidence is None:
            raise ConfidenceTaggingError(f"Missing confidence for grounded work item #{work_item_id}.")
        cited_confidences.append(confidence)

    derived_confidence = min(cited_confidences, key=_confidence_rank)
    if match_type == "heuristic" and _confidence_rank(derived_confidence) > _confidence_rank(Confidence.MEDIUM):
        return Confidence.MEDIUM
    return derived_confidence


def _confidence_rank(confidence: Confidence) -> int:
    ranking = {
        Confidence.HIGH: 3,
        Confidence.MEDIUM: 2,
        Confidence.LOW: 1,
        Confidence.NONE: 0,
    }
    return ranking[confidence]