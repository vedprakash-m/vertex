"""ADF-W3.7 remainder: unit tests for src/core/context_gap_reply.py."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import builtins
import os
from pathlib import Path
import shutil

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
        feature="workstream_registry", program="samplealpha", lane="deployment", field="why",
        severity="quality_degraded", impact_estimate="high", count=1, first_seen=_NOW, last_seen=_NOW,
        message="deep_context.why is missing", fix_hint="add deep_context.why",
    )


def _unsupported_gap() -> RankedGap:
    return RankedGap(
        feature="kpis", program="samplealpha", lane=None, field="validated",
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


def _seed_registry(programs_root: Path, *, program_id: str = "samplealpha") -> Path:
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


def _assert_manual_only_refusal(proposal: ContextGapAnswerProposal, programs_root: Path) -> None:
    def unexpected_io(*args, **kwargs):
        pytest.fail("Legacy reply application attempted filesystem I/O")

    with pytest.MonkeyPatch.context() as patch:
        for method in ("exists", "stat", "open", "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "replace"):
            patch.setattr(Path, method, unexpected_io)
        patch.setattr(builtins, "open", unexpected_io)
        patch.setattr(shutil, "copy2", unexpected_io)
        patch.setattr(os, "replace", unexpected_io)
        with pytest.raises(ContextGapReplyError, match="manual-diff-only.*vertex context manual-diff"):
            apply_context_gap_answer(proposal, programs_root=programs_root)


@pytest.mark.parametrize("field", ["why", "what", "how"])
@pytest.mark.parametrize("stale", [False, True])
def test_apply_refuses_approved_field_without_io(tmp_path: Path, field: str, stale: bool) -> None:
    programs_root = tmp_path / "programs"
    path = _seed_registry(programs_root)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["workstreams"][0]["deep_context"][field] = "Prior scalar"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    gap = replace(_deep_context_gap(), field=field)
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(
            _parsed_reply("The reason this workstream exists is X."),
            gap=gap, solicitation_id="solicitation-1", proposal_id="answer-1",
        )
    )
    assert proposal.status == "approved"
    assert proposal.target == ConfigWriteTarget(workstream_id="deployment", field=field)
    if stale:
        document["workstreams"][0]["deep_context"][field] = "New manual scalar"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    _assert_manual_only_refusal(proposal, programs_root)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_apply_preserves_existing_backup_without_io(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = _seed_registry(programs_root)
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(
            _parsed_reply(), gap=_deep_context_gap(), solicitation_id="solicitation-1", proposal_id="answer-1"
        )
    )

    backup_path = path.with_suffix(f"{path.suffix}.bak")
    backup_path.write_bytes(b"Existing retained backup")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    _assert_manual_only_refusal(proposal, programs_root)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_apply_raises_when_workstream_not_found(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_registry(programs_root)
    gap = RankedGap(
        feature="workstream_registry", program="samplealpha", lane="nonexistent", field="why",
        severity="quality_degraded", impact_estimate="high", count=1, first_seen=_NOW, last_seen=_NOW,
        message="x", fix_hint="x",
    )
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(_parsed_reply(), gap=gap, solicitation_id="solicitation-1", proposal_id="answer-1")
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    _assert_manual_only_refusal(proposal, programs_root)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("registry", [None, "not: [valid", "false", "workstreams: not-a-list"])
def test_apply_refuses_missing_or_corrupt_registry_without_io(tmp_path: Path, registry: str | None) -> None:
    programs_root = tmp_path / "programs"
    if registry is not None:
        path = registry_path_for_program("samplealpha", programs_root=programs_root)
        path.parent.mkdir(parents=True)
        path.write_text(registry, encoding="utf-8")
    proposal = approve_context_gap_answer(
        assemble_context_gap_answer_proposal(
            _parsed_reply(), gap=_deep_context_gap(), solicitation_id="solicitation-1", proposal_id="answer-1"
        )
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    _assert_manual_only_refusal(proposal, programs_root)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
