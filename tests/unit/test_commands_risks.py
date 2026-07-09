from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.decision_register import save_decisions
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, DecisionEntry, DecisionStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.risk_register_engine import load_risk_history, load_risk_register, save_risk_register


runner = CliRunner()


def test_risks_add_and_list_cli(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "demo")

    add_result = runner.invoke(
        app,
        [
            "risks",
            "add",
            "--program",
            "demo",
            "--title",
            "Kusto dependency",
            "--probability",
            "likely",
            "--impact",
            "high",
        ],
    )
    list_result = runner.invoke(app, ["risks", "list", "--program", "demo"])

    assert add_result.exit_code == 0
    assert "Added risk" in add_result.stdout
    assert list_result.exit_code == 0
    assert "RISK REGISTER" in list_result.stdout
    assert "Kusto dependency" in list_result.stdout


def test_risks_list_cli_json(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root)

    result = runner.invoke(app, ["risks", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["program_id"] == "demo"
    assert payload["risks"][0]["id"] == "risk-demo-1"
    assert payload["risks"][0]["identified_date"] == "2026-05-01"


def test_risks_list_cli_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root)

    result = runner.invoke(app, ["risks", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[0]["id"] == "risk-demo-1"
    assert rows[0]["risk_score"] == "9"


def test_risks_list_uses_program_reality(monkeypatch) -> None:
    from unittest.mock import MagicMock
    from src.core.program_reality import FactAssessment
    from src.core.truth_levels import TruthLevel
    mock_risk = RiskEntry(
        id="risk-demo-1",
        program_id="demo",
        title="Dependency handoff",
        description="An external handoff could slip.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="demo",
        mitigation_plan="Review weekly.",
        mitigation_due_date=date(2026, 5, 20),
        linked_workstream_ids=("ws-demo",),
        linked_work_item_ids=(1001,),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=7,
        last_reviewed_date=date(2026, 5, 5),
        entity_refs=("WI:1001",),
    )
    # Track E follow-up: the risk board now consumes FactAssessment metadata
    # (truth_level/disputed/stale/evidence), so the mock must be a real
    # FactAssessment rather than a loose MagicMock — json.dumps serializes these.
    mock_assessment = FactAssessment(
        record=mock_risk,
        fact_id="fact-demo-1",
        truth_level=TruthLevel.SOURCE_VALIDATED,
        disputed=True,
        stale=False,
        provisional_inputs=False,
        evidence=("email:msg-1",),
    )
    mock_reality = MagicMock()
    mock_reality.risks.return_value = (mock_assessment,)
    monkeypatch.setattr("src.commands.risks.ProgramReality.load", lambda program_id, **kwargs: mock_reality)

    result = runner.invoke(app, ["risks", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert mock_reality.risks.called
    assert payload["risks"][0]["id"] == "risk-demo-1"
    # Track E follow-up: truth-level metadata is now preserved through to JSON output.
    assert payload["risks"][0]["truth_level"] == "source_validated"
    assert payload["risks"][0]["disputed"] is True


def test_risks_update_records_status_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root)

    result = runner.invoke(
        app,
        [
            "risks",
            "update",
            "--program",
            "demo",
            "--id",
            "risk-demo-1",
            "--status",
            "mitigated",
            "--note",
            "Mitigation complete.",
            "--reviewer",
            "demo",
        ],
    )

    history = load_risk_history("demo", "risk-demo-1", programs_root=programs_root)
    entries = load_risk_register("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert entries[0].status == RiskStatus.MITIGATED
    assert len(history) == 1
    assert history[0]["new_status"] == "mitigated"


def test_risks_review_mark_reviewed_updates_stale_entries(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root, last_reviewed_date=None, identified_date=date(2026, 1, 1))

    result = runner.invoke(app, ["risks", "review", "--program", "demo", "--mark-reviewed"])
    entries = load_risk_register("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Reviewed 1 stale risk" in result.stdout
    assert entries[0].last_reviewed_date == datetime.now(timezone.utc).date()


def test_risks_list_show_links_renders_raid_chain(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root, linked_action_ids=("action-demo-1",))
    append_action(
        "demo",
        ActionItem(
            id="action-demo-1",
            program_id="demo",
            text="Close the dependency handoff.",
            owner_alias="demo",
            due_date=date(2026, 5, 20),
            status=ActionStatus.IN_PROGRESS,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id="risk-demo-1",
            workstream_id="ws-demo",
            created_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    save_decisions(
        "demo",
        (
            DecisionEntry(
                id="decision-demo-1",
                program_id="demo",
                title="Dependency owner",
                context="Clarify the owner for the remaining handoff work.",
                decision="Owner stays with the deployment lead.",
                rationale=None,
                alternatives_considered=(),
                decided_by="demo",
                decision_date=date(2026, 5, 12),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=("action-demo-1",),
                workstream_id="ws-demo",
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-demo-1",
                program_id="demo",
                text="The partner team can finish the handoff this week.",
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id="risk-demo-1",
                linked_milestone_id=None,
                owner_alias="demo",
                identified_date=date(2026, 5, 2),
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["risks", "list", "--program", "demo", "--show-links"])

    assert result.exit_code == 0
    assert "RAID:" in result.stdout
    assert "risk:risk-demo-1 -> assumption:assumption-demo-1 -> action:action-demo-1 -> decision:decision-demo-1" in result.stdout
    assert "mitigating action present" in result.stdout


def test_risks_link_command_links_existing_action(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root)
    append_action(
        "demo",
        ActionItem(
            id="action-demo-2",
            program_id="demo",
            text="Track the dependency mitigation.",
            owner_alias="demo",
            due_date=date(2026, 5, 25),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="ws-demo",
            created_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["risks", "link", "risk-demo-1", "action-demo-2", "--program", "demo"])
    entries = load_risk_register("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Linked action action-demo-2 to risk risk-demo-1 in demo." in result.stdout
    assert entries[0].linked_action_ids == ("action-demo-2",)


def test_risks_link_command_dry_run_skips_write(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root)
    append_action(
        "demo",
        ActionItem(
            id="action-demo-2",
            program_id="demo",
            text="Track the dependency mitigation.",
            owner_alias="demo",
            due_date=date(2026, 5, 25),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="ws-demo",
            created_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["risks", "link", "risk-demo-1", "action-demo-2", "--program", "demo", "--dry-run"])
    entries = load_risk_register("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Would link action action-demo-2 to risk risk-demo-1 in demo." in result.stdout
    assert entries[0].linked_action_ids == ()


def test_risks_link_command_rejects_conflicting_action_link(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root)
    append_action(
        "demo",
        ActionItem(
            id="action-demo-2",
            program_id="demo",
            text="Track the dependency mitigation.",
            owner_alias="demo",
            due_date=date(2026, 5, 25),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id="risk-other",
            workstream_id="ws-demo",
            created_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["risks", "link", "risk-demo-1", "action-demo-2", "--program", "demo"])

    assert result.exit_code == 2
    assert "already linked to risk" in result.output
    assert "risk-other" in result.output


def test_risks_link_command_uses_program_reality(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    calls: list[tuple[str, str, str, Path]] = []
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)

    mock_risk = RiskEntry(
        id="risk-demo-1",
        program_id="demo",
        title="Dependency handoff",
        description="An external handoff could slip.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="demo",
        mitigation_plan="Review weekly.",
        mitigation_due_date=date(2026, 5, 20),
        linked_workstream_ids=("ws-demo",),
        linked_work_item_ids=(1001,),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=7,
        last_reviewed_date=date(2026, 5, 5),
        entity_refs=("WI:1001",),
    )
    mock_action = ActionItem(
        id="action-demo-2",
        program_id="demo",
        text="Track the dependency mitigation.",
        owner_alias="demo",
        due_date=date(2026, 5, 25),
        status=ActionStatus.OPEN,
        source_signal_id=None,
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(1001,),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id="ws-demo",
        created_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )
    risk_assessment = MagicMock()
    risk_assessment.record = mock_risk
    action_assessment = MagicMock()
    action_assessment.record = mock_action
    mock_reality = MagicMock()
    mock_reality.risks.return_value = (risk_assessment,)
    mock_reality.actions.return_value = (action_assessment,)
    monkeypatch.setattr("src.commands.risks.ProgramReality.load", lambda program_id, **kwargs: mock_reality)
    monkeypatch.setattr(
        "src.commands.risks.link_risk_action",
        lambda program_id, risk_id, action_id, programs_root: calls.append((program_id, risk_id, action_id, programs_root)),
    )

    result = runner.invoke(app, ["risks", "link", "risk-demo-1", "action-demo-2", "--program", "demo"])

    assert result.exit_code == 0
    assert calls == [("demo", "risk-demo-1", "action-demo-2", programs_root)]
    assert "Linked action action-demo-2 to risk risk-demo-1 in demo." in result.stdout


def _seed_risk_register(
    programs_root: Path,
    *,
    last_reviewed_date: date | None = date(2026, 5, 5),
    identified_date: date = date(2026, 5, 1),
    linked_action_ids: tuple[str, ...] = (),
) -> None:
    save_risk_register(
        "demo",
        (
            RiskEntry(
                id="risk-demo-1",
                program_id="demo",
                title="Dependency handoff",
                description="An external handoff could slip.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.DEPENDENCY,
                owner_alias="demo",
                mitigation_plan="Review weekly.",
                mitigation_due_date=date(2026, 5, 20),
                linked_workstream_ids=("ws-demo",),
                linked_work_item_ids=(1001,),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=linked_action_ids,
                status=RiskStatus.OPEN,
                identified_date=identified_date,
                identified_in_vertex_issue=7,
                last_reviewed_date=last_reviewed_date,
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )