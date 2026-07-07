from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any

from pypdf import PdfReader

from src.ai.discovery._mime_text import MIMETextError, parse_eml_message
from src.ai.discovery._structured_event_markers import (
    StructuredEventMarkerError,
    entity_resolution_from_payload,
    marker_occurrence_or_default,
    parse_structured_event_marker_result,
)
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, derive_candidate_dedupe_key
from src.core.ledger.event_log import ConfidenceTier, compute_dedupe_core_hash
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.source_refs import NewsletterRef, source_document_key
from src.core.ledger.ulid import new_ulid


class NewsletterExtractorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedNewsletterCandidateBatch:
    batch_id: str
    candidates: tuple[CandidateEvent, ...]


_MONTH_NAME_DATE_PATTERN = re.compile(
    r"(?P<value>\b(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+\d{1,2},\s+\d{4}\b)",
    flags=re.IGNORECASE,
)
_NUMERIC_DATE_PATTERN = re.compile(r"(?P<value>\b\d{2}[_-]\d{2}[_-]\d{2,4}\b)")
_ISSUE_NUMBER_PATTERN = re.compile(r"issue[\s_]+(?P<value>\d+)\b", flags=re.IGNORECASE)


def extract_newsletter_candidates(
    *,
    program_id: str,
    source_path: Path,
    relative_path: str,
    batch_id: str,
    pipeline: str = "newsletter",
) -> ExtractedNewsletterCandidateBatch:
    if not source_path.exists() or not source_path.is_file():
        raise NewsletterExtractorError(f"Newsletter source not found: {source_path}")
    publication_date = extract_newsletter_publication_date(source_path)
    if publication_date is None:
        raise NewsletterExtractorError(f"Newsletter filename does not contain a parseable date: {source_path.name}")
    issue_number = extract_newsletter_issue_number(source_path)
    text = _normalize_newsletter_text(source_path)
    candidates: list[CandidateEvent] = []
    publication_candidate = _artifact_publication_candidate(
        program_id=program_id,
        relative_path=relative_path,
        publication_date=publication_date,
        issue_number=issue_number,
        batch_id=batch_id,
        pipeline=pipeline,
    )
    candidates.append(publication_candidate)
    for section_index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = parse_structured_event_marker_result(stripped)
        except StructuredEventMarkerError as error:
            raise NewsletterExtractorError(str(error)) from error
        if parsed is None:
            continue
        event_type = parsed.event_type
        payload = parsed.payload
        validate_event_payload(event_type, payload)
        source_ref = NewsletterRef(
            file_path=relative_path,
            publication_date=publication_date,
            issue_number=issue_number,
            section=f"line:{section_index}",
        )
        dedupe_payload = _dedupe_payload_for(event_type, payload)
        dedupe_core_hash = compute_dedupe_core_hash(event_type, dedupe_payload)
        document_key = source_document_key(source_ref)
        occurred_at, temporal_confidence = marker_occurrence_or_default(
            parsed,
            default_occurred_at=datetime.combine(publication_date, datetime.min.time(), tzinfo=timezone.utc),
            default_temporal_confidence="approximate",
        )
        candidates.append(
            CandidateEvent(
                candidate_id=new_ulid(datetime.now(timezone.utc)),
                program_id=program_id,
                proposed_event_type=event_type,
                proposed_payload=payload,
                proposed_occurred_at=occurred_at,
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
    return ExtractedNewsletterCandidateBatch(batch_id=batch_id, candidates=tuple(candidates))


def _artifact_publication_candidate(
    *,
    program_id: str,
    relative_path: str,
    publication_date: date,
    issue_number: int | None,
    batch_id: str,
    pipeline: str,
) -> CandidateEvent:
    if issue_number is not None:
        artifact_id = f"published_artifact:issue-{issue_number:03d}"
        title = f"Issue {issue_number}"
    else:
        slug = Path(relative_path).stem.strip().lower().replace(" ", "-")
        artifact_id = f"published_artifact:newsletter:{publication_date.isoformat()}:{slug}"
        title = Path(relative_path).stem.strip()
    payload = {
        "artifact_id": artifact_id,
        "artifact_kind": "newsletter",
        "title": title,
        "location": relative_path,
        "period_start": publication_date.isoformat(),
        "period_end": publication_date.isoformat(),
    }
    validate_event_payload("artifact.published.v1", payload)
    source_ref = NewsletterRef(
        file_path=relative_path,
        publication_date=publication_date,
        issue_number=issue_number,
    )
    dedupe_payload = _dedupe_payload_for("artifact.published.v1", payload)
    dedupe_core_hash = compute_dedupe_core_hash("artifact.published.v1", dedupe_payload)
    document_key = source_document_key(source_ref)
    return CandidateEvent(
        candidate_id=new_ulid(datetime.now(timezone.utc)),
        program_id=program_id,
        proposed_event_type="artifact.published.v1",
        proposed_payload=payload,
        proposed_occurred_at=datetime.combine(publication_date, datetime.min.time(), tzinfo=timezone.utc),
        proposed_temporal_confidence="approximate",
        proposed_confidence=ConfidenceTier.SOURCE_AUTHORITATIVE.value,
        source_ref=source_ref,
        pipeline=pipeline,
        extraction_confidence=1.0,
        entity_resolution=(
            CandidateEntityResolution(
                raw_name=artifact_id,
                resolved_entity_id=artifact_id,
                match_kind="imported",
                score=1.0,
            ),
        ),
        dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=document_key,
        corroborating_refs=(),
        batch_id=batch_id,
    )


def normalize_newsletter_text(source_path: Path) -> str:
    """Extract plain prose text from a newsletter file (EML, PDF, HTML, or text)."""
    return _normalize_newsletter_text(source_path)


def _normalize_newsletter_text(source_path: Path) -> str:
    if source_path.suffix.lower() == ".eml":
        raw_text = source_path.read_text(encoding="utf-8")
        try:
            return parse_eml_message(source_path).body_text
        except MIMETextError as error:
            if "<html" not in raw_text.lower() and "<body" not in raw_text.lower():
                raise NewsletterExtractorError(str(error)) from error
            parser = _HTMLTextExtractor()
            parser.feed(raw_text)
            parser.close()
            return parser.get_text()
    if source_path.suffix.lower() == ".pdf":
        try:
            return _extract_pdf_text(source_path)
        except NewsletterExtractorError:
            return ""
    raw_text = source_path.read_text(encoding="utf-8")
    if source_path.suffix.lower() in {".html", ".htm"}:
        parser = _HTMLTextExtractor()
        parser.feed(raw_text)
        parser.close()
        return parser.get_text()
    return raw_text.strip()


def _extract_pdf_text(source_path: Path) -> str:
    try:
        reader = PdfReader(str(source_path))
    except Exception as error:
        raise NewsletterExtractorError(f"Unable to read newsletter PDF {source_path.name}: {error}") from error
    parts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception as error:
            raise NewsletterExtractorError(f"Unable to extract text from newsletter PDF {source_path.name}: {error}") from error
        stripped = text.strip()
        if stripped:
            parts.append(stripped)
    return "\n".join(parts)


def extract_newsletter_publication_date(path: Path) -> date | None:
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
    month_name_match = _MONTH_NAME_DATE_PATTERN.search(stem)
    if month_name_match is not None:
        raw_value = month_name_match.group("value")
        for pattern in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(raw_value, pattern).date()
            except ValueError:
                continue
    numeric_match = _NUMERIC_DATE_PATTERN.search(stem)
    if numeric_match is not None:
        raw_value = numeric_match.group("value")
        normalized = raw_value.replace("-", "_")
        for pattern in ("%m_%d_%Y", "%m_%d_%y"):
            try:
                return datetime.strptime(normalized, pattern).date()
            except ValueError:
                continue
    return None


def extract_newsletter_issue_number(path: Path) -> int | None:
    match = _ISSUE_NUMBER_PATTERN.search(path.stem)
    if match is None:
        return None
    return int(match.group("value"))


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


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ol", "p", "section", "table", "td", "th", "tr", "ul"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def get_text(self) -> str:
        text = " ".join(self._parts)
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())
