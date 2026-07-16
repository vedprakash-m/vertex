"""ADF-W2.9/W2.11/W4.8/W5.11/W5.12/W5.14 (v1.51 deep-dive): shared staging
store for the five AISchemaGateway-pattern proposal types that had no
durable persistence and hence no CLI reviewer -- confirmed by a repo-wide
grep finding zero callers of ``apply_risk_proposal``,
``approve_meeting_action``, ``to_top3_now_entry``,
``apply_dependency_blast_radius_proposal``, or any approve/reject function
on ``GovernanceDecisionBriefProposal``.

``ProgramSynthesis`` is deliberately excluded: it already has its own
content-addressed, QG-29-released persistence (``program_synthesis.py``)
and is consumed via a release-gated read, not a human accept/reject staging
flow like these five.

Storage: ``programs/<program_id>/journal/ai_review_proposals.jsonl``,
append-only, rotated at 10MB -- mirrors ``ai_proposal_store.py``'s proven
convention exactly (same rotation threshold, same "latest record per id
wins" status-transition model: an accept/reject is a fresh append of the
updated proposal, never an in-place rewrite of prior lines).

Zone A only.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal

from src.core.dependency_blast_radius import DependencyBlastRadiusProposal
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.governance_decision_brief import GovernanceDecisionBriefProposal, GovernanceDecisionOption
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records, validate_jsonl_row
from src.core.meeting_action import MeetingAction
from src.core.models_v2 import RiskCategory, RiskImpact, RiskProbability
from src.core.risk_proposal import RiskProposal
from src.core.top_three_candidates import TopThreeCandidateProposal

ReviewProposalType = Literal[
    "risk",
    "meeting_action",
    "top_three",
    "governance_decision_brief",
    "dependency_blast_radius",
]

ReviewProposal = (
    RiskProposal
    | MeetingAction
    | TopThreeCandidateProposal
    | GovernanceDecisionBriefProposal
    | DependencyBlastRadiusProposal
)

#: All five valid type keys, for CLI choice validation and iteration.
REVIEW_PROPOSAL_TYPES: tuple[ReviewProposalType, ...] = (
    "risk",
    "meeting_action",
    "top_three",
    "governance_decision_brief",
    "dependency_blast_radius",
)

# Mirrors ai_proposal_store.py's rotation threshold exactly (spec Section 9.7).
_AI_REVIEW_PROPOSALS_MAX_BYTES = 10 * 1024 * 1024


class AIReviewProposalStoreError(Exception):
    """Raised for an unknown proposal_type or malformed stored record."""


def get_ai_review_proposals_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "ai_review_proposals.jsonl"


def stage_proposal(
    program_id: str,
    proposal_type: ReviewProposalType,
    proposal: ReviewProposal,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    """Append a newly-generated (status="staged") or status-transitioned
    (approved/rejected) proposal. Every call is a fresh append -- reading
    back takes the latest record per ``(proposal_type, id)`` pair, exactly
    mirroring ``ai_proposal_store.py``'s ``update_ai_proposal_status`` /
    latest-wins convention, so accept/reject never mutates prior history."""
    if proposal_type not in REVIEW_PROPOSAL_TYPES:
        raise AIReviewProposalStoreError(f"Unknown proposal_type: {proposal_type!r}")
    path = get_ai_review_proposals_path(program_id, programs_root=programs_root)
    record = _to_record(proposal_type, proposal)
    if record.get("program_id") != program_id:
        raise AIReviewProposalStoreError(
            f"Proposal program_id {record.get('program_id')!r} does not match target program {program_id!r}."
        )
    append_jsonl_line(path, json.dumps(record, ensure_ascii=False) + "\n", max_bytes=_AI_REVIEW_PROPOSALS_MAX_BYTES)
    return path


def load_proposals(
    program_id: str,
    *,
    proposal_type: ReviewProposalType | None = None,
    status_filter: set[str] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ReviewProposal, ...]:
    """Load proposals, latest record per ``(proposal_type, id)`` wins (an
    accept/reject re-append supersedes the earlier "staged" record for the
    same id). Optionally narrowed to one ``proposal_type`` and/or a status
    set (e.g. ``{"staged"}`` for "what's pending review")."""
    path = get_ai_review_proposals_path(program_id, programs_root=programs_root)
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for record in read_jsonl_records(path):
        validate_jsonl_row(record, required_fields=("proposal_type", "id", "program_id", "status"), field_name="AI review proposal row")
        key = (record["proposal_type"], record["id"])
        latest_by_key[key] = record

    proposals: list[ReviewProposal] = []
    for (record_type, _record_id), record in latest_by_key.items():
        if proposal_type is not None and record_type != proposal_type:
            continue
        if status_filter is not None and record.get("status") not in status_filter:
            continue
        proposals.append(_from_record(record_type, record))

    proposals.sort(key=lambda item: (_proposal_type_of(item), getattr(item, "proposed_at"), item.id))
    return tuple(proposals)


def load_proposal(
    program_id: str,
    proposal_type: ReviewProposalType,
    proposal_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ReviewProposal | None:
    for proposal in load_proposals(program_id, proposal_type=proposal_type, programs_root=programs_root):
        if proposal.id == proposal_id:
            return proposal
    return None


def _proposal_type_of(proposal: ReviewProposal) -> str:
    if isinstance(proposal, RiskProposal):
        return "risk"
    if isinstance(proposal, MeetingAction):
        return "meeting_action"
    if isinstance(proposal, TopThreeCandidateProposal):
        return "top_three"
    if isinstance(proposal, GovernanceDecisionBriefProposal):
        return "governance_decision_brief"
    if isinstance(proposal, DependencyBlastRadiusProposal):
        return "dependency_blast_radius"
    raise AIReviewProposalStoreError(f"Unrecognized proposal instance: {type(proposal)!r}")


# ---------------------------------------------------------------------------
# Per-type serialization. Kept here (not on the domain dataclasses) to match
# ``ai_proposal_store.py``'s established convention of owning record
# encode/decode in the store module.
# ---------------------------------------------------------------------------


def _to_record(proposal_type: ReviewProposalType, proposal: ReviewProposal) -> dict[str, Any]:
    if proposal_type == "risk":
        assert isinstance(proposal, RiskProposal)
        return _risk_to_record(proposal)
    if proposal_type == "meeting_action":
        assert isinstance(proposal, MeetingAction)
        return _meeting_action_to_record(proposal)
    if proposal_type == "top_three":
        assert isinstance(proposal, TopThreeCandidateProposal)
        return _top_three_to_record(proposal)
    if proposal_type == "governance_decision_brief":
        assert isinstance(proposal, GovernanceDecisionBriefProposal)
        return _governance_decision_brief_to_record(proposal)
    if proposal_type == "dependency_blast_radius":
        assert isinstance(proposal, DependencyBlastRadiusProposal)
        return _blast_radius_to_record(proposal)
    raise AIReviewProposalStoreError(f"Unknown proposal_type: {proposal_type!r}")


def _from_record(proposal_type: str, record: dict[str, Any]) -> ReviewProposal:
    if proposal_type == "risk":
        return _risk_from_record(record)
    if proposal_type == "meeting_action":
        return _meeting_action_from_record(record)
    if proposal_type == "top_three":
        return _top_three_from_record(record)
    if proposal_type == "governance_decision_brief":
        return _governance_decision_brief_from_record(record)
    if proposal_type == "dependency_blast_radius":
        return _blast_radius_from_record(record)
    raise AIReviewProposalStoreError(f"Unknown proposal_type: {proposal_type!r}")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _require_utc(value).isoformat()


def _require_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _require_utc(parsed)


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value else None


def _risk_to_record(proposal: RiskProposal) -> dict[str, Any]:
    return {
        "proposal_type": "risk",
        "id": proposal.id,
        "program_id": proposal.program_id,
        "candidate_risk_id": proposal.candidate_risk_id,
        "causal_title": proposal.causal_title,
        "why_it_matters": proposal.why_it_matters,
        "probability": proposal.probability.value,
        "impact": proposal.impact.value,
        "category": proposal.category.value,
        "mitigation": proposal.mitigation,
        "owner_alias": proposal.owner_alias,
        "by_when": proposal.by_when.isoformat() if proposal.by_when else None,
        "fallback": proposal.fallback,
        "evidence_refs": list(proposal.evidence_refs),
        "ai_run_id": proposal.ai_run_id,
        "status": proposal.status,
        "rejection_reason": proposal.rejection_reason,
        "proposed_at": _iso(proposal.proposed_at),
        "decided_at": _iso(proposal.decided_at),
    }


def _risk_from_record(record: dict[str, Any]) -> RiskProposal:
    return RiskProposal(
        id=record["id"],
        program_id=record["program_id"],
        candidate_risk_id=record["candidate_risk_id"],
        causal_title=record["causal_title"],
        why_it_matters=record["why_it_matters"],
        probability=RiskProbability.from_string(record["probability"]),
        impact=RiskImpact.from_string(record["impact"]),
        category=RiskCategory.from_string(record["category"]),
        mitigation=record["mitigation"],
        owner_alias=record.get("owner_alias"),
        by_when=date.fromisoformat(record["by_when"]) if record.get("by_when") else None,
        fallback=record["fallback"],
        evidence_refs=tuple(record.get("evidence_refs", ())),
        ai_run_id=record["ai_run_id"],
        status=record.get("status", "staged"),  # type: ignore[arg-type]
        rejection_reason=record.get("rejection_reason"),
        proposed_at=_parse_datetime(record["proposed_at"]) if record.get("proposed_at") else datetime.now(timezone.utc),
        decided_at=_parse_optional_datetime(record.get("decided_at")),
    )


def _meeting_action_to_record(action: MeetingAction) -> dict[str, Any]:
    return {
        "proposal_type": "meeting_action",
        "id": action.id,
        "program_id": action.program_id,
        "meeting_ref": action.meeting_ref,
        "commitment": action.commitment,
        "owner_alias": action.owner_alias,
        "due_date": action.due_date.isoformat() if action.due_date else None,
        "linked_work_item_id": action.linked_work_item_id,
        "blocks": list(action.blocks),
        "source_span": action.source_span,
        "extraction_method": action.extraction_method,
        "status": action.status,
        "rejection_reason": action.rejection_reason,
        "proposed_at": _iso(action.proposed_at),
        "decided_at": _iso(action.decided_at),
    }


def _meeting_action_from_record(record: dict[str, Any]) -> MeetingAction:
    return MeetingAction(
        id=record["id"],
        program_id=record["program_id"],
        meeting_ref=record["meeting_ref"],
        commitment=record["commitment"],
        owner_alias=record.get("owner_alias"),
        due_date=date.fromisoformat(record["due_date"]) if record.get("due_date") else None,
        linked_work_item_id=record.get("linked_work_item_id"),
        blocks=tuple(record.get("blocks", ())),
        source_span=record["source_span"],
        extraction_method=record["extraction_method"],  # type: ignore[arg-type]
        status=record.get("status", "staged"),  # type: ignore[arg-type]
        rejection_reason=record.get("rejection_reason"),
        proposed_at=_parse_datetime(record["proposed_at"]) if record.get("proposed_at") else datetime.now(timezone.utc),
        decided_at=_parse_optional_datetime(record.get("decided_at")),
    )


def _top_three_to_record(proposal: TopThreeCandidateProposal) -> dict[str, Any]:
    return {
        "proposal_type": "top_three",
        "id": proposal.id,
        "program_id": proposal.program_id,
        "item_id": proposal.item_id,
        "reason": proposal.reason,
        "evidence_refs": list(proposal.evidence_refs),
        "urgency": proposal.urgency,
        "decision_or_action_needed": proposal.decision_or_action_needed,
        "owner_alias": proposal.owner_alias,
        "confidence": proposal.confidence,
        "ai_run_id": proposal.ai_run_id,
        "status": proposal.status,
        "rejection_reason": proposal.rejection_reason,
        "proposed_at": _iso(proposal.proposed_at),
        "decided_at": _iso(proposal.decided_at),
    }


def _top_three_from_record(record: dict[str, Any]) -> TopThreeCandidateProposal:
    return TopThreeCandidateProposal(
        id=record["id"],
        program_id=record["program_id"],
        item_id=record["item_id"],
        reason=record["reason"],
        evidence_refs=tuple(record.get("evidence_refs", ())),
        urgency=record["urgency"],  # type: ignore[arg-type]
        decision_or_action_needed=record["decision_or_action_needed"],
        owner_alias=record.get("owner_alias"),
        confidence=record["confidence"],  # type: ignore[arg-type]
        ai_run_id=record["ai_run_id"],
        status=record.get("status", "staged"),  # type: ignore[arg-type]
        rejection_reason=record.get("rejection_reason"),
        proposed_at=_parse_datetime(record["proposed_at"]) if record.get("proposed_at") else datetime.now(timezone.utc),
        decided_at=_parse_optional_datetime(record.get("decided_at")),
    )


def _governance_decision_brief_to_record(proposal: GovernanceDecisionBriefProposal) -> dict[str, Any]:
    return {
        "proposal_type": "governance_decision_brief",
        "id": proposal.id,
        "program_id": proposal.program_id,
        "decision_ask_id": proposal.decision_ask_id,
        "decision": proposal.decision,
        "context": proposal.context,
        "options": [{"label": option.label, "tradeoffs": option.tradeoffs} for option in proposal.options],
        "recommendation": proposal.recommendation,
        "consequences_of_delay": proposal.consequences_of_delay,
        "owner_alias": proposal.owner_alias,
        "due_date": proposal.due_date.isoformat() if proposal.due_date else None,
        "evidence_refs": list(proposal.evidence_refs),
        "ai_run_id": proposal.ai_run_id,
        "status": proposal.status,
        "rejection_reason": proposal.rejection_reason,
        "proposed_at": _iso(proposal.proposed_at),
        "decided_at": _iso(proposal.decided_at),
    }


def _governance_decision_brief_from_record(record: dict[str, Any]) -> GovernanceDecisionBriefProposal:
    return GovernanceDecisionBriefProposal(
        id=record["id"],
        program_id=record["program_id"],
        decision_ask_id=record["decision_ask_id"],
        decision=record["decision"],
        context=record["context"],
        options=tuple(
            GovernanceDecisionOption(label=entry["label"], tradeoffs=entry["tradeoffs"])
            for entry in record.get("options", ())
        ),
        recommendation=record["recommendation"],
        consequences_of_delay=record["consequences_of_delay"],
        owner_alias=record.get("owner_alias"),
        due_date=date.fromisoformat(record["due_date"]) if record.get("due_date") else None,
        evidence_refs=tuple(record.get("evidence_refs", ())),
        ai_run_id=record["ai_run_id"],
        status=record.get("status", "staged"),  # type: ignore[arg-type]
        rejection_reason=record.get("rejection_reason"),
        proposed_at=_parse_datetime(record["proposed_at"]) if record.get("proposed_at") else datetime.now(timezone.utc),
        decided_at=_parse_optional_datetime(record.get("decided_at")),
    )


def _blast_radius_to_record(proposal: DependencyBlastRadiusProposal) -> dict[str, Any]:
    return {
        "proposal_type": "dependency_blast_radius",
        "id": proposal.id,
        "program_id": proposal.program_id,
        "dependency_id": proposal.dependency_id,
        "next_proving_event": proposal.next_proving_event,
        "blast_radius_narrative": proposal.blast_radius_narrative,
        "evidence_refs": list(proposal.evidence_refs),
        "ai_run_id": proposal.ai_run_id,
        "status": proposal.status,
        "rejection_reason": proposal.rejection_reason,
        "proposed_at": _iso(proposal.proposed_at),
        "decided_at": _iso(proposal.decided_at),
    }


def _blast_radius_from_record(record: dict[str, Any]) -> DependencyBlastRadiusProposal:
    return DependencyBlastRadiusProposal(
        id=record["id"],
        program_id=record["program_id"],
        dependency_id=record["dependency_id"],
        next_proving_event=record["next_proving_event"],
        blast_radius_narrative=record["blast_radius_narrative"],
        evidence_refs=tuple(record.get("evidence_refs", ())),
        ai_run_id=record["ai_run_id"],
        status=record.get("status", "staged"),  # type: ignore[arg-type]
        rejection_reason=record.get("rejection_reason"),
        proposed_at=_parse_datetime(record["proposed_at"]) if record.get("proposed_at") else datetime.now(timezone.utc),
        decided_at=_parse_optional_datetime(record.get("decided_at")),
    )


__all__ = [
    "AIReviewProposalStoreError",
    "REVIEW_PROPOSAL_TYPES",
    "ReviewProposal",
    "ReviewProposalType",
    "get_ai_review_proposals_path",
    "load_proposal",
    "load_proposals",
    "stage_proposal",
]
