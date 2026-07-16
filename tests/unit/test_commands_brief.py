from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

import cli
from src.ai.cost_guard import CostGuard
from src.commands.brief import _build_incident_learning_brief_lines, _build_program_narrative_lines, build_brief, render_brief
from src.core.analytics_store import get_program_autonomy_audit_path
from src.core.analytics_store import replace_contradiction_state
from src.core.brief_intervention_store import BriefInterventionStatus, append_brief_intervention_resolution, load_brief_intervention_resolutions
from src.core.catchup_scan import WatchPollResult
from src.core.catchup_state_store import CatchupState, write_catchup_state
from src.core.claim_tracker import append_claim_entry, append_decision_ask, load_open_decision_asks
from src.core.feedback.calibration_router import write_forecast_calibration
from src.core.feedback.salience_modeler import refresh_author_salience
from src.core.incident_journal_store import append_incident_entry
from src.core.knowledge_store import KnowledgeStore, PersonDirectory
from src.core.models import Confidence, RiskLevel, WorkItem
from src.core.models_v2 import ClaimEntry, Contradiction, ContradictionPacket, DataSourceType, DecisionAsk, ForecastCalibrationModifier, IncidentEntry, ResolvedContradiction, WorkstreamCalibration
from tests.support.report_test_setup import stage_v2_report_workspace


import pytest

runner = CliRunner()

