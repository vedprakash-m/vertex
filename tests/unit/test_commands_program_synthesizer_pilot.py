"""ADF-W2.9: tests for the blind A/B comparison harness CLI
(``vertex program-synthesizer-pilot compare`` / ``summary``).

Mirrors ``test_commands_decision_brief_pilot.py``'s shape: the generator
functions (``generate_program_synthesis`` / ``generate_program_synthesis_
via_context_gateway``) already have their own dedicated unit tests; these
tests monkeypatch them directly to exercise only this module's own wiring --
AI-mode/config gating, deployment resolution, blind randomization, and
comparison recording.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.program_synthesizer import ProgramSynthesisOutcome
from src.commands.program_synthesizer_pilot import (
    ProgramSynthesizerPilotError,
    run_context_gateway_comparison,
)
from src.core.blind_ab_comparison import read_comparisons
from src.core.program_synthesis import (
    ProgramSynthesis,
    ProgramSynthesisRecommendation,
    ProgramSynthesisRequest,
    SynthesisInputItem,
)

_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _write_ai_enabled_program(programs_root: Path, program_id: str = "acme", *, enabled: bool = True) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        f"""
schema_version: '2.0'
id: {program_id}
name: Acme
ai:
  enabled: {"true" if enabled else "false"}
  budget_usd_per_run: 0.5
  temperature: 0.2
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _request(program_id: str = "acme") -> ProgramSynthesisRequest:
    return ProgramSynthesisRequest(
        program_id=program_id,
        as_of=_NOW,
        items=(
            SynthesisInputItem(category="strategic_risk", item_id="risk-1", summary="Vendor delay.", severity="high"),
        ),
    )


def _outcome(*, released: bool, through_line: str = "") -> ProgramSynthesisOutcome:
    synthesis = (
        ProgramSynthesis(
            program_id="acme",
            ai_run_id="run-1",
            through_line=through_line,
            long_poles=("Vendor delivery",),
            facts=("Vendor confirmed a delay.",),
            inferences=("The milestone is at risk.",),
            recommendations=(ProgramSynthesisRecommendation(text="Escalate to vendor management.", evidence_refs=("risk-1",)),),
            generated_at=_NOW,
            prompt_version="v1",
            source_item_count=1,
        )
        if released
        else None
    )
    return ProgramSynthesisOutcome(ai_run_id="run-1", released=released, synthesis=synthesis, findings=())


def test_run_comparison_raises_when_ai_mode_disabled(tmp_path: Path) -> None:
    set_ai_mode(AIMode.DISABLED)
    try:
        try:
            run_context_gateway_comparison(program_id="acme", programs_root=tmp_path / "programs")
            assert False, "expected ProgramSynthesizerPilotError"
        except ProgramSynthesizerPilotError as error:
            assert "disabled" in str(error)
    finally:
        set_ai_mode(AIMode.ACTIVE)


def test_run_comparison_raises_when_program_ai_not_enabled(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root, enabled=False)

    try:
        run_context_gateway_comparison(program_id="acme", programs_root=programs_root)
        assert False, "expected ProgramSynthesizerPilotError"
    except ProgramSynthesizerPilotError as error:
        assert "AI enabled" in str(error)


def test_run_comparison_skips_when_no_candidate_items(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    empty_request = ProgramSynthesisRequest(program_id="acme", as_of=_NOW, items=())
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.assemble_program_synthesis_request",
        lambda program_id, **kwargs: empty_request,
    )

    result = run_context_gateway_comparison(program_id="acme", programs_root=programs_root)

    assert result.compared is False
    assert result.skip_reason is not None and "no candidate items" in result.skip_reason


def test_run_comparison_raises_when_no_deployment_configured(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.assemble_program_synthesis_request",
        lambda program_id, **kwargs: _request(program_id),
    )
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ()
    )

    try:
        run_context_gateway_comparison(program_id="acme", programs_root=programs_root)
        assert False, "expected ProgramSynthesizerPilotError"
    except ProgramSynthesizerPilotError as error:
        assert "deployment" in str(error)


def test_run_comparison_records_blind_judgment(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.assemble_program_synthesis_request",
        lambda program_id, **kwargs: _request(program_id),
    )
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr("src.commands.program_synthesizer_pilot.FallbackAIClient", lambda **kwargs: object())
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.generate_program_synthesis",
        lambda request, *, client, programs_root: _outcome(released=True, through_line="baseline through-line"),
    )
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.generate_program_synthesis_via_context_gateway",
        lambda request, *, client, programs_root: _outcome(released=True, through_line="candidate through-line"),
    )

    echoed: list[str] = []
    prompts: list[str] = []

    def _prompt(_message: str) -> str:
        prompts.append(_message)
        return "b"

    result = run_context_gateway_comparison(
        program_id="acme", seed=1234, programs_root=programs_root, prompt_fn=_prompt, echo_fn=echoed.append,
    )

    assert result.compared is True
    assert result.program_id == "acme"
    # One prompt for the win/loss/tie/neither choice, one for BL-D3's
    # critical-error rubric question.
    assert len(prompts) == 2

    records = read_comparisons("acme", surface="program_synthesizer", programs_root=programs_root)
    assert len(records) == 1
    record = records[0]
    assert record.choice == "b"
    # _prompt returns "b" for both prompts; "b" is not a "y"/"yes" answer.
    assert record.critical_error is False
    texts = {record.option_a_text, record.option_b_text}
    assert any("baseline through-line" in t for t in texts)
    assert any("candidate through-line" in t for t in texts)


def test_run_comparison_records_critical_error_when_reviewer_flags_it(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.assemble_program_synthesis_request",
        lambda program_id, **kwargs: _request(program_id),
    )
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr("src.commands.program_synthesizer_pilot.FallbackAIClient", lambda **kwargs: object())
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.generate_program_synthesis",
        lambda request, *, client, programs_root: _outcome(released=True, through_line="baseline through-line"),
    )
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.generate_program_synthesis_via_context_gateway",
        lambda request, *, client, programs_root: _outcome(released=True, through_line="candidate through-line"),
    )

    answers = iter(["a", "yes"])
    result = run_context_gateway_comparison(
        program_id="acme", seed=1234, programs_root=programs_root,
        prompt_fn=lambda _msg: next(answers), echo_fn=lambda _msg: None,
    )

    assert result.compared is True
    records = read_comparisons("acme", surface="program_synthesizer", programs_root=programs_root)
    assert records[0].critical_error is True


def test_run_comparison_skips_when_either_side_not_released(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.assemble_program_synthesis_request",
        lambda program_id, **kwargs: _request(program_id),
    )
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr("src.commands.program_synthesizer_pilot.FallbackAIClient", lambda **kwargs: object())
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.generate_program_synthesis",
        lambda request, *, client, programs_root: _outcome(released=False),
    )
    monkeypatch.setattr(
        "src.commands.program_synthesizer_pilot.generate_program_synthesis_via_context_gateway",
        lambda request, *, client, programs_root: _outcome(released=True, through_line="candidate through-line"),
    )

    result = run_context_gateway_comparison(
        program_id="acme", programs_root=programs_root, prompt_fn=lambda _msg: "tie", echo_fn=lambda _msg: None,
    )

    assert result.compared is False
    assert result.skip_reason is not None and "discarded/rejected" in result.skip_reason
    assert read_comparisons("acme", surface="program_synthesizer", programs_root=programs_root) == ()
