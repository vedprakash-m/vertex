"""ADF-W5.11: src/commands/cockpit_tui.py -- the interactive terminal
cockpit loop (read-only navigation + the 'review' mutation launch)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import src.commands.cockpit_tui as tui_module
from src.commands.cockpit_tui import run_cockpit_tui_loop
from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    finalize_cockpit_snapshot,
)
from src.core.adoption_telemetry import GoldenWorkflow, read_adoption_events
from src.core.ai_review_proposal_store import load_proposal, stage_proposal
from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskKind, RiskProbability, RiskStatus
from src.core.risk_proposal import RiskProposal
from src.core.risk_register_engine import load_risk_register, save_risk_register

_NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def _snapshot(*, findings: tuple[CockpitFinding, ...] = ()) -> CockpitSnapshot:
    snap = CockpitSnapshot(
        schema_version="1", program_id="xpf", edition_id=None, generated_at=_NOW, as_of=_NOW,
        program_summary=ProgramCockpitSummary(
            overall_risk="yellow", readiness_percent=60, blocker_count=1, top_three_candidates=(), next_action=None
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=4, required_total=5, stale_sources=("kusto",), degraded_sources=(),
            manual_sources=(), newest_watermarks={},
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=0.4, verification_coverage=0.2, extraction_quality=(), contradiction_count=0
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=0.5, frontier_cost_usd=2.0, cache_hit_rate=0.1, context_tokens_in=50
        ),
        value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None),
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0,
            duplicate_preventions=0, audit_coverage=None,
        ),
        findings=findings,
        input_hash="",
    )
    return finalize_cockpit_snapshot(snap)


def _finding(finding_id: str = "f1", summary: str = "A finding") -> CockpitFinding:
    return CockpitFinding(
        finding_id=finding_id, area="source", status="blocked", summary=summary, detail="Detail text.",
        owner="alex", next_command="vertex gather --program xpf", evidence_refs=("sig-1",), observed_at=_NOW,
    )


def _scripted_input(responses: list[str]):
    iterator = iter(responses)

    def _input(prompt: str) -> str:
        return next(iterator)

    return _input


@pytest.fixture(autouse=True)
def _stub_builder(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def _build(*args, **kwargs):
        calls["n"] += 1
        return _snapshot(findings=(_finding(),))

    monkeypatch.setattr(tui_module, "build_cockpit_snapshot", _build)
    return calls


def test_quit_immediately_exits_after_showing_summary(_stub_builder) -> None:
    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_scripted_input(["q"]), output_fn=output.append)
    assert any("Vertex Cockpit TUI" in line for line in output)
    assert any("[1]" in line for line in output)


def test_finding_number_shows_explain_detail(_stub_builder) -> None:
    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_scripted_input(["1", "q"]), output_fn=output.append)
    joined = "\n".join(output)
    assert "A finding" in joined
    assert "Detail text." in joined
    assert "alex" in joined
    assert "vertex gather --program xpf" in joined
    assert "sig-1" in joined


def test_out_of_range_finding_number_reports_an_error(_stub_builder) -> None:
    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_scripted_input(["99", "q"]), output_fn=output.append)
    assert any("No finding [99]" in line for line in output)


def test_unrecognized_input_reports_an_error(_stub_builder) -> None:
    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_scripted_input(["bogus", "q"]), output_fn=output.append)
    assert any("Unrecognized input" in line for line in output)


def test_refresh_rebuilds_the_snapshot(_stub_builder) -> None:
    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_scripted_input(["r", "q"]), output_fn=output.append)
    assert _stub_builder["n"] == 2  # once at start, once on refresh


def test_eof_exits_cleanly_without_crashing(_stub_builder) -> None:
    def _input(prompt: str) -> str:
        raise EOFError

    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_input, output_fn=output.append)  # must not raise
    assert any("Vertex Cockpit TUI" in line for line in output)


def test_keyboard_interrupt_exits_cleanly(_stub_builder) -> None:
    def _input(prompt: str) -> str:
        raise KeyboardInterrupt

    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_input, output_fn=output.append)  # must not raise


def test_max_iterations_bounds_the_loop_for_non_interactive_callers(_stub_builder) -> None:
    def _input(prompt: str) -> str:
        return "r"  # would loop forever without max_iterations

    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_input, output_fn=output.append, max_iterations=3)
    assert _stub_builder["n"] == 4  # 1 initial + 3 refreshes


def test_no_findings_renders_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui_module, "build_cockpit_snapshot", lambda *a, **k: _snapshot(findings=()))
    output: list[str] = []
    run_cockpit_tui_loop("xpf", input_fn=_scripted_input(["q"]), output_fn=output.append)
    assert any("(none)" in line for line in output)


# ---------------------------------------------------------------------------
# ADF-W5.11: 'review' mutation launch -- the same real risks.py write path
# the CLI uses, not a mocked stand-in. build_cockpit_snapshot is stubbed
# (as in every other test in this file) since these tests are about the
# risk-register write path and the loop's routing, not snapshot rendering.
# ---------------------------------------------------------------------------

def _stale_risk_entry(risk_id: str = "risk-1") -> RiskEntry:
    return RiskEntry(
        id=risk_id, program_id="xpf", title="Vendor delay signal",
        description="Vendor X reported a delay.", probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH, category=RiskCategory.EXTERNAL, owner_alias="alex",
        mitigation_plan="Escalate.", mitigation_due_date=None,
        linked_workstream_ids=(), linked_work_item_ids=(), linked_milestone_ids=(),
        linked_claim_ids=(), linked_action_ids=(), status=RiskStatus.OPEN,
        identified_date=date(2026, 1, 1), identified_in_vertex_issue=None,
        last_reviewed_date=None, entity_refs=(),
    )


def test_review_command_reviews_a_stale_risk_through_the_real_write_path(_stub_builder, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_risk_register("xpf", (_stale_risk_entry(),), programs_root=programs_root)

    output: list[str] = []
    run_cockpit_tui_loop(
        "xpf",
        input_fn=_scripted_input(["review", "y", "", "", "q"]),
        output_fn=output.append,
        programs_root=programs_root,
    )

    entries = load_risk_register("xpf", programs_root=programs_root)
    assert entries[0].last_reviewed_date == datetime.now(timezone.utc).date()
    assert any("Reviewed 1 stale risk" in line for line in output)
    # Section 10.3a: "refresh its read model after a completed command" --
    # build_cockpit_snapshot is called once at start, once after the review.
    assert _stub_builder["n"] == 2
    # The same real write path the CLI uses, including its adoption telemetry.
    assert len(read_adoption_events("xpf", programs_root=programs_root)) == 1
    assert read_adoption_events("xpf", programs_root=programs_root)[0].workflow == GoldenWorkflow.RISK_DEPENDENCY_REVIEW


def test_review_command_declining_leaves_the_risk_unreviewed(_stub_builder, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    save_risk_register("xpf", (_stale_risk_entry(),), programs_root=programs_root)

    output: list[str] = []
    run_cockpit_tui_loop(
        "xpf",
        input_fn=_scripted_input(["review", "n", "q"]),
        output_fn=output.append,
        programs_root=programs_root,
    )

    entries = load_risk_register("xpf", programs_root=programs_root)
    assert entries[0].last_reviewed_date is None
    assert any("Reviewed 0 stale risk" in line for line in output)


def test_review_command_no_stale_risks_reports_none(_stub_builder, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    # No risks at all -- run_risk_review_session's own "no stale risks" branch.
    output: list[str] = []
    run_cockpit_tui_loop(
        "xpf",
        input_fn=_scripted_input(["review", "q"]),
        output_fn=output.append,
        programs_root=programs_root,
    )
    assert any("No stale risks" in line for line in output)


# ---------------------------------------------------------------------------
# ADF-W5.11: 'proposals <type>' mutation launch -- the same real
# ai_proposals.py write path the `ai-proposals review` CLI uses.
# ---------------------------------------------------------------------------

def _staged_risk_proposal(proposal_id: str = "risk-proposal-1") -> RiskProposal:
    return RiskProposal(
        id=proposal_id, program_id="xpf", candidate_risk_id="risk-candidate-1",
        causal_title="Vendor SDK delay blocks integration milestone",
        why_it_matters="The integration milestone cannot complete without the vendor SDK.",
        probability=RiskProbability.LIKELY, impact=RiskImpact.HIGH, category=RiskCategory.EXTERNAL,
        mitigation="Escalate to vendor account team.", owner_alias="jordanr",
        by_when=date(2026, 8, 1), fallback="Build an internal shim.", evidence_refs=("signal-1",),
        ai_run_id="ai-run-1", proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )


def test_proposals_command_with_type_token_accepts_a_staged_proposal(_stub_builder, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stage_proposal("xpf", "risk", _staged_risk_proposal(), programs_root=programs_root)
    save_risk_register("xpf", (_candidate_risk_entry(),), programs_root=programs_root)

    output: list[str] = []
    run_cockpit_tui_loop(
        "xpf",
        input_fn=_scripted_input(["proposals risk", "y", "q"]),
        output_fn=output.append,
        programs_root=programs_root,
    )

    approved = load_proposal("xpf", "risk", "risk-proposal-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"
    assert any("Reviewed 1 risk proposal(s)" in line for line in output)
    # Section 10.3a: refresh after the completed command.
    assert _stub_builder["n"] == 2


def test_proposals_command_without_type_token_prompts_for_one(_stub_builder, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stage_proposal("xpf", "risk", _staged_risk_proposal(), programs_root=programs_root)
    save_risk_register("xpf", (_candidate_risk_entry(),), programs_root=programs_root)

    output: list[str] = []
    run_cockpit_tui_loop(
        "xpf",
        input_fn=_scripted_input(["proposals", "risk", "y", "q"]),
        output_fn=output.append,
        programs_root=programs_root,
    )

    approved = load_proposal("xpf", "risk", "risk-proposal-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"


def test_proposals_command_rejects_when_declined_with_a_reason(_stub_builder, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stage_proposal("xpf", "risk", _staged_risk_proposal(), programs_root=programs_root)

    output: list[str] = []
    run_cockpit_tui_loop(
        "xpf",
        input_fn=_scripted_input(["proposals risk", "n", "Too speculative.", "q"]),
        output_fn=output.append,
        programs_root=programs_root,
    )

    rejected = load_proposal("xpf", "risk", "risk-proposal-1", programs_root=programs_root)
    assert rejected is not None
    assert rejected.status == "rejected"


def test_proposals_command_unrecognized_type_reports_an_error(_stub_builder, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    output: list[str] = []
    run_cockpit_tui_loop(
        "xpf",
        input_fn=_scripted_input(["proposals bogus_type", "q"]),
        output_fn=output.append,
        programs_root=programs_root,
    )
    assert any("Unrecognized proposal type" in line for line in output)


def _candidate_risk_entry() -> RiskEntry:
    return RiskEntry(
        id="risk-candidate-1", program_id="xpf", title="Vendor SDK delay",
        description="Machine-detected candidate risk.", probability=RiskProbability.UNASSESSED,
        impact=RiskImpact.UNASSESSED, category=RiskCategory.EXTERNAL, owner_alias="unassigned",
        mitigation_plan=None, mitigation_due_date=None, linked_workstream_ids=(), linked_work_item_ids=(),
        linked_milestone_ids=(), linked_claim_ids=(), linked_action_ids=(), status=RiskStatus.OPEN,
        identified_date=date(2026, 7, 1), identified_in_vertex_issue=None, last_reviewed_date=None,
        entity_refs=(), kind=RiskKind.CANDIDATE.value,
    )
