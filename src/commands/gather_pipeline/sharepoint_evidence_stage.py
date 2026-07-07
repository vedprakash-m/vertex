"""SharePoint evidence stage: approved SharePoint signals → WorkstreamEvidence (SP3-1/SP3-2/SP3-3/SP3-4).

Deterministic marker-to-evidence mapping — no ContentExtractionAgent (no-double-AI rule, §8.2).
SharePoint signals already contain structured markers (Decision:/Risk:/Milestone:/Metric:).
This stage reads approved journal signals and maps them directly to WorkstreamEvidence.

Zone boundary: Zone A only. Must not import from src/ai/ or src/m365/.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from src.core.evidence_models import EtaRecord, SourceRef, WorkstreamEvidence
from src.core.models import RiskLevel
from src.core.models_v2 import ReviewPolicy, Signal

_LOG = logging.getLogger(__name__)

_BLOCKER_KEYWORDS = re.compile(
    r"\b(blocker|blocking|critical|escalat[ei]|sev[12]|severity [12]|red)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_OWNER_RE = re.compile(r"\b(?:owner|owned by|dri|contact)[:\s]+([A-Za-z][A-Za-z0-9._-]{1,40})\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SharePointEvidenceStageResult:
    lanes_updated: int
    evidence_written: int
    gaps: tuple[str, ...]


def run_sharepoint_evidence_stage(
    *,
    program_id: str,
    programs_root: Path,
    as_of: datetime,
    workstreams: "tuple[Any, ...]" = (),
) -> SharePointEvidenceStageResult:
    """SP3-1: Read approved SharePoint signals, map markers → WorkstreamEvidence.

    Deterministic mapping only — no AI extraction (§8.2 no-double-AI rule).
    Returns summary of lanes updated and evidence records written.

    Args:
        workstreams: Sequence of Workstream objects for per-workstream keyword routing (SP3-3).
    """
    from src.core.jsonl_utils import read_jsonl_records
    from src.commands.gather_pipeline.evidence_extraction_stage import persist_evidence

    signals_path = programs_root / program_id / "signals" / "journal.jsonl"
    if not signals_path.exists():
        return SharePointEvidenceStageResult(lanes_updated=0, evidence_written=0, gaps=())

    # Load approved SharePoint signals
    sp_signals: list[Signal] = []
    for record in read_jsonl_records(signals_path):
        if not isinstance(record, dict):
            continue
        source = record.get("source", "")
        if source not in ("sharepoint", "lt_deck"):
            continue
        review_policy = record.get("review_policy")
        if review_policy not in ("approved", "auto_approved"):
            continue
        try:
            sp_signals.append(_deserialize_signal(record))
        except (KeyError, ValueError) as exc:
            _LOG.debug("Skipping malformed SharePoint signal: %s", exc)

    if not sp_signals:
        return SharePointEvidenceStageResult(lanes_updated=0, evidence_written=0, gaps=())

    # Build workstream keyword map for routing (SP3-3)
    ws_keyword_map = _build_workstream_keyword_map(workstreams)

    # Group signals by routed workstream lane
    lane_signals: dict[str, list[Signal]] = {}
    for signal in sp_signals:
        lane_id = _route_signal_to_lane(signal, ws_keyword_map, program_id)
        lane_signals.setdefault(lane_id, []).append(signal)

    lanes_updated = 0
    evidence_written = 0
    gaps: list[str] = []

    # Also gather existing non-SharePoint evidence per lane for alignment computation (SP3-4)
    ado_risk_by_lane = _load_ado_risk_levels(programs_root, program_id)

    for lane_id, signals in lane_signals.items():
        try:
            evidence = _build_workstream_evidence_for_lane(
                lane_id=lane_id,
                signals=tuple(signals),
                as_of=as_of,
                ado_risk_level=ado_risk_by_lane.get(lane_id),
            )
            written = persist_evidence(
                evidence,
                program_id=program_id,
                programs_root=programs_root,
                backing_signal_ids=tuple(s.id for s in signals),
            )
            if written:
                evidence_written += 1
            lanes_updated += 1
        except Exception as exc:
            _LOG.warning("SharePoint evidence stage error for lane %s: %s", lane_id, exc)
            gaps.append(f"lane {lane_id}: {exc}")

    return SharePointEvidenceStageResult(
        lanes_updated=lanes_updated,
        evidence_written=evidence_written,
        gaps=tuple(gaps),
    )


# ── Marker mapping helpers ────────────────────────────────────────────────────


def _build_workstream_evidence_for_lane(
    *,
    lane_id: str,
    signals: tuple[Signal, ...],
    as_of: datetime,
    ado_risk_level: "RiskLevel | None",
) -> WorkstreamEvidence:
    """SP3-1: Deterministically map SharePoint signals → WorkstreamEvidence for one lane."""
    risk_markers: list[str] = []
    milestone_markers: list[str] = []
    decision_markers: list[str] = []
    metric_markers: list[str] = []
    owners: set[str] = set()
    etas: list[EtaRecord] = []
    source_refs: list[SourceRef] = []
    raw_excerpts: list[str] = []
    confidence = 0.0
    cadence_days = 30

    for signal in signals:
        text = signal.text or ""
        source_ref = _make_source_ref(signal)
        if source_ref:
            source_refs.append(source_ref)

        # Classify marker by prefix
        upper = text.upper()
        if upper.startswith("RISK:") or upper.startswith("RISK "):
            risk_markers.append(text)
        elif upper.startswith("MILESTONE:") or upper.startswith("MILESTONE "):
            milestone_markers.append(text)
            eta = _parse_eta_from_milestone(text)
            if eta:
                etas.append(eta)
        elif upper.startswith("DECISION:") or upper.startswith("DECISION "):
            decision_markers.append(text)
        elif upper.startswith("METRIC:") or upper.startswith("METRIC "):
            metric_markers.append(text)

        # Extract owners
        for match in _OWNER_RE.finditer(text):
            owners.add(match.group(1))

        raw_excerpts.append(text[:200])
        # Confidence: max across signals (backfill = HIGH → 0.90, WorkIQ = MEDIUM → 0.75)
        from src.core.models_v2 import Confidence as ConfEnum
        sig_confidence = 0.90 if signal.confidence == ConfEnum.HIGH else 0.75
        confidence = max(confidence, sig_confidence)

        # cadence_days from metadata
        meta = signal.metadata or {}
        if isinstance(meta.get("cadence_days"), int):
            cadence_days = meta["cadence_days"]

    # Derive risk_level: any Risk: marker → at least MEDIUM; blocker keyword → HIGH
    risk_level = _derive_risk_level(risk_markers)

    # Blocking items: Risk: markers with blocker keywords
    blocking_items = tuple(
        marker
        for marker in risk_markers
        if _BLOCKER_KEYWORDS.search(marker)
    )

    # Narrative: top 3 markers deterministically (§9.2 no ContentExtractionAgent)
    top_markers = (risk_markers + decision_markers + milestone_markers)[:3]
    narrative_summary = "\n".join(
        f"{m.split(':', 1)[0]}: {m.split(':', 1)[1].strip()}" if ":" in m else m
        for m in top_markers
    ) or "No SharePoint evidence."

    # SP3-4: Compute lt_deck_alignment
    lt_deck_alignment = _compute_lt_deck_alignment(risk_markers, ado_risk_level)

    stale_after = as_of + timedelta(days=cadence_days)

    return WorkstreamEvidence(
        lane_id=lane_id,
        synthesized_at=as_of,
        risk_level=risk_level,
        etas=tuple(etas),
        blocking_items=blocking_items,
        owners=tuple(sorted(owners)),
        source_refs=tuple(source_refs),
        raw_excerpts=tuple(raw_excerpts[:3]),
        confidence=confidence if confidence > 0 else 0.75,
        narrative_summary=narrative_summary,
        stale_after=stale_after,
        lt_deck_alignment=lt_deck_alignment,
    )


def _derive_risk_level(risk_markers: list[str]) -> RiskLevel:
    """SP3-1: Derive risk_level from Risk: markers.

    0 markers → UNKNOWN; any marker → MEDIUM; blocker/critical keyword → HIGH.
    """
    if not risk_markers:
        return RiskLevel.UNKNOWN
    for marker in risk_markers:
        if _BLOCKER_KEYWORDS.search(marker):
            return RiskLevel.HIGH
    return RiskLevel.MEDIUM


def _compute_lt_deck_alignment(
    risk_markers: list[str],
    ado_risk_level: "RiskLevel | None",
) -> "Literal['aligned', 'diverged', 'lt_only'] | None":
    """SP3-4: Compute lt_deck_alignment for a workstream lane.

    Algorithm from §9.3:
    - 0 Risk: markers → LT deck says GREEN
    - 1+ markers, no blocker → LT deck says YELLOW
    - 1+ markers with blocker/critical → LT deck says RED

    Alignment:
    - If both agree (both GREEN, or both YELLOW/RED) → "aligned"
    - If LT deck is YELLOW/RED but ADO/WorkIQ is GREEN → "diverged"
    - If LT deck has evidence but no ADO/WorkIQ evidence → "lt_only"
    - If no LT deck evidence for this lane → None (caller passes ado_risk_level=None if no LT deck)
    """
    if not risk_markers and ado_risk_level is None:
        return None  # no LT deck evidence for this lane at all

    # Derive LT deck risk view
    lt_risk = _derive_risk_level(risk_markers)

    if ado_risk_level is None:
        return "lt_only"

    # Map to simple 3-tier for comparison
    def _tier(r: RiskLevel) -> int:
        if r in (RiskLevel.HIGH, RiskLevel.BLOCKED):
            return 2
        if r == RiskLevel.MEDIUM:
            return 1
        return 0  # LOW / DONE / UNKNOWN → GREEN

    lt_tier = _tier(lt_risk)
    ado_tier = _tier(ado_risk_level)

    if lt_tier == 0 and ado_tier == 0:
        return "aligned"
    if lt_tier > 0 and ado_tier > 0:
        return "aligned"
    if lt_tier > 0 and ado_tier == 0:
        return "diverged"
    # LT deck is GREEN but ADO shows risk — not technically a divergence (ADO is more detailed)
    return "aligned"


def _parse_eta_from_milestone(marker: str) -> EtaRecord | None:
    """Extract an EtaRecord from a Milestone: marker text."""
    date_match = _DATE_RE.search(marker)
    if not date_match:
        return None
    try:
        eta_date = date.fromisoformat(date_match.group(1))
    except ValueError:
        return None
    # Extract label: text after "Milestone:" prefix up to date
    label_raw = re.sub(r"^[Mm]ilestone:\s*", "", marker)
    label = label_raw.split(",")[0].strip()[:80] or "Milestone"
    # Derive status from keywords
    status = "open"
    if re.search(r"\b(done|complete|shipped|closed|met)\b", marker, re.IGNORECASE):
        status = "closed"
    elif re.search(r"\b(at risk|miss|blocked|slip)\b", marker, re.IGNORECASE):
        status = "missed"
    owner_match = _OWNER_RE.search(marker)
    owner = owner_match.group(1) if owner_match else None
    return EtaRecord(label=label, eta_date=eta_date, owner=owner, status=status)


def _make_source_ref(signal: Signal) -> SourceRef | None:
    """SP3-1/§8.2: Build evidence_models.SourceRef adapter from a SharePoint signal."""
    meta = signal.metadata or {}
    source = signal.source or "sharepoint"

    if source == "lt_deck":
        source_type: str = "lt_deck"
    else:
        source_type = "sharepoint_docx"

    # Extract source_date from signal timestamp
    source_date = signal.timestamp.date() if signal.timestamp else None

    description = meta.get("doc_title") or meta.get("doc_id") or source
    permalink = meta.get("doc_url")

    try:
        return SourceRef(
            source_type=source_type,  # type: ignore[arg-type]
            description=str(description),
            source_date=source_date,
            author=None,
            permalink=str(permalink) if permalink else None,
        )
    except Exception:
        return None


# ── Workstream routing (SP3-3) ────────────────────────────────────────────────


def _build_workstream_keyword_map(workstreams: "tuple[Any, ...]") -> "dict[str, list[str]]":
    """Build {workstream_id: [keyword, ...]} map from Workstream.aliases + name.

    Keyword matching uses word boundaries (§SP3-3 regex requirement).
    """
    ws_keywords: dict[str, list[str]] = {}
    for ws in workstreams:
        ws_id = getattr(ws, "id", None)
        if not ws_id:
            continue
        keywords = []
        name = getattr(ws, "name", None)
        if name:
            keywords.append(str(name).lower())
        for alias in getattr(ws, "aliases", ()):
            if alias:
                keywords.append(str(alias).lower())
        if keywords:
            ws_keywords[ws_id] = keywords
    return ws_keywords


def _route_signal_to_lane(
    signal: Signal,
    ws_keyword_map: dict[str, list[str]],
    program_id: str,
) -> str:
    """SP3-3: Route a SharePoint signal to a workstream lane using keyword matching.

    Explicit routing (signal.workstream_id already set) takes priority.
    Falls back to keyword matching against signal text.
    Unrouted signals → umbrella lane (program_id).
    """
    # Prefer explicit workstream_id if not the umbrella program_id
    if signal.workstream_id and signal.workstream_id != program_id:
        return signal.workstream_id

    text = (signal.text or "").lower()
    for ws_id, keywords in ws_keyword_map.items():
        for keyword in keywords:
            # SP3-3: word boundary matching to prevent false positives
            pattern = r"\b" + re.escape(keyword) + r"\b"
            if re.search(pattern, text):
                return ws_id

    return program_id  # umbrella lane


# ── ADO risk level loading for alignment ─────────────────────────────────────


def _load_ado_risk_levels(programs_root: Path, program_id: str) -> "dict[str, RiskLevel]":
    """SP3-4: Load existing ADO/WorkIQ risk levels from evidence_store.jsonl for alignment computation."""
    from src.core.jsonl_utils import read_jsonl_records

    evidence_path = programs_root / program_id / "journal" / "evidence_store.jsonl"
    if not evidence_path.exists():
        return {}

    risk_by_lane: dict[str, RiskLevel] = {}
    for record in read_jsonl_records(evidence_path):
        if not isinstance(record, dict):
            continue
        # Skip SharePoint-sourced records (we want ADO/WorkIQ risk for comparison)
        source_refs = record.get("source_refs", [])
        is_sp = any(
            isinstance(r, dict) and r.get("source_type", "").startswith(("lt_deck", "sharepoint"))
            for r in source_refs
        )
        if is_sp:
            continue
        lane_id = record.get("lane_id")
        risk_raw = record.get("risk_level")
        if lane_id and risk_raw:
            try:
                risk_by_lane[lane_id] = RiskLevel(risk_raw)
            except ValueError:
                pass

    return risk_by_lane


# ── Signal deserialization helper ─────────────────────────────────────────────


def _deserialize_signal(record: dict[str, Any]) -> Signal:
    """Minimal Signal deserialization from journal JSONL record for evidence stage."""
    from src.core.models_v2 import Confidence, ReviewPolicy as _ReviewPolicy

    ts_raw = record.get("timestamp") or record.get("detected_at") or ""
    try:
        if ts_raw.endswith("Z"):
            ts_raw = ts_raw[:-1] + "+00:00"
        timestamp = datetime.fromisoformat(ts_raw)
    except (ValueError, AttributeError):
        timestamp = datetime.now(timezone.utc)

    confidence_raw = record.get("confidence", "medium")
    try:
        confidence = Confidence(confidence_raw)
    except ValueError:
        confidence = Confidence.MEDIUM

    return Signal(
        id=str(record.get("id") or record.get("signal_id") or ""),
        timestamp=timestamp,
        source=str(record.get("source", "")),
        program_id=str(record.get("program_id", "")),
        workstream_id=record.get("workstream_id"),
        entity_refs=tuple(record.get("entity_refs") or ()),
        text=str(record.get("text", ""))[:255],
        raw_ref=record.get("raw_ref"),
        confidence=confidence,
        review_policy=_ReviewPolicy(record.get("review_policy", "pending")),
        metadata=record.get("metadata") or {},
    )