@pytest.fixture(autouse=True)
def _mock_brief_utc_now(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.brief._utc_now", lambda: datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc))


def test_build_brief_renders_now_and_watch_sections(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    rendered = render_brief(report)

    assert "Morning Brief - acme" in rendered
    assert "Catchup since 2026-05-20 09:00Z: 2 new signal(s), 4 discovered, 12 item(s) scanned." in rendered
    assert "Catchup detail: ADO#1234 target date changed from Jun 15 to Jun 22." in rendered
    assert "Catchup detail: Repair risk-up signal is contradicted by Kusto." in rendered
    assert "Low attention + known slip bias on deployment." in rendered
    assert "Contradiction WI:123 (deployment): Claim date disagrees with current ADO target date. Prefer workiq (high)." in rendered
    assert "Incident learning WI:123 (deployment): Deployment capacity rollback exposed hidden coupling. Source: IcM 22001. (high confidence)" in rendered
    assert "Claim c-1 (deployment) due 2026-05-24" in rendered
    assert "Decision ask d-1 is ready for nudge after 16 day(s) inactive" in rendered
    assert "Claim c-2 (repair) due 2026-06-01" in rendered
    assert "Decision ask d-2 is in watch at 8 day(s) open" in rendered
    assert "Staged" in rendered
    assert "Id: contradiction-wi-123-review | Review contradiction on deployment" in rendered
    assert "Approve: vertex brief --program acme --approve contradiction-wi-123-review" in rendered
    assert "Id: decision-ask-d-1-nudge | Stage nudge for decision ask d-1" in rendered
    assert "Dismiss: vertex brief --program acme --dismiss decision-ask-d-1-nudge" in rendered
    assert "Id: incident-deployment-wi-123-readiness | Review readiness after incident learning on deployment" in rendered
    assert "Apply: vertex readiness fetch --program acme" in rendered


def test_build_brief_matches_golden_fixture(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    rendered = render_brief(report)
    fixture_path = Path(__file__).resolve().parents[1] / "golden" / "brief_now_section.txt"

    assert rendered == fixture_path.read_text(encoding="utf-8").rstrip("\n")


def test_build_brief_surfaces_program_narrative_when_synthesis_released(tmp_path: Path) -> None:
    # ADF-W2.9: a new, always-last "Program Narrative" section surfacing
    # the latest QG-29-released ProgramSynthesis's through-line/long-poles,
    # mirroring cockpit_builder.py's own release-gated read.
    from src.core.program_synthesis import ProgramSynthesis, persist_program_synthesis
    from src.core.quality_gates.ai_release_audit import ReleaseTerminal, record_ai_release_decision

    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    synthesis = ProgramSynthesis(
        program_id="acme",
        ai_run_id="run-1",
        through_line="Deployment workstream is the program's critical path this cycle.",
        long_poles=("Vendor delivery risk remains open.",),
        facts=(),
        inferences=(),
        recommendations=(),
        generated_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        prompt_version="program_synthesis.v1",
        source_item_count=3,
    )
    persist_program_synthesis(synthesis, programs_root=programs_root)
    record_ai_release_decision(
        program_id="acme",
        ai_run_id="run-1",
        terminal=ReleaseTerminal.RELEASED,
        reason="test",
        validator_finding_count=0,
        programs_root=programs_root,
    )

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    rendered = render_brief(report)

    assert "Program Narrative" in rendered
    assert "- Deployment workstream is the program's critical path this cycle." in rendered
    assert "- Long pole: Vendor delivery risk remains open." in rendered
    # The section must render after (not instead of) the existing sections.
    assert rendered.index("Program Narrative") > rendered.index("Staged")


def test_build_brief_omits_program_narrative_section_when_no_synthesis_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert report.program_narrative_lines == ()
    assert "Program Narrative" not in render_brief(report)


def test_build_program_narrative_lines_degrades_on_failure(monkeypatch, tmp_path: Path) -> None:
    def _raise(program_id, **kwargs):
        raise RuntimeError("fact store unavailable")

    monkeypatch.setattr("src.core.program_synthesis.load_latest_released_program_synthesis", _raise)

    lines = _build_program_narrative_lines("acme", programs_root=tmp_path / "programs")

    assert lines == ()


def test_build_brief_incident_learning_lines_include_higher_order_class_patterns() -> None:
    lines = _build_incident_learning_brief_lines(
        (
            IncidentEntry(
                program_id="acme",
                incident_id="22001",
                signal_id="icm-22001",
                observed_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 20, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22001: WI:123 rollout validation regressed under failover.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:123",),
                confidence=Confidence.HIGH,
            ),
            IncidentEntry(
                program_id="acme",
                incident_id="22002",
                signal_id="icm-22002",
                observed_at=datetime(2026, 5, 21, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 21, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22002: WI:456 rollout validation regressed after failover.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:456",),
                confidence=Confidence.MEDIUM,
            ),
            IncidentEntry(
                program_id="acme",
                incident_id="22003",
                signal_id="icm-22003",
                observed_at=datetime(2026, 5, 22, 15, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 5, 22, 15, 5, tzinfo=timezone.utc),
                belief_change_summary="IcM 22003: WI:789 rollout validation regressed during failover drills.",
                workstream_id="deployment",
                severity=2,
                ado_entity_refs=("WI:789",),
                confidence=Confidence.MEDIUM,
            ),
        ),
        weight_by_workstream={"deployment": 0.6},
    )

    now_lines = [line.text for target, line in lines if target == "now"]

    assert any(text.startswith("Incident class ") for text in now_lines)


def test_brief_command_dry_run_renders_without_writing_artifact(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(cli.app, ["brief", "--program", "acme", "--today", "--dry-run"])

    assert result.exit_code == 0
    assert "Morning Brief - acme" in result.stdout
    assert "Saved brief:" not in result.stdout
    assert not any((tmp_path / "programs" / "acme" / "publications").rglob("brief_*.txt"))


def test_build_brief_surfaces_catchup_truncation_note(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    write_catchup_state(
        "acme",
        CatchupState(
            last_catchup_at=datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc),
            last_catchup_source="ado",
            last_scan_cursor_ado=datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc),
            last_result=WatchPollResult(
                program_id="acme",
                since=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
                polled_at=datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc),
                scanned_items=500,
                discovered_signals=500,
                new_signals=2,
                auto_reviews_written=0,
                trajectory_updates=1,
                ado_calls=3,
                new_signal_summaries=(
                    "ADO#1234 target date changed from Jun 15 to Jun 22.",
                ),
                total_changed_items=650,
            ),
        ),
        programs_root=programs_root,
    )

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    rendered = render_brief(report)

    assert "Catchup truncated after 500 of 650 changed item(s). Run vertex gather for full refresh." in rendered


def test_build_brief_surfaces_authored_engms_reference_docs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [deployment]",
                "    description: Canonical readiness design notes.",
            )
        ),
        encoding="utf-8",
    )

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    rendered = render_brief(report)

    assert "Reference Docs" in rendered
    assert "Acme Readiness Spec | https://eng.ms/acme-readiness | Canonical readiness design notes." in rendered


