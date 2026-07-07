from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
from src.core.config_loader import ConfigError
from src.core.action_tracker import append_action, load_actions
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, TrajectoryPoint
from src.core.trajectory import backfill_trajectory_points


runner = CliRunner()


def test_actions_list_cli(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root)

    result = runner.invoke(app, ["actions", "list", "--program", "demo"])

    assert result.exit_code == 0
    assert "ACTION REGISTER" in result.stdout
    assert "Follow up with the firmware team" in result.stdout


def test_actions_list_cli_json(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root, status=ActionStatus.OPEN, source_signal_id="meeting-close:demo:lt-sync-123")

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["program_id"] == "demo"
    assert payload["meeting_close_batches"][0]["meeting_id"] == "lt-sync-123"
    assert payload["meeting_close_batches"][0]["status_counts"] == {"open": 1}
    assert payload["actions"][0]["id"] == "action-demo-1"
    assert payload["actions"][0]["created_at"] == "2026-05-10T09:00:00+00:00"


def test_actions_list_cli_uses_fact_projection_without_unrelated_risk_loader(
    monkeypatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root, status=ActionStatus.OPEN)

    def _fail_risk_loader(*_args: object, **_kwargs: object) -> object:
        raise ConfigError("risk register unavailable")

    monkeypatch.setattr("src.core.program_fact_store.load_risk_register", _fail_risk_loader)

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["actions"][0]["id"] == "action-demo-1"


def test_actions_list_cli_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root, status=ActionStatus.OPEN)

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[0]["id"] == "action-demo-1"
    assert rows[0]["status"] == "open"


def test_actions_review_cli_uses_fact_projection_without_unrelated_risk_loader(
    monkeypatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root, status=ActionStatus.PROPOSED)

    def _fail_risk_loader(*_args: object, **_kwargs: object) -> object:
        raise ConfigError("risk register unavailable")

    monkeypatch.setattr("src.core.program_fact_store.load_risk_register", _fail_risk_loader)

    result = runner.invoke(app, ["actions", "review", "--program", "demo"], input="q\n")

    assert result.exit_code == 0
    assert "action-demo-1" in result.stdout


def test_actions_list_cli_csv_surfaces_meeting_close_batch_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Send revised recap",
        status=ActionStatus.OPEN,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1002,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[-1]["id"] == "meeting-batch:lt-sync-123"
    assert rows[-1]["status"] == "summary"
    assert rows[-1]["source_type"] == "meeting_close_batch"
    assert rows[-1]["source_signal_id"] == "meeting-close:demo:lt-sync-123"
    assert rows[-1]["text"] == "2 actions | open=1, proposed=1"


def test_actions_list_cli_csv_surfaces_meeting_close_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1001,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))
    pattern_rows = [row for row in rows if row["source_type"] == "meeting_close_pattern"]
    pattern_map = {row["id"]: row for row in pattern_rows}

    assert result.exit_code == 0
    assert len(pattern_rows) == 8
    assert pattern_map["meeting-pattern:workstream:ws-demo"]["status"] == "summary"
    assert pattern_map["meeting-pattern:workstream:ws-demo"]["source_type"] == "meeting_close_pattern"
    assert pattern_map["meeting-pattern:workstream:ws-demo"]["text"] == "repeated workstream ws-demo across 2 meetings | lt-sync-123, staff-sync-456"
    assert pattern_map["meeting-pattern:owner:owner"]["text"] == "repeated owner owner across 2 meetings | lt-sync-123, staff-sync-456"
    assert pattern_map["meeting-pattern:owner_workstream:owner:ws-demo"]["text"] == "repeated owner_workstream owner / ws-demo across 2 meetings | lt-sync-123, staff-sync-456"
    assert pattern_map["meeting-pattern:owner_work_item:owner:WI:1001"]["text"] == "repeated owner_work_item owner / WI:1001 across 2 meetings | lt-sync-123, staff-sync-456"
    assert pattern_map["meeting-pattern:owner_due_date:owner:2026-05-20"]["text"] == "repeated owner_due_date owner / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456"
    assert pattern_map["meeting-pattern:workstream_due_date:ws-demo:2026-05-20"]["text"] == "repeated workstream_due_date ws-demo / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456"
    assert pattern_map["meeting-pattern:due_date:2026-05-20"]["text"] == "repeated due_date 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456"
    assert pattern_map["meeting-pattern:work_item:WI:1001"]["text"] == "repeated work_item WI:1001 across 2 meetings | lt-sync-123, staff-sync-456"


