"""Per-source-type auto-resolution gate for autonomous source discovery (discover.md §8.4).

This module owns ONE thing: the deterministic decision of whether a discovered
candidate may be auto-resolved without PM review. It does NOT compute confidence
— each discovery adapter scores its own candidates (e.g.
``workiq_calendar_discovery._meeting_match_score`` combines title/owner/recurrence;
``m365_discovery_support.candidate_match_score`` does title overlap). This gate
consumes that already-computed confidence plus whatever corroboration is
available and applies the §8.4 per-source-type thresholds in one calibratable
place, replacing the previous uniform 0.75 title threshold.

Pure Zone A: imports only ``discovery_intent`` value objects. The LLM judge
(§8.6) may re-rank candidates but never calls this — auto-resolution is always
deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.discovery_intent import SourceRefKind

# discover.md §8.4 — auto-resolution gate thresholds (initial heuristics; calibrate
# against the Issue-078 gold corpus before widening autonomy beyond unique matches).
HARD_GATE = 0.85
SOFT_GATE = 0.75


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """Inputs to the per-source-type auto-resolution gate (discover.md §8.4).

    ``confidence`` is the adapter-computed score for the candidate (not computed
    here). The corroboration fields are only consulted in the 0.75–0.85 band.
    """

    confidence: float
    unique: bool  # exactly one plausible candidate maps to the intent
    exact_match: bool = False
    recent_rejection: bool = False
    owner_overlap: bool = False  # organizer/attendee overlaps workstream owners/DRIs
    nonzero_yield_windows: int = 0  # gather windows with >0 signal yield
    subject_thread_continuity: bool = False


def passes_auto_resolution_gate(ref_kind: SourceRefKind, ctx: ResolutionContext) -> bool:
    """Return True only when a candidate may be auto-resolved without PM review.

    Mirrors discover.md §8.4: unique + (exact OR >= hard gate) auto-resolves for
    any type; in the 0.75–0.85 band each source type requires its own
    corroboration. Meeting series can never rely on repeated yield (an unresolved
    meeting cannot hydrate transcripts) — they require organizer/attendee/title
    corroboration instead. A recent PM rejection or non-uniqueness always blocks.
    """
    if ctx.recent_rejection or not ctx.unique:
        return False
    if ctx.exact_match or ctx.confidence >= HARD_GATE:
        return True
    if ctx.confidence < SOFT_GATE:
        return False
    if ref_kind == SourceRefKind.MEETING_SERIES:
        return ctx.owner_overlap
    if ref_kind in (SourceRefKind.TEAMS_CHAT, SourceRefKind.TEAMS_CHANNEL):
        return ctx.nonzero_yield_windows >= 2
    if ref_kind == SourceRefKind.EMAIL_THREAD:
        return ctx.nonzero_yield_windows >= 1 and ctx.subject_thread_continuity
    return False
