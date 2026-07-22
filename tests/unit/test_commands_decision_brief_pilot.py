"""ADF-W2.9 P5: tests for the blind A/B comparison harness CLI
(``vertex decision-brief-pilot compare`` / ``summary``).

The advisor functions (``advise_on_decision_brief`` / ``advise_on_decision_
brief_via_context_gateway``) already have their own dedicated unit tests
(``test_ai_decision_brief_advisor.py``); these tests monkeypatch them
directly to exercise only this module's own wiring: AI-mode/config gating,
deployment resolution, blind randomization, and comparison recording.
"""
from __future__ import annotations

from pathlib import Path

from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands.decision_brief_pilot import (
    ContextGatewayPilotError,
    run_context_gateway_comparison,
)
from src.core.blind_ab_comparison import read_comparisons
from src.core.decision_brief_engine import DecisionBrief, DecisionItem


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


def _item(section_id: str = "risks") -> DecisionItem:
    return DecisionItem(
        section_id=section_id,
        section_title="Risks",
        current_text="The vendor delivery risk remains open.",
        proposed_text="Escalate the vendor delivery risk to leadership.",
        evidence_delta_lines=("Vendor confirmed a two-week slip.",),
        top_signals=(),
        vitality_summary="declining",
        confidence="medium",
        kpi_summary="On-time delivery: 62%.",
        stale_claims=(),
        accept_command="vertex accept --id risks",
        reject_command="vertex reject --id risks",
        accept_modified_command="vertex accept --id risks --modified",
    )


def _brief(*items: DecisionItem) -> DecisionBrief:
    return DecisionBrief(
        issue_number=12,
        edition_name="acme_weekly",
        generated_at="2026-06-06 12:00",
        items=tuple(items),
        total_pending=len(items),
        ai_enriched=False,
    )


def _with_advice(item: DecisionItem, *, verdict: str, reasoning: str) -> DecisionItem:
    from dataclasses import replace

    return replace(item, verdict=verdict, verdict_reasoning=reasoning)


def test_run_comparison_raises_when_ai_mode_disabled(tmp_path: Path) -> None:
    set_ai_mode(AIMode.DISABLED)
    try:
        try:
            run_context_gateway_comparison(edition_name="acme_weekly", programs_root=tmp_path / "programs")
            assert False, "expected ContextGatewayPilotError"
        except ContextGatewayPilotError as error:
            assert "disabled" in str(error)
    finally:
        set_ai_mode(AIMode.ACTIVE)


def test_run_comparison_raises_when_program_ai_not_enabled(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root, enabled=False)
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.load_pending_decision_brief",
        lambda **kwargs: (_brief(_item()), "acme"),
    )

    try:
        run_context_gateway_comparison(edition_name="acme_weekly", programs_root=programs_root)
        assert False, "expected ContextGatewayPilotError"
    except ContextGatewayPilotError as error:
        assert "AI enabled" in str(error)


def test_run_comparison_raises_when_no_deployment_configured(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.load_pending_decision_brief",
        lambda **kwargs: (_brief(_item()), "acme"),
    )
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ()
    )

    try:
        run_context_gateway_comparison(edition_name="acme_weekly", programs_root=programs_root)
        assert False, "expected ContextGatewayPilotError"
    except ContextGatewayPilotError as error:
        assert "deployment" in str(error)


def test_run_comparison_records_blind_judgment_for_each_item(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    brief = _brief(_item("risks"), _item("dependencies"))
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.load_pending_decision_brief",
        lambda **kwargs: (brief, "acme"),
    )
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr("src.commands.decision_brief_pilot.FallbackAIClient", lambda **kwargs: object())
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.advise_on_decision_brief",
        lambda *, client, brief, program_id, programs_root=None: _brief(
            *[_with_advice(item, verdict="ACCEPT", reasoning="baseline reasoning") for item in brief.items]
        ),
    )
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.advise_on_decision_brief_via_context_gateway",
        lambda *, client, brief, program_id, programs_root=None: _brief(
            *[_with_advice(item, verdict="REVISE", reasoning="candidate reasoning") for item in brief.items]
        ),
    )

    echoed: list[str] = []
    prompts: list[str] = []

    def _prompt(_message: str) -> str:
        prompts.append(_message)
        return "b"

    result = run_context_gateway_comparison(
        edition_name="acme_weekly",
        seed=1234,
        programs_root=programs_root,
        prompt_fn=_prompt,
        echo_fn=echoed.append,
    )

    assert result.program_id == "acme"
    assert result.issue_number == 12
    assert set(result.compared_item_ids) == {"risks", "dependencies"}
    assert result.skipped_item_ids == ()

    records = read_comparisons("acme", surface="decision_brief_advisor", programs_root=programs_root)
    assert len(records) == 2
    for record in records:
        assert record.choice == "b"
        # "b" is not a "y"/"yes" answer to the critical-error question either.
        assert record.critical_error is False
        # Exactly one of the two texts is the baseline, one is the candidate.
        texts = {record.option_a_text, record.option_b_text}
        assert any("baseline reasoning" in t for t in texts)
        assert any("candidate reasoning" in t for t in texts)


def test_run_comparison_records_critical_error_when_reviewer_flags_it(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    brief = _brief(_item("risks"))
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.load_pending_decision_brief",
        lambda **kwargs: (brief, "acme"),
    )
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr("src.commands.decision_brief_pilot.FallbackAIClient", lambda **kwargs: object())
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.advise_on_decision_brief",
        lambda *, client, brief, program_id, programs_root=None: _brief(
            *[_with_advice(item, verdict="ACCEPT", reasoning="baseline reasoning") for item in brief.items]
        ),
    )
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.advise_on_decision_brief_via_context_gateway",
        lambda *, client, brief, program_id, programs_root=None: _brief(
            *[_with_advice(item, verdict="REVISE", reasoning="candidate reasoning") for item in brief.items]
        ),
    )

    answers = iter(["a", "y"])
    result = run_context_gateway_comparison(
        edition_name="acme_weekly", seed=1234, programs_root=programs_root,
        prompt_fn=lambda _msg: next(answers), echo_fn=lambda _msg: None,
    )

    assert result.compared_item_ids == ("risks",)
    records = read_comparisons("acme", surface="decision_brief_advisor", programs_root=programs_root)
    assert records[0].critical_error is True


def test_run_comparison_skips_items_missing_advice_from_either_side(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_ai_enabled_program(programs_root)
    brief = _brief(_item("risks"))
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.load_pending_decision_brief",
        lambda **kwargs: (brief, "acme"),
    )
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr("src.commands.decision_brief_pilot.FallbackAIClient", lambda **kwargs: object())
    # Baseline never got a verdict (e.g. AI declined) -> no advice for this item.
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.advise_on_decision_brief", lambda *, client, brief, program_id, programs_root=None: brief
    )
    monkeypatch.setattr(
        "src.commands.decision_brief_pilot.advise_on_decision_brief_via_context_gateway",
        lambda *, client, brief, program_id, programs_root=None: _brief(
            *[_with_advice(item, verdict="ACCEPT", reasoning="candidate reasoning") for item in brief.items]
        ),
    )

    result = run_context_gateway_comparison(
        edition_name="acme_weekly",
        programs_root=programs_root,
        prompt_fn=lambda _msg: "tie",
        echo_fn=lambda _msg: None,
    )

    assert result.compared_item_ids == ()
    assert result.skipped_item_ids == ("risks",)
    assert read_comparisons("acme", surface="decision_brief_advisor", programs_root=programs_root) == ()
