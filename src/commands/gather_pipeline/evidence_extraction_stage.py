"""Transcript-based WorkstreamEvidence extraction stage (ME-02, Zone B).

Extracts structured evidence from transcript Signal objects using ContentExtractionAgent.
Email signals are intentionally excluded — their 255-char preview is insufficient for
reliable extraction. Use `vertex enrich --workiq` (ME-03) for email bodies.

Zone boundary: This module is Zone B (src/commands/ calls src/ai/).
It must not import from src/m365/.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from src.ai.content_extractor import ContentExtractionAgent, ExtractionContext
from src.core.evidence_models import WorkstreamEvidence
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.models_v2 import Signal

log = logging.getLogger(__name__)

_MIN_TRANSCRIPT_CHARS = 200
_EVIDENCE_STORE_FILENAME = "evidence_store.jsonl"


def _evidence_store_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "journal" / _EVIDENCE_STORE_FILENAME


def _evidence_dedup_key(evidence: WorkstreamEvidence) -> str:
    """P4-3: stable content fingerprint for deduplication.

    Excludes `synthesized_at`/`stale_after` (re-runs produce different timestamps
    but identical content → should dedup). Includes all content-bearing fields so
    a genuinely different extraction (different risk/etas/narrative) does NOT dedup.
    """
    import hashlib

    canonical = {
        "lane_id": evidence.lane_id,
        "risk_level": evidence.risk_level.value,
        "confidence": round(evidence.confidence, 4),
        "etas": [
            {
                "label": e.label,
                "eta_date": e.eta_date.isoformat(),
                "owner": e.owner,
                "status": e.status,
                "ado_id": e.ado_id,
            }
            for e in evidence.etas
        ],
        "blocking_items": list(evidence.blocking_items),
        "owners": list(evidence.owners),
        "source_refs": [
            {
                "source_type": r.source_type,
                "description": r.description,
                "source_date": r.source_date.isoformat() if r.source_date else None,
                "author": r.author,
                "permalink": r.permalink,
                "extraction_method": r.extraction_method,
                "canonical_id": r.canonical_id,
            }
            for r in evidence.source_refs
        ],
        "raw_excerpts": list(evidence.raw_excerpts),
        "narrative_summary": evidence.narrative_summary,
        "verification_state": evidence.verification_state.value,
        "privacy_classification": evidence.privacy_classification,
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_evidence_extraction_stage(
    *,
    workiq_signals: Sequence[Signal],
    program_id: str,
    programs_root: Path,
    ask_ai_fn: Callable[[str], str],
    as_of: datetime,
    dry_run: bool = False,
) -> dict[str, WorkstreamEvidence]:
    """Extract WorkstreamEvidence from transcript signals.

    Returns a dict of {lane_id: WorkstreamEvidence} for lanes with extracted evidence.
    Skips email signals (insufficient content). Skips lanes with no transcripts.
    Never raises — failures are logged and the lane is skipped.
    """
    transcript_signals = _filter_transcript_signals(workiq_signals)
    if not transcript_signals:
        return {}

    grouped = _group_by_lane(transcript_signals)
    agent = ContentExtractionAgent(ask_ai_fn=ask_ai_fn)
    results: dict[str, WorkstreamEvidence] = {}

    for lane_id, sigs in grouped.items():
        combined_text = "\n\n".join(s.text for s in sigs if s.text)
        if len(combined_text) < _MIN_TRANSCRIPT_CHARS:
            log.debug(
                "Skipping lane %s — combined transcript text too short (%d chars)",
                lane_id, len(combined_text),
            )
            continue

        # Capture enrichments now so ME-05 quality recording can reference it below.
        enrichments = tuple(_signals_as_enrichments(sigs))

        # ExtractionContext requires lane_why/lane_what; not available during gather
        # (registry is not re-read here). Pass empty strings — ContentExtractionAgent
        # still extracts from body_text even without why/what context.
        ctx = ExtractionContext(
            lane_id=lane_id,
            lane_name=lane_id,   # registry display name not available here
            lane_why="",
            lane_what="",
            enrichments=enrichments,   # enrichments inside ctx, NOT a second arg to extract()
        )
        try:
            evidence = agent.extract(ctx)   # extract() takes only ctx; enrichments are in ctx
        except Exception as exc:  # noqa: BLE001
            log.warning("ContentExtractionAgent failed for lane %s: %s", lane_id, exc)
            continue
        if evidence is None:
            continue
        results[lane_id] = evidence
        if not dry_run:
            # P4-0: link this evidence to the transcript signals that produced it so
            # blurb synthesis (P4-1) can gate on signal approval (§17.8 Option A).
            backing_signal_ids = tuple(s.id for s in sigs if s.id)
            persist_evidence(
                evidence,
                program_id=program_id,
                programs_root=programs_root,
                backing_signal_ids=backing_signal_ids,
            )
            # ME-05: record quality metrics; enrichments is in scope from the capture above
            from src.core.evidence_quality import EvidenceQualityRecord, record_evidence_quality
            body_chars = sum(len(e.body_text or "") for e in enrichments)
            qrec = EvidenceQualityRecord(
                run_at=as_of,
                lane_id=lane_id,
                confidence=evidence.confidence,
                etas_found=len(evidence.etas),
                owners_found=len(evidence.owners),
                blocking_found=len(evidence.blocking_items),
                body_text_chars=body_chars,
                source_type="transcript",
                extractor="ContentExtractionAgent",
            )
            try:
                record_evidence_quality(qrec, program_id=program_id, programs_root=programs_root)
            except Exception as qexc:  # noqa: BLE001
                log.warning("Failed to record evidence quality for lane %s: %s", lane_id, qexc)

    return results


def _filter_transcript_signals(signals: Sequence[Signal]) -> list[Signal]:
    return [
        s for s in signals
        if (s.metadata or {}).get("source_type") == "transcript"
        and s.workstream_id is not None
        and s.text
    ]


def _group_by_lane(signals: list[Signal]) -> dict[str, list[Signal]]:
    grouped: dict[str, list[Signal]] = defaultdict(list)
    for sig in signals:
        if sig.workstream_id:
            grouped[sig.workstream_id].append(sig)
    return dict(grouped)


def _signals_as_enrichments(signals: list[Signal]) -> list:
    """Convert Signal objects to real Enrichment objects for ExtractionContext.enrichments.

    All required Enrichment fields are populated: source_id and author are mandatory.
    source="transcript" is the only valid Literal value for meeting transcripts.
    """
    from src.core.models import Enrichment

    enrichments = []
    for sig in signals:
        if not sig.text:
            continue
        enrichments.append(Enrichment(
            source="transcript",
            source_id=sig.raw_ref or sig.id,                           # required; was source_ref= (wrong)
            author=(sig.metadata or {}).get("sender_alias", "unknown"), # required; was absent
            timestamp=sig.timestamp,
            excerpt=sig.text[:120],
            permalink=None,
            body_text=sig.text,   # full transcript content for extraction
        ))
    return enrichments


def persist_evidence(
    evidence: WorkstreamEvidence,
    *,
    program_id: str,
    programs_root: Path,
    backing_signal_ids: tuple[str, ...] = (),
) -> bool:
    """Write one WorkstreamEvidence record to evidence_store.jsonl (deduplicated).

    Public name (no leading underscore) so ME-03 enrich.py can import it directly.
    Uses a custom JSON serializer to handle date objects nested inside EtaRecord.eta_date
    and SourceRef.source_date (which json.dumps cannot serialize natively).

    P4-3: skips the write and returns False when an identical content fingerprint
    (`_dedup_key`) already exists in the store — prevents unbounded growth on re-runs.
    Returns True when a new record was written.

    P4-0 (§17.8 Option A): ``backing_signal_ids`` links this evidence to the journal
    Signal(s) that gate its approval for blurb synthesis. The ME-02 gather path passes
    the transcript signal ids; the ME-03 enrich path passes the PENDING enrich signal id.
    Stored as ``_backing_signal_ids`` on the JSONL record (not on the dataclass).
    """
    from datetime import date as _date

    def _json_default(obj: object) -> str:
        if isinstance(obj, (datetime, _date)):
            return obj.isoformat()
        raise TypeError(f"Not JSON serializable: {type(obj)}")

    if any(ref.source_type.startswith("workiq_") and ref.canonical_id for ref in evidence.source_refs):
        from src.commands.workiq_evidence_safety import UnsafeWorkIQEvidenceError, sanitize_workiq_evidence

        try:
            evidence = sanitize_workiq_evidence(evidence)
        except UnsafeWorkIQEvidenceError as exc:
            quarantine_path = programs_root / program_id / "journal" / "evidence_quarantine.jsonl"
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            append_jsonl_line(
                quarantine_path,
                json.dumps(
                    {
                        "lane_id": evidence.lane_id,
                        "quarantined_at": datetime.now(timezone.utc).isoformat(),
                        "signal_types": list(exc.signal_types),
                        "source_ids": [ref.canonical_id for ref in evidence.source_refs if ref.canonical_id],
                    },
                    sort_keys=True,
                ) + "\n",
            )
            log.warning("Quarantined unsafe WorkIQ evidence for lane %s: %s", evidence.lane_id, exc)
            return False

    path = _evidence_store_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    dedup_key = _evidence_dedup_key(evidence)
    if _dedup_key_exists(path, dedup_key):
        log.debug(
            "Skipping duplicate evidence for lane %s (dedup_key=%s)",
            evidence.lane_id,
            dedup_key[:12],
        )
        return False

    data = asdict(evidence)
    data = {k: v for k, v in data.items() if v is not None}
    # Explicit datetime override (asdict does not convert datetime objects to strings)
    data["synthesized_at"] = evidence.synthesized_at.isoformat()
    data["verification_state"] = evidence.verification_state.value
    if evidence.stale_after:
        data["stale_after"] = evidence.stale_after.isoformat()
    data["_dedup_key"] = dedup_key
    if backing_signal_ids:
        data["_backing_signal_ids"] = list(backing_signal_ids)
    # _json_default handles nested date objects: etas[].eta_date, source_refs[].source_date
    append_jsonl_line(path, json.dumps(data, ensure_ascii=False, default=_json_default) + "\n")
    return True


def _dedup_key_exists(path: Path, dedup_key: str) -> bool:
    """Return True if ``dedup_key`` already appears in an existing evidence record."""
    if not path.exists():
        return False
    for record in read_jsonl_records(path):
        if record.get("_dedup_key") == dedup_key:
            return True
    return False
