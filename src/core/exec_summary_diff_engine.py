from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from src.core.chronicle import ProgramEvent
from src.core.models_v2 import RiskDerivedLevel, RiskEntry, RiskStatus
from src.core.narrative_store import get_narratives_dir, load_narratives


@dataclass(frozen=True, slots=True)
class ExecSummaryStalenessFinding:
    workstream_id: str
    workstream_section_id: str
    exec_bullet_text: str
    workstream_lead_sentence: str
    prior_workstream_lead_sentence: str
    divergence_score: float        # SequenceMatcher ratio (0.0–1.0)
    is_stale: bool                 # True if divergence_score < threshold


def extract_lead_sentence(text: str) -> str:
    """
    Extract the first non-comment, non-header sentence from the narrative text.
    """
    # 1. Remove comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # 2. Get lines
    lines = [line.strip() for line in text.splitlines()]
    
    for line in lines:
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        # Strip markdown bold/italic/links
        cleaned = re.sub(r"\*\*|__|\*|_", "", line)
        cleaned = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        # Split by sentence boundaries: period, question mark, or exclamation followed by space/end
        sentences = re.split(r'(?<=[.!?])\s+', cleaned)
        if sentences:
            return sentences[0].strip()
    return ""


def parse_exec_summary_bullets(text: str) -> dict[str, str]:
    """
    Parse the exec_summary.md text and return a mapping of workstream_id -> bullet text.
    """
    bullets = {}
    lines = text.splitlines()
    current_ws_id = None
    
    for line in lines:
        line_str = line.strip()
        # Match <!-- vertex:ws-lead: workstream-id -->
        match = re.match(r"^<!--\s*vertex:ws-lead:\s*(\S+)\s*-->$", line_str, re.IGNORECASE)
        if match:
            current_ws_id = match.group(1).strip()
            continue
        
        if current_ws_id:
            # If we see a bullet line
            if line_str.startswith(("*", "-", "+")):
                # Strip bullet symbol
                bullet_text = re.sub(r"^[\*\-\+]\s*", "", line_str).strip()
                bullets[current_ws_id] = bullet_text
                current_ws_id = None  # Reset
            elif line_str and not line_str.startswith("<!--"):
                # Non-empty, non-comment line that is not a bullet
                bullets[current_ws_id] = line_str
                current_ws_id = None
                
    return bullets


