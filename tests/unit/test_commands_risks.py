from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.decision_register import save_decisions
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, DecisionEntry, DecisionStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.adoption_telemetry import GoldenWorkflow, read_adoption_events
from src.core.exceptions import ConfigError
from src.core.program_reality import ProgramReality
from src.core.risk_register_engine import assess_risk_staleness, load_risk_history, load_risk_register, save_risk_register


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

    # ADF-W5.14: a completed risk review records risk_dependency_review adoption.
    adoption_events = read_adoption_events("demo", programs_root=programs_root)
    assert len(adoption_events) == 1
    assert adoption_events[0].workflow == GoldenWorkflow.RISK_DEPENDENCY_REVIEW


def test_risks_review_no_stale_entries_does_not_record_adoption(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    _seed_risk_register(programs_root, last_reviewed_date=datetime.now(timezone.utc).date())

    result = runner.invoke(app, ["risks", "review", "--program", "demo", "--mark-reviewed"])

    assert result.exit_code == 0
    assert "No stale risks" in result.stdout
    assert read_adoption_events("demo", programs_root=programs_root) == ()


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


@pytest.mark.parametrize("format", ("json", "csv", "human"))
@pytest.mark.parametrize("status", tuple(RiskStatus))
def test_risks_review_labels_preserve_metadata_and_lifecycle(monkeypatch, tmp_path: Path, format: str, status: RiskStatus) -> None:
    programs_root, entries = _seed_review_label_cases(monkeypatch, tmp_path, status)
    assessments = {a.record.id: a for a in ProgramReality.load("demo", programs_root=programs_root).risks()}

    result = runner.invoke(app, ["risks", "list", "--program", "demo", "--format", format])

    assert result.exit_code == 0, result.output
    expected_order = [entry.id for entry in sorted(entries, key=lambda entry: entry.title.lower())]
    if format == "json":
        payload = json.loads(result.stdout)
        assert set(payload) == {"program_id", "status_filter", "risks"}
        assert payload["program_id"] == "demo"
        assert payload["status_filter"] is None
        rows = payload["risks"]
    elif format == "csv":
        rows = list(csv.DictReader(result.stdout.splitlines()))
    else:
        rows = [{"id": line.split(" | ")[0][2:], "staleness": line.split(" | ")[3]}
                for line in result.stdout.splitlines() if line.startswith("- ")]
    assert [row["id"] for row in rows] == expected_order
    by_id = {row["id"]: row for row in rows}
    for entry in entries:
        active = status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
        expected = "current"
        if active:
            expected = "unreviewed" if entry.last_reviewed_date is None or entry.last_reviewed_date > _REVIEW_AS_OF else (
                "stale" if (_REVIEW_AS_OF - entry.last_reviewed_date).days > 30 else "current"
            )
        row = by_id[entry.id]
        assert row["staleness"] == expected
        assert assess_risk_staleness(entry, _REVIEW_AS_OF) is (
            active and entry.id in {"risk-missing_old", "risk-day31"}
        )
        assessment = assessments[entry.id]
        if format == "json":
            assert row["status"] == status.value
            assert row["risk_score"] == 9
            assert row["last_reviewed_date"] == (
                entry.last_reviewed_date.isoformat() if entry.last_reviewed_date is not None else None
            )
            assert row["identified_date"] == entry.identified_date.isoformat()
            assert row["truth_level"] == assessment.truth_level.value
            assert row["disputed"] is assessment.disputed
            assert row["fact_stale"] is assessment.stale
            assert row["evidence"] == list(assessment.evidence)
        elif format == "csv":
            assert row["status"] == status.value
            assert row["risk_score"] == "9"
            assert row["last_reviewed_date"] == (
                entry.last_reviewed_date.isoformat() if entry.last_reviewed_date is not None else ""
            )
            assert row["truth_level"] == assessment.truth_level.value
            assert row["disputed"] == str(assessment.disputed).lower()
            assert row["fact_stale"] == str(assessment.stale).lower()
        else:
            assert f"- {entry.id} | {status.value} | score 9 | {expected} |" in result.stdout


@pytest.mark.parametrize("format", ("json", "csv", "human"))
def test_risks_review_labels_board_filter_and_empty(monkeypatch, tmp_path: Path, format: str) -> None:
    programs_root, entries = _seed_review_label_cases(monkeypatch, tmp_path, RiskStatus.OPEN)
    closed = replace(entries[0], id="closed-risk", title="A closed risk", status=RiskStatus.CLOSED)
    save_risk_register("demo", entries + (closed,), programs_root=programs_root)
    board = runner.invoke(app, ["risks", "--program", "demo", "--format", format])
    listed = runner.invoke(app, ["risks", "list", "--program", "demo", "--format", format])
    filtered = runner.invoke(app, ["risks", "list", "--program", "demo", "--status", "closed", "--format", format])
    empty = runner.invoke(app, ["risks", "list", "--program", "demo", "--status", "escalated", "--format", format])
    assert all(result.exit_code == 0 for result in (board, listed, filtered, empty))
    if format == "json":
        board_payload, list_payload = json.loads(board.stdout), json.loads(listed.stdout)
        assert [(r["id"], r["staleness"]) for r in board_payload["risks"]] == [
            (r["id"], r["staleness"]) for r in list_payload["risks"]
        ]
        assert [r["id"] for r in list_payload["risks"]][-1] == "closed-risk"
        assert [r["id"] for r in json.loads(filtered.stdout)["risks"]] == ["closed-risk"]
        assert json.loads(filtered.stdout)["status_filter"] == "closed"
        assert json.loads(empty.stdout)["risks"] == []
    elif format == "csv":
        assert list(csv.DictReader(board.stdout.splitlines())) == list(csv.DictReader(listed.stdout.splitlines()))
        assert [r["id"] for r in csv.DictReader(filtered.stdout.splitlines())] == ["closed-risk"]
        assert list(csv.DictReader(empty.stdout.splitlines())) == []
        assert filtered.stdout.splitlines()[0] == listed.stdout.splitlines()[0] == empty.stdout.splitlines()[0]
    else:
        assert board.stdout == listed.stdout
        assert "- closed-risk | closed | score 9 | current |" in filtered.stdout
        assert "unreviewed" not in filtered.stdout
        assert empty.stdout.strip() == "No risks found for demo."


@pytest.mark.parametrize("status", tuple(RiskStatus))
def test_risks_review_labels_direct_csv_without_assessments(monkeypatch, tmp_path: Path, status: RiskStatus) -> None:
    from src.commands.risks import render_risks_csv

    _, entries = _seed_review_label_cases(monkeypatch, tmp_path, status)
    rows = {r["id"]: r for r in csv.DictReader(render_risks_csv(entries, as_of=_REVIEW_AS_OF).splitlines())}
    active = status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    for name in ("missing_recent", "missing_old", "future"):
        assert rows[f"risk-{name}"]["staleness"] == ("unreviewed" if active else "current")
    assert rows["risk-today"]["staleness"] == rows["risk-day30"]["staleness"] == "current"
    assert rows["risk-day31"]["staleness"] == ("stale" if active else "current")
    assert all(row[field] == "" for row in rows.values() for field in ("truth_level", "disputed", "fact_stale"))


@pytest.mark.parametrize("format", ("json", "csv", "human"))
def test_risks_review_labels_empty_register(monkeypatch, tmp_path: Path, format: str) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    save_risk_register("demo", (), programs_root=programs_root)

    result = runner.invoke(app, ["risks", "list", "--program", "demo", "--format", format])

    assert result.exit_code == 0
    if format == "json":
        assert json.loads(result.stdout) == {"program_id": "demo", "status_filter": None, "risks": []}
    elif format == "csv":
        assert len(result.stdout.splitlines()) == 1
        assert list(csv.DictReader(result.stdout.splitlines())) == []
    else:
        assert result.stdout.strip() == "No risks found for demo."


@pytest.mark.parametrize("format", ("json", "csv", "human"))
def test_risks_review_labels_do_not_renew_after_display_or_touch(monkeypatch, tmp_path: Path, format: str) -> None:
    programs_root, _ = _seed_review_label_cases(monkeypatch, tmp_path, RiskStatus.OPEN)
    path = programs_root / "demo" / "risk_register.yaml"
    original = path.read_bytes()
    before = load_risk_register("demo", programs_root=programs_root)
    args = ["risks", "list", "--program", "demo", "--format", format]
    first = runner.invoke(app, args)
    repeated = runner.invoke(app, args)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    touched = runner.invoke(app, args)
    assert all(result.exit_code == 0 for result in (first, repeated, touched))
    if format == "json":
        labels = [[(r["id"], r["staleness"], r["last_reviewed_date"])
                   for r in json.loads(result.stdout)["risks"]] for result in (first, repeated, touched)]
        assert labels[0] == labels[1] == labels[2]
        assert ("risk-missing_recent", "unreviewed", None) in labels[0]
        assert ("risk-future", "unreviewed", "2026-10-30") in labels[0]
    else:
        assert first.stdout == repeated.stdout == touched.stdout
        assert "unreviewed" in touched.stdout
    assert path.read_bytes() == original
    assert load_risk_register("demo", programs_root=programs_root) == before


@pytest.mark.parametrize("format", ("json", "csv", "human"))
def test_risks_review_labels_preserve_malformed_review_failure(monkeypatch, tmp_path: Path, format: str) -> None:
    programs_root, _ = _seed_review_label_cases(monkeypatch, tmp_path, RiskStatus.OPEN)
    path = programs_root / "demo" / "risk_register.yaml"
    malformed = path.read_text(encoding="utf-8").replace("last_reviewed_date: null", "last_reviewed_date: not-a-date", 1)
    path.write_text(malformed, encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid risk entry"):
        ProgramReality.load("demo", programs_root=programs_root)
    result = runner.invoke(app, ["risks", "list", "--program", "demo", "--format", format])
    assert result.exit_code != 0
    assert "unreviewed" not in result.stdout
    assert "current" not in result.stdout
    assert path.read_text(encoding="utf-8") == malformed


_REVIEW_AS_OF = date(2026, 10, 29)


def _seed_review_label_cases(monkeypatch, tmp_path: Path, status: RiskStatus) -> tuple[Path, tuple[RiskEntry, ...]]:
    class ReviewClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.risks.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.risks.datetime", ReviewClock)
    _seed_risk_register(programs_root)
    base = load_risk_register("demo", programs_root=programs_root)[0]
    cases = (
        ("missing_recent", None, _REVIEW_AS_OF),
        ("missing_old", None, _REVIEW_AS_OF - timedelta(days=31)),
        ("future", _REVIEW_AS_OF + timedelta(days=1), _REVIEW_AS_OF),
        ("today", _REVIEW_AS_OF, _REVIEW_AS_OF),
        ("day30", _REVIEW_AS_OF - timedelta(days=30), _REVIEW_AS_OF),
        ("day31", _REVIEW_AS_OF - timedelta(days=31), _REVIEW_AS_OF),
    )
    entries = tuple(
        replace(base, id=f"risk-{name}", title=f"Review {name}", status=status, identified_date=identified,
                last_reviewed_date=reviewed)
        for name, reviewed, identified in cases
    )
    save_risk_register("demo", entries, programs_root=programs_root)
    return programs_root, entries


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