"""ADF-W4.5 remainder (specs/arch-data-fix.md Section 8.10.2): dependency
blast-radius proposals.

"The output must explain: upstream source; downstream impact; affected
milestone/workstream; owner; current status; next proving event;
blast-radius narrative."

Five of those seven are already real, structured fields on `Dependency`
(`from_*` is the upstream source, `to_*`+`risk_if_broken` is the downstream
impact, `to_workstream_id`/`to_milestone_id` is the affected
milestone/workstream, `owner_alias` is the owner, `status` is the current
status) -- the actual gap this module closes is the remaining two:
``next_proving_event`` and ``blast_radius_narrative``, added additively to
`Dependency` in ADF-W4.5 with a backward-compatible `None` default, the
same pattern ADF-W4.4 already used for `evidence_tier`/`evidence_refs`.

This module owns the Zone-A-safe half: the proposal type, deterministic
assembly of a request from a `Dependency`'s own already-structured fields,
and applying an approved proposal (never touching a `Dependency` without
explicit human approval). The AI call is Zone B -- see
``src/ai/dependency_blast_radius_generator.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.core.models_v2 import Dependency
from src.core.proposal_audit import record_proposal_event

BlastRadiusStatus = Literal["staged", "approved", "rejected"]


class DependencyBlastRadiusError(Exception):
    """Raised when a DependencyBlastRadiusProposal cannot be assembled or applied."""


@dataclass(frozen=True, slots=True)
class DependencyBlastRadiusRequest:
    program_id: str
    dependency_id: str
    from_summary: str
    to_summary: str
    risk_if_broken: str
    current_status: str
    evidence_texts: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DependencyBlastRadiusProposal:
    id: str
    program_id: str
    dependency_id: str
    next_proving_event: str
    blast_radius_narrative: str
    evidence_refs: tuple[str, ...]
    ai_run_id: str
    status: BlastRadiusStatus = "staged"
    rejection_reason: str | None = None
    # ADF-W2.11/W4.8 (ADR-0017): additive workflow-measurement timestamps.
    proposed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None


def assemble_dependency_blast_radius_request(
    dependency: Dependency, *, evidence_texts: tuple[str, ...] = ()
) -> DependencyBlastRadiusRequest:
    """Deterministic assembly entirely from the dependency's own already-
    structured fields -- five of Section 8.10.2's seven required output
    facets are already real data here, not proposed."""
    from_summary = _describe_endpoint(
        program_id=dependency.from_program_id,
        workstream_id=dependency.from_workstream_id,
        item_id=dependency.from_item_id,
        milestone_id=dependency.from_milestone_id,
    )
    to_summary = _describe_endpoint(
        program_id=dependency.to_program_id,
        workstream_id=dependency.to_workstream_id,
        item_id=dependency.to_item_id,
        milestone_id=dependency.to_milestone_id,
    )
    return DependencyBlastRadiusRequest(
        program_id=dependency.from_program_id,
        dependency_id=dependency.id,
        from_summary=from_summary,
        to_summary=to_summary,
        risk_if_broken=dependency.risk_if_broken,
        current_status=dependency.status.value,
        evidence_texts=evidence_texts,
        evidence_refs=dependency.evidence_refs,
    )


def _describe_endpoint(
    *, program_id: str, workstream_id: str | None, item_id: int | None, milestone_id: str | None
) -> str:
    parts = [f"program={program_id}"]
    if workstream_id:
        parts.append(f"workstream={workstream_id}")
    if item_id is not None:
        parts.append(f"WI:{item_id}")
    if milestone_id:
        parts.append(f"milestone={milestone_id}")
    return ", ".join(parts)


def apply_dependency_blast_radius_proposal(
    dependency: Dependency, proposal: DependencyBlastRadiusProposal
) -> Dependency:
    """Only fires on human approval, and only against the exact dependency
    the proposal was assembled from -- mirrors every other
    proposal-to-record application built this session
    (ADF-W4.5's `apply_risk_proposal`, ADF-W3.5's routing gate)."""
    if proposal.status != "approved":
        raise DependencyBlastRadiusError(
            f"DependencyBlastRadiusProposal {proposal.id!r} has status={proposal.status!r}, not 'approved' -- "
            "only a human-approved proposal may be applied."
        )
    if dependency.id != proposal.dependency_id:
        raise DependencyBlastRadiusError(
            f"DependencyBlastRadiusProposal {proposal.id!r} targets dependency {proposal.dependency_id!r}, "
            f"not {dependency.id!r}."
        )
    return replace(
        dependency,
        next_proving_event=proposal.next_proving_event,
        blast_radius_narrative=proposal.blast_radius_narrative,
    )


def approve_blast_radius_proposal(
    proposal: DependencyBlastRadiusProposal, *, programs_root: Path | None = None
) -> DependencyBlastRadiusProposal:
    if proposal.status == "rejected":
        raise DependencyBlastRadiusError(
            f"DependencyBlastRadiusProposal {proposal.id!r} was rejected ({proposal.rejection_reason}) -- cannot approve."
        )
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="dependency_blast_radius", proposal_id=proposal.id,
        event="approved", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id,
    )
    return replace(proposal, status="approved", decided_at=decided_at)


def reject_blast_radius_proposal(
    proposal: DependencyBlastRadiusProposal, *, reason: str, programs_root: Path | None = None
) -> DependencyBlastRadiusProposal:
    decided_at = datetime.now(timezone.utc)
    record_proposal_event(
        program_id=proposal.program_id, proposal_type="dependency_blast_radius", proposal_id=proposal.id,
        event="rejected", programs_root=programs_root, at=decided_at, proposed_at=proposal.proposed_at,
        ai_run_id=proposal.ai_run_id, rejection_reason=reason,
    )
    return replace(proposal, status="rejected", rejection_reason=reason, decided_at=decided_at)


__all__ = [
    "BlastRadiusStatus",
    "DependencyBlastRadiusError",
    "DependencyBlastRadiusProposal",
    "DependencyBlastRadiusRequest",
    "apply_dependency_blast_radius_proposal",
    "approve_blast_radius_proposal",
    "assemble_dependency_blast_radius_request",
    "reject_blast_radius_proposal",
]