def check_exec_summary_staleness(
    edition: str,
    issue_number: int,
    prior_issue_number: int | None = None,
    *,
    reports_root: Path,
    threshold: float = 0.82,
) -> list[ExecSummaryStalenessFinding]:
    """
    Compare executive summary bullets against current workstream narrative lead sentences.
    An exec bullet is flagged as stale if the similarity ratio is less than threshold
    and the workstream narrative lead sentence has changed since the prior issue.
    """
    findings: list[ExecSummaryStalenessFinding] = []

    # 1. Load current narratives
    current_narratives_dir = get_narratives_dir(edition, issue_number, reports_root=reports_root)
    exec_summary_path = current_narratives_dir / "exec_summary.md"
    if not exec_summary_path.exists():
        return findings

    exec_summary_text = exec_summary_path.read_text(encoding="utf-8")
    bullets = parse_exec_summary_bullets(exec_summary_text)
    if not bullets:
        return findings

    # Load current and prior narratives
    current_narratives = load_narratives(edition, issue_number, reports_root=reports_root)
    
    resolved_prior_issue = prior_issue_number or (issue_number - 1)
    prior_narratives = load_narratives(edition, resolved_prior_issue, reports_root=reports_root)

    for ws_id, bullet_text in bullets.items():
        # Look for the narrative file corresponding to ws_id
        # Filename could be ws_{ws_id}.md or chapter_{ws_id}.md
        possible_filenames = [f"ws_{ws_id}.md", f"chapter_{ws_id}.md", f"{ws_id}.md"]
        current_text = ""
        section_filename = ""
        
        for fname in possible_filenames:
            if fname in current_narratives:
                current_text = current_narratives[fname]
                section_filename = fname
                break
                
        if not current_text:
            continue

        prior_text = ""
        for fname in possible_filenames:
            if fname in prior_narratives:
                prior_text = prior_narratives[fname]
                break

        current_lead = extract_lead_sentence(current_text)
        prior_lead = extract_lead_sentence(prior_text)

        # Check if the narrative has changed since the prior issue
        narrative_changed = True
        if prior_lead:
            narrative_change_ratio = difflib.SequenceMatcher(None, prior_lead, current_lead).ratio()
            if narrative_change_ratio >= 0.99:
                narrative_changed = False

        # If it hasn't changed, we don't flag as stale to avoid false alarms
        if not narrative_changed:
            continue

        # Compute similarity between the executive bullet and the current lead sentence
        divergence_score = difflib.SequenceMatcher(None, bullet_text, current_lead).ratio()
        is_stale = divergence_score < threshold

        if is_stale:
            findings.append(
                ExecSummaryStalenessFinding(
                    workstream_id=ws_id,
                    workstream_section_id=section_filename.removesuffix(".md"),
                    exec_bullet_text=bullet_text,
                    workstream_lead_sentence=current_lead,
                    prior_workstream_lead_sentence=prior_lead,
                    divergence_score=divergence_score,
                    is_stale=True,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# FR-SG-25: Cross-workstream executive summary synthesis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkstreamStatusEntry:
    workstream_id: str
    lead_sentence: str
    risk_proposal: RiskDerivedLevel | None
    stale_executive_bullet: bool


@dataclass(frozen=True, slots=True)
class CrossWorkstreamExecSummary:
    """FR-SG-25: Synthesized cross-workstream executive summary.

    Deterministic from Chronicle + risk register + staleness findings.
    """

    program_id: str
    issue_number: int | None
    as_of: datetime
    workstream_entries: tuple[WorkstreamStatusEntry, ...]
    top_risk_ids: tuple[str, ...]
    recent_chronicle_descriptions: tuple[str, ...]
    timeline_note: str | None
    gate_conditions: tuple[str, ...]
    stale_bullet_count: int


def generate_cross_workstream_exec_summary(
    program_id: str,
    issue_number: int | None,
    *,
    workstream_ids: tuple[str, ...],
    risk_entries: tuple[RiskEntry, ...],
    chronicle_events: tuple[ProgramEvent, ...],
    staleness_findings: list[ExecSummaryStalenessFinding],
    risk_proposals: dict[str, RiskDerivedLevel] | None = None,
    workstream_lead_sentences: dict[str, str] | None = None,
    as_of: datetime,
    recent_event_days: int = 14,
    max_top_risks: int = 3,
) -> CrossWorkstreamExecSummary:
    """FR-SG-25: Generate a cross-workstream executive summary.

    Synthesizes program identity, recent chronicle events, current workstream
    state, top risks with rationale, and timeline credibility. Fully
    deterministic — no AI required.

    Args:
        program_id: program identifier
        issue_number: current issue number (for provenance)
        workstream_ids: ordered list of workstream IDs to include
        risk_entries: full program risk register
        chronicle_events: full program chronicle
        staleness_findings: output of check_exec_summary_staleness()
        risk_proposals: per-workstream RiskDerivedLevel proposals (optional)
        workstream_lead_sentences: per-workstream first sentence from narrative (optional)
        as_of: reference timestamp (UTC)
        recent_event_days: window for recent chronicle events
        max_top_risks: how many top risks to include
    """
    resolved_as_of = _ensure_utc_exec(as_of)
    window_start = resolved_as_of - timedelta(days=recent_event_days)

    # Recent chronicle events in the window
    recent_events = tuple(
        ev for ev in chronicle_events
        if _ensure_utc_exec(ev.event_date) >= window_start
    )
    recent_descriptions = tuple(ev.description for ev in recent_events)

    # Top risks: ESCALATED first, then OPEN, capped at max_top_risks
    active = [e for e in risk_entries if e.status in ("escalated", "open")]
    active.sort(key=lambda e: (0 if e.status == "escalated" else 1))
    top_risk_ids = tuple(e.id for e in active[:max_top_risks])

    # Stale bullet index
    stale_ws_ids = {f.workstream_id for f in staleness_findings if f.is_stale}

    # Per-workstream entries
    entries: list[WorkstreamStatusEntry] = []
    for ws_id in workstream_ids:
        lead = (workstream_lead_sentences or {}).get(ws_id, "")
        proposal = (risk_proposals or {}).get(ws_id)
        entries.append(
            WorkstreamStatusEntry(
                workstream_id=ws_id,
                lead_sentence=lead,
                risk_proposal=proposal,
                stale_executive_bullet=ws_id in stale_ws_ids,
            )
        )

    # Gate conditions: extract from chronicle "approval" events
    gate_conditions = tuple(
        ev.description
        for ev in chronicle_events
        if ev.event_type == "approval" and "gate" in ev.description.lower()
    )

    # Timeline note: summarize slip history from chronicle
    slips = [ev for ev in chronicle_events if ev.event_type == "dfd_slip"]
    if slips:
        timeline_note = f"{len(slips)} DFD slip(s) recorded in chronicle."
    else:
        timeline_note = None

    return CrossWorkstreamExecSummary(
        program_id=program_id,
        issue_number=issue_number,
        as_of=resolved_as_of,
        workstream_entries=tuple(entries),
        top_risk_ids=top_risk_ids,
        recent_chronicle_descriptions=recent_descriptions,
        timeline_note=timeline_note,
        gate_conditions=gate_conditions,
        stale_bullet_count=len(stale_ws_ids),
    )


def _ensure_utc_exec(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