def test_actions_list_cli_csv_surfaces_repeated_meeting_close_action_text_pattern(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        text="Follow up with the firmware team",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
        owner_alias="owner-a",
        workstream_id="ws-alpha",
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Follow up with the firmware team",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1003,),
        owner_alias="owner-b",
        workstream_id="ws-beta",
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))
    text_pattern = next(row for row in rows if row["id"] == "meeting-pattern:action_text:follow-up-with-the-firmware-team")

    assert result.exit_code == 0
    assert text_pattern["source_type"] == "meeting_close_pattern"
    assert text_pattern["text"] == "repeated action_text follow up with the firmware team across 2 meetings | lt-sync-123, staff-sync-456"


def test_actions_review_cli_approves_proposed_action(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root, status=ActionStatus.PROPOSED)

    result = runner.invoke(app, ["actions", "review", "--program", "demo", "--reviewer", "owner"], input="a\n\n")
    actions = load_actions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert actions[0].status is ActionStatus.OPEN


def test_actions_review_cli_apply_ado_for_fully_approved_meeting_close_batch(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Send revised recap",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1002,),
    )
    apply_calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "src.commands.actions.apply_ado_proposal",
        lambda proposal_reference, *, programs_root: apply_calls.append((proposal_reference, programs_root))
        or SimpleNamespace(applied_count=2, skipped_count=0, conflict_count=0, failed_count=0),
    )

    result = runner.invoke(
        app,
        ["actions", "review", "--program", "demo", "--reviewer", "owner", "--apply-ado"],
        input="a\n\na\n\n",
    )

    actions = load_actions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert all(action.status is ActionStatus.OPEN for action in actions)
    assert apply_calls == [("meeting-action-lt-sync-123", programs_root)]
    assert "Applied meeting-close proposal meeting-action-lt-sync-123: 2 applied | 0 skipped | 0 conflict | 0 failed" in result.stdout


def test_actions_review_cli_skips_apply_ado_for_non_fully_approved_meeting_batch(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Send revised recap",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1002,),
    )
    apply_calls: list[str] = []
    monkeypatch.setattr(
        "src.commands.actions.apply_ado_proposal",
        lambda proposal_reference, *, programs_root: apply_calls.append(proposal_reference),
    )

    result = runner.invoke(
        app,
        ["actions", "review", "--program", "demo", "--reviewer", "owner", "--apply-ado"],
        input="a\n\nc\n\n",
    )

    actions = load_actions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert {action.status for action in actions} == {ActionStatus.OPEN, ActionStatus.CANCELLED}
    assert apply_calls == []
    assert "Skipped meeting-close ADO apply for lt-sync-123: only fully approved batches can be applied." in result.stdout


def test_actions_review_cli_reports_multi_meeting_batch_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Send revised recap",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1002,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-3",
        text="Zebra capture rollout decision",
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1001,),
    )
    apply_calls: list[str] = []
    monkeypatch.setattr(
        "src.commands.actions.apply_ado_proposal",
        lambda proposal_reference, *, programs_root: apply_calls.append(proposal_reference)
        or SimpleNamespace(applied_count=2, skipped_count=0, conflict_count=0, failed_count=0),
    )

    result = runner.invoke(
        app,
        ["actions", "review", "--program", "demo", "--reviewer", "owner", "--apply-ado"],
        input="a\n\na\n\nc\n\n",
    )

    assert result.exit_code == 0
    assert apply_calls == ["meeting-action-lt-sync-123"]
    assert "Applied meeting-close proposal meeting-action-lt-sync-123: 2 applied | 0 skipped | 0 conflict | 0 failed" in result.stdout
    assert "Skipped meeting-close ADO apply for staff-sync-456: only fully approved batches can be applied." in result.stdout
    assert (
        "Meeting-close batch summary: 2 meetings | 3 actions | 1 applied batch | 1 skipped batch | 2 applied | 0 skipped | 0 conflict | 0 failed"
        in result.stdout
    )
    assert "Meeting-close pattern summary: 8 recurring cross-meeting patterns" in result.stdout
    assert "- repeated workstream ws-demo across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner owner across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_workstream owner / ws-demo across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_work_item owner / WI:1001 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_due_date owner / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated workstream_due_date ws-demo / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated due_date 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated work_item WI:1001 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout


def test_actions_review_cli_apply_ado_pattern_summary_excludes_pending_batches(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        text="Approve lt sync recap",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Cancel staff sync recap",
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1002,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-3",
        text="Pending exec sync recap",
        source_signal_id="meeting-close:demo:exec-sync-789",
        linked_work_item_ids=(1003,),
    )
    apply_calls: list[str] = []
    monkeypatch.setattr(
        "src.commands.actions.apply_ado_proposal",
        lambda proposal_reference, *, programs_root: apply_calls.append(proposal_reference)
        or SimpleNamespace(applied_count=1, skipped_count=0, conflict_count=0, failed_count=0),
    )

    result = runner.invoke(
        app,
        ["actions", "review", "--program", "demo", "--reviewer", "owner", "--apply-ado"],
        input="a\n\nc\n\nq\n",
    )

    assert result.exit_code == 0
    assert apply_calls == ["meeting-action-lt-sync-123"]
    assert "Skipped meeting-close ADO apply for exec-sync-789: pending review decisions remain." in result.stdout
    assert "Meeting-close pattern summary: 6 recurring cross-meeting patterns" in result.stdout
    assert "lt-sync-123, staff-sync-456" in result.stdout
    assert "exec-sync-789" not in result.stdout.split("Meeting-close pattern summary:", 1)[1]


def test_actions_review_cli_surfaces_meeting_close_batches_before_prompting(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1003,),
    )

    result = runner.invoke(
        app,
        ["actions", "review", "--program", "demo", "--reviewer", "owner"],
        input="q\n",
    )

    assert result.exit_code == 0
    assert "MEETING-CLOSE BATCHES — 2" in result.stdout
    assert "- lt-sync-123 | 1 actions | earliest due 2026-05-20 | proposed=1" in result.stdout
    assert "- staff-sync-456 | 1 actions | earliest due 2026-05-20 | proposed=1" in result.stdout


def test_actions_list_cli_marks_resolution_candidates(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root, status=ActionStatus.OPEN)
    backfill_trajectory_points(
        "demo",
        1001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 11),
                state="Resolved",
                assigned_to="owner@example.com",
                target_date=None,
                risk_level=None,
                area_path="One\\Demo",
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo"])

    assert result.exit_code == 0
    assert "candidate for resolution" in result.stdout


def test_actions_list_cli_surfaces_meeting_close_batch_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Send revised recap",
        status=ActionStatus.OPEN,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1002,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-3",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1001,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo"])

    assert result.exit_code == 0
    assert "MEETING-CLOSE BATCHES — 2" in result.stdout
    assert "- lt-sync-123 | 2 actions | earliest due 2026-05-20 | open=1, proposed=1" in result.stdout
    assert "- staff-sync-456 | 1 actions | earliest due 2026-05-20 | proposed=1" in result.stdout
    assert "MEETING-CLOSE PATTERNS — 8" in result.stdout
    assert "- repeated workstream ws-demo across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner owner across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_workstream owner / ws-demo across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_work_item owner / WI:1001 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_due_date owner / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated workstream_due_date ws-demo / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated due_date 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated work_item WI:1001 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout


