from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.core.models_v2 import SectionRevisionProposal, SectionRevisionStatus, Signal


@dataclass(frozen=True, slots=True)
class DecisionSignal:
    signal_id: str
    text: str
    timestamp: str | None
    source: str | None


@dataclass(frozen=True, slots=True)
class DecisionItem:
    section_id: str
    section_title: str
    current_text: str
    proposed_text: str | None
    evidence_delta_lines: tuple[str, ...]
    top_signals: tuple[DecisionSignal, ...]
    vitality_summary: str
    confidence: str
    kpi_summary: str | None
    stale_claims: tuple[str, ...]
    accept_command: str
    reject_command: str
    accept_modified_command: str
    # Populated by AI advisor when --ai is passed
    verdict: str | None = None
    verdict_reasoning: str | None = None
    suggested_text: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionBrief:
    issue_number: int
    edition_name: str
    generated_at: str
    items: tuple[DecisionItem, ...]
    total_pending: int
    ai_enriched: bool


def build_decision_brief(
    *,
    proposals: tuple[SectionRevisionProposal, ...],
    signal_map: dict[str, Signal],
    edition_name: str,
    issue_number: int,
    generated_at: datetime | None = None,
) -> DecisionBrief:
    pending = tuple(p for p in proposals if p.status == SectionRevisionStatus.PENDING)
    items = tuple(
        _build_decision_item(p, signal_map=signal_map, edition_name=edition_name)
        for p in pending
    )
    ts = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return DecisionBrief(
        issue_number=issue_number,
        edition_name=edition_name,
        generated_at=ts,
        items=items,
        total_pending=len(items),
        ai_enriched=False,
    )


def _build_decision_item(
    proposal: SectionRevisionProposal,
    *,
    signal_map: dict[str, Signal],
    edition_name: str,
) -> DecisionItem:
    brief = proposal.evidence_brief
    delta_lines = _format_delta_lines(
        new_items=brief.new_items,
        closed_items=brief.closed_items,
        risk_changed_items=brief.risk_changed_items,
        eta_changed_items=brief.eta_changed_items,
        ado_delta_summary=brief.ado_delta_summary,
    )
    top_signals = tuple(
        _resolve_signal(sid, signal_map=signal_map)
        for sid in brief.top_signals
    )
    return DecisionItem(
        section_id=proposal.section_id,
        section_title=_section_title(proposal.section_id),
        current_text=proposal.current_text,
        proposed_text=proposal.proposed_text,
        evidence_delta_lines=delta_lines,
        top_signals=top_signals,
        vitality_summary=brief.vitality_summary,
        confidence=brief.confidence.value,
        kpi_summary=brief.kpi_summary,
        stale_claims=brief.stale_claims,
        accept_command=f"vertex apply-proposals --edition {edition_name} --accept {proposal.section_id}",
        reject_command=f"vertex apply-proposals --edition {edition_name} --reject {proposal.section_id}",
        accept_modified_command=(
            f"vertex apply-proposals --edition {edition_name} --accept-modified "
            f"{proposal.section_id}=<revised_text>"
        ),
    )


def _format_delta_lines(
    *,
    new_items: tuple[int, ...],
    closed_items: tuple[int, ...],
    risk_changed_items: tuple[int, ...],
    eta_changed_items: tuple[int, ...],
    ado_delta_summary: str,
) -> tuple[str, ...]:
    lines: list[str] = []
    if new_items:
        ids = ", ".join(f"WI-{i}" for i in new_items[:5])
        suffix = " ..." if len(new_items) > 5 else ""
        lines.append(f"{len(new_items)} new work item(s): {ids}{suffix}")
    if closed_items:
        ids = ", ".join(f"WI-{i}" for i in closed_items[:5])
        suffix = " ..." if len(closed_items) > 5 else ""
        lines.append(f"{len(closed_items)} closed work item(s): {ids}{suffix}")
    if risk_changed_items:
        ids = ", ".join(f"WI-{i}" for i in risk_changed_items[:5])
        suffix = " ..." if len(risk_changed_items) > 5 else ""
        lines.append(f"{len(risk_changed_items)} risk change(s): {ids}{suffix}")
    if eta_changed_items:
        ids = ", ".join(f"WI-{i}" for i in eta_changed_items[:5])
        suffix = " ..." if len(eta_changed_items) > 5 else ""
        lines.append(f"{len(eta_changed_items)} ETA change(s): {ids}{suffix}")
    if ado_delta_summary:
        lines.append(ado_delta_summary)
    if not lines:
        lines.append("No ADO changes in evidence window.")
    return tuple(lines)


def _resolve_signal(signal_id: str, *, signal_map: dict[str, Signal]) -> DecisionSignal:
    signal = signal_map.get(signal_id)
    if signal is None:
        return DecisionSignal(
            signal_id=signal_id,
            text="(signal text unavailable)",
            timestamp=None,
            source=None,
        )
    return DecisionSignal(
        signal_id=signal_id,
        text=signal.text or "(no text)",
        timestamp=signal.timestamp.strftime("%Y-%m-%d") if signal.timestamp else None,
        source=signal.source,
    )


def _section_title(section_id: str) -> str:
    if section_id == "exec_summary":
        return "Executive Summary"
    normalized = section_id.removeprefix("ws:").replace("_", " ").replace("-", " ").strip()
    return " ".join(word.capitalize() for word in normalized.split()) or section_id
