"""Zone A reader for the WorkstreamEvidence store (``evidence_store.jsonl``).

Centralizes reconstruction of ``WorkstreamEvidence`` records from the JSONL
sidecar so the writer (``evidence_extraction_stage.persist_evidence``), the
doctor checks, and the report/blurb synthesis path all share one decode path
(there was previously a divergent copy in ``doctor_checks/context_checks.py``).

P4-0 (§17.8 Option A): records optionally carry ``_backing_signal_ids`` linking
the evidence to the journal Signal(s) that gate its approval. A record is
approved-for-synthesis when all backing signals for that source are in the approved set,
OR when no backing signals are recorded (backward-compatible legacy records
written before P4-0, e.g. by the ME-02 gather path prior to this wiring).

This module is Zone A: it imports only from ``src/core/`` (evidence_models,
models, models_v2, signal_review, jsonl_utils) — never from ``src/ai/`` or
``src/m365/``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from src.core.evidence_models import EtaRecord, SourceRef, VerificationState, WorkstreamEvidence
from src.core.jsonl_utils import parse_jsonl_line
from src.core.models import RiskLevel

_EVIDENCE_STORE_FILENAME = "evidence_store.jsonl"


def evidence_store_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "journal" / _EVIDENCE_STORE_FILENAME


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """A decoded evidence_store.jsonl row with its provenance links."""

    evidence: WorkstreamEvidence
    backing_signal_ids: tuple[str, ...]
    dedup_key: str | None


def load_evidence_records(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[EvidenceRecord, ...]:
    """Read and decode every evidence_store.jsonl row, in file order.

    Corrupt/unparseable lines are skipped (not raised) — this is a read path
    used by report synthesis and must degrade gracefully.
    """
    path = evidence_store_path(program_id, programs_root)
    if not path.exists():
        return ()
    records: list[EvidenceRecord] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = parse_jsonl_line(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        decoded = _decode_evidence_record(payload)
        if decoded is not None:
            records.append(decoded)
    return tuple(records)


def load_latest_evidence_by_lane(
    program_id: str,
    *,
    programs_root: Path,
) -> dict[str, WorkstreamEvidence]:
    """Most recent WorkstreamEvidence per lane (last row wins). For doctor/diagnostic use."""
    by_source = load_latest_evidence_by_source(program_id, programs_root=programs_root)
    return _aggregate_records_by_lane(by_source.values())


def load_latest_evidence_by_source(
    program_id: str,
    *,
    programs_root: Path,
) -> dict[tuple[str, str], EvidenceRecord]:
    """Latest revision per ``(lane_id, canonical_source_id)``.

    Legacy rows without a canonical source retain last-row-per-lane behavior.
    New rich WorkIQ rows must carry exactly one canonical source id.
    """

    latest: dict[tuple[str, str], EvidenceRecord] = {}
    for record in load_evidence_records(program_id, programs_root=programs_root):
        source_id = canonical_evidence_source_id(record.evidence) or "__legacy__"
        latest[(record.evidence.lane_id, source_id)] = record
    return latest


def load_approved_evidence_by_lane(
    program_id: str,
    *,
    programs_root: Path,
    approved_signal_ids: frozenset[str] | set[str] | None = None,
) -> dict[str, WorkstreamEvidence]:
    """P4-1 gate: latest evidence per lane, filtered by signal approval.

    When ``approved_signal_ids`` is None the gate is disabled (returns all latest
    evidence — used by doctor and backward-compatible callers). When provided, a
    record passes when:

    * it has no ``_backing_signal_ids`` (legacy record pre-P4-0), OR
    * all of its backing signal ids are in ``approved_signal_ids``.

    This is the §17.8 Option A enforcement point: unapproved ME-03 enrich evidence
    (whose backing PENDING signal has not been reviewed) is excluded from blurb
    synthesis, preserving the G-5 human-in-the-loop invariant.
    """
    latest = load_latest_evidence_by_source(program_id, programs_root=programs_root)
    if approved_signal_ids is None:
        return _aggregate_records_by_lane(latest.values())
    approved_set = frozenset(approved_signal_ids)
    approved_records: list[EvidenceRecord] = []
    for rec in latest.values():
        if not rec.backing_signal_ids:
            # Legacy record (pre-P4-0) — no approval link to check. Admit for
            # backward compatibility (these are ME-02 gather-path records).
            approved_records.append(rec)
            continue
        # Source-specific evidence is admitted only when every backing signal for
        # that source is approved. This prevents one approved source from leaking
        # unapproved material bundled into the same record.
        if all(sid in approved_set for sid in rec.backing_signal_ids):
            approved_records.append(
                replace(
                    rec,
                    evidence=replace(rec.evidence, verification_state=VerificationState.HUMAN_VERIFIED),
                )
            )
    return _aggregate_records_by_lane(approved_records)


def canonical_evidence_source_id(evidence: WorkstreamEvidence) -> str | None:
    ids = tuple(dict.fromkeys(ref.canonical_id for ref in evidence.source_refs if ref.canonical_id))
    return ids[0] if len(ids) == 1 else None


def aggregate_approved_sources(evidences: tuple[WorkstreamEvidence, ...]) -> WorkstreamEvidence:
    """Deterministically aggregate approved per-source evidence for one lane."""

    if not evidences:
        raise ValueError("At least one evidence source is required")
    lane_id = evidences[0].lane_id
    if any(evidence.lane_id != lane_id for evidence in evidences):
        raise ValueError("Cannot aggregate evidence from different lanes")
    ordered = tuple(sorted(evidences, key=lambda item: (item.synthesized_at, canonical_evidence_source_id(item) or "")))
    risk_rank = {RiskLevel.UNKNOWN: 0, RiskLevel.DONE: 1, RiskLevel.LOW: 2, RiskLevel.MEDIUM: 3, RiskLevel.HIGH: 4, RiskLevel.BLOCKED: 5}
    verification_rank = {
        VerificationState.UNVERIFIED: 0,
        VerificationState.MODEL_SELF_ATTESTED: 1,
        VerificationState.HUMAN_VERIFIED: 2,
        VerificationState.SOURCE_VERIFIED: 3,
    }
    privacy_rank = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}

    def _dedupe(values):
        return tuple(dict.fromkeys(values))

    etas = []
    eta_keys: set[tuple] = set()
    for evidence in ordered:
        for eta in evidence.etas:
            key = (eta.label, eta.eta_date, eta.owner, eta.status, eta.ado_id)
            if key not in eta_keys:
                eta_keys.add(key)
                etas.append(eta)
    refs = []
    ref_keys: set[tuple] = set()
    for evidence in ordered:
        for ref in evidence.source_refs:
            ref_key = (ref.source_type, ref.canonical_id, ref.description, ref.source_date, ref.author, ref.permalink, ref.extraction_method)
            if ref_key not in ref_keys:
                ref_keys.add(ref_key)
                refs.append(ref)
    stale_values = [evidence.stale_after for evidence in ordered if evidence.stale_after is not None]
    verification = min((evidence.verification_state for evidence in ordered), key=lambda value: verification_rank[value])
    privacy = max((evidence.privacy_classification for evidence in ordered), key=lambda value: privacy_rank.get(value, 1))
    return WorkstreamEvidence(
        lane_id=lane_id,
        synthesized_at=max(evidence.synthesized_at for evidence in ordered),
        risk_level=max((evidence.risk_level for evidence in ordered), key=lambda value: risk_rank[value]),
        etas=tuple(etas),
        blocking_items=_dedupe(item for evidence in ordered for item in evidence.blocking_items),
        owners=_dedupe(owner for evidence in ordered for owner in evidence.owners),
        source_refs=tuple(refs),
        raw_excerpts=_dedupe(excerpt for evidence in ordered for excerpt in evidence.raw_excerpts),
        confidence=min(evidence.confidence for evidence in ordered),
        narrative_summary=ordered[-1].narrative_summary,
        stale_after=min(stale_values) if stale_values else None,
        lt_deck_alignment=ordered[-1].lt_deck_alignment,
        verification_state=verification,
        privacy_classification=privacy,
    )


def _aggregate_records_by_lane(records) -> dict[str, WorkstreamEvidence]:
    grouped: dict[str, list[WorkstreamEvidence]] = {}
    for record in records:
        grouped.setdefault(record.evidence.lane_id, []).append(record.evidence)
    return {lane_id: aggregate_approved_sources(tuple(evidences)) for lane_id, evidences in grouped.items()}


def _decode_evidence_record(payload: dict) -> EvidenceRecord | None:
    try:
        lane_id = payload["lane_id"]
        synthesized_at = datetime.fromisoformat(payload["synthesized_at"])
        risk_level = RiskLevel.from_string(payload.get("risk_level"))
        etas = tuple(
            EtaRecord(
                label=e.get("label", ""),
                eta_date=date.fromisoformat(e["eta_date"]),
                owner=e.get("owner"),
                status=e.get("status", "open"),
                ado_id=e.get("ado_id"),
            )
            for e in (payload.get("etas") or [])
            if isinstance(e, dict) and "eta_date" in e
        )
        source_refs = tuple(
            SourceRef(
                source_type=r.get("source_type", "manual"),
                description=r.get("description", ""),
                source_date=date.fromisoformat(r["source_date"]) if r.get("source_date") else None,
                author=r.get("author"),
                permalink=r.get("permalink"),
                extraction_method=r.get("extraction_method"),
                canonical_id=r.get("canonical_id"),
            )
            for r in (payload.get("source_refs") or [])
            if isinstance(r, dict)
        )
        stale_after_raw = payload.get("stale_after")
        stale_after = datetime.fromisoformat(stale_after_raw) if stale_after_raw else None
        lt_deck_alignment_raw = payload.get("lt_deck_alignment")
        lt_deck_alignment = lt_deck_alignment_raw if lt_deck_alignment_raw in ("aligned", "diverged", "lt_only") else None
        evidence = WorkstreamEvidence(
            lane_id=lane_id,
            synthesized_at=synthesized_at,
            risk_level=risk_level,
            etas=etas,
            blocking_items=tuple(payload.get("blocking_items") or []),
            owners=tuple(payload.get("owners") or []),
            source_refs=source_refs,
            raw_excerpts=tuple(payload.get("raw_excerpts") or []),
            confidence=float(payload.get("confidence", 0.0)),
            narrative_summary=payload.get("narrative_summary", "") or "",
            stale_after=stale_after,
            lt_deck_alignment=lt_deck_alignment,
            verification_state=VerificationState(payload.get("verification_state", "unverified")),
            privacy_classification=str(payload.get("privacy_classification") or "internal"),
        )
    except (KeyError, ValueError, TypeError):
        return None
    backing = payload.get("_backing_signal_ids") or []
    backing_signal_ids = tuple(backing) if isinstance(backing, list) else ()
    dedup_key = payload.get("_dedup_key")
    return EvidenceRecord(
        evidence=evidence,
        backing_signal_ids=backing_signal_ids,
        dedup_key=dedup_key if isinstance(dedup_key, str) else None,
    )
