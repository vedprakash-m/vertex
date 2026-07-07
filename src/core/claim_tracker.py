from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
from time import monotonic
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, uuid5

import portalocker

from src.core.claim_extraction_calibration_store import ClaimExtractionCalibrationRecord, append_claim_extraction_calibration_record
from src.core.jsonl_utils import (
    compute_file_checksum,
    jsonl_checksum_matches,
    list_jsonl_quarantine_paths,
    parse_jsonl_line,
    quarantine_and_rewrite_jsonl,
    validate_jsonl_row,
    write_checksum_file,
)
from src.core.models import Confidence
from src.core.models import WorkItem
from src.core.models_v2 import ClaimEntry, ClaimStatusUpdate, DecisionAsk, ResurfacingPolicy


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"
MAX_CLAIMS_PER_CONFIRM = 20
_DUE_DATE_DEDUP_WINDOW_DAYS = 7
_SIMILARITY_THRESHOLD = 0.82
_WI_REF_PATTERN = re.compile(r"\bWI:(\d+)\b", re.IGNORECASE)
_ISO_DATE_PATTERN = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_EMAIL_PATTERN = re.compile(r"\b([a-z0-9._-]+)@[a-z0-9.-]+\b", re.IGNORECASE)
_AT_ALIAS_PATTERN = re.compile(r"@([a-z][a-z0-9._-]+)", re.IGNORECASE)
_OWNER_ALIAS_PATTERN = re.compile(
    r"\b(?:owner:?|assign(?:ed)? to)\s+([a-z][a-z0-9._-]+(?:@[a-z0-9.-]+)?)\b",
    re.IGNORECASE,
)
_MONTH_DATE_PATTERN = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(20\d{2}))?\b",
    re.IGNORECASE,
)
_CLAIM_HINTS = (
    "expected by",
    "expect by",
    "targeting",
    "on track for",
    "will deliver by",
    "scheduled for",
    "follow up by",
    "follow-up by",
    "commit to",
    "committed to",
    "will follow up",
    "action item",
    "agreed to",
    "will deliver",
    "will handle",
    "will ensure",
    "will unblock",
    "is going to",
)
_ASK_HINTS = (
    "need decision",
    "needs decision",
    "decision required",
    "need lt decision",
    "ask:",
    "request decision",
    "need alignment",
    "escalate to",
    "escalating to",
    "blocker for",
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

TrackedEntry = ClaimEntry | DecisionAsk
ClaimLogEntry = ClaimEntry | DecisionAsk | ClaimStatusUpdate


@dataclass(frozen=True, slots=True)
class ClaimExtractionResult:
    claims: tuple[ClaimEntry, ...]
    decision_asks: tuple[DecisionAsk, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimPersistResult:
    written_claims: tuple[ClaimEntry, ...]
    written_decision_asks: tuple[DecisionAsk, ...]
    warnings: tuple[str, ...] = ()
    calibration_record: ClaimExtractionCalibrationRecord | None = None


@dataclass(frozen=True, slots=True)
class ClaimAssessment:
    claim: ClaimEntry
    effective_status: str
    reason: str | None = None
    confidence: Confidence = Confidence.HIGH


@dataclass(frozen=True, slots=True)
class LocatedTrackedEntry:
    program_id: str
    entry: TrackedEntry
    effective_status: str


def get_claims_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "claims.jsonl"


def get_claims_quarantine_dir(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_claims_path(program_id, programs_root).parent / "quarantine"


def get_claims_checksum_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_claims_path(program_id, programs_root).with_suffix(".sha256")


def list_claim_quarantine_paths(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, ...]:
    quarantine_dir = get_claims_quarantine_dir(program_id, programs_root)
    return list_jsonl_quarantine_paths(quarantine_dir, stem="claims")


def append_claim_entry(entry: ClaimEntry, programs_root: Path = PROGRAMS_ROOT) -> Path:
    _sync_claim_entry_fact(entry, programs_root=programs_root)
    target = get_claims_path(entry.program_id, programs_root)
    _append_jsonl(target, _claim_to_record(entry))
    return target


def append_decision_ask(entry: DecisionAsk, programs_root: Path = PROGRAMS_ROOT) -> Path:
    _sync_decision_ask_fact(entry, programs_root=programs_root)
    target = get_claims_path(entry.program_id, programs_root)
    _append_jsonl(target, _decision_ask_to_record(entry))
    return target


def append_claim_status_update(program_id: str, update: ClaimStatusUpdate, programs_root: Path = PROGRAMS_ROOT) -> Path:
    _sync_claim_status_update_fact(program_id, update, programs_root=programs_root)
    target = get_claims_path(program_id, programs_root)
    _append_jsonl(target, _status_update_to_record(update))
    return target


def touch_decision_ask(
    program_id: str,
    decision_ask_id: str,
    *,
    updated_at: datetime,
    updated_by: str,
    note: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return append_claim_status_update(
        program_id,
        ClaimStatusUpdate(
            claim_id=decision_ask_id,
            new_status="open",
            updated_at=updated_at,
            updated_by=updated_by,
            note=note,
        ),
        programs_root=programs_root,
    )


def read_claim_log(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[ClaimLogEntry, ...]:
    path = get_claims_path(program_id, programs_root)
    if not path.exists():
        return ()
    entries: list[ClaimLogEntry] = []
    for record in _read_jsonl(path):
        raw_record_type = record.get("record_type", "claim")
        if not isinstance(raw_record_type, str):
            raise TypeError("record_type must be a string")
        record_type = raw_record_type
        if record_type == "status_update":
            entries.append(_status_update_from_record(record))
        elif record_type == "decision_ask":
            entries.append(_decision_ask_from_record(record))
        elif record_type == "claim":
            entries.append(_claim_from_record(record))
        else:
            raise ValueError(f"Unknown claim log record_type '{record_type}'")
    return tuple(entries)


def load_claim_entries(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[ClaimEntry, ...]:
    path = get_claims_path(program_id, programs_root)
    if not path.exists():
        return ()
    entries: list[ClaimEntry] = []
    for raw_record in _read_jsonl(path):
        raw_record_type = raw_record.get("record_type", "claim")
        if not isinstance(raw_record_type, str):
            raise TypeError("record_type must be a string")
        record_type = raw_record_type
        if record_type == "status_update":
            continue
        if record_type == "decision_ask":
            continue
        if record_type != "claim":
            raise ValueError(f"Unknown claim log record_type '{record_type}'")
        # Strict field-presence gate: surfaces missing/None fields with a clear error
        # before the deeper type-coercion parsers would fail with KeyError/TypeError.
        validate_jsonl_row(
            raw_record,
            required_fields=("id", "program_id", "edition_id", "issue_number", "text", "claim_date"),
            field_name="claim row",
        )
        entries.append(_claim_from_record(raw_record))
    return tuple(entries)


def load_decision_asks(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[DecisionAsk, ...]:
    path = get_claims_path(program_id, programs_root)
    if not path.exists():
        return ()
    latest_statuses = load_latest_claim_statuses(program_id, programs_root)
    parsed: list[DecisionAsk] = []
    for raw_record in _read_jsonl(path):
        record_type = raw_record.get("record_type", "claim")
        if record_type != "decision_ask":
            continue
        validate_jsonl_row(
            raw_record,
            required_fields=("id", "program_id", "edition_id", "issue_number", "text", "ask_date"),
            field_name="decision ask row",
        )
        entry = _decision_ask_from_record(raw_record)
        if isinstance(entry, DecisionAsk):
            parsed.append(entry)
    return tuple(_project_decision_ask_last_touch(entry, latest_statuses) for entry in parsed)


def load_claim_status_updates(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[ClaimStatusUpdate, ...]:
    return tuple(entry for entry in read_claim_log(program_id, programs_root) if isinstance(entry, ClaimStatusUpdate))


def load_latest_claim_statuses(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> dict[str, ClaimStatusUpdate]:
    latest: dict[str, ClaimStatusUpdate] = {}
    for entry in read_claim_log(program_id, programs_root):
        if isinstance(entry, ClaimStatusUpdate):
            latest[entry.claim_id] = entry
    return latest


def resolve_entry_status(entry: TrackedEntry, latest_statuses: dict[str, ClaimStatusUpdate]) -> str:
    update = latest_statuses.get(entry.id)
    if update is not None:
        return update.new_status
    return entry.status


def load_open_claims(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[ClaimEntry, ...]:
    latest_statuses = load_latest_claim_statuses(program_id, programs_root)
    return tuple(
        entry
        for entry in load_claim_entries(program_id, programs_root)
        if resolve_entry_status(entry, latest_statuses) == "open"
    )


def load_open_decision_asks(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[DecisionAsk, ...]:
    latest_statuses = load_latest_claim_statuses(program_id, programs_root)
    return tuple(
        entry
        for entry in load_decision_asks(program_id, programs_root)
        if resolve_entry_status(entry, latest_statuses) == "open"
    )


def locate_tracked_entry(
    entry_id: str,
    *,
    program_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> LocatedTrackedEntry | None:
    candidate_programs = (program_id,) if program_id is not None else tuple(
        path.name for path in programs_root.iterdir() if path.is_dir()
    )
    found: list[LocatedTrackedEntry] = []
    for candidate_program in candidate_programs:
        latest_statuses = load_latest_claim_statuses(candidate_program, programs_root)
        all_entries: tuple[TrackedEntry, ...] = (
            *load_claim_entries(candidate_program, programs_root),
            *load_decision_asks(candidate_program, programs_root),
        )
        for entry in all_entries:
            if entry.id == entry_id:
                found.append(
                    LocatedTrackedEntry(
                        program_id=candidate_program,
                        entry=entry,
                        effective_status=resolve_entry_status(entry, latest_statuses),
                    )
                )
    if len(found) != 1:
        return None
    return found[0]


def _project_decision_ask_last_touch(
    entry: DecisionAsk,
    latest_statuses: dict[str, ClaimStatusUpdate],
) -> DecisionAsk:
    update = latest_statuses.get(entry.id)
    if update is None or update.new_status != "open":
        return entry
    if entry.last_touched_at is not None and entry.last_touched_at >= update.updated_at:
        return entry
    return replace(entry, last_touched_at=update.updated_at)


def _sync_claim_entry_fact(entry: ClaimEntry, *, programs_root: Path) -> None:
    from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore

    entity_refs = tuple(entry.entity_refs) or (f"CLAIM:{entry.id}",)
    ProgramFactStore(entry.program_id, db_root=_resolve_fact_db_root(programs_root)).append_fact(
        ProgramFactInput(
            fact_type="claim.entry",
            scope="program",
            entity_refs=entity_refs,
            payload=_claim_to_record(entry),
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            created_by="vertex.claim_tracker",
        ),
        recorded_at=datetime.combine(entry.claim_date, datetime.min.time(), tzinfo=timezone.utc),
    )


def _sync_decision_ask_fact(entry: DecisionAsk, *, programs_root: Path) -> None:
    from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore

    entity_refs = tuple(entry.entity_refs) or (f"ASK:{entry.id}",)
    ProgramFactStore(entry.program_id, db_root=_resolve_fact_db_root(programs_root)).append_fact(
        ProgramFactInput(
            fact_type="decision.ask",
            scope="program",
            entity_refs=entity_refs,
            payload=_decision_ask_to_record(entry),
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            created_by="vertex.claim_tracker",
        ),
        recorded_at=datetime.combine(entry.ask_date, datetime.min.time(), tzinfo=timezone.utc),
    )


def _sync_claim_status_update_fact(program_id: str, update: ClaimStatusUpdate, *, programs_root: Path) -> None:
    from src.core.program_fact_store import ProgramFactInput, ProgramFactStore

    ProgramFactStore(program_id, db_root=_resolve_fact_db_root(programs_root)).append_fact(
        ProgramFactInput(
            fact_type="claim.status_update",
            scope="program",
            entity_refs=(f"CLAIM_STATUS:{update.claim_id}:{update.updated_at.isoformat()}",),
            payload=_status_update_to_record(update),
            created_by="vertex.claim_tracker",
        ),
        recorded_at=update.updated_at,
    )


def _resolve_fact_db_root(programs_root: Path) -> Path | None:
    if programs_root == PROGRAMS_ROOT:
        return None
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


def extract_claims_from_confirmed_narratives(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    narratives: dict[str, str],
    items: tuple[WorkItem, ...] = (),
    valid_workstream_ids: tuple[str, ...] = (),
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
) -> ClaimExtractionResult:
    item_lookup = {item.id: item for item in items}
    claims: list[ClaimEntry] = []
    decision_asks: list[DecisionAsk] = []
    warnings: list[str] = []
    resolved_area_paths = workstream_area_paths or {}

    for filename, content in sorted(narratives.items()):
        for sentence in _candidate_sentences(content):
            text = _normalize_text(sentence)
            if not text:
                continue
            refs = _extract_entity_refs(text)
            workstream_id = _infer_workstream_id(
                filename,
                valid_workstream_ids=valid_workstream_ids,
                refs=refs,
                item_lookup=item_lookup,
                workstream_area_paths=resolved_area_paths,
            )
            due_date = _extract_due_date(text, claim_date)
            owner_alias = _infer_owner_alias(text, refs, item_lookup)
            if _looks_like_decision_ask(text):
                decision_asks.append(
                    DecisionAsk(
                        id=_build_entry_id(
                            kind="decision_ask",
                            program_id=program_id,
                            edition_id=edition_id,
                            issue_number=issue_number,
                            workstream_id=workstream_id,
                            text=text,
                            refs=refs,
                            due_date=None,
                        ),
                        program_id=program_id,
                        edition_id=edition_id,
                        issue_number=issue_number,
                        text=text,
                        entity_refs=refs,
                        ask_date=claim_date,
                        owner_alias=owner_alias,
                    )
                )
                continue
            if due_date is None or not _looks_like_claim(text):
                continue
            claims.append(
                ClaimEntry(
                    id=_build_entry_id(
                        kind="claim",
                        program_id=program_id,
                        edition_id=edition_id,
                        issue_number=issue_number,
                        workstream_id=workstream_id,
                        text=text,
                        refs=refs,
                        due_date=due_date,
                    ),
                    program_id=program_id,
                    edition_id=edition_id,
                    issue_number=issue_number,
                    workstream_id=workstream_id,
                    text=text,
                    entity_refs=refs,
                    claim_date=claim_date,
                    owner_alias=owner_alias,
                    due_date=due_date,
                )
            )

    total_entries = len(claims) + len(decision_asks)
    if total_entries > MAX_CLAIMS_PER_CONFIRM:
        allowed_claims = max(0, MAX_CLAIMS_PER_CONFIRM - len(decision_asks))
        claims = claims[:allowed_claims]
        if len(claims) + len(decision_asks) > MAX_CLAIMS_PER_CONFIRM:
            decision_asks = decision_asks[: max(0, MAX_CLAIMS_PER_CONFIRM - len(claims))]
        warnings.append(
            f"Claim extraction produced {total_entries} candidate entries; truncated to {MAX_CLAIMS_PER_CONFIRM}."
        )

    return ClaimExtractionResult(
        claims=tuple(claims),
        decision_asks=tuple(decision_asks),
        warnings=tuple(warnings),
    )


def record_confirmed_claims(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    narratives: dict[str, str],
    items: tuple[WorkItem, ...] = (),
    valid_workstream_ids: tuple[str, ...] = (),
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
    extraction_result: ClaimExtractionResult | None = None,
    extraction_mode: str = "regex",
    programs_root: Path = PROGRAMS_ROOT,
) -> ClaimPersistResult:
    regex_extracted = extract_claims_from_confirmed_narratives(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        claim_date=claim_date,
        narratives=narratives,
        items=items,
        valid_workstream_ids=valid_workstream_ids,
        workstream_area_paths=workstream_area_paths,
    )
    extracted = (
        _normalize_claim_extraction_result(
            extraction_result,
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            claim_date=claim_date,
        )
        if extraction_result is not None
        else regex_extracted
    )
    existing_claims = load_open_claims(program_id, programs_root)
    existing_asks = load_open_decision_asks(program_id, programs_root)
    warnings = list(extracted.warnings)
    written_claims: list[ClaimEntry] = []
    written_asks: list[DecisionAsk] = []
    skipped_claims = 0
    skipped_asks = 0

    for claim in extracted.claims:
        if _is_duplicate_claim(claim, (*existing_claims, *written_claims)):
            skipped_claims += 1
            continue
        append_claim_entry(claim, programs_root)
        written_claims.append(claim)

    for decision_ask in extracted.decision_asks:
        if _is_duplicate_decision_ask(decision_ask, (*existing_asks, *written_asks)):
            skipped_asks += 1
            continue
        append_decision_ask(decision_ask, programs_root)
        written_asks.append(decision_ask)

    if skipped_claims:
        warnings.append(f"Skipped {skipped_claims} duplicate claim candidate(s).")
    if skipped_asks:
        warnings.append(f"Skipped {skipped_asks} duplicate decision ask candidate(s).")

    calibration_record: ClaimExtractionCalibrationRecord | None = None
    if extraction_result is not None and extraction_mode.strip().lower() == "calibration":
        calibration_record = build_claim_extraction_calibration_record(
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            claim_date=claim_date,
            narratives=narratives,
            items=items,
            valid_workstream_ids=valid_workstream_ids,
            workstream_area_paths=workstream_area_paths,
            ai_extracted=extracted,
        )
        append_claim_extraction_calibration_record(calibration_record, programs_root=programs_root)
        difference_count = calibration_record.ai_only_count + calibration_record.regex_only_count
        if difference_count >= 3:
            if calibration_record.ai_only_count >= calibration_record.regex_only_count:
                warnings.append(
                    "AI extraction found more claims than regex. Review with `vertex claims --show-ai-only` before confirming."
                )
            else:
                warnings.append(
                    "Regex extraction found more claims than AI. Review the claim extraction comparison before confirming."
                )

    return ClaimPersistResult(
        written_claims=tuple(written_claims),
        written_decision_asks=tuple(written_asks),
        warnings=tuple(warnings),
        calibration_record=calibration_record,
    )


def build_claim_extraction_calibration_record(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    narratives: dict[str, str],
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
    ai_extracted: ClaimExtractionResult,
) -> ClaimExtractionCalibrationRecord:
    regex_extracted = extract_claims_from_confirmed_narratives(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        claim_date=claim_date,
        narratives=narratives,
        items=items,
        valid_workstream_ids=valid_workstream_ids,
        workstream_area_paths=workstream_area_paths,
    )
    normalized_ai = _normalize_claim_extraction_result(
        ai_extracted,
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        claim_date=claim_date,
    )
    return _build_claim_extraction_calibration_record(
        program_id=program_id,
        issue_number=issue_number,
        claim_date=claim_date,
        ai_extracted=normalized_ai,
        regex_extracted=regex_extracted,
    )


def assess_claim_entries(
    claims: tuple[ClaimEntry, ...],
    *,
    items: tuple[WorkItem, ...],
    as_of: datetime,
    latest_statuses: dict[str, ClaimStatusUpdate] | None = None,
) -> tuple[ClaimAssessment, ...]:
    latest = latest_statuses or {}
    item_lookup = {item.id: item for item in items}
    assessments: list[ClaimAssessment] = []
    for claim in claims:
        manual_status = resolve_entry_status(claim, latest)
        if manual_status != "open":
            assessments.append(ClaimAssessment(claim=claim, effective_status=manual_status, confidence=Confidence.HIGH))
            continue
        effective_status = "open"
        reason: str | None = None
        referenced_item = _first_referenced_item(claim.entity_refs, item_lookup)
        if claim.due_date is not None and referenced_item is not None and referenced_item.target_date is not None and referenced_item.target_date > claim.due_date:
            if as_of.date() > claim.due_date:
                effective_status = "stale"
                reason = f"Claim due {claim.due_date.isoformat()} passed; current ADO target date is {referenced_item.target_date.isoformat()}."
            else:
                effective_status = "contradicted"
                reason = f"Current ADO target date is {referenced_item.target_date.isoformat()}, later than the claimed date {claim.due_date.isoformat()}."
        elif claim.due_date is not None and as_of.date() > claim.due_date:
            effective_status = "stale"
            reason = f"Claim due {claim.due_date.isoformat()} has passed."
        assessments.append(
            ClaimAssessment(
                claim=claim,
                effective_status=effective_status,
                reason=reason,
                confidence=Confidence.HIGH,
            )
        )
    return tuple(assessments)


def _candidate_sentences(content: str) -> tuple[str, ...]:
    lines = [segment.strip() for segment in re.split(r"[\n\r]+", content) if segment.strip()]
    sentences: list[str] = []
    for line in lines:
        stripped_line = line.lstrip("-*• ").strip()
        if not stripped_line:
            continue
        sentences.extend(segment.strip() for segment in re.split(r"(?<=[.!?])\s+", stripped_line) if segment.strip())
    return tuple(sentences)


def _normalize_text(text: str) -> str:
    normalized = " ".join(text.split()).strip()
    return normalized.rstrip(". ")


def _extract_entity_refs(text: str) -> tuple[str, ...]:
    refs = {f"WI:{match.group(1)}" for match in _WI_REF_PATTERN.finditer(text)}
    return tuple(sorted(refs))


def _extract_due_date(text: str, reference_date: date) -> date | None:
    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match is not None:
        year, month, day = (int(part) for part in iso_match.groups())
        return date(year, month, day)
    month_match = _MONTH_DATE_PATTERN.search(text)
    if month_match is None:
        return None
    month_name, day_text, year_text = month_match.groups()
    year = int(year_text) if year_text is not None else reference_date.year
    month = _MONTHS[month_name.lower()]
    return date(year, month, int(day_text))


def _infer_owner_alias(text: str, refs: tuple[str, ...], item_lookup: dict[int, WorkItem]) -> str | None:
    for pattern in (_AT_ALIAS_PATTERN, _OWNER_ALIAS_PATTERN, _EMAIL_PATTERN):
        match = pattern.search(text)
        if match is None:
            continue
        alias = _normalize_alias(match.group(1))
        if alias is not None:
            return alias
    item = _first_referenced_item(refs, item_lookup)
    if item is None:
        return None
    return _normalize_alias(item.assigned_to_email or item.assigned_to)


def _first_referenced_item(refs: tuple[str, ...], item_lookup: dict[int, WorkItem]) -> WorkItem | None:
    for ref in refs:
        if not ref.upper().startswith("WI:"):
            continue
        try:
            item_id = int(ref.split(":", 1)[1])
        except ValueError:
            continue
        item = item_lookup.get(item_id)
        if item is not None:
            return item
    return None


def _normalize_alias(value: str | None) -> str | None:
    if value is None:
        return None
    alias = value.strip().lower()
    if "@" in alias:
        alias = alias.split("@", 1)[0]
    alias = re.sub(r"[^a-z0-9._-]", "", alias)
    return alias or None


def _looks_like_claim(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _CLAIM_HINTS)


def _looks_like_decision_ask(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _ASK_HINTS)


def _workstream_id_from_filename(filename: str, valid_workstream_ids: tuple[str, ...]) -> str | None:
    stem = Path(filename).stem
    if not stem.startswith("ws_"):
        return None
    candidate = stem[3:]
    if not valid_workstream_ids or candidate in valid_workstream_ids:
        return candidate or None
    return None


def _infer_workstream_id(
    filename: str,
    *,
    valid_workstream_ids: tuple[str, ...],
    refs: tuple[str, ...],
    item_lookup: dict[int, WorkItem],
    workstream_area_paths: dict[str, tuple[str, ...]],
) -> str | None:
    from_filename = _workstream_id_from_filename(filename, valid_workstream_ids)
    if from_filename is not None:
        return from_filename
    item = _first_referenced_item(refs, item_lookup)
    if item is None:
        return None
    for workstream_id, area_paths in workstream_area_paths.items():
        if any(item.area_path.startswith(area_path) for area_path in area_paths):
            return workstream_id
    return None


def _build_entry_id(
    *,
    kind: str,
    program_id: str,
    edition_id: str,
    issue_number: int,
    workstream_id: str | None,
    text: str,
    refs: tuple[str, ...],
    due_date: date | None,
) -> str:
    payload = "|".join(
        (
            kind,
            program_id,
            edition_id,
            str(issue_number),
            workstream_id or "",
            _normalize_text(text).lower(),
            ",".join(refs),
            due_date.isoformat() if due_date is not None else "",
        )
    )
    return str(uuid5(NAMESPACE_URL, payload))


def _normalize_claim_extraction_result(
    extraction_result: ClaimExtractionResult,
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
) -> ClaimExtractionResult:
    normalized_claims = tuple(
        ClaimEntry(
            id=_build_entry_id(
                kind="claim",
                program_id=program_id,
                edition_id=edition_id,
                issue_number=issue_number,
                workstream_id=entry.workstream_id,
                text=entry.text,
                refs=tuple(entry.entity_refs),
                due_date=entry.due_date,
            ),
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            workstream_id=entry.workstream_id,
            text=entry.text,
            entity_refs=tuple(entry.entity_refs),
            claim_date=claim_date,
            owner_alias=entry.owner_alias,
            due_date=entry.due_date,
        )
        for entry in extraction_result.claims
    )
    normalized_asks = tuple(
        DecisionAsk(
            id=_build_entry_id(
                kind="decision_ask",
                program_id=program_id,
                edition_id=edition_id,
                issue_number=issue_number,
                workstream_id=None,
                text=entry.text,
                refs=tuple(entry.entity_refs),
                due_date=None,
            ),
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            text=entry.text,
            entity_refs=tuple(entry.entity_refs),
            ask_date=claim_date,
            owner_alias=entry.owner_alias,
            status=entry.status,
            resolution=entry.resolution,
            expiry_date=entry.expiry_date,
            resurfacing_policy=entry.resurfacing_policy,
            affected_milestone_ids=tuple(entry.affected_milestone_ids),
            last_touched_at=entry.last_touched_at,
        )
        for entry in extraction_result.decision_asks
    )
    return ClaimExtractionResult(
        claims=normalized_claims,
        decision_asks=normalized_asks,
        warnings=tuple(extraction_result.warnings),
    )


def _build_claim_extraction_calibration_record(
    *,
    program_id: str,
    issue_number: int,
    claim_date: date,
    ai_extracted: ClaimExtractionResult,
    regex_extracted: ClaimExtractionResult,
) -> ClaimExtractionCalibrationRecord:
    ai_keys = {_claim_calibration_key(entry) for entry in ai_extracted.claims}
    regex_keys = {_claim_calibration_key(entry) for entry in regex_extracted.claims}
    shared_count = len(ai_keys & regex_keys)
    union_count = len(ai_keys | regex_keys)
    agreement_rate = 1.0 if union_count == 0 else round(shared_count / union_count, 4)
    return ClaimExtractionCalibrationRecord(
        program_id=program_id,
        issue_number=issue_number,
        recorded_at=datetime(claim_date.year, claim_date.month, claim_date.day, tzinfo=timezone.utc),
        mode="calibration",
        ai_claim_count=len(ai_keys),
        regex_claim_count=len(regex_keys),
        shared_claim_count=shared_count,
        ai_only_count=len(ai_keys - regex_keys),
        regex_only_count=len(regex_keys - ai_keys),
        agreement_rate=agreement_rate,
    )


def _claim_calibration_key(entry: ClaimEntry) -> tuple[str, tuple[str, ...], str | None]:
    return (
        _normalize_text(entry.text).lower(),
        tuple(sorted(entry.entity_refs)),
        entry.due_date.isoformat() if entry.due_date is not None else None,
    )


def _is_duplicate_claim(candidate: ClaimEntry, existing: tuple[ClaimEntry, ...]) -> bool:
    for entry in existing:
        if set(candidate.entity_refs) != set(entry.entity_refs):
            continue
        if not _due_dates_are_similar(candidate.due_date, entry.due_date):
            continue
        if SequenceMatcher(None, _normalize_text(candidate.text).lower(), _normalize_text(entry.text).lower()).ratio() >= _SIMILARITY_THRESHOLD:
            return True
    return False


def _is_duplicate_decision_ask(candidate: DecisionAsk, existing: tuple[DecisionAsk, ...]) -> bool:
    for entry in existing:
        if set(candidate.entity_refs) != set(entry.entity_refs):
            continue
        if SequenceMatcher(None, _normalize_text(candidate.text).lower(), _normalize_text(entry.text).lower()).ratio() >= _SIMILARITY_THRESHOLD:
            return True
    return False


def _due_dates_are_similar(left: date | None, right: date | None) -> bool:
    if left is None or right is None:
        return left == right
    return abs((left - right).days) <= _DUE_DATE_DEDUP_WINDOW_DAYS


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, default=_json_default) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)
    write_checksum_file(path)


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    entries: list[dict[str, Any]] = []
    valid_lines: list[str] = []
    invalid_found = False
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = parse_jsonl_line(line)
            except json.JSONDecodeError:
                invalid_found = True
                continue
            if not isinstance(payload, dict):
                invalid_found = True
                continue
            entries.append(payload)
            valid_lines.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")

    if invalid_found:
        quarantine_and_rewrite_jsonl(path, valid_lines)

    return tuple(entries)


def read_claim_log_checksum(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> str | None:
    checksum_path = get_claims_checksum_path(program_id, programs_root)
    if not checksum_path.exists():
        return None
    value = checksum_path.read_text(encoding="utf-8").strip()
    return value or None


def claim_log_checksum_matches(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> bool | None:
    claims_path = get_claims_path(program_id, programs_root)
    checksum_path = get_claims_checksum_path(program_id, programs_root)
    return jsonl_checksum_matches(claims_path, checksum_path)


def _claim_to_record(entry: ClaimEntry) -> dict[str, Any]:
    return {
        "record_type": "claim",
        "id": entry.id,
        "program_id": entry.program_id,
        "edition_id": entry.edition_id,
        "issue_number": entry.issue_number,
        "workstream_id": entry.workstream_id,
        "text": entry.text,
        "entity_refs": list(entry.entity_refs),
        "claim_date": entry.claim_date.isoformat(),
        "owner_alias": entry.owner_alias,
        "due_date": entry.due_date.isoformat() if entry.due_date is not None else None,
        "status": entry.status,
        "contradiction_status": entry.contradiction_status,
        "source_confidence_tier": entry.source_confidence_tier,
        "last_validated_date": entry.last_validated_date.isoformat() if entry.last_validated_date is not None else None,
    }


def _claim_from_record(record: dict[str, Any]) -> ClaimEntry:
    raw_lvd = record.get("last_validated_date")
    contradiction_status = _required_string(record.get("contradiction_status"), field_name="contradiction_status")
    if contradiction_status == "none":
        contradiction_status = "ok"
    if contradiction_status not in ("ok", "contradicted", "unresolved"):
        raise ValueError(f"Unsupported contradiction_status {contradiction_status!r}")
    source_confidence_tier = _required_string(record.get("source_confidence_tier"), field_name="source_confidence_tier")
    if source_confidence_tier == "grounded":
        source_confidence_tier = "high"
    if source_confidence_tier not in ("high", "medium", "low"):
        raise ValueError(f"Unsupported source_confidence_tier {source_confidence_tier!r}")
    return ClaimEntry(
        id=_required_string(record["id"], field_name="id"),
        program_id=_required_string(record["program_id"], field_name="program_id"),
        edition_id=_required_string(record["edition_id"], field_name="edition_id"),
        issue_number=_required_int(record["issue_number"], field_name="issue_number"),
        workstream_id=_optional_string(record.get("workstream_id"), field_name="workstream_id"),
        text=_required_string(record["text"], field_name="text"),
        entity_refs=_string_tuple(record.get("entity_refs"), field_name="entity_refs"),
        claim_date=_parse_date(record["claim_date"]),
        owner_alias=_optional_string(record.get("owner_alias"), field_name="owner_alias"),
        due_date=_parse_optional_date(record.get("due_date")),
        status="open",
        contradiction_status=cast(Literal["ok", "contradicted", "unresolved"], contradiction_status),
        source_confidence_tier=cast(Literal["high", "medium", "low"], source_confidence_tier),
        last_validated_date=_parse_optional_date(raw_lvd) if raw_lvd else None,
    )


def _decision_ask_to_record(entry: DecisionAsk) -> dict[str, Any]:
    return {
        "record_type": "decision_ask",
        "id": entry.id,
        "program_id": entry.program_id,
        "edition_id": entry.edition_id,
        "issue_number": entry.issue_number,
        "text": entry.text,
        "entity_refs": list(entry.entity_refs),
        "ask_date": entry.ask_date.isoformat(),
        "owner_alias": entry.owner_alias,
        "status": entry.status,
        "resolution": entry.resolution,
        "expiry_date": entry.expiry_date.isoformat() if entry.expiry_date is not None else None,
        "resurfacing_policy": _resurfacing_policy_to_record(entry.resurfacing_policy),
        "affected_milestone_ids": list(entry.affected_milestone_ids),
        "last_touched_at": entry.last_touched_at.astimezone(timezone.utc).isoformat() if entry.last_touched_at is not None else None,
    }


def _decision_ask_from_record(record: dict[str, Any]) -> DecisionAsk:
    _status_val = _required_string(record.get("status"), field_name="status")
    if _status_val not in ("open", "resolved", "deferred"):
        raise ValueError(f"Unsupported decision ask status {_status_val!r}")
    return DecisionAsk(
        id=_required_string(record["id"], field_name="id"),
        program_id=_required_string(record["program_id"], field_name="program_id"),
        edition_id=_required_string(record["edition_id"], field_name="edition_id"),
        issue_number=_required_int(record["issue_number"], field_name="issue_number"),
        text=_required_string(record["text"], field_name="text"),
        entity_refs=_string_tuple(record.get("entity_refs"), field_name="entity_refs"),
        ask_date=_parse_date(record["ask_date"]),
        owner_alias=_optional_string(record.get("owner_alias"), field_name="owner_alias"),
        status=cast(Literal["open", "resolved", "deferred"], _status_val),
        resolution=_optional_string(record.get("resolution"), field_name="resolution"),
        expiry_date=_parse_optional_date(record.get("expiry_date")),
        resurfacing_policy=_resurfacing_policy_from_record(record.get("resurfacing_policy")),
        affected_milestone_ids=_string_tuple(record.get("affected_milestone_ids"), field_name="affected_milestone_ids"),
        last_touched_at=_parse_optional_datetime(record.get("last_touched_at")),
    )


def _status_update_to_record(update: ClaimStatusUpdate) -> dict[str, Any]:
    return {
        "record_type": update.record_type,
        "claim_id": update.claim_id,
        "new_status": update.new_status,
        "updated_at": update.updated_at.astimezone(timezone.utc).isoformat(),
        "updated_by": update.updated_by,
        "note": update.note,
    }


def _status_update_from_record(record: dict[str, Any]) -> ClaimStatusUpdate:
    _new_status_val = _required_string(record["new_status"], field_name="new_status")
    if _new_status_val == "closed":
        _new_status_val = "resolved"
    if _new_status_val not in ("open", "met", "contradicted", "stale", "deferred", "resolved"):
        raise ValueError(f"Unsupported claim status '{_new_status_val}'")
    return ClaimStatusUpdate(
        claim_id=_required_string(record["claim_id"], field_name="claim_id"),
        new_status=cast(Literal["open", "met", "contradicted", "stale", "deferred", "resolved"], _new_status_val),
        updated_at=_parse_datetime(record["updated_at"]),
        updated_by=_required_string(record["updated_by"], field_name="updated_by"),
        note=_optional_string(record.get("note"), field_name="note"),
    )


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO timestamp string, found {type(value).__name__}.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include timezone information.")
    return parsed.astimezone(timezone.utc)


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_datetime(value)


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _resurfacing_policy_to_record(policy: ResurfacingPolicy | None) -> dict[str, int] | None:
    if policy is None:
        return None
    return {
        "watch_days": policy.watch_days,
        "nudge_days": policy.nudge_days,
        "escalate_days": policy.escalate_days,
    }


def _resurfacing_policy_from_record(value: Any) -> ResurfacingPolicy | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Decision ask resurfacing_policy must be an object.")
    return ResurfacingPolicy(
        watch_days=_int_or_default(value.get("watch_days"), field_name="resurfacing_policy.watch_days", default=7),
        nudge_days=_int_or_default(value.get("nudge_days"), field_name="resurfacing_policy.nudge_days", default=14),
        escalate_days=_int_or_default(value.get("escalate_days"), field_name="resurfacing_policy.escalate_days", default=21),
    )


def _parse_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO date string, found {type(value).__name__}.")
    return date.fromisoformat(value)


def _parse_optional_date(value: Any) -> date | None:
    if value is None:
        return None
    return _parse_date(value)


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    text = value.strip()
    return text or None


def _string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list of strings")
    parsed: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise TypeError(f"{field_name} must contain strings only")
        parsed.append(entry)
    return tuple(parsed)


def _int_or_default(value: Any, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")