def test_actions_list_cli_json_surfaces_meeting_close_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1001,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)
    patterns = {(pattern["pattern_type"], pattern["key"]): pattern for pattern in payload["meeting_close_patterns"]}

    assert result.exit_code == 0
    assert patterns[("workstream", "ws-demo")]["meeting_count"] == 2
    assert patterns[("owner", "owner")]["meeting_count"] == 2
    assert patterns[("owner_workstream", "owner:ws-demo")]["meeting_count"] == 2
    assert patterns[("owner_work_item", "owner:WI:1001")]["meeting_count"] == 2
    assert patterns[("owner_due_date", "owner:2026-05-20")]["meeting_count"] == 2
    assert patterns[("workstream_due_date", "ws-demo:2026-05-20")]["meeting_count"] == 2
    assert patterns[("due_date", "2026-05-20")]["meeting_count"] == 2
    assert patterns[("work_item", "WI:1001")]["meeting_count"] == 2


def test_actions_review_cli_surfaces_meeting_close_patterns_before_prompting(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1001,),
    )

    result = runner.invoke(
        app,
        ["actions", "review", "--program", "demo", "--reviewer", "owner"],
        input="q\n",
    )

    assert result.exit_code == 0
    assert "MEETING-CLOSE PATTERNS — 8" in result.stdout
    assert "- repeated workstream ws-demo across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner owner across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_workstream owner / ws-demo across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_work_item owner / WI:1001 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_due_date owner / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated workstream_due_date ws-demo / 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated due_date 2026-05-20 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated work_item WI:1001 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout


def test_actions_list_cli_json_surfaces_repeated_meeting_close_owner_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1003,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)
    owner_pattern = next(
        pattern for pattern in payload["meeting_close_patterns"] if pattern["pattern_type"] == "owner"
    )

    assert result.exit_code == 0
    assert owner_pattern["key"] == "owner"
    assert owner_pattern["meeting_count"] == 2
    assert owner_pattern["meeting_ids"] == ["lt-sync-123", "staff-sync-456"]


def test_actions_list_cli_json_surfaces_repeated_meeting_close_due_date_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1003,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)
    due_date_pattern = next(
        pattern for pattern in payload["meeting_close_patterns"] if pattern["pattern_type"] == "due_date"
    )

    assert result.exit_code == 0
    assert due_date_pattern["key"] == "2026-05-20"
    assert due_date_pattern["meeting_count"] == 2
    assert due_date_pattern["meeting_ids"] == ["lt-sync-123", "staff-sync-456"]


def test_actions_list_cli_json_surfaces_repeated_meeting_close_owner_workstream_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1003,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)
    owner_workstream_pattern = next(
        pattern for pattern in payload["meeting_close_patterns"] if pattern["pattern_type"] == "owner_workstream"
    )

    assert result.exit_code == 0
    assert owner_workstream_pattern["key"] == "owner:ws-demo"
    assert owner_workstream_pattern["label"] == "owner / ws-demo"
    assert owner_workstream_pattern["meeting_count"] == 2


def test_actions_list_cli_json_surfaces_repeated_meeting_close_owner_due_date_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1003,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)
    owner_due_date_pattern = next(
        pattern for pattern in payload["meeting_close_patterns"] if pattern["pattern_type"] == "owner_due_date"
    )

    assert result.exit_code == 0
    assert owner_due_date_pattern["key"] == "owner:2026-05-20"
    assert owner_due_date_pattern["label"] == "owner / 2026-05-20"
    assert owner_due_date_pattern["meeting_count"] == 2
    assert owner_due_date_pattern["meeting_ids"] == ["lt-sync-123", "staff-sync-456"]


def test_actions_list_cli_json_surfaces_repeated_meeting_close_workstream_due_date_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1003,),
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)
    workstream_due_date_pattern = next(
        pattern for pattern in payload["meeting_close_patterns"] if pattern["pattern_type"] == "workstream_due_date"
    )

    assert result.exit_code == 0
    assert workstream_due_date_pattern["key"] == "ws-demo:2026-05-20"
    assert workstream_due_date_pattern["label"] == "ws-demo / 2026-05-20"
    assert workstream_due_date_pattern["meeting_count"] == 2
    assert workstream_due_date_pattern["meeting_ids"] == ["lt-sync-123", "staff-sync-456"]


