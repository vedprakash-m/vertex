from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from typing import Any

from src.core.models_v2 import Signal
from src.core.signal_ranking import signal_source_family


_SEMANTIC_DEDUP_MIN_MATURITY_LEVEL = 2
_SEMANTIC_DEDUP_TEXT_SIMILARITY = 0.94
_SEMANTIC_DEDUP_TOKEN_SIMILARITY = 0.85
_SEMANTIC_DEDUP_MIN_TEXT_LENGTH = 48
_SEMANTIC_DEDUP_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class DedupDropEvent:
    dropped_fingerprint: str
    kept_fingerprint: str
    reason: str  # "exact_fingerprint" | "semantic_near_duplicate"
    similarity_score: float  # 0.0 for exact matches, [0,1] for semantic


@dataclass(frozen=True, slots=True)
class DedupResult:
    signals: tuple[Signal, ...]
    drop_log: tuple[DedupDropEvent, ...]


def signal_fingerprint(signal: Signal) -> str:
    metadata = signal.metadata or {}
    source = signal.source.strip().lower()
    entity_refs = tuple(sorted(str(entry).strip() for entry in signal.entity_refs))

    if source == "ado/odata":
        return _join_fingerprint(
            source,
            signal.raw_ref,
            entity_refs,
            metadata.get("field"),
            metadata.get("prior"),
            metadata.get("current"),
        )
    if source == "ado/revision":
        return _join_fingerprint(
            source,
            signal.raw_ref,
            entity_refs,
            metadata.get("revision_number") or metadata.get("rev"),
        )
    if source == "kusto":
        return _join_fingerprint(
            source,
            metadata.get("query_id"),
            entity_refs,
            metadata.get("event_timestamp"),
        )
    if source == "kusto_kpi":
        return _join_fingerprint(
            source,
            metadata.get("query_id"),
            entity_refs,
            metadata.get("event_timestamp"),
        )
    if source.startswith("workiq/"):
        return _join_fingerprint(source, metadata.get("message_id"), entity_refs)
    if signal_source_family(source) == "icm":
        return _join_fingerprint("icm", metadata.get("incident_id"), entity_refs)
    if source == "manual":
        return _join_fingerprint(source, _text_hash(signal.text), entity_refs)
    if source == "vertex/ado_update":
        return _join_fingerprint(
            source,
            metadata.get("proposal_id"),
            metadata.get("work_item_id"),
            metadata.get("update_type"),
        )
    if source == "ado/comment":
        return _join_fingerprint(source, metadata.get("work_item_id"), metadata.get("comment_id"))
    if source == "ado/wiql":
        return _join_fingerprint(source, metadata.get("query_id"), entity_refs, metadata.get("date"))
    if source == "ado/pr":
        return _join_fingerprint(
            source,
            signal.program_id,
            signal.workstream_id,
            metadata.get("repository_ids"),
            metadata.get("date"),
        )
    if source == "vertex/freshness":
        return _join_fingerprint(
            source,
            signal.program_id,
            metadata.get("work_item_id"),
            metadata.get("finding_type"),
            metadata.get("date"),
        )
    if source == "vertex/catchup":
        return _join_fingerprint(
            source,
            signal.program_id,
            _coerce_work_item_id(signal),
            _infer_catchup_kind(signal),
            signal.timestamp.astimezone(timezone.utc).date().isoformat() if signal.timestamp.tzinfo is not None else signal.timestamp.date().isoformat(),
            _text_hash(str(metadata.get("current") or "unset")),
        )

    return _join_fingerprint(source, signal.raw_ref, entity_refs, _stable_json(metadata), signal.text)


def is_duplicate_signal(signal: Signal, existing_signals: Iterable[Signal]) -> bool:
    fingerprint = signal_fingerprint(signal)
    return any(signal_fingerprint(existing) == fingerprint for existing in existing_signals)


def dedupe_signals(
    candidates: Iterable[Signal],
    existing_signals: Iterable[Signal] = (),
    *,
    program_maturity_level: int = 0,
) -> tuple[Signal, ...]:
    return _dedupe_core(candidates, existing_signals, program_maturity_level=program_maturity_level).signals


def dedupe_signals_with_audit(
    candidates: Iterable[Signal],
    existing_signals: Iterable[Signal] = (),
    *,
    program_maturity_level: int = 0,
) -> DedupResult:
    """Like dedupe_signals() but also returns a drop log for FR-SG-36 audit trail."""
    return _dedupe_core(candidates, existing_signals, program_maturity_level=program_maturity_level)


