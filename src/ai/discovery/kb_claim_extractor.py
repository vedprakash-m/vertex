from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.core.knowledge_candidate_store import KnowledgeCandidate, KnowledgeCandidateEntityResolution, build_candidate
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import KnowledgeDocumentRef
from src.core.ledger.ulid import new_ulid


_CLAIM_LINE_PATTERN = re.compile(r"^Claim:\s*(?P<body>.+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractedKnowledgeCandidateBatch:
    batch_id: str
    candidates: tuple[KnowledgeCandidate, ...]


def extract_claim_candidates_from_markdown(
    *,
    markdown_text: str,
    scope: str,
    vault_hash: str,
    original_filename: str,
    origin_path: str | None,
    ingested_at: datetime,
    batch_id: str | None = None,
    source_document_key_suffix: str | None = None,
) -> ExtractedKnowledgeCandidateBatch:
    effective_batch_id = batch_id or new_ulid(datetime.now(timezone.utc))
    candidates: list[KnowledgeCandidate] = []
    for line_number, raw_line in enumerate(markdown_text.splitlines(), start=1):
        parsed = _parse_claim_line(raw_line)
        if parsed is None:
            continue
        subject = parsed.get("subject")
        predicate = parsed.get("predicate")
        if subject is None or predicate is None:
            continue
        value = parsed.get("value")
        valid_from = _parse_datetime_or_date(parsed.get("valid_from")) or ingested_at
        valid_until = _parse_datetime_or_date(parsed.get("valid_until"))
        section = parsed.get("section") or f"line:{line_number}"
        source_ref = KnowledgeDocumentRef(
            vault_hash=vault_hash,
            original_filename=original_filename,
            origin_kind="local_path",
            origin_path=origin_path,
            origin_url=None,
            ingested_at=ingested_at,
            section=section,
        )
        resolution = _resolve_subject(subject)
        candidates.append(
            build_candidate(
                candidate_id=new_ulid(datetime.now(timezone.utc)),
                scope=scope,
                subject=subject,
                predicate=predicate,
                value=value,
                valid_from=valid_from,
                valid_until=valid_until,
                proposed_confidence=_parse_confidence_tier(parsed.get("confidence")),
                source_ref=source_ref,
                pipeline="kb_extract",
                extraction_confidence=0.95,
                entity_resolution=(resolution,),
                corroborating_refs=(),
                batch_id=effective_batch_id,
            )
        )
    return ExtractedKnowledgeCandidateBatch(batch_id=effective_batch_id, candidates=tuple(candidates))


def _parse_claim_line(raw_line: str) -> dict[str, str] | None:
    match = _CLAIM_LINE_PATTERN.match(raw_line.strip())
    if match is None:
        return None
    try:
        processed = process_generated_text(match.group("body"))
    except AIPipelineError as error:
        raise ValueError(f"Unsafe extracted claim line: {error}") from error
    result: dict[str, str] = {}
    for part in processed.text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", maxsplit=1)
        result[key.strip().lower()] = value.strip()
    return result


def _parse_datetime_or_date(value: str | None) -> datetime | None:
    if value is None or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    if "T" not in normalized:
        normalized = f"{normalized}T00:00:00+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_subject(subject: str) -> KnowledgeCandidateEntityResolution:
    if ":" in subject:
        return KnowledgeCandidateEntityResolution(
            raw_name=subject,
            resolved_entity_id=subject,
            match_kind="exact_id",
            score=1.0,
        )
    return KnowledgeCandidateEntityResolution(
        raw_name=subject,
        resolved_entity_id=None,
        match_kind="unresolved",
        score=0.0,
    )


def _parse_confidence_tier(value: str | None) -> ConfidenceTier:
    if value is None or not value.strip():
        return ConfidenceTier.AI_EXTRACTED
    try:
        return ConfidenceTier(value.strip().lower())
    except ValueError as error:
        raise ValueError(
            "Invalid claim confidence tier. Expected one of: "
            + ", ".join(tier.value for tier in ConfidenceTier)
        ) from error
