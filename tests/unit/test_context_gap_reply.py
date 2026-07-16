"""ADF-W3.7 remainder: unit tests for src/core/context_gap_reply.py."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.context_gap_reply import (
    ConfigWriteTarget,
    ContextGapAnswerProposal,
    ContextGapReplyError,
    ParsedReply,
    apply_context_gap_answer,
    approve_context_gap_answer,
    assemble_context_gap_answer_proposal,
    reject_context_gap_answer,
    resolve_apply_target,
)
from src.core.context_gap_store import RankedGap
from src.core.workstream_registry import registry_path_for_program

_NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _deep_context_gap() -> RankedGap:
    return RankedGap(
        feature="workstream_registry", program="xpf", lane="deployment", field="why",
        severity="quality_degraded", impact_estimate="high", count=1, first_seen=_NOW, last_seen=_NOW,
        message="deep_context.why is missing", fix_hint="add deep_context.why",
    )


def _unsupported_gap() -> RankedGap:
    return RankedGap(
        feature="kpis", program="xpf", lane=None, field="validated",
        severity="quality_degraded", impact_estimate="medium", count=1, first_seen=_NOW, last_seen=_NOW,
        message="kpi not validated", fix_hint="run a live Kusto query",
    )


def _parsed_reply(text: str = "The reason this workstream exists is X.") -> ParsedReply:
    return ParsedReply(sender_email="alex@example.com", subject="Re: [Vertex] Missing info", body_text=text, reference_marker="solicitation-1")


def test_resolve_apply_target_for_deep_context_gap() -> None:
    target = resolve_apply_target(_deep_context_gap())
    assert target == ConfigWriteTarget(workstream_id="deployment", field="why")


def test_resolve_apply_target_none_for_unsupported_gap() -> None:
    assert resolve_apply_target(_unsupported_gap()) is None


def test_assemble_proposal_carries_target_for_deep_context_gap() -> None:
    proposal = assemble_context_gap_answer_proposal(
        _parsed_reply(), gap=_deep_context_gap(), solicitation_id="solicitation-1", proposal_id="answer-1"
    )
    assert proposal.target == ConfigWriteTarget(workstream_id="deployment", field="why")
    assert proposal.is_auto_applicable is True
    assert proposal.proposed_value == "The reason this workstream exists is X."
    assert proposal.sender_email == "alex@example.com"


def test_assemble_proposal_no_target_for_unsupported_gap() -> None:
    proposal = assemble_context_gap_answer_proposal(
        _parsed_reply(), gap=_unsupported_gap(), solicitation_id="solicitation-2", proposal_id="answer-2"
    )
    assert proposal.target is None
    assert proposal.is_auto_applicable is False


def test_approve_and_reject_lifecycle() -> None:
    proposal = assemble_context_gap_answer_proposal(
        _parsed_reply(), gap=_deep_context_gap(), solicitation_id="solicitation-1", proposal_id="answer-1"
    )
    approved = approve_context_gap_answer(proposal)
    assert approved.status == "approved"

    rejected = reject_context_gap_answer(proposal, reason="not a real answer")
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "not a real answer"
    with pytest.raises(ContextGapReplyError, match="rejected"):
        approve_context_gap_answer(rejected)


def test_apply_raises_when_not_approved(tmp_path: Path) -> None:
    proposal = assemble_context_gap_answer_proposal(
        _parsed_reply(), gap=_deep_context_gap(), solicitation_id="solicitation-1", proposal_id="answer-1"
    )
    with pytest.raises(ContextGapReplyError, match="not 'approved'"):
        apply_context_gap_answer(proposal, programs_root=tmp_path)


def test_apply_raises_when_no_target(tmp_path: Path) -> None:
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(
            _parsed_reply(), gap=_unsupported_gap(), solicitation_id="solicitation-2", proposal_id="answer-2"
        )
    )
    with pytest.raises(ContextGapReplyError, match="no auto-apply target"):
        apply_context_gap_answer(proposal, programs_root=tmp_path)


def _seed_registry(programs_root: Path, *, program_id: str = "xpf") -> Path:
    path = registry_path_for_program(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": "1.0",
        "workstreams": [
            {"id": "deployment", "name": "Deployment", "deep_context": {"what": "existing what text"}},
            {"id": "repair", "name": "Repair"},
        ],
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_apply_writes_the_field_and_preserves_other_workstreams(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = _seed_registry(programs_root)
    gap = _deep_context_gap()  # lane="deployment", field="why"
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(
            _parsed_reply("The reason this workstream exists is X."),
            gap=gap, solicitation_id="solicitation-1", proposal_id="answer-1",
        )
    )

    apply_context_gap_answer(proposal, programs_root=programs_root)

    updated = yaml.safe_load(path.read_text(encoding="utf-8"))
    deployment = next(entry for entry in updated["workstreams"] if entry["id"] == "deployment")
    assert deployment["deep_context"]["why"] == "The reason this workstream exists is X."
    assert deployment["deep_context"]["what"] == "existing what text"  # untouched
    repair = next(entry for entry in updated["workstreams"] if entry["id"] == "repair")
    assert repair == {"id": "repair", "name": "Repair"}  # untouched


def test_apply_writes_a_backup_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = _seed_registry(programs_root)
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(
            _parsed_reply(), gap=_deep_context_gap(), solicitation_id="solicitation-1", proposal_id="answer-1"
        )
    )

    apply_context_gap_answer(proposal, programs_root=programs_root)

    backup_path = path.with_suffix(f"{path.suffix}.bak")
    assert backup_path.exists()
    backed_up = yaml.safe_load(backup_path.read_text(encoding="utf-8"))
    deployment = next(entry for entry in backed_up["workstreams"] if entry["id"] == "deployment")
    assert "why" not in deployment.get("deep_context", {})  # backup has the pre-apply content


def test_apply_raises_when_workstream_not_found(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_registry(programs_root)
    gap = RankedGap(
        feature="workstream_registry", program="xpf", lane="nonexistent", field="why",
        severity="quality_degraded", impact_estimate="high", count=1, first_seen=_NOW, last_seen=_NOW,
        message="x", fix_hint="x",
    )
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(_parsed_reply(), gap=gap, solicitation_id="solicitation-1", proposal_id="answer-1")
    )
    with pytest.raises(ContextGapReplyError, match="not found"):
        apply_context_gap_answer(proposal, programs_root=programs_root)


def test_apply_raises_when_registry_file_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(
            _parsed_reply(), gap=_deep_context_gap(), solicitation_id="solicitation-1", proposal_id="answer-1"
        )
    )
    with pytest.raises(ContextGapReplyError, match="does not exist"):
        apply_context_gap_answer(proposal, programs_root=programs_root)
