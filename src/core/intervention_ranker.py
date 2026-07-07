from __future__ import annotations

import hashlib
import json
from src.core.jsonl_utils import parse_jsonl_line
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.ask_lifecycle import DecisionAskLifecycleStage, build_decision_ask_lifecycle_proposals
from src.core.claim_tracker import ClaimAssessment
from src.core.incident_learning_synthesizer import IncidentClassPattern, IncidentRefPattern, build_incident_class_patterns, build_incident_ref_patterns, normalize_incident_learning_summary, normalize_incident_ref
from src.core.models import Confidence
from src.core.models_v2 import ContradictionPacket, DecisionAsk, IncidentEntry


@dataclass(frozen=True, slots=True)
class InterventionProposal:
    proposal_id: str
    title: str
    evidence_summary: str
    proposed_action: str
    command: str
    rollback: str
    source_hash: str
    priority: int
    confidence: Confidence = Confidence.NONE


def rank_brief_interventions(
    program_id: str,
    *,
    claim_assessments: tuple[ClaimAssessment, ...],
    decision_asks: tuple[DecisionAsk, ...],
    contradiction_packets: tuple[ContradictionPacket, ...],
    salience_weights: dict[str, float],
    as_of: datetime,
    programs_root: Path,
    incident_entries: tuple[IncidentEntry, ...] = (),
    limit: int = 3,
) -> tuple[InterventionProposal, ...]:
    edit_magnitude_by_section = _load_edit_magnitude_by_section(program_id, programs_root)
    incident_patterns = build_incident_ref_patterns(incident_entries)
    incident_class_patterns = build_incident_class_patterns(incident_entries)
    proposals: list[InterventionProposal] = []

    for packet in contradiction_packets:
        if not packet.contradictions:
            continue
        primary = packet.contradictions[0]
        workstream_id = packet.workstream_id or "unmapped"
        feasibility = _feasibility_score(workstream_id, edit_magnitude_by_section)
        salience = salience_weights.get(packet.workstream_id or "", 0.2)
        priority = 520 + _confidence_priority(packet.confidence) + int(salience * 100) + int(feasibility * 100)
        preferred = ""
        if packet.recommended_resolution is not None:
            preferred = f" Prefer {packet.recommended_resolution.winning_source.value}."
        proposals.append(
            InterventionProposal(
                proposal_id=_build_proposal_id("contradiction", f"wi-{packet.work_item_id}-review"),
                title=f"Review contradiction on {workstream_id}",
                evidence_summary=f"WI:{packet.work_item_id} {primary.summary}{preferred}",
                proposed_action="Refresh the contradiction report and decide whether the claim or narrative needs correction.",
                command=f"vertex reconcile --program {program_id} --refresh --dry-run",
                rollback="No state changes occur until you update the underlying claim or source of truth.",
                source_hash=_build_source_hash(
                    "contradiction",
                    str(packet.work_item_id),
                    workstream_id,
                    primary.summary,
                    packet.confidence.value,
                    packet.recommended_resolution.winning_source.value if packet.recommended_resolution is not None else "",
                    packet.recommended_resolution.confidence.value if packet.recommended_resolution is not None else "",
                ),
                priority=priority,
                confidence=packet.confidence,
            )
        )

    for proposal in build_decision_ask_lifecycle_proposals(
        decision_asks,
        as_of=as_of,
        minimum_stage=DecisionAskLifecycleStage.NUDGE,
    ):
        age_days = proposal.age_days
        urgency = 120 if proposal.stage is DecisionAskLifecycleStage.NUDGE else 170
        title = (
            f"Stage nudge for decision ask {proposal.ask.id}"
            if proposal.stage is DecisionAskLifecycleStage.NUDGE
            else f"Stage escalation for decision ask {proposal.ask.id}"
        )
        evidence = f"{proposal.inactive_days} day(s) inactive. {proposal.ask.text}"
        if proposal.inactive_days != proposal.age_days:
            evidence = f"{proposal.age_days} day(s) open / {proposal.inactive_days} day(s) inactive. {proposal.ask.text}"
        related_patterns = _related_incident_patterns_for_ask(proposal.ask, incident_patterns)
        incident_priority = 0
        if related_patterns:
            related_pattern = related_patterns[0]
            evidence = f"{evidence} Recent incident learning: {_render_incident_pattern_evidence(related_pattern)}"
            incident_priority += 140
            if related_pattern.max_severity is not None and related_pattern.max_severity <= 2:
                incident_priority += 30
            if related_pattern.entry_count > 1:
                incident_priority += 15
        proposal_confidence = related_patterns[0].confidence if related_patterns else Confidence.NONE
        priority = 430 + min(age_days, 30) * 5 + urgency
        priority += incident_priority
        proposals.append(
            InterventionProposal(
                proposal_id=_build_proposal_id("decision-ask", f"{proposal.ask.id}-{proposal.stage.value}"),
                title=title,
                evidence_summary=evidence,
                proposed_action=proposal.proposed_action,
                command=proposal.command,
                rollback="Do not send the draft and defer or resolve the ask if the follow-up is not needed.",
                source_hash=_build_source_hash(
                    "decision-ask",
                    proposal.ask.id,
                    proposal.stage.value,
                    proposal.ask.text,
                    proposal.ask.owner_alias or "",
                    proposal.ask.expiry_date.isoformat() if proposal.ask.expiry_date is not None else "",
                    proposal.ask.last_touched_at.isoformat() if proposal.ask.last_touched_at is not None else "",
                ),
                priority=priority,
                confidence=proposal_confidence,
            )
        )

    for assessment in claim_assessments:
        claim = assessment.claim
        if claim.due_date is None:
            continue
        days_until_due = (claim.due_date - as_of.date()).days
        if assessment.effective_status not in {"stale", "contradicted"} and days_until_due > 7:
            continue
        workstream_id = claim.workstream_id or "unmapped"
        feasibility = _feasibility_score(workstream_id, edit_magnitude_by_section)
        salience = salience_weights.get(claim.workstream_id or "", 0.2)
        urgency = 170 if assessment.effective_status in {"stale", "contradicted"} else 120 if days_until_due <= 3 else 80
        priority = 320 + urgency + int(salience * 100) + int(feasibility * 100)
        proposals.append(
            InterventionProposal(
                proposal_id=_build_proposal_id("claim", f"{claim.id}-review"),
                title=f"Review claim {claim.id}",
                evidence_summary=f"Due {claim.due_date.isoformat()} on {workstream_id}. {claim.text}",
                proposed_action="Review the claim status and decide whether to resolve, defer, or refresh the evidence.",
                command=f"vertex claims --program {program_id}",
                rollback="Leave the claim open; no tracker state changes happen until you record a resolution.",
                source_hash=_build_source_hash(
                    "claim",
                    claim.id,
                    claim.workstream_id or "",
                    claim.text,
                    claim.due_date.isoformat(),
                    assessment.effective_status,
                ),
                priority=priority,
                confidence=Confidence.NONE,
            )
        )

    for pattern in incident_patterns:
        if not _pattern_needs_intervention(pattern):
            continue
        workstream_id = pattern.workstream_id or "unmapped"
        feasibility = _feasibility_score(workstream_id, edit_magnitude_by_section)
        salience = salience_weights.get(pattern.workstream_id or "", 0.2)
        priority = 390 + _confidence_priority(pattern.confidence) + int(salience * 100) + int(feasibility * 100)
        if pattern.entry_count > 1:
            priority += 40
        if pattern.max_severity is not None and pattern.max_severity <= 2:
            priority += 20
        proposals.append(
            InterventionProposal(
                proposal_id=_build_proposal_id("incident", f"{workstream_id}-{pattern.ref}-readiness"),
                title=f"Review readiness after incident learning on {workstream_id}",
                evidence_summary=_render_incident_pattern_evidence(pattern),
                proposed_action="Refresh readiness and verify whether rollback, incident response ownership, or launch gating needs an update.",
                command=f"vertex readiness fetch --program {program_id}",
                rollback="Rerun readiness after updating local evidence or config; the snapshot is a local projection.",
                source_hash=_build_source_hash(
                    "incident",
                    pattern.ref,
                    workstream_id,
                    pattern.summary_text,
                    str(pattern.entry_count),
                    str(pattern.max_severity or ""),
                    pattern.confidence.value,
                ),
                priority=priority,
                confidence=pattern.confidence,
            )
        )

    for cpat in incident_class_patterns:
        if not _class_pattern_needs_intervention(cpat):
            continue
        workstream_id = cpat.workstream_ids[0] if cpat.workstream_ids else "unmapped"
        feasibility = _feasibility_score(workstream_id, edit_magnitude_by_section)
        salience = max((salience_weights.get(workstream, 0.2) for workstream in cpat.workstream_ids), default=0.2)
        priority = 410 + _confidence_priority(cpat.confidence) + int(salience * 100) + int(feasibility * 100)
        if cpat.entry_count > 2:
            priority += 45
        if cpat.max_severity is not None and cpat.max_severity <= 2:
            priority += 20
        proposals.append(
            InterventionProposal(
                proposal_id=_build_proposal_id("incident-class", f"{workstream_id}-{cpat.class_label}-readiness"),
                title=f"Review recurring incident class on {workstream_id}",
                evidence_summary=_render_incident_class_pattern_evidence(cpat),
                proposed_action="Refresh readiness and verify whether a repeated incident class requires a durable guardrail, policy, or ownership change.",
                command=f"vertex readiness fetch --program {program_id}",
                rollback="Rerun readiness after updating local evidence or config; the snapshot is a local projection.",
                source_hash=_build_source_hash(
                    "incident-class",
                    cpat.class_label,
                    workstream_id,
                    cpat.summary_text,
                    str(cpat.entry_count),
                    str(cpat.max_severity or ""),
                    cpat.confidence.value,
                ),
                priority=priority,
                confidence=cpat.confidence,
            )
        )

    ranked = tuple(sorted(proposals, key=lambda proposal: (-proposal.priority, proposal.title)))
    return ranked[:limit]

