from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import (
    get_proposals_path,
    load_proposals,
    stage_extracted_proposals,
    update_proposal_status,
)


def _workspace(tmp_path: Path, state: str) -> tuple[Path, Path]:
    programs = tmp_path / "programs"
    edition = programs / "sample" / "editions" / "sample_weekly.yaml"
    edition.parent.mkdir(parents=True)
    edition.write_text("program_id: sample\n", encoding="utf-8")
    proposal = ContextUpdateProposal(
        proposal_id="reviewed",
        program_id="sample",
        issue_number=1,
        edition_id="sample_weekly",
        source_type="confirmed_overrides",
        extracted_at=datetime(2026, 6, 27, 12, tzinfo=timezone.utc),
        extractor_version="1.0.0",
        source_artifact="overrides/issue_001.yaml",
        source_field="assumptions.assume-1.text",
        extraction_method="overrides_yaml",
        target_store="assumptions",
        target_key="assume-1",
        target_field="text",
        source_value="reviewed value",
        current_value="baseline",
        current_value_hash=None,
        confidence="high",
        batch_eligible=True,
        extraction_method_rationale="synthetic confirmed override",
        conflict_key="assumptions:assume-1:text",
    )
    stage_extracted_proposals("sample", 1, (proposal,), programs_root=programs)
    if state != "pending":
        update_proposal_status(
            "sample", proposal_id=proposal.proposal_id, new_status="accepted",
            actor="synthetic-reviewer", issue_number=1, programs_root=programs,
        )
    queue = get_proposals_path("sample", 1, programs_root=programs)
    records = json.loads(queue.read_text(encoding="utf-8"))
    record = records[0]
    if state == "changed_value":
        record["source_value"] = "unreviewed value"
    elif state == "changed_target":
        record["target_key"] = "assume-2"
    elif state == "changed_evidence":
        record["source_artifact"] = "overrides/issue_099.yaml"
    elif state == "historic_missing":
        record["decision_history"] = []
    elif state == "historic_hashless":
        record["decision_history"][-1].pop("payload_digest")
    elif state == "historic_reapply":
        record["decision_history"][-1]["from_status"] = "accepted"
    elif state == "applied_metadata":
        update_proposal_status(
            "sample", proposal_id=proposal.proposal_id, new_status="accepted",
            actor="synthetic-applier", issue_number=1, programs_root=programs,
        )
        return programs, queue
    queue.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return programs, queue


def _synthesize(programs: Path, monkeypatch: pytest.MonkeyPatch):
    from cli import app
    import src.commands.context as context_command

    consumed: list[ContextUpdateProposal] = []

    def capture_synthesis(**kwargs):
        consumed.extend(kwargs["accepted"])
        return SimpleNamespace(available=False, note="synthetic synthesis boundary reached")

    monkeypatch.setattr(context_command, "_run_context_synthesis", capture_synthesis)
    result = CliRunner().invoke(app, [
        "context", "synthesize", "--edition", "sample_weekly", "--issue", "1",
        "--programs-root", str(programs), "--dry-run",
    ])
    return result, consumed


@pytest.mark.parametrize("state", ["unchanged", "applied_metadata"])
def test_bound_acceptance_reaches_synthesis_without_rewriting_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    programs, queue = _workspace(tmp_path, state)
    before = queue.read_bytes()

    result, consumed = _synthesize(programs, monkeypatch)

    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "synthetic synthesis boundary reached" in result.output
    assert len(consumed) == 1
    assert consumed[0].source_value == "reviewed value"
    acceptance = next(
        decision for decision in reversed(consumed[0].decision_history)
        if decision.to_status == "accepted" and decision.from_status != "accepted"
    )
    assert acceptance.payload_digest == consumed[0].approval_payload_digest()
    assert replace(consumed[0], decision_history=()).approval_payload_digest() == acceptance.payload_digest
    assert queue.read_bytes() == before


@pytest.mark.parametrize(("state", "reason"), [
    ("changed_value", "proposal payload changed after acceptance"),
    ("changed_target", "proposal payload changed after acceptance"),
    ("changed_evidence", "proposal payload changed after acceptance"),
    ("historic_missing", "acceptance has no payload binding"),
    ("historic_hashless", "acceptance has no payload binding"),
    ("historic_reapply", "acceptance has no payload binding"),
])
def test_unbound_acceptance_is_refused_before_synthesis_without_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, reason: str,
) -> None:
    programs, queue = _workspace(tmp_path, state)
    before = queue.read_bytes()

    result, consumed = _synthesize(programs, monkeypatch)

    assert queue.read_bytes() == before
    assert consumed == [], f"unbound proposal reached synthesis: {result.output}"
    assert result.exit_code != 0
    assert reason in result.output
    assert "re-review" in result.output


def test_pending_proposal_does_not_reach_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, queue = _workspace(tmp_path, "pending")
    before = queue.read_bytes()

    result, consumed = _synthesize(programs, monkeypatch)

    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "No accepted Zone A proposals" in result.output
    assert consumed == []
    assert queue.read_bytes() == before


@pytest.mark.parametrize(("state", "reason"), [
    ("changed_value", "proposal payload changed after acceptance"),
    ("changed_target", "proposal payload changed after acceptance"),
    ("changed_evidence", "proposal payload changed after acceptance"),
    ("historic_missing", "acceptance has no payload binding"),
    ("historic_hashless", "acceptance has no payload binding"),
    ("historic_reapply", "acceptance has no payload binding"),
    ("pending", "not 'accepted'"),
    ("unchanged", None),
    ("applied_metadata", None),
])
def test_direct_synthesizer_checks_binding_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, reason: str | None,
) -> None:
    import src.ai.context_synthesizer as synthesis

    programs, queue = _workspace(tmp_path, state)
    before = queue.read_bytes()
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    inputs = synthesis.SynthesisInputs(
        program_id="sample", edition_id="sample_weekly", issue_number=1,
        accepted_proposals=(proposal,), published_narrative="Synthetic published narrative.",
        knowledge_doc_name="context.md", knowledge_doc_path=tmp_path / "context.md",
        current_knowledge_doc=None,
    )
    dispatched = []
    draft = synthesis.KnowledgeDocDraft(
        summary="Synthetic summary.", highlights=(), open_risks=(),
        next_milestones=(), as_of_date="2026-06-27",
    )

    def route(*args, **kwargs):
        dispatched.append(True)
        return SimpleNamespace(value=draft)

    monkeypatch.setattr(synthesis, "route_through_tiers", route)
    result = synthesis.ContextSynthesizer(client=SimpleNamespace()).synthesize(inputs)

    assert queue.read_bytes() == before
    if reason is None:
        assert result.available
        assert dispatched == [True]
        assert result.proposal.target_store == "knowledge_doc"
    else:
        assert dispatched == []
        assert not result.available
        assert reason in result.note
        assert "re-review" in result.note


@pytest.mark.parametrize(("field", "value"), [
    ("program_id", "other"),
    ("edition_id", "other_weekly"),
    ("issue_number", 2),
])
def test_direct_synthesizer_refuses_bound_proposal_outside_input_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str | int,
) -> None:
    import src.ai.context_synthesizer as synthesis

    programs, queue = _workspace(tmp_path, "unchanged")
    before = queue.read_bytes()
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    assert proposal.acceptance_binding_error() is None
    inputs = synthesis.SynthesisInputs(
        program_id="sample", edition_id="sample_weekly", issue_number=1,
        accepted_proposals=(proposal,), published_narrative="Synthetic published narrative.",
        knowledge_doc_name="context.md", knowledge_doc_path=tmp_path / "context.md",
        current_knowledge_doc=None,
    )
    inputs = replace(inputs, **{field: value})
    dispatched = []

    def route(*args, **kwargs):
        dispatched.append(True)
        return SimpleNamespace(value=None)

    monkeypatch.setattr(synthesis, "route_through_tiers", route)
    result = synthesis.ContextSynthesizer(client=SimpleNamespace()).synthesize(inputs)

    assert queue.read_bytes() == before
    assert dispatched == []
    assert not result.available
    assert result.proposal is None
    assert result.note == "accepted proposal does not match synthesis program/edition/issue scope"
