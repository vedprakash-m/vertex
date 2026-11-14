"""Input integrity for both interactive blind-comparison pilots."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from src.ai.ai_mode import AIMode
from src.ai.program_synthesizer import ProgramSynthesisOutcome
from src.commands import decision_brief_pilot, program_synthesizer_pilot
from src.core.blind_ab_comparison import (
    blind_ab_comparison_path,
    read_comparisons,
    recommend_swap_decision,
    record_comparison,
    summarize_comparisons,
)
from src.core.decision_brief_engine import DecisionBrief, DecisionItem
from src.core.program_synthesis import (
    ProgramSynthesis,
    ProgramSynthesisRequest,
    SynthesisInputItem,
)


def _prepare_review(monkeypatch, tmp_path: Path, variant: str, *, sections=("risks",)):
    root = tmp_path / "programs"
    program = root / "acme"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text(
        "schema_version: '2.0'\nid: acme\nname: Acme\nai:\n  enabled: true\n"
        "  budget_usd_per_run: 0.5\n  temperature: 0.2\n",
        encoding="utf-8",
    )
    if variant == "decision":
        module = decision_brief_pilot
        items = tuple(
            DecisionItem(
                section_id=section, section_title=section, current_text="Current",
                proposed_text="Proposed", evidence_delta_lines=("Evidence",),
                top_signals=(), vitality_summary="declining", confidence="medium",
                kpi_summary="Metric", stale_claims=(), accept_command="accept",
                reject_command="reject", accept_modified_command="modify",
            )
            for section in sections
        )
        brief = DecisionBrief(
            issue_number=12, edition_name="acme_weekly", generated_at="2026-06-06 12:00",
            items=items, total_pending=len(items), ai_enriched=False,
        )
        monkeypatch.setattr(module, "load_pending_decision_brief", lambda **kwargs: (brief, "acme"))
        monkeypatch.setattr(module, "_preflight_decision_brief_workspace", lambda: None)
        baseline = Mock(side_effect=lambda *, client, brief, program_id, programs_root: replace(
            brief, items=tuple(replace(item, verdict="ACCEPT", verdict_reasoning="baseline") for item in brief.items),
        ))
        candidate = Mock(side_effect=lambda *, client, brief, program_id, programs_root: replace(
            brief, items=tuple(replace(item, verdict="REVISE", verdict_reasoning="candidate") for item in brief.items),
        ))
        monkeypatch.setattr(module, "advise_on_decision_brief", baseline)
        monkeypatch.setattr(module, "advise_on_decision_brief_via_context_gateway", candidate)
        surface = "decision_brief_advisor"
        kwargs = {"edition_name": "acme_weekly"}
        cli_args = ["compare", "--edition", "acme_weekly"]
    else:
        module = program_synthesizer_pilot
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        request = ProgramSynthesisRequest(
            program_id="acme", as_of=now,
            items=(SynthesisInputItem(category="strategic_risk", item_id="risk-1", summary="Delay", severity="high"),),
        )
        monkeypatch.setattr(module, "assemble_program_synthesis_request", lambda program_id, **kwargs: request)

        def outcome(text: str) -> ProgramSynthesisOutcome:
            synthesis = ProgramSynthesis(
                program_id="acme", ai_run_id="run-1", through_line=text,
                long_poles=(), facts=(), inferences=(), recommendations=(),
                generated_at=now, prompt_version="v1", source_item_count=1,
            )
            return ProgramSynthesisOutcome(ai_run_id="run-1", released=True, synthesis=synthesis, findings=())

        baseline = Mock(return_value=outcome("baseline"))
        candidate = Mock(return_value=outcome("candidate"))
        monkeypatch.setattr(module, "generate_program_synthesis", baseline)
        monkeypatch.setattr(module, "generate_program_synthesis_via_context_gateway", candidate)
        surface = "program_synthesizer"
        kwargs = {"program_id": "acme"}
        cli_args = ["compare", "--program", "acme"]

    monkeypatch.setattr(module, "get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr(module, "resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",))
    monkeypatch.setattr(module, "FallbackAIClient", lambda **kwargs: object())
    return SimpleNamespace(
        root=root, module=module, surface=surface, kwargs=kwargs, cli_args=cli_args,
        baseline=baseline, candidate=candidate,
    )


@pytest.fixture(params=("decision", "synthesis"))
def review(request, monkeypatch, tmp_path: Path):
    return _prepare_review(monkeypatch, tmp_path, request.param)


@pytest.mark.parametrize(
    ("preference", "critical", "expected_choice", "expected_critical"),
    (
        ("  A ", " YES ", "a", True),
        ("B", "N", "b", False),
        ("Tie", "NO", "tie", False),
        (" Neither ", "", "neither", False),
        ("a", "y", "a", True),
    ),
)
def test_normalized_existing_choices(review, preference, critical, expected_choice, expected_critical) -> None:
    answers = iter((preference, critical))
    echoed: list[str] = []
    review.module.run_context_gateway_comparison(
        **review.kwargs, programs_root=review.root, seed=1,
        prompt_fn=lambda _message: next(answers), echo_fn=echoed.append,
    )
    record, = read_comparisons("acme", surface=review.surface, programs_root=review.root)
    assert (record.choice, record.critical_error) == (expected_choice, expected_critical)
    assert not any(message.startswith("Enter ") for message in echoed)
    review.baseline.assert_called_once()
    review.candidate.assert_called_once()


@pytest.mark.parametrize(
    ("invalid_preference", "invalid_critical"),
    (("unexpected", "not sure"), ("", "b"), ("tiee", "true")),
)
def test_invalid_answers_are_reprompted_before_recording(review, invalid_preference, invalid_critical) -> None:
    answers = iter((invalid_preference, "  B ", invalid_critical, " Yes "))
    prompts: list[str] = []
    echoed: list[str] = []

    def prompt(message: str) -> str:
        assert read_comparisons("acme", programs_root=review.root) == ()
        prompts.append(message)
        return next(answers)

    review.module.run_context_gateway_comparison(
        **review.kwargs, programs_root=review.root, seed=1, prompt_fn=prompt, echo_fn=echoed.append,
    )
    assert len(prompts) == 4 and prompts[0] == prompts[1] and prompts[2] == prompts[3]
    assert "Enter a, b, tie, or neither." in echoed
    assert "Enter y or n." in echoed
    record, = read_comparisons("acme", surface=review.surface, programs_root=review.root)
    assert (record.choice, record.critical_error) == ("b", True)
    assert recommend_swap_decision(summarize_comparisons((record,), surface=record.surface)) == "hold"
    review.baseline.assert_called_once()
    review.candidate.assert_called_once()


@pytest.mark.parametrize(
    ("answers", "succeeds", "expected_critical"),
    (
        ("unexpected\nb\nnot sure\ny\n", True, True),
        ("b\n\nn\n", True, False),
        ("b\nnot sure\n", False, None),
    ),
)
def test_actual_cli_retries_and_eof(review, monkeypatch, answers, succeeds, expected_critical) -> None:
    run = review.module.run_context_gateway_comparison
    monkeypatch.setattr(
        review.module, "run_context_gateway_comparison",
        lambda **kwargs: run(programs_root=review.root, **kwargs),
    )
    result = CliRunner().invoke(review.module.app, review.cli_args, input=answers)
    assert (result.exit_code == 0) is succeeds, result.output
    assert "[y/n]" in result.output
    if answers.startswith("unexpected"):
        assert "Enter a, b, tie, or neither." in result.output
    if answers == "b\n\nn\n":
        assert result.output.count("Did either option contain a critical error") == 2
    if "not sure" in answers:
        assert "Enter y or n." in result.output
    records = read_comparisons("acme", surface=review.surface, programs_root=review.root)
    if succeeds:
        assert len(records) == 1
        assert (records[0].choice, records[0].critical_error) == ("b", expected_critical)
    else:
        assert records == ()
    review.baseline.assert_called_once()
    review.candidate.assert_called_once()


def test_interrupted_invalid_preference_writes_nothing(review) -> None:
    answers = iter(("invalid",))

    def prompt(_message: str) -> str:
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    with pytest.raises(KeyboardInterrupt):
        review.module.run_context_gateway_comparison(
            **review.kwargs, programs_root=review.root, prompt_fn=prompt, echo_fn=lambda _message: None,
        )
    assert read_comparisons("acme", programs_root=review.root) == ()


def test_interrupted_invalid_critical_answer_preserves_existing_judgment(review) -> None:
    record_comparison(
        program_id="acme", surface=review.surface, item_id="previous",
        option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
        programs_root=review.root, critical_error=False,
    )
    path = blind_ab_comparison_path("acme", programs_root=review.root)
    original = path.read_bytes()
    answers = iter(("b", "not sure"))

    def prompt(_message: str) -> str:
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    with pytest.raises(KeyboardInterrupt):
        review.module.run_context_gateway_comparison(
            **review.kwargs, programs_root=review.root, prompt_fn=prompt, echo_fn=lambda _message: None,
        )
    assert path.read_bytes() == original
    record, = read_comparisons("acme", surface=review.surface, programs_root=review.root)
    assert (record.item_id, record.choice, record.critical_error) == ("previous", "a", False)
    review.baseline.assert_called_once()
    review.candidate.assert_called_once()


def test_interruption_preserves_earlier_item_in_same_decision_brief(monkeypatch, tmp_path: Path) -> None:
    review = _prepare_review(monkeypatch, tmp_path, "decision", sections=("risks", "dependencies"))
    answers = iter(("a", "n", "b", "not sure"))

    def prompt(_message: str) -> str:
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    with pytest.raises(KeyboardInterrupt):
        review.module.run_context_gateway_comparison(
            **review.kwargs, programs_root=review.root, prompt_fn=prompt, echo_fn=lambda _message: None,
        )
    record, = read_comparisons("acme", surface=review.surface, programs_root=review.root)
    assert (record.item_id, record.choice, record.critical_error) == ("risks", "a", False)
    review.baseline.assert_called_once()
    review.candidate.assert_called_once()


def _isolate_summary_workspace(review, monkeypatch, tmp_path: Path) -> None:
    if review.module is decision_brief_pilot:
        monkeypatch.setattr(
            review.module, "_resolve_decision_brief_workspace",
            lambda: (review.root, tmp_path / "reports", tmp_path / "archive"),
        )
    monkeypatch.setattr(
        review.module, "read_comparisons",
        lambda program_id, *, surface, **kwargs: read_comparisons(
            program_id, surface=surface, programs_root=review.root,
        ),
    )


@pytest.mark.parametrize("existing", [False, True])
def test_summary_preserves_valid_and_absent_journals(review, monkeypatch, tmp_path: Path, existing: bool) -> None:
    if existing:
        for surface, critical_error in ((review.surface, False), ("other-surface", True)):
            record_comparison(
                program_id="acme", surface=surface, item_id="valid",
                option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
                programs_root=review.root, critical_error=critical_error,
            )
    _isolate_summary_workspace(review, monkeypatch, tmp_path)

    result = CliRunner().invoke(review.module.app, ["summary", "--program", "acme"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert f"{int(existing)} comparison(s) recorded" in result.output
    assert not result.stderr
    if existing:
        assert "Critical errors: 0" in result.output
        assert "INSUFFICIENT_DATA" in result.output
    review.baseline.assert_not_called()
    review.candidate.assert_not_called()


@pytest.mark.parametrize("kind", ["malformed", "nonobject", "encoding", "foreign", "timestamp"])
def test_summary_refuses_invalid_journal_before_recommending(review, monkeypatch, tmp_path: Path, kind: str) -> None:
    record_comparison(
        program_id="acme", surface=review.surface, item_id="previous",
        option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
        programs_root=review.root,
    )
    path = blind_ab_comparison_path("acme", programs_root=review.root)
    valid_line = path.read_bytes()
    if kind == "malformed":
        invalid_line = b'{"private-marker":\n'
    elif kind == "nonobject":
        invalid_line = b'["private-marker"]\n'
    elif kind == "encoding":
        invalid_line = b"\xff\n"
    else:
        row = json.loads(valid_line)
        if kind == "foreign":
            row["program_id"] = "private-other-program"
        else:
            row["recorded_at"] = "private-invalid-timestamp"
        invalid_line = (json.dumps(row) + "\n").encode()
    path.write_bytes(valid_line * 30 + invalid_line)
    original = path.read_bytes()
    checksum = path.with_suffix(".sha256").read_bytes()
    _isolate_summary_workspace(review, monkeypatch, tmp_path)

    result = CliRunner().invoke(review.module.app, ["summary", "--program", "acme"])
    assert result.exit_code == 2, (result.output, result.exception)
    assert "Blind-comparison journal" in result.stderr
    assert "private-" not in result.output
    assert "comparison(s) recorded" not in result.output
    assert "BL-D3 decision" not in result.output
    assert path.read_bytes() == original
    assert path.with_suffix(".sha256").read_bytes() == checksum
    assert not (path.parent / "quarantine").exists()