def test_actions_list_cli_json_surfaces_repeated_meeting_close_claim_and_risk_patterns(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
        linked_claim_id="claim-demo-7",
        linked_risk_id="risk-demo-3",
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1001,),
        linked_claim_id="claim-demo-7",
        linked_risk_id="risk-demo-3",
    )

    result = runner.invoke(app, ["actions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)
    patterns = {(pattern["pattern_type"], pattern["key"]): pattern for pattern in payload["meeting_close_patterns"]}

    assert result.exit_code == 0
    assert patterns[("claim", "claim-demo-7")]["meeting_count"] == 2
    assert patterns[("owner_claim", "owner:claim-demo-7")]["meeting_count"] == 2
    assert patterns[("workstream_claim", "ws-demo:claim-demo-7")]["meeting_count"] == 2
    assert patterns[("work_item_claim", "WI:1001:claim-demo-7")]["meeting_count"] == 2
    assert patterns[("risk", "risk-demo-3")]["meeting_count"] == 2
    assert patterns[("owner_risk", "owner:risk-demo-3")]["meeting_count"] == 2
    assert patterns[("workstream_risk", "ws-demo:risk-demo-3")]["meeting_count"] == 2
    assert patterns[("work_item_risk", "WI:1001:risk-demo-3")]["meeting_count"] == 2


def test_actions_review_cli_surfaces_meeting_close_claim_and_risk_patterns_before_prompting(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(
        programs_root,
        action_id="action-demo-1",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:lt-sync-123",
        linked_work_item_ids=(1001,),
        linked_claim_id="claim-demo-7",
        linked_risk_id="risk-demo-3",
    )
    _seed_action(
        programs_root,
        action_id="action-demo-2",
        text="Capture rollout decision",
        status=ActionStatus.PROPOSED,
        source_signal_id="meeting-close:demo:staff-sync-456",
        linked_work_item_ids=(1001,),
        linked_claim_id="claim-demo-7",
        linked_risk_id="risk-demo-3",
    )

    result = runner.invoke(
        app,
        ["actions", "review", "--program", "demo", "--reviewer", "owner"],
        input="q\n",
    )

    assert result.exit_code == 0
    assert "- repeated claim claim-demo-7 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_claim owner / claim-demo-7 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated workstream_claim ws-demo / claim-demo-7 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated work_item_claim WI:1001 / claim-demo-7 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated risk risk-demo-3 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated owner_risk owner / risk-demo-3 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated workstream_risk ws-demo / risk-demo-3 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout
    assert "- repeated work_item_risk WI:1001 / risk-demo-3 across 2 meetings | lt-sync-123, staff-sync-456" in result.stdout


def test_actions_resolve_cli_marks_action_done(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.actions.PROGRAMS_ROOT", programs_root)
    _seed_action(programs_root, status=ActionStatus.OPEN)

    result = runner.invoke(
        app,
        [
            "actions",
            "resolve",
            "--program",
            "demo",
            "--id",
            "action-demo-1",
            "--status",
            "done",
            "--note",
            "Completed in ADO.",
            "--resolver",
            "owner",
        ],
    )
    actions = load_actions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert actions[0].status is ActionStatus.DONE
    assert actions[0].resolution_note == "Completed in ADO."


def _seed_action(
    programs_root: Path,
    *,
    action_id: str = "action-demo-1",
    text: str = "Follow up with the firmware team",
    status: ActionStatus = ActionStatus.PROPOSED,
    source_signal_id: str = "signal-demo-1",
    linked_work_item_ids: tuple[int, ...] = (1001,),
    owner_alias: str = "owner",
    workstream_id: str = "ws-demo",
    linked_claim_id: str | None = None,
    linked_risk_id: str | None = None,
) -> None:
    append_action(
        "demo",
        ActionItem(
            id=action_id,
            program_id="demo",
            text=text,
            owner_alias=owner_alias,
            due_date=date(2026, 5, 20),
            status=status,
            source_signal_id=source_signal_id,
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=linked_work_item_ids,
            linked_claim_id=linked_claim_id,
            linked_risk_id=linked_risk_id,
            workstream_id=workstream_id,
            created_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )