"""ADF-W2.9: unit tests for src/core/program_synthesis.py."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import (
    AIProposal,
    AIProposalStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    WorkstreamSynthesis,
)
from src.core.program_reality import RealityConflict
from src.core.program_synthesis import (
    INPUT_CATEGORIES,
    ProgramSynthesis,
    ProgramSynthesisRecommendation,
    ProgramSynthesisRequest,
    SynthesisInputItem,
    assemble_program_synthesis_request,
    content_hash_for_synthesis,
    load_latest_released_program_synthesis,
    persist_program_synthesis,
    program_synthesis_path,
)
from src.core.quality_gates.ai_release_audit import ReleaseTerminal, record_ai_release_decision
from src.core.truth_levels import TruthLevel

_AS_OF = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _synthesis(*, program_id: str = "xpf", ai_run_id: str = "run-1") -> ProgramSynthesis:
    return ProgramSynthesis(
        program_id=program_id,
        ai_run_id=ai_run_id,
        through_line="Program is on track except for X.",
        long_poles=("milestone M1",),
        facts=("fact A",),
        inferences=("inference B",),
        recommendations=(ProgramSynthesisRecommendation(text="Do X", evidence_refs=("item-1",)),),
        generated_at=_AS_OF,
        prompt_version="program_synthesis.v1",
        source_item_count=3,
    )


def test_synthesis_input_item_categories_are_a_documented_set() -> None:
    item = SynthesisInputItem(category="strategic_risk", item_id="r1", summary="s")
    assert item.category in INPUT_CATEGORIES


def test_persist_and_reload_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    synthesis = _synthesis()
    path = persist_program_synthesis(synthesis, programs_root=programs_root)
    assert path == program_synthesis_path("xpf", "run-1", programs_root=programs_root)
    assert path.exists()

    # Not released yet -- must not be surfaced.
    assert load_latest_released_program_synthesis("xpf", programs_root=programs_root) is None

    record_ai_release_decision(
        program_id="xpf",
        ai_run_id="run-1",
        terminal=ReleaseTerminal.RELEASED,
        reason="test release",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    reloaded = load_latest_released_program_synthesis("xpf", programs_root=programs_root)
    assert reloaded is not None
    assert reloaded.through_line == synthesis.through_line
    assert reloaded.recommendations[0].text == "Do X"
    assert reloaded.recommendations[0].evidence_refs == ("item-1",)


def test_rejected_synthesis_is_never_surfaced(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(ai_run_id="run-rejected"), programs_root=programs_root)
    record_ai_release_decision(
        program_id="xpf",
        ai_run_id="run-rejected",
        terminal=ReleaseTerminal.REJECTED,
        reason="failed validation",
        validator_finding_count=1,
        programs_root=programs_root,
    )
    assert load_latest_released_program_synthesis("xpf", programs_root=programs_root) is None


def test_load_latest_released_returns_the_most_recent_release(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    older = replace(_synthesis(ai_run_id="run-old"), generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    newer = replace(_synthesis(ai_run_id="run-new"), generated_at=datetime(2026, 6, 15, tzinfo=timezone.utc))
    persist_program_synthesis(older, programs_root=programs_root)
    persist_program_synthesis(newer, programs_root=programs_root)
    for run_id in ("run-old", "run-new"):
        record_ai_release_decision(
            program_id="xpf",
            ai_run_id=run_id,
            terminal=ReleaseTerminal.RELEASED,
            reason="test",
            validator_finding_count=0,
            programs_root=programs_root,
        )
    latest = load_latest_released_program_synthesis("xpf", programs_root=programs_root)
    assert latest is not None
    assert latest.ai_run_id == "run-new"


def test_load_latest_released_returns_none_when_no_directory(tmp_path: Path) -> None:
    assert load_latest_released_program_synthesis("xpf", programs_root=tmp_path / "programs") is None


def test_persist_survives_disk_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    programs_root = tmp_path / "programs"

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = persist_program_synthesis(_synthesis(), programs_root=programs_root)
    assert result is None


def test_content_hash_is_deterministic_and_sensitive_to_content() -> None:
    a = _synthesis()
    b = _synthesis()
    c = replace(_synthesis(), through_line="Different through-line.")
    assert content_hash_for_synthesis(a) == content_hash_for_synthesis(b)
    assert content_hash_for_synthesis(a) != content_hash_for_synthesis(c)


# ---------------------------------------------------------------------------
# assemble_program_synthesis_request: each real accessor is monkeypatched at
# its use site in src.core.program_synthesis, matching the established
# ProgramReality.load mocking convention used across tests/unit/.
# ---------------------------------------------------------------------------


def _accepted_proposal() -> AIProposal:
    return AIProposal(
        id="proposal-1",
        workstream_id="ws-1",
        synthesis=WorkstreamSynthesis(
            workstream_id="ws-1",
            overall_assessment="Workstream is healthy.",
            proposed_risk=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            key_findings=("finding 1",),
            evidence_refs=("sig-1",),
            open_questions=(),
            recommended_actions=(),
        ),
        status=AIProposalStatus.ACCEPTED,
        created_at=_AS_OF,
        resolved_at=_AS_OF,
        resolved_by="pm@example.com",
    )


def _risk_entry() -> RiskEntry:
    return RiskEntry(
        id="risk-1",
        program_id="xpf",
        title="Vendor delay",
        description="Vendor X is delayed.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.EXTERNAL,
        owner_alias="alice",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 1, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(),
    )


def _mock_reality() -> SimpleNamespace:
    milestone_record = SimpleNamespace(name="Milestone A", status="at_risk", target_date=None)
    milestone_assessment = SimpleNamespace(
        record=milestone_record, fact_id="fact-m1", truth_level=TruthLevel.RAW_OBSERVED, evidence=("ev-1",)
    )
    conflict = RealityConflict(
        conflict_id="conflict-1", entity_refs=("WI:1",), family="icm_vs_evidence_risk", open=True, description="disagreement"
    )
    return SimpleNamespace(
        milestones=lambda: (milestone_assessment,),
        conflicts=lambda open_only=True: (conflict,),
    )


def test_assemble_program_synthesis_request_wires_real_accessors(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.core.program_synthesis as module

    monkeypatch.setattr(module, "load_ai_proposals", lambda program_id, status, programs_root: (_accepted_proposal(),))
    monkeypatch.setattr(module.ProgramReality, "load", lambda program_id, **kwargs: _mock_reality())
    monkeypatch.setattr(module, "load_risk_register", lambda program_id, programs_root: (_risk_entry(),))
    monkeypatch.setattr(module, "load_program", lambda program_id, programs_root: None)  # no signal store -> 0 breaches
    monkeypatch.setattr(module, "load_source_waivers", lambda program_id, programs_root: ())

    request = assemble_program_synthesis_request("xpf", programs_root=Path("unused"), as_of=_AS_OF)

    categories = {item.category for item in request.items}
    assert categories == {"verified_workstream_synthesis", "critical_path_milestone", "strategic_risk", "contradiction"}

    workstream_items = [item for item in request.items if item.category == "verified_workstream_synthesis"]
    assert workstream_items[0].item_id == "proposal-1"
    assert workstream_items[0].summary == "Workstream is healthy."

    milestone_items = [item for item in request.items if item.category == "critical_path_milestone"]
    assert milestone_items[0].severity == "at_risk"

    risk_items = [item for item in request.items if item.category == "strategic_risk"]
    assert "Vendor delay" in risk_items[0].summary

    conflict_items = [item for item in request.items if item.category == "contradiction"]
    assert conflict_items[0].item_id == "conflict-1"

    joined_notes = " ".join(request.coverage_notes)
    assert "dependency_blast_radius" in joined_notes
    assert "decision_or_overdue_ask" in joined_notes
    assert "learned_salience_or_slip_bias" in joined_notes


def test_assemble_program_synthesis_request_filters_out_terminal_milestones(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.core.program_synthesis as module

    terminal_record = SimpleNamespace(name="Done milestone", status="completed", target_date=None)
    terminal_assessment = SimpleNamespace(
        record=terminal_record, fact_id="fact-m2", truth_level=TruthLevel.RAW_OBSERVED, evidence=()
    )
    reality = SimpleNamespace(milestones=lambda: (terminal_assessment,), conflicts=lambda open_only=True: ())

    monkeypatch.setattr(module, "load_ai_proposals", lambda program_id, status, programs_root: ())
    monkeypatch.setattr(module.ProgramReality, "load", lambda program_id, **kwargs: reality)
    monkeypatch.setattr(module, "load_risk_register", lambda program_id, programs_root: ())
    monkeypatch.setattr(module, "load_program", lambda program_id, programs_root: None)
    monkeypatch.setattr(module, "load_source_waivers", lambda program_id, programs_root: ())

    request = assemble_program_synthesis_request("xpf", programs_root=Path("unused"), as_of=_AS_OF)
    assert request.items == ()


def test_program_synthesis_request_defaults_and_recommendation_shape() -> None:
    request = ProgramSynthesisRequest(program_id="xpf", as_of=_AS_OF, items=())
    assert request.coverage_notes == ()
    recommendation = ProgramSynthesisRecommendation(text="Do X", evidence_refs=("a", "b"))
    assert recommendation.evidence_refs == ("a", "b")
