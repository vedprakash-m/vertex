"""ADF-W2.9 (specs/arch-data-fix.md Section 8.10.5): "Human-authored
executive summary remains authoritative. AI fills or proposes; it does not
silently overwrite."

This rule already existed as inline behavior in ``report_ai.py``'s exec
summary path (``if not loaded_exec_summary_text.strip(): ...``) before this
item -- this module names it once so every synthesis-consuming surface
(cockpit today; newsletter/brief/decision-brief/LT-deck are each still
gated on their own future wiring item, see specs/arch-data-fix.md) applies
the identical precedence rule rather than re-deriving it per call site.
"""

from __future__ import annotations

from dataclasses import dataclass


def should_ai_fill(human_text: str | None) -> bool:
    """True only when there is no human-authored content to preserve --
    AI may fill an empty field, never overwrite a non-empty one."""
    return not (human_text or "").strip()


@dataclass(frozen=True, slots=True)
class PrecedenceResolution:
    text: str
    source: str  # "human" | "ai"


def resolve_human_or_ai_text(*, human_text: str | None, ai_text: str | None) -> PrecedenceResolution:
    """The generalized decision: human text wins whenever it is non-empty;
    AI text is used only to fill an empty field. Never a merge, never a
    silent overwrite."""
    if not should_ai_fill(human_text):
        return PrecedenceResolution(text=(human_text or ""), source="human")
    if ai_text and ai_text.strip():
        return PrecedenceResolution(text=ai_text.strip(), source="ai")
    return PrecedenceResolution(text=(human_text or ""), source="human")


__all__ = ["PrecedenceResolution", "resolve_human_or_ai_text", "should_ai_fill"]
