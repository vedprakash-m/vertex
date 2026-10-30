"""ADF-W2.9: unit tests for src/core/program_synthesis.py."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from src.core.exceptions import ConfigError
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


def _synthesis(*, program_id: str = "samplealpha", ai_run_id: str = "run-1") -> ProgramSynthesis:
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
    assert path == program_synthesis_path("samplealpha", "run-1", programs_root=programs_root)
    assert path.exists()

    # Not released yet -- must not be surfaced.
    assert load_latest_released_program_synthesis("samplealpha", programs_root=programs_root) is None

    record_ai_release_decision(
        program_id="samplealpha",
        ai_run_id="run-1",
        terminal=ReleaseTerminal.RELEASED,
        reason="test release",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    reloaded = load_latest_released_program_synthesis("samplealpha", programs_root=programs_root)
    assert reloaded is not None
    assert reloaded.through_line == synthesis.through_line
    assert reloaded.recommendations[0].text == "Do X"
    assert reloaded.recommendations[0].evidence_refs == ("item-1",)


def test_rejected_synthesis_is_never_surfaced(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(ai_run_id="run-rejected"), programs_root=programs_root)
    record_ai_release_decision(
        program_id="samplealpha",
        ai_run_id="run-rejected",
        terminal=ReleaseTerminal.REJECTED,
        reason="failed validation",
        validator_finding_count=1,
        programs_root=programs_root,
    )
    assert load_latest_released_program_synthesis("samplealpha", programs_root=programs_root) is None


def test_load_latest_released_returns_the_most_recent_release(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    older = replace(_synthesis(ai_run_id="run-old"), generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    newer = replace(_synthesis(ai_run_id="run-new"), generated_at=datetime(2026, 6, 15, tzinfo=timezone.utc))
    persist_program_synthesis(older, programs_root=programs_root)
    persist_program_synthesis(newer, programs_root=programs_root)
    for run_id in ("run-old", "run-new"):
        record_ai_release_decision(
            program_id="samplealpha",
            ai_run_id=run_id,
            terminal=ReleaseTerminal.RELEASED,
            reason="test",
            validator_finding_count=0,
            programs_root=programs_root,
        )
    latest = load_latest_released_program_synthesis("samplealpha", programs_root=programs_root)
    assert latest is not None
    assert latest.ai_run_id == "run-new"


def test_load_latest_released_returns_none_when_no_directory(tmp_path: Path) -> None:
    assert load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path / "programs") is None


def test_retained_synthesis_empty_directory_ignores_non_json_files(tmp_path: Path) -> None:
    directory = program_synthesis_path("samplealpha", "unused", programs_root=tmp_path).parent
    directory.mkdir(parents=True)
    assert load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path) is None
    (directory / "notes.txt").write_text("Not a synthesis", encoding="utf-8")
    assert load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path) is None


def test_retained_synthesis_uppercase_json_extension_remains_readable(tmp_path: Path) -> None:
    path = persist_program_synthesis(_synthesis(), programs_root=tmp_path)
    assert path is not None
    path.rename(path.with_suffix(".JSON"))
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="run-1", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=tmp_path,
    )
    assert load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path) == _synthesis()


@pytest.mark.parametrize("damage,error_class", [
    ("json", "JSONDecodeError"), ("null", "TypeError"), ("list", "TypeError"),
    ("missing", "KeyError"), ("date", "ValueError"), ("recommendation", "TypeError"),
    ("unicode", "UnicodeDecodeError"), ("read", "PermissionError"), ("disappeared", "FileNotFoundError"),
])
def test_retained_synthesis_candidate_failure_never_falls_back_to_older_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, error_class: str,
) -> None:
    older = replace(_synthesis(ai_run_id="run-old"), generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    for synthesis in (older, _synthesis()):
        persist_program_synthesis(synthesis, programs_root=tmp_path)
        record_ai_release_decision(
            program_id="samplealpha", ai_run_id=synthesis.ai_run_id, terminal=ReleaseTerminal.RELEASED,
            reason="synthetic control", validator_finding_count=0, programs_root=tmp_path,
        )
    path = program_synthesis_path("samplealpha", "run-1", programs_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if damage in {"json", "null", "list"}:
        path.write_text({"json": "{private-payload", "null": "null", "list": "[]"}[damage], encoding="utf-8")
    elif damage == "unicode":
        path.write_bytes(b"\xff")
    elif damage == "missing":
        del payload["ai_run_id"]
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif damage == "date":
        payload["generated_at"] = "private-invalid-date"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif damage == "recommendation":
        payload["recommendations"] = [None]
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        read_text = Path.read_text

        def fail_candidate_read(candidate, *args, **kwargs):
            if candidate == path:
                if damage == "disappeared":
                    raise FileNotFoundError("private-path-and-data")
                raise PermissionError("private-path-and-data")
            return read_text(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_candidate_read)

    with pytest.raises(ConfigError) as raised:
        load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path)

    assert error_class in str(raised.value)
    assert "retained" in str(raised.value).lower()
    assert "private" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


@pytest.mark.parametrize("failure", [PermissionError, NotADirectoryError])
def test_retained_synthesis_enumeration_failure_is_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[OSError],
) -> None:
    directory = program_synthesis_path("samplealpha", "unused", programs_root=tmp_path).parent
    directory.mkdir(parents=True)
    iterdir = Path.iterdir

    def fail_enumeration(path):
        if path == directory:
            yield directory / "unused.txt"
            raise failure("private directory")
        yield from iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_enumeration)
    with pytest.raises(ConfigError) as raised:
        load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path)
    assert failure.__name__ in str(raised.value)
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("error", [
    RuntimeError("implementation error"), TypeError("release programming error"), ConfigError("release state error"),
])
def test_retained_synthesis_release_errors_propagate_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    persist_program_synthesis(_synthesis(), programs_root=tmp_path)

    def fail_release(*args, **kwargs):
        raise error

    monkeypatch.setattr("src.core.program_synthesis.is_ai_output_released", fail_release)
    with pytest.raises(type(error)) as raised:
        load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path)
    assert raised.value is error


def test_retained_synthesis_parser_implementation_failure_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    persist_program_synthesis(_synthesis(), programs_root=tmp_path)
    error = RuntimeError("implementation error")

    def fail_parse(*args, **kwargs):
        raise error

    monkeypatch.setattr("src.core.program_synthesis._synthesis_from_dict", fail_parse)
    with pytest.raises(RuntimeError) as raised:
        load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path)
    assert raised.value is error


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
        program_id="samplealpha",
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

    request = assemble_program_synthesis_request("samplealpha", programs_root=Path("unused"), as_of=_AS_OF)

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

    request = assemble_program_synthesis_request("samplealpha", programs_root=Path("unused"), as_of=_AS_OF)
    assert request.items == ()


def test_program_synthesis_request_defaults_and_recommendation_shape() -> None:
    request = ProgramSynthesisRequest(program_id="samplealpha", as_of=_AS_OF, items=())
    assert request.coverage_notes == ()
    recommendation = ProgramSynthesisRecommendation(text="Do X", evidence_refs=("a", "b"))
    assert recommendation.evidence_refs == ("a", "b")
