from __future__ import annotations

from dataclasses import dataclass
import re

from src.core.config_loader import VerbositySettings
from src.core.models import EditionType


DEFAULT_EXEC_SUMMARY_MAX_WORDS = 150
SUBJECT_MAX_CHARS = 80
_MARKDOWN_LINK_PATTERN = re.compile(r"!?\[([^\]]*)\]\([^\)]+\)")
_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
# Strip full HTML block elements (table, etc.) including their text content before word counting.
_HTML_BLOCK_PATTERN = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class VerbosityViolation:
    location: str
    message: str


def split_sentences(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    if not stripped:
        return ()
    return tuple(sentence for sentence in re.split(r"(?<=[.!?])\s+", stripped) if sentence)


def count_words(text: str) -> int:
    normalized = _visible_text_for_word_count(text)
    return len(re.findall(r"\b\w+(?:['’-]\w+)?\b", normalized))


def _visible_text_for_word_count(text: str) -> str:
    # Remove full HTML table blocks first (table data is not prose wordcount).
    normalized = _HTML_BLOCK_PATTERN.sub(" ", text)
    normalized = _HTML_TAG_PATTERN.sub(" ", normalized)   # strip remaining HTML tags
    normalized = _MARKDOWN_LINK_PATTERN.sub(r"\1", normalized)
    return _URL_PATTERN.sub("", normalized)


def enforce_verbosity(
    workstream_blurbs: dict[str, str],
    exec_summary_text: str,
    scorecard_summaries: dict[str, str],
    subject_line: str | None,
    verbosity: VerbositySettings,
    edition_type: EditionType | None = None,
) -> tuple[VerbosityViolation, ...]:
    violations: list[VerbosityViolation] = []
    workstream_max_words = verbosity.workstream_blurb_max_words_for(edition_type)
    exec_summary_max_words = verbosity.exec_summary_max_words_for(
        edition_type,
        default=DEFAULT_EXEC_SUMMARY_MAX_WORDS,
    )

    for section_id, blurb in workstream_blurbs.items():
        sentences = split_sentences(blurb)
        words = count_words(blurb)
        if (
            verbosity.workstream_blurb_max_sentences is not None
            and len(sentences) > verbosity.workstream_blurb_max_sentences
        ):
            violations.append(
                VerbosityViolation(
                    location=f"workstream:{section_id}",
                    message=(
                        f"Workstream blurb exceeds {verbosity.workstream_blurb_max_sentences} sentences."
                    ),
                )
            )
        if workstream_max_words is not None and words > workstream_max_words:
            violations.append(
                VerbosityViolation(
                    location=f"workstream:{section_id}",
                    message=f"Workstream blurb exceeds {workstream_max_words} words.",
                )
            )
        if (
            verbosity.workstream_blurb_max_paragraphs is not None
            and _paragraph_count(blurb) > verbosity.workstream_blurb_max_paragraphs
        ):
            violations.append(
                VerbosityViolation(
                    location=f"workstream:{section_id}",
                    message="Workstream blurb must stay within one paragraph.",
                )
            )

    if exec_summary_max_words is not None and count_words(exec_summary_text) > exec_summary_max_words:
        violations.append(
            VerbosityViolation(
                location="exec_summary",
                message=f"Executive summary exceeds {exec_summary_max_words} words.",
            )
        )

    for dimension_name, summary in scorecard_summaries.items():
        sentences = split_sentences(summary)
        if (
            verbosity.scorecard_summary_max_sentences is not None
            and len(sentences) > verbosity.scorecard_summary_max_sentences
        ):
            violations.append(
                VerbosityViolation(
                    location=f"scorecard:{dimension_name}",
                    message=(
                        f"Scorecard summary exceeds {verbosity.scorecard_summary_max_sentences} sentences."
                    ),
                )
            )

    if subject_line is not None and len(subject_line) > SUBJECT_MAX_CHARS:
        violations.append(
            VerbosityViolation(
                location="subject",
                message=f"Subject line exceeds {SUBJECT_MAX_CHARS} characters.",
            )
        )

    return tuple(violations)


def _paragraph_count(text: str) -> int:
    return len([paragraph for paragraph in re.split(r"\n\s*\n", text.strip()) if paragraph])
