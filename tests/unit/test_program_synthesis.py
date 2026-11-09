"""ADF-W2.9: unit tests for src/core/program_synthesis.py."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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


def _persist_timestamp_pair(
    programs_root: Path,
    *,
    program_id: str = "samplealpha",
    timestamps: tuple[datetime, datetime] = (
        datetime(2026, 6, 1),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
    ),
    released: tuple[bool, bool] = (True, True),
) -> tuple[ProgramSynthesis, ProgramSynthesis]:
    records = tuple(
        replace(
            _synthesis(program_id=program_id, ai_run_id=f"synthetic-time-{index}"),
            generated_at=timestamp,
            through_line=f"SYNTHETIC-CANARY-{index}",
        )
        for index, timestamp in enumerate(timestamps)
    )
    for synthesis, is_released in zip(records, released):
        assert persist_program_synthesis(synthesis, programs_root=programs_root) is not None
        if is_released:
            record_ai_release_decision(
                program_id=program_id, ai_run_id=synthesis.ai_run_id, terminal=ReleaseTerminal.RELEASED,
                reason="synthetic control", validator_finding_count=0, programs_root=programs_root,
            )
    return records[0], records[1]


def _persist_foreign_synthesis(
    programs_root: Path,
    *,
    program_id: str = "samplealpha",
    foreign_id: object = "SYNTHETIC-FOREIGN-ID",
    released: bool = True,
) -> Path:
    synthesis = replace(
        _synthesis(program_id=program_id, ai_run_id="synthetic-foreign"),
        through_line="SYNTHETIC-FOREIGN-CANARY",
    )
    path = persist_program_synthesis(synthesis, programs_root=programs_root)
    assert path is not None
    if released:
        record_ai_release_decision(
            program_id=program_id, ai_run_id=synthesis.ai_run_id, terminal=ReleaseTerminal.RELEASED,
            reason="synthetic control", validator_finding_count=0,
            released_content_hash=content_hash_for_synthesis(synthesis), programs_root=programs_root,
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["program_id"] = foreign_id
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("foreign_id", ["SYNTHETIC-FOREIGN-ID", "", None, 0, False, [], {}])
def test_retained_synthesis_rejects_explicit_foreign_program_identity_without_mutation(
    tmp_path: Path, foreign_id: object,
) -> None:
    import traceback

    root = tmp_path / "programs"
    _persist_foreign_synthesis(root, foreign_id=foreign_id)
    before = {p.relative_to(root): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}

    with pytest.raises(ConfigError) as raised:
        load_latest_released_program_synthesis("samplealpha", programs_root=root)

    assert str(raised.value) == "Retained program synthesis read failed (ValueError)."
    diagnostic = "".join(traceback.format_exception(raised.value))
    assert "SYNTHETIC-FOREIGN" not in diagnostic and str(tmp_path) not in diagnostic
    assert {p.relative_to(root): p.read_bytes() if p.is_file() else None for p in root.rglob("*")} == before


@pytest.mark.parametrize("foreign_first", [False, True])
@pytest.mark.parametrize("released", [False, True])
def test_foreign_synthesis_never_falls_back_to_another_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_first: bool, released: bool,
) -> None:
    valid = replace(_synthesis(ai_run_id="synthetic-valid"), generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    valid_path = persist_program_synthesis(valid, programs_root=tmp_path)
    assert valid_path is not None
    record_ai_release_decision(
        program_id=valid.program_id, ai_run_id=valid.ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=tmp_path,
    )
    foreign_path = _persist_foreign_synthesis(tmp_path, released=released)
    directory = valid_path.parent
    paths = (foreign_path, valid_path) if foreign_first else (valid_path, foreign_path)
    iterdir = Path.iterdir

    def ordered(candidate: Path):
        return iter(paths) if candidate == directory else iterdir(candidate)

    monkeypatch.setattr(Path, "iterdir", ordered)
    before = {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}
    with pytest.raises(ConfigError):
        load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path)
    assert {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")} == before


@pytest.mark.parametrize("missing", [False, True])
def test_retained_synthesis_matching_or_absent_program_identity_remains_readable(tmp_path: Path, missing: bool) -> None:
    synthesis = _synthesis()
    path = persist_program_synthesis(synthesis, programs_root=tmp_path)
    assert path is not None
    record_ai_release_decision(
        program_id=synthesis.program_id, ai_run_id=synthesis.ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=tmp_path,
    )
    if missing:
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["program_id"]
        path.write_text(json.dumps(payload), encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}
    assert load_latest_released_program_synthesis(synthesis.program_id, programs_root=tmp_path) == synthesis
    assert {p.relative_to(tmp_path): p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")} == before


def test_foreign_synthesis_is_confined_to_selected_programs_root(tmp_path: Path) -> None:
    selected, other = tmp_path / "selected", tmp_path / "other"
    synthesis = _synthesis()
    assert persist_program_synthesis(synthesis, programs_root=selected) is not None
    record_ai_release_decision(
        program_id=synthesis.program_id, ai_run_id=synthesis.ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=selected,
    )
    _persist_foreign_synthesis(other)
    assert load_latest_released_program_synthesis(synthesis.program_id, programs_root=selected) == synthesis
    with pytest.raises(ConfigError):
        load_latest_released_program_synthesis(synthesis.program_id, programs_root=other)


@pytest.mark.parametrize("naive_first", [False, True])
def test_incomparable_released_synthesis_timestamps_raise_safe_error_without_mutation(
    tmp_path: Path, naive_first: bool,
) -> None:
    import traceback

    timestamps = (datetime(2026, 6, 1), datetime(2026, 7, 1, tzinfo=timezone.utc))
    if not naive_first:
        timestamps = timestamps[::-1]
    root = tmp_path / "programs"
    _persist_timestamp_pair(root, timestamps=timestamps)
    before = {p.relative_to(root): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}

    with pytest.raises(ConfigError) as raised:
        load_latest_released_program_synthesis("samplealpha", programs_root=root)

    assert str(raised.value) == "Retained program synthesis timestamps cannot be compared."
    assert "SYNTHETIC-CANARY" not in "".join(traceback.format_exception(raised.value))
    assert str(tmp_path) not in str(raised.value)
    assert {p.relative_to(root): p.read_bytes() if p.is_file() else None for p in root.rglob("*")} == before


@pytest.mark.parametrize("naive", [False, True])
def test_comparable_synthesis_timestamps_preserve_existing_ordering(tmp_path: Path, naive: bool) -> None:
    if naive:
        timestamps = (datetime(2026, 6, 1), datetime(2026, 7, 1))
        latest_index = 1
    else:
        timestamps = (
            datetime(2026, 7, 1, 5, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 8, tzinfo=timezone(timedelta(hours=5))),
        )
        latest_index = 0
    records = _persist_timestamp_pair(tmp_path, timestamps=timestamps)
    assert load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path) == records[latest_index]


@pytest.mark.parametrize("released", [(True, False), (False, True), (False, False)])
def test_unreleased_incomparable_synthesis_does_not_change_selection(
    tmp_path: Path, released: tuple[bool, bool],
) -> None:
    records = _persist_timestamp_pair(tmp_path, released=released)
    expected = records[released.index(True)] if any(released) else None
    assert load_latest_released_program_synthesis("samplealpha", programs_root=tmp_path) == expected


def test_incomparable_synthesis_is_confined_to_selected_programs_root(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    records = _persist_timestamp_pair(selected, released=(True, False))
    _persist_timestamp_pair(other)
    assert load_latest_released_program_synthesis("samplealpha", programs_root=selected) == records[0]
    with pytest.raises(ConfigError):
        load_latest_released_program_synthesis("samplealpha", programs_root=other)


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


@pytest.mark.parametrize("damage", ["envelope", "payload"])
def test_released_synthesis_rejects_damaged_release_ledger_without_repair(tmp_path: Path, damage: str) -> None:
    from src.core.ledger.event_log import get_event_logs_dir

    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="run-1", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=programs_root,
    )
    event_dir = get_event_logs_dir("samplealpha", programs_root=programs_root)
    path = next(event_dir.glob("*.events.jsonl"))
    if damage == "envelope":
        path.write_bytes(path.read_bytes() + b'{"broken":')
    else:
        row = json.loads(path.read_text(encoding="utf-8"))
        del row["payload"]["validator_finding_count"]
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = {
        p.relative_to(programs_root): p.read_bytes() if p.is_file() else None
        for p in programs_root.rglob("*")
    }

    with pytest.raises(ConfigError):
        load_latest_released_program_synthesis("samplealpha", programs_root=programs_root)

    assert {
        p.relative_to(programs_root): p.read_bytes() if p.is_file() else None
        for p in programs_root.rglob("*")
    } == before


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