def _dedupe_core(
    candidates: Iterable[Signal],
    existing_signals: Iterable[Signal],
    *,
    program_maturity_level: int,
) -> DedupResult:
    existing = tuple(existing_signals)
    seen: dict[str, str] = {}  # fingerprint → fingerprint (self-mapping for seen signals)
    fingerprint_to_fp: dict[str, str] = {}  # maps fingerprint → representative fingerprint
    drop_log: list[DedupDropEvent] = []
    accepted: list[Signal] = []

    for signal in existing:
        fp = signal_fingerprint(signal)
        seen[fp] = fp

    for candidate in candidates:
        fingerprint = signal_fingerprint(candidate)
        if fingerprint in seen:
            drop_log.append(DedupDropEvent(
                dropped_fingerprint=fingerprint,
                kept_fingerprint=seen[fingerprint],
                reason="exact_fingerprint",
                similarity_score=0.0,
            ))
            continue
        if _semantic_dedup_enabled(program_maturity_level):
            near_dup: Signal | None = None
            near_score = 0.0
            for prior in (*existing, *accepted):
                is_dup, score = _is_semantic_near_duplicate_with_score(candidate, prior)
                if is_dup:
                    near_dup = prior
                    near_score = score
                    break
            if near_dup is not None:
                drop_log.append(DedupDropEvent(
                    dropped_fingerprint=fingerprint,
                    kept_fingerprint=signal_fingerprint(near_dup),
                    reason="semantic_near_duplicate",
                    similarity_score=near_score,
                ))
                continue
        seen[fingerprint] = fingerprint
        accepted.append(candidate)

    return DedupResult(signals=tuple(accepted), drop_log=tuple(drop_log))


def _semantic_dedup_enabled(program_maturity_level: int) -> bool:
    return program_maturity_level >= _SEMANTIC_DEDUP_MIN_MATURITY_LEVEL


def _is_semantic_near_duplicate(candidate: Signal, existing: Signal) -> bool:
    is_dup, _ = _is_semantic_near_duplicate_with_score(candidate, existing)
    return is_dup


def _is_semantic_near_duplicate_with_score(candidate: Signal, existing: Signal) -> tuple[bool, float]:
    """Returns (is_near_dup, similarity_score). Score is 0.0 when not applicable."""
    if signal_source_family(candidate.source) != "workiq":
        return False, 0.0
    if signal_source_family(existing.source) != "workiq":
        return False, 0.0
    if candidate.program_id != existing.program_id:
        return False, 0.0
    if candidate.workstream_id != existing.workstream_id:
        return False, 0.0

    candidate_refs = _normalized_entity_refs(candidate)
    existing_refs = _normalized_entity_refs(existing)
    if not candidate_refs or candidate_refs != existing_refs:
        return False, 0.0

    age_delta_seconds = abs((candidate.timestamp - existing.timestamp).total_seconds())
    if age_delta_seconds > _SEMANTIC_DEDUP_MAX_AGE_SECONDS:
        return False, 0.0

    candidate_text = _normalized_text(candidate.text)
    existing_text = _normalized_text(existing.text)
    if min(len(candidate_text), len(existing_text)) < _SEMANTIC_DEDUP_MIN_TEXT_LENGTH:
        return False, 0.0

    text_similarity = SequenceMatcher(None, candidate_text, existing_text).ratio()
    if text_similarity >= _SEMANTIC_DEDUP_TEXT_SIMILARITY:
        return True, text_similarity

    token_sim = _token_similarity(candidate_text, existing_text)
    if token_sim >= _SEMANTIC_DEDUP_TOKEN_SIMILARITY:
        return True, token_sim

    return False, max(text_similarity, token_sim)


def _normalized_entity_refs(signal: Signal) -> tuple[str, ...]:
    return tuple(sorted(str(entry).strip().lower() for entry in signal.entity_refs if str(entry).strip()))


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).strip().lower()


def _token_similarity(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    return len(shared) / max(len(left_tokens), len(right_tokens))


def _join_fingerprint(*parts: Any) -> str:
    flattened = [_normalize_part(part) for part in parts]
    return "|".join(flattened)


def _normalize_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple):
        return ",".join(_normalize_part(entry) for entry in value)
    if isinstance(value, list):
        return ",".join(_normalize_part(entry) for entry in value)
    return str(value).strip().lower()


def _stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _text_hash(value: str) -> str:
    normalized = " ".join(value.split()).strip().lower().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _coerce_work_item_id(signal: Signal) -> str:
    metadata = signal.metadata or {}
    value = metadata.get("work_item_id")
    if value is not None:
        return str(value).strip().lower()
    for entity_ref in signal.entity_refs:
        text = str(entity_ref).strip()
        if text.upper().startswith("WI:"):
            return text.split(":", 1)[1].strip().lower()
    return ""


def _infer_catchup_kind(signal: Signal) -> str:
    metadata = signal.metadata or {}
    field_name = str(metadata.get("field") or "").strip()
    if field_name == "TargetDate":
        prior = _parse_date_like(metadata.get("prior"))
        current = _parse_date_like(metadata.get("current"))
        if prior is not None and current is not None and current > prior:
            return "eta_slip"
        return "eta_pull_in"
    if field_name == "AssignedTo":
        return "silent_owner_change"
    if field_name in {"State", "System.State"}:
        return "state_change"
    return "generic_change"


def _parse_date_like(value: object) -> date | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    for parser in (_parse_iso_datetime, _parse_iso_date):
        parsed = parser(normalized)
        if parsed is not None:
            return parsed
    return None


def _parse_iso_datetime(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None