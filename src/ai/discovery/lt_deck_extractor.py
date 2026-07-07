from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from src.ai.discovery._structured_event_markers import (
    StructuredEventMarkerError,
    StructuredEventMarkerParseResult,
    entity_resolution_from_payload,
    marker_occurrence_or_default,
    parse_structured_event_marker_result,
)
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.event_log import ConfidenceTier, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.source_refs import LTDeckRef, source_document_key
from src.core.ledger.ulid import new_ulid


_SLIDE_PATH_PATTERN = re.compile(r"^ppt/slides/slide(?P<number>\d+)\.xml$", re.IGNORECASE)


class LTDeckExtractorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedLTDeckCandidateBatch:
    batch_id: str
    candidates: tuple[CandidateEvent, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _SlideContent:
    slide_number: int
    slide_title: str | None
    lines: tuple[str, ...]


def extract_lt_deck_candidates_from_pptx(
    *,
    program_id: str,
    source_path: Path,
    relative_path: str,
    batch_id: str,
    pipeline: str = "lt_deck",
    continue_on_marker_errors: bool = False,
) -> ExtractedLTDeckCandidateBatch:
    if not source_path.exists() or not source_path.is_file():
        raise LTDeckExtractorError(f"LT deck source not found: {source_path}")
    deck_date = _extract_lt_deck_date(source_path)
    if deck_date is None:
        raise LTDeckExtractorError(f"LT deck filename does not contain a parseable date: {source_path.name}")

    occurred_at = datetime.combine(deck_date, datetime.min.time(), tzinfo=timezone.utc)
    candidates: list[CandidateEvent] = []
    try:
        slides = _read_slide_contents(source_path)
    except (BadZipFile, ElementTree.ParseError) as error:
        raise LTDeckExtractorError(f"LT deck could not be parsed: {source_path.name}") from error
    warnings: list[str] = []
    for slide in slides:
        for line in slide.lines:
            try:
                parsed = _parse_marker_line(line)
            except LTDeckExtractorError as error:
                if not continue_on_marker_errors:
                    raise
                warnings.append(
                    f"slide {slide.slide_number}"
                    + (f" ({slide.slide_title})" if slide.slide_title else "")
                    + f": {error}"
                )
                continue
            if parsed is None:
                continue
            event_type = parsed.event_type
            payload = parsed.payload
            validate_event_payload(event_type, payload)
            source_ref = LTDeckRef(
                file_path=relative_path,
                deck_date=deck_date,
                slide_number=slide.slide_number,
                slide_title=slide.slide_title,
            )
            dedupe_payload = _dedupe_payload_for(event_type, payload)
            dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
            document_key = source_document_key(source_ref)
            event_occurred_at, temporal_confidence = marker_occurrence_or_default(
                parsed,
                default_occurred_at=occurred_at,
                default_temporal_confidence="approximate",
            )
            candidates.append(
                CandidateEvent(
                    candidate_id=new_ulid(datetime.now(timezone.utc)),
                    program_id=program_id,
                    proposed_event_type=event_type,
                    proposed_payload=payload,
                    proposed_occurred_at=event_occurred_at,
                    proposed_temporal_confidence=temporal_confidence,
                    proposed_confidence=ConfidenceTier.SOURCE_AUTHORITATIVE.value,
                    source_ref=source_ref,
                    pipeline=pipeline,
                    extraction_confidence=0.95,
                    entity_resolution=_entity_resolution(event_type, payload),
                    dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
                    dedupe_core_hash=dedupe_core_hash,
                    source_document_key=document_key,
                    corroborating_refs=(),
                    batch_id=batch_id,
                )
            )
    return ExtractedLTDeckCandidateBatch(
        batch_id=batch_id,
        candidates=tuple(candidates),
        warnings=tuple(warnings),
    )


def extract_lt_deck_date_from_path(path: Path):
    """Return the date embedded in an LT deck filename, or None if not parseable."""
    return _extract_lt_deck_date(path)


def get_lt_deck_prose_text(source_path: Path) -> str:
    """Extract all slide text from a PPTX file as concatenated prose lines."""
    try:
        slides = _read_slide_contents(source_path)
    except (BadZipFile, ElementTree.ParseError):
        return ""
    return "\n".join("\n".join(slide.lines) for slide in slides)


def _read_slide_contents(source_path: Path) -> tuple[_SlideContent, ...]:
    slides: list[_SlideContent] = []
    with ZipFile(source_path) as archive:
        slide_members = sorted(
            (
                (int(match.group("number")), name)
                for name in archive.namelist()
                for match in [_SLIDE_PATH_PATTERN.match(name)]
                if match is not None
            ),
            key=lambda item: item[0],
        )
        for slide_number, member_name in slide_members:
            root = ElementTree.fromstring(archive.read(member_name))
            texts = [node.text.strip() for node in root.iter() if node.tag.endswith("}t") and isinstance(node.text, str) and node.text.strip()]
            if not texts:
                continue
            lines: list[str] = []
            for text in texts:
                lines.extend(part.strip() for part in text.splitlines() if part.strip())
            slide_title = next((line for line in lines if _parse_marker_line(line) is None), lines[0] if lines else None)
            slides.append(_SlideContent(slide_number=slide_number, slide_title=slide_title, lines=tuple(lines)))
    return tuple(slides)


def _parse_marker_line(line: str) -> StructuredEventMarkerParseResult | None:
    try:
        return parse_structured_event_marker_result(line)
    except StructuredEventMarkerError as error:
        raise LTDeckExtractorError(str(error)) from error


def _entity_resolution(event_type: str, payload: dict[str, Any]) -> tuple[CandidateEntityResolution, ...]:
    schema = get_event_schema(event_type)
    return tuple(
        CandidateEntityResolution(**item)
        for item in entity_resolution_from_payload(schema.entity_ref_fields, payload)
    )


def _dedupe_payload_for(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = get_event_schema(event_type)
    if not schema.dedupe_core_fields:
        return dict(payload)
    return {field_name: payload[field_name] for field_name in schema.dedupe_core_fields if field_name in payload}


def _extract_lt_deck_date(path: Path):
    stem = path.stem
    for token in stem.replace("_", " ").split():
        normalized = token.strip().rstrip("-")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.date()
        for pattern in ("%Y%m%d", "%Y%m", "%Y%m-%d"):
            try:
                return datetime.strptime(normalized, pattern).date()
            except ValueError:
                continue
    return None