def test_build_brief_uses_fetched_engms_summary_when_available(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [deployment]",
                "    description: Canonical readiness design notes.",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.brief.summarize_engms_page", lambda page: "Canonical readiness design notes. Fresh operator detail from eng.ms.")

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    rendered = render_brief(report)

    assert "Acme Readiness Spec | https://eng.ms/acme-readiness | Canonical readiness design notes. Fresh operator detail from eng.ms." in rendered


def test_build_brief_surfaces_ai_cost_ceiling_breach(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    CostGuard(
        edition="acme_weekly",
        run_id="brief-run-001",
        budget_usd=0.5,
        programs_root=programs_root,
    ).record_actual(0.6)

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    rendered = render_brief(report)

    assert "AI cost ceiling exceeded for acme_weekly: $0.600 / $0.50 across 1 AI call(s) (run brief-run-001)." in rendered


def test_build_brief_omits_resolved_staged_interventions_with_same_source_hash(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    initial_report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
    )
    proposal = next(item for item in initial_report.staged_interventions if item.proposal_id == "decision-ask-d-1-nudge")
    append_brief_intervention_resolution(
        "acme",
        proposal_id=proposal.proposal_id,
        title=proposal.title,
        command=proposal.command,
        source_hash=proposal.source_hash,
        status=BriefInterventionStatus.DISMISSED,
        resolved_at=datetime(2026, 5, 21, 9, 1, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    report = build_brief(
        "acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 21, 9, 2, tzinfo=timezone.utc),
    )

    assert all(item.proposal_id != "decision-ask-d-1-nudge" for item in report.staged_interventions)
    assert any(item.proposal_id == "contradiction-wi-123-review" for item in report.staged_interventions)


def test_brief_command_dismiss_records_resolution_and_renders_updated_brief(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(
        cli.app,
        ["brief", "--program", "acme", "--dismiss", "decision-ask-d-1-nudge", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "Dry run: would mark staged intervention decision-ask-d-1-nudge as dismissed." in result.stdout

    result = runner.invoke(
        cli.app,
        ["brief", "--program", "acme", "--dismiss", "decision-ask-d-1-nudge"],
    )

    assert result.exit_code == 0
    resolutions = load_brief_intervention_resolutions("acme", programs_root=programs_root)
    assert resolutions["decision-ask-d-1-nudge"].status is BriefInterventionStatus.DISMISSED
    assert "Recorded dismissed for staged intervention decision-ask-d-1-nudge." in result.stdout
    assert "Id: decision-ask-d-1-nudge" not in result.stdout
    assert any((tmp_path / "programs" / "acme" / "publications").rglob("brief_*.txt"))


def test_brief_command_approve_decision_ask_nudge_applies_and_renders_updated_brief(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)
    monkeypatch.setattr(
        "src.commands.notify.load_bundle",
        lambda edition_name, reports_root=None, *, editions_root=None, programs_root=None: SimpleNamespace(
            config=SimpleNamespace(author=SimpleNamespace(email="author@example.com")),
            program_context=SimpleNamespace(
                workstreams=(),
                leadership_readers=(SimpleNamespace(name="ltlead"),),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.notify.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(PersonDirectory(alias="ltlead", email="ltlead@example.com", display_name="LT Lead"),),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    result = runner.invoke(
        cli.app,
        ["brief", "--program", "acme", "--approve", "decision-ask-d-1-nudge"],
    )

    assert result.exit_code == 0
    resolutions = load_brief_intervention_resolutions("acme", programs_root=programs_root)
    assert resolutions["decision-ask-d-1-nudge"].status is BriefInterventionStatus.APPROVED
    assert "Recorded approved for staged intervention decision-ask-d-1-nudge." in result.stdout
    assert "EML:" in result.stdout
    assert "Id: decision-ask-d-1-nudge" not in result.stdout
    eml_paths = sorted((programs_root / "acme" / "publications" / "acme_weekly" / "decision_ask_nudges").glob("*.eml"))
    assert len(eml_paths) == 1
    audit_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_payloads[-1]["action_type"] == "decision_ask_nudge"


def test_brief_command_approve_incident_linked_decision_ask_nudge_carries_incident_context_in_dry_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    append_decision_ask(
        DecisionAsk(
            id="d-incident",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on deployment rollback guardrails.",
            entity_refs=("WI:123",),
            ask_date=date(2026, 5, 5),
            owner_alias="operator",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)
    monkeypatch.setattr(
        "src.commands.notify.load_bundle",
        lambda edition_name, reports_root=None, *, editions_root=None, programs_root=None: SimpleNamespace(
            config=SimpleNamespace(author=SimpleNamespace(email="author@example.com")),
            program_context=SimpleNamespace(
                workstreams=(),
                leadership_readers=(SimpleNamespace(name="ltlead"),),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.notify.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(PersonDirectory(alias="ltlead", email="ltlead@example.com", display_name="LT Lead"),),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    result = runner.invoke(
        cli.app,
        ["brief", "--program", "acme", "--approve", "decision-ask-d-incident-nudge", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "Context: WI:123: Deployment capacity rollback exposed hidden coupling. Source: IcM 22001." in result.stdout
    assert "Dry run: would mark staged intervention decision-ask-d-incident-nudge as approved and write a decision-ask nudge draft." in result.stdout


def test_brief_command_approve_incident_linked_decision_ask_nudge_records_incident_ref_in_audit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    append_decision_ask(
        DecisionAsk(
            id="d-incident",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on deployment rollback guardrails.",
            entity_refs=("WI:123",),
            ask_date=date(2026, 5, 5),
            owner_alias="operator",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.decisions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)
    monkeypatch.setattr(
        "src.commands.notify.load_bundle",
        lambda edition_name, reports_root=None, *, editions_root=None, programs_root=None: SimpleNamespace(
            config=SimpleNamespace(author=SimpleNamespace(email="author@example.com")),
            program_context=SimpleNamespace(
                workstreams=(),
                leadership_readers=(SimpleNamespace(name="ltlead"),),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.notify.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(PersonDirectory(alias="ltlead", email="ltlead@example.com", display_name="LT Lead"),),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    result = runner.invoke(
        cli.app,
        ["brief", "--program", "acme", "--approve", "decision-ask-d-incident-nudge"],
    )

    assert result.exit_code == 0
    audit_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_payloads[-1]["action_type"] == "decision_ask_nudge"
    assert "ICM:22001" in audit_payloads[-1]["evidence_refs"]


def test_brief_command_approve_decision_ask_escalation_applies_and_renders_updated_brief(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")
    _seed_brief_escalation_context(tmp_path)
    _seed_brief_escalation_rule(programs_root)
    append_decision_ask(
        DecisionAsk(
            id="d-3",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT escalation on WI:1201 rollout sequencing.",
            entity_refs=("WI:1201",),
            ask_date=date(2026, 4, 20),
            owner_alias="priya",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_brief_escalation_items(timestamp), 0),
    )

    result = runner.invoke(
        cli.app,
        ["brief", "--program", "acme", "--approve", "decision-ask-d-3-escalate"],
    )

    assert result.exit_code == 0
    resolutions = load_brief_intervention_resolutions("acme", programs_root=programs_root)
    assert resolutions["decision-ask-d-3-escalate"].status is BriefInterventionStatus.APPROVED
    assert "Recorded approved for staged intervention decision-ask-d-3-escalate." in result.stdout
    assert "EML:" in result.stdout
    assert "Id: decision-ask-d-3-escalate" not in result.stdout
    eml_paths = sorted((programs_root / "acme" / "publications" / "acme_weekly" / "escalations").glob("*.eml"))
    assert len(eml_paths) == 1
    decision_asks = load_open_decision_asks("acme", programs_root=programs_root)
    assert next(ask for ask in decision_asks if ask.id == "d-3").last_touched_at is not None
    audit_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_payloads[-1]["action_type"] == "decision_ask_escalation"


def test_brief_command_approve_incident_readiness_review_fetches_snapshot_and_renders_updated_brief(
    monkeypatch,
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    _seed_brief_workspace(programs_root, "acme")

    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.brief.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)
    monkeypatch.setattr(
        "src.commands.brief.fetch_readiness_snapshot",
        lambda program_id, programs_root: (object(), programs_root / program_id / "readiness_snapshot.yaml"),
    )
    monkeypatch.setattr(
        "src.commands.brief.render_readiness_snapshot_output",
        lambda snapshot, *, output_format, snapshot_path, warnings: (
            "Launch Readiness - acme\n"
            f"Snapshot: {snapshot_path}\n"
            "QG-RD-recent_incident_learning PASS 0 recent attribution-backed incident learnings recorded."
        ),
    )

    result = runner.invoke(
        cli.app,
        ["brief", "--program", "acme", "--approve", "incident-deployment-wi-123-readiness"],
    )

    assert result.exit_code == 0
    resolutions = load_brief_intervention_resolutions("acme", programs_root=programs_root)
    assert resolutions["incident-deployment-wi-123-readiness"].status is BriefInterventionStatus.APPROVED
    assert "Recorded approved for staged intervention incident-deployment-wi-123-readiness." in result.stdout
    assert "Launch Readiness - acme" in result.stdout
    assert "QG-RD-recent_incident_learning PASS" in result.stdout
    assert "Id: incident-deployment-wi-123-readiness" not in result.stdout


def _seed_brief_workspace(programs_root: Path, program_id: str) -> None:
    # Create edition YAML so get_program_output_dir resolves the program correctly.
    # Only write minimal stub if a full YAML wasn't already staged (e.g., by stage_v2_report_workspace).
    edition_path = programs_root / program_id / "editions" / f"{program_id}_weekly.yaml"
    edition_path.parent.mkdir(parents=True, exist_ok=True)
    if not edition_path.exists():
        edition_path.write_text(f"id: {program_id}_weekly\nprogram_id: {program_id}\n", encoding="utf-8")
    write_catchup_state(
        program_id,
        CatchupState(
            last_catchup_at=datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc),
            last_catchup_source="ado",
            last_scan_cursor_ado=datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc),
            last_result=WatchPollResult(
                program_id=program_id,
                since=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
                polled_at=datetime(2026, 5, 21, 8, 30, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=4,
                new_signals=2,
                auto_reviews_written=0,
                trajectory_updates=1,
                ado_calls=3,
                new_signal_summaries=(
                    "ADO#1234 target date changed from Jun 15 to Jun 22.",
                    "Repair risk-up signal is contradicted by Kusto.",
                ),
            ),
        ),
        programs_root=programs_root,
    )
    append_claim_entry(
        ClaimEntry(
            id="c-1",
            program_id=program_id,
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="deployment",
            text="Deployment ETA still needs leadership attention.",
            entity_refs=("WI:123",),
            claim_date=date(2026, 5, 19),
            owner_alias="operator",
            due_date=date(2026, 5, 24),
        ),
        programs_root=programs_root,
    )
    append_claim_entry(
        ClaimEntry(
            id="c-2",
            program_id=program_id,
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="repair",
            text="Repair is on track for early June.",
            entity_refs=("WI:124",),
            claim_date=date(2026, 5, 19),
            owner_alias="operator",
            due_date=date(2026, 6, 1),
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="d-1",
            program_id=program_id,
            edition_id="acme_weekly",
            issue_number=77,
            text="Need LT decision on contingency scope.",
            entity_refs=(),
            ask_date=date(2026, 5, 5),
            owner_alias="operator",
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="d-2",
            program_id=program_id,
            edition_id="acme_weekly",
            issue_number=77,
            text="Confirm the rollout owner and date still look current.",
            entity_refs=("WI:125",),
            ask_date=date(2026, 5, 13),
            owner_alias="operator",
        ),
        programs_root=programs_root,
    )
    (programs_root / program_id / "journal").mkdir(parents=True, exist_ok=True)
    (programs_root / program_id / "journal" / "edit_patterns.jsonl").write_text(
        "\n".join(
            [
                '{"program_id": "acme", "edition_id": "acme_weekly", "issue_number": 77, "section_id": "deployment", "recorded_at": "2026-05-20T10:00:00+00:00", "summary": "x", "before_excerpt": "x", "after_excerpt": "y", "before_word_count": 1, "after_word_count": 1, "task_type": "workstream_blurb", "author_override_magnitude": 0.8}',
                '{"program_id": "acme", "edition_id": "acme_weekly", "issue_number": 77, "section_id": "repair", "recorded_at": "2026-05-20T11:00:00+00:00", "summary": "x", "before_excerpt": "x", "after_excerpt": "y", "before_word_count": 1, "after_word_count": 1, "task_type": "workstream_blurb", "author_override_magnitude": 0.3}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    refresh_author_salience(
        program_id,
        programs_root=programs_root,
        author_alias="operator",
        as_of=datetime(2026, 5, 21, 8, 45, tzinfo=timezone.utc),
    )
    write_forecast_calibration(
        program_id,
        modifier=ForecastCalibrationModifier(
            workstream_modifiers={"deployment": 0.2, "repair": 0.1},
            dri_modifiers={},
            confidence=Confidence.LOW,
        ),
        workstream_rows=(
            WorkstreamCalibration(workstream_id="deployment", met=1, contradicted=4, stale=0),
            WorkstreamCalibration(workstream_id="repair", met=3, contradicted=2, stale=0),
        ),
        dri_rows=(),
        as_of=datetime(2026, 5, 21, 8, 46, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    replace_contradiction_state(
        program_id,
        (
            ContradictionPacket(
                work_item_id=123,
                workstream_id="deployment",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="journal",
                        source_b="ado",
                        summary="Claim date disagrees with current ADO target date.",
                        confidence=Confidence.HIGH,
                        evidence_refs=("c-1", "WI:123"),
                    ),
                ),
                confidence=Confidence.HIGH,
                recommended_resolution=ResolvedContradiction(
                    winning_source=DataSourceType.WORKIQ,
                    confidence=Confidence.HIGH,
                    rationale="Owner history shows persistent ADO optimism.",
                    evidence_refs=("c-1",),
                ),
                generated_at=datetime(2026, 5, 21, 8, 40, tzinfo=timezone.utc),
            ),
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id=program_id,
            incident_id="22001",
            signal_id="icm-22001",
            observed_at=datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 20, 15, 5, tzinfo=timezone.utc),
            belief_change_summary="IcM 22001: Deployment capacity rollback exposed hidden coupling.",
            workstream_id="deployment",
            severity=2,
            ado_entity_refs=("WI:123",),
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )


def _seed_brief_escalation_context(workspace_root: Path) -> None:
    _set_brief_workstream_accountable(workspace_root / "programs")
    _seed_brief_people_entry(workspace_root / "knowledge")


def _seed_brief_escalation_rule(programs_root: Path) -> None:
    (programs_root / "acme" / "escalation_rules.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "rules": [
                    {
                        "name": "unresolved_ask",
                        "conditions": [
                            {"field": "decision_ask_age_days", "op": ">=", "value": 21},
                            {"field": "decision_ask_status", "op": "==", "value": "open"},
                        ],
                        "cooldown_hours": 24,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _set_brief_workstream_accountable(programs_root: Path) -> None:
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    workstreams = document.get("workstreams")
    assert isinstance(workstreams, list)
    for entry in workstreams:
        if not isinstance(entry, dict) or entry.get("id") != "acme":
            continue
        entry["raci"] = {
            "accountable": "priya",
            "responsible": [],
            "consulted": [],
            "informed": [],
        }
        break
    workstreams_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _seed_brief_people_entry(knowledge_root: Path) -> None:
    path = knowledge_root / "people_directory.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    people = document.setdefault("people", [])
    assert isinstance(people, list)
    people.append(
        {
            "alias": "priya",
            "email": "priya@example.com",
            "display_name": "Priya Mehta",
        }
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _sample_brief_escalation_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1201,
            type="Feature",
            title="Escalation-worthy rollout follow-up",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="Sprint 1",
            target_date=as_of.date(),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={"changed_date": as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )
