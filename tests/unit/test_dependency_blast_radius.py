"""ADF-W4.5 remainder: unit tests for src/core/dependency_blast_radius.py."""

from __future__ import annotations

import pytest

from src.core.dependency_blast_radius import (
    DependencyBlastRadiusError,
    DependencyBlastRadiusProposal,
    apply_dependency_blast_radius_proposal,
    approve_blast_radius_proposal,
    assemble_dependency_blast_radius_request,
    reject_blast_radius_proposal,
)
from src.core.models_v2 import Dependency, DependencyEvidenceTier, DependencyStatus, DependencyType


def _dependency(*, dep_id: str = "dep-1") -> Dependency:
    return Dependency(
        id=dep_id,
        from_program_id="xpf",
        from_workstream_id="deployment",
        from_item_id=1001,
        from_milestone_id=None,
        to_program_id="armada",
        to_workstream_id="platform",
        to_item_id=None,
        to_milestone_id="ms-1",
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Armada's platform milestone slips by two sprints.",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias="alex",
        evidence_tier=DependencyEvidenceTier.AUTHORED,
        evidence_refs=("sig-1",),
    )


def _proposal(*, dep_id: str = "dep-1", status: str = "staged") -> DependencyBlastRadiusProposal:
    return DependencyBlastRadiusProposal(
        id="blast-radius-1",
        program_id="xpf",
        dependency_id=dep_id,
        next_proving_event="The platform API contract review scheduled for next sprint.",
        blast_radius_narrative="If unresolved, Armada's platform milestone slips, cascading to two downstream teams.",
        evidence_refs=("sig-1",),
        ai_run_id="run-1",
        status=status,  # type: ignore[arg-type]
    )


def test_assemble_request_from_dependency() -> None:
    request = assemble_dependency_blast_radius_request(_dependency(), evidence_texts=("Signal text.",))
    assert request.dependency_id == "dep-1"
    assert "program=xpf" in request.from_summary
    assert "WI:1001" in request.from_summary
    assert "program=armada" in request.to_summary
    assert "milestone=ms-1" in request.to_summary
    assert request.risk_if_broken == "Armada's platform milestone slips by two sprints."
    assert request.current_status == "active"
    assert request.evidence_refs == ("sig-1",)
    assert request.evidence_texts == ("Signal text.",)


def test_apply_approved_proposal_populates_new_fields() -> None:
    dependency = _dependency()
    proposal = approve_blast_radius_proposal(_proposal())
    updated = apply_dependency_blast_radius_proposal(dependency, proposal)
    assert updated.next_proving_event == proposal.next_proving_event
    assert updated.blast_radius_narrative == proposal.blast_radius_narrative
    # original fields untouched
    assert updated.id == dependency.id
    assert updated.status == dependency.status


def test_apply_staged_proposal_raises() -> None:
    with pytest.raises(DependencyBlastRadiusError, match="not 'approved'"):
        apply_dependency_blast_radius_proposal(_dependency(), _proposal(status="staged"))


def test_apply_proposal_to_wrong_dependency_raises() -> None:
    dependency = _dependency(dep_id="dep-2")
    proposal = approve_blast_radius_proposal(_proposal(dep_id="dep-1"))
    with pytest.raises(DependencyBlastRadiusError, match="targets dependency"):
        apply_dependency_blast_radius_proposal(dependency, proposal)


def test_approve_rejected_proposal_raises() -> None:
    proposal = reject_blast_radius_proposal(_proposal(), reason="not material")
    with pytest.raises(DependencyBlastRadiusError, match="rejected"):
        approve_blast_radius_proposal(proposal)


def test_reject_records_reason() -> None:
    proposal = reject_blast_radius_proposal(_proposal(), reason="duplicate of dep-9")
    assert proposal.status == "rejected"
    assert proposal.rejection_reason == "duplicate of dep-9"


def test_dependency_new_fields_default_to_none() -> None:
    dependency = _dependency()
    assert dependency.next_proving_event is None
    assert dependency.blast_radius_narrative is None