def _pattern_needs_intervention(pattern: IncidentRefPattern) -> bool:
    return (
        pattern.entry_count > 1
        or (pattern.max_severity is not None and pattern.max_severity <= 2)
        or pattern.confidence is Confidence.HIGH
    )


def _class_pattern_needs_intervention(pattern: IncidentClassPattern) -> bool:
    return (
        pattern.entry_count > 2
        or (pattern.max_severity is not None and pattern.max_severity <= 2)
        or pattern.confidence is Confidence.HIGH
    )


def _render_incident_pattern_evidence(pattern: IncidentRefPattern) -> str:
    incident_refs = ", ".join(pattern.incident_refs)
    if pattern.entry_count == 1:
        return f"{pattern.ref}: {pattern.summary_text}. Source: {incident_refs}. ({pattern.confidence.value.lower()} confidence)"
    return (
        f"{pattern.ref}: repeated across {pattern.entry_count} incident learnings. {pattern.summary_text}. "
        f"Source: {incident_refs}. ({pattern.confidence.value.lower()} confidence)"
    )


def _render_incident_class_pattern_evidence(pattern: IncidentClassPattern) -> str:
    incident_refs = ", ".join(pattern.incident_refs)
    linked_refs = f" Refs: {', '.join(pattern.linked_refs)}." if pattern.linked_refs else ""
    return (
        f"Incident class {pattern.class_label}: repeated across {pattern.entry_count} incident learnings. "
        f"{pattern.summary_text}. Source: {incident_refs}.{linked_refs} ({pattern.confidence.value.lower()} confidence)"
    )


