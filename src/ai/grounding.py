from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from src.core.models import WorkItem
from src.core.verbosity_enforcer import split_sentences


_CITATION_PATTERN = re.compile(r"\[#(\d+)\]")
_TRAILING_PUNCTUATION = re.compile(r"([.!?]+)$")
_NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")


class GroundingError(Exception):
    """Raised when AI-generated text cites work items outside the allowed set."""


@dataclass(frozen=True, slots=True)
class GroundedSentence:
    text: str
    cited_work_item_ids: tuple[int, ...]
    match_type: Literal["explicit", "heuristic"]


@dataclass(frozen=True, slots=True)
class GroundingResult:
    grounded_text: str
    removed_claims: tuple[str, ...]
    cited_work_item_ids: tuple[int, ...]
    grounded_sentences: tuple[GroundedSentence, ...] = ()


def ground_text(
    text: str,
    allowed_items: tuple[WorkItem, ...] | list[WorkItem],
) -> GroundingResult:
    items = tuple(allowed_items)
    allowed_ids = {item.id for item in items}
    grounded_sentences: list[str] = []
    sentence_results: list[GroundedSentence] = []
    removed_claims: list[str] = []
    cited_ids: list[int] = []

    for sentence in split_sentences(text):
        stripped_sentence = sentence.strip()
        if not stripped_sentence:
            continue

        citation_ids = tuple(int(match) for match in _CITATION_PATTERN.findall(stripped_sentence))
        if citation_ids:
            invalid_ids = sorted({work_item_id for work_item_id in citation_ids if work_item_id not in allowed_ids})
            if invalid_ids:
                invalid_id_list = ", ".join(str(work_item_id) for work_item_id in invalid_ids)
                raise GroundingError(f"Grounding rejected invalid work item citations: {invalid_id_list}")
            grounded_sentences.append(stripped_sentence)
            cited_ids.extend(citation_ids)
            sentence_results.append(
                GroundedSentence(
                    text=stripped_sentence,
                    cited_work_item_ids=tuple(dict.fromkeys(citation_ids)),
                    match_type="explicit",
                )
            )
            continue

        matched_work_item_id = _match_work_item(stripped_sentence, items)
        if matched_work_item_id is None:
            removed_claims.append(stripped_sentence)
            continue

        grounded_sentence = _append_citation(stripped_sentence, matched_work_item_id)
        grounded_sentences.append(grounded_sentence)
        cited_ids.append(matched_work_item_id)
        sentence_results.append(
            GroundedSentence(
                text=grounded_sentence,
                cited_work_item_ids=(matched_work_item_id,),
                match_type="heuristic",
            )
        )

    return GroundingResult(
        grounded_text=" ".join(grounded_sentences),
        removed_claims=tuple(removed_claims),
        cited_work_item_ids=tuple(dict.fromkeys(cited_ids)),
        grounded_sentences=tuple(sentence_results),
    )


def _match_work_item(sentence: str, allowed_items: tuple[WorkItem, ...]) -> int | None:
    normalized_sentence = _normalize_text(sentence)
    if not normalized_sentence:
        return None

    matches: list[int] = []
    for item in allowed_items:
        if _matches_text(normalized_sentence, item.title):
            matches.append(item.id)
            continue
        if any(_matches_text(normalized_sentence, comment.text) for comment in item.comments):
            matches.append(item.id)

    unique_matches = tuple(dict.fromkeys(matches))
    if len(unique_matches) != 1:
        return None
    return unique_matches[0]


def _matches_text(normalized_sentence: str, candidate_text: str) -> bool:
    normalized_candidate = _normalize_text(candidate_text)
    if not normalized_candidate:
        return False
    return normalized_sentence in normalized_candidate or normalized_candidate in normalized_sentence


def _append_citation(sentence: str, work_item_id: int) -> str:
    match = _TRAILING_PUNCTUATION.search(sentence)
    citation = f" [#{work_item_id}]"
    if match is None:
        return sentence + citation
    punctuation = match.group(1)
    return sentence[: match.start()] + citation + punctuation


def _normalize_text(text: str) -> str:
    lowered = text.lower().strip()
    collapsed = _NON_WORD_PATTERN.sub(" ", lowered)
    return " ".join(fragment for fragment in collapsed.split() if fragment)