def _related_incident_patterns_for_ask(
    ask: DecisionAsk,
    patterns: tuple[IncidentRefPattern, ...],
) -> tuple[IncidentRefPattern, ...]:
    if not ask.entity_refs:
        return ()
    ask_refs = {normalize_incident_ref(ref) for ref in ask.entity_refs if normalize_incident_ref(ref)}
    if not ask_refs:
        return ()
    return tuple(pattern for pattern in patterns if pattern.ref in ask_refs)


def _confidence_priority(confidence: Confidence) -> int:
    return {
        Confidence.HIGH: 220,
        Confidence.MEDIUM: 150,
        Confidence.LOW: 80,
        Confidence.NONE: 20,
    }[confidence]


def _confidence_rank(confidence: Confidence) -> int:
    return _confidence_priority(confidence)


def _feasibility_score(workstream_id: str, edit_magnitude_by_section: dict[str, float]) -> float:
    magnitude = edit_magnitude_by_section.get(workstream_id)
    if magnitude is None:
        return 0.8
    bounded = max(0.0, min(1.0, magnitude))
    return round(1.0 - bounded, 2)


def _build_proposal_id(prefix: str, raw_value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", raw_value.strip().lower()).strip("-")
    return f"{prefix}-{normalized or 'item'}"


def _build_source_hash(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(part.strip() for part in parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _load_edit_magnitude_by_section(program_id: str, programs_root: Path) -> dict[str, float]:
    path = programs_root / program_id / "journal" / "edit_patterns.jsonl"
    if not path.exists():
        return {}

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = parse_jsonl_line(line)
        except json.JSONDecodeError:
            continue
        section_id = record.get("section_id")
        magnitude = record.get("author_override_magnitude")
        if not isinstance(section_id, str) or not isinstance(magnitude, (int, float)):
            continue
        totals[section_id] = totals.get(section_id, 0.0) + float(magnitude)
        counts[section_id] = counts.get(section_id, 0) + 1
    return {
        section_id: round(totals[section_id] / counts[section_id], 2)
        for section_id in totals
        if counts[section_id] > 0
    }
