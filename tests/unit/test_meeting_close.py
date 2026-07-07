from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

import cli
from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands import meeting_close as meeting_close_command
from src.core.action_tracker import load_actions
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus
from src.m365.transcript_reader import TranscriptRecord


runner = CliRunner()


def test_meeting_close_renders_packet_and_writes_local_artifacts(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())
    opened_paths: list[Path] = []
    monkeypatch.setattr(meeting_close_command, "_open_html_artifact", lambda path: opened_paths.append(path))
    monkeypatch.setattr(meeting_close_command, "_get_terminal_width", lambda: 140)

    result = runner.invoke(cli.app, ["meeting-close", "--program", "acme", "--transcript", "lt-sync-123", "--html", "--teams"])

    assert result.exit_code == 0
    golden = (Path(__file__).parents[1] / "golden" / "meeting_close_output.txt").read_text(encoding="utf-8")
    assert result.stdout == golden

    packet_path = repo_root / "programs" / "acme" / "publications" / "acme" / "meeting_close" / "lt-sync-123.txt"
    assert packet_path.exists()
    html_path = repo_root / "programs" / "acme" / "publications" / "acme" / "meeting_close" / "lt-sync-123.html"
    assert html_path.exists()
    teams_path = repo_root / "programs" / "acme" / "publications" / "acme" / "meeting_close" / "lt-sync-123.teams.md"
    assert teams_path.exists()
    proposal_path = repo_root / "programs" / "acme" / "publications" / "acme" / "ado_proposals" / "meeting-action-lt-sync-123.json"
    assert proposal_path.exists()
    proposal_payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal_payload["update_type"] == "meeting_action"
    assert proposal_payload["entries"][0]["action"] == "add_comment"
    assert opened_paths == [html_path]
    teams_text = teams_path.read_text(encoding="utf-8")
    assert "# LT Sync follow-up" in teams_text
    assert "## Mapped actions" in teams_text
    assert "## Net-new or incomplete actions" in teams_text


def test_meeting_close_review_layout_collapses_for_narrow_terminal(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())

    artifacts = meeting_close_command.generate_meeting_close_artifacts(
        program_id="acme",
        meeting_id="lt-sync-123",
        title_override=None,
        emit_html=False,
        emit_teams=False,
        dry_run=True,
        programs_root=repo_root / "programs",
    )

    rendered = meeting_close_command._render_review_layout(
        artifacts,
        programs_root=repo_root / "programs",
        terminal_width=90,
    )

    assert "Action review" in rendered
    assert "Evidence excerpts" in rendered
    assert "matched transcript ref WI:101" in rendered
    assert "dry-run (not written)" in rendered


def test_meeting_close_json_output_reports_teams_artifact(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())

    result = runner.invoke(
        cli.app,
        ["meeting-close", "--program", "acme", "--transcript", "lt-sync-123", "--teams", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["teams_path"] == "programs/acme/publications/acme/meeting_close/lt-sync-123.teams.md"


def test_meeting_close_promotes_actions_into_review_queue_idempotently(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())

    first_result = runner.invoke(
        cli.app,
        [
            "meeting-close",
            "--program",
            "acme",
            "--transcript",
            "lt-sync-123",
            "--promote-actions",
            "--format",
            "json",
        ],
    )
    second_result = runner.invoke(
        cli.app,
        [
            "meeting-close",
            "--program",
            "acme",
            "--transcript",
            "lt-sync-123",
            "--promote-actions",
            "--format",
            "json",
        ],
    )

    assert first_result.exit_code == 0
    first_payload = json.loads(first_result.stdout)
    assert first_payload["action_log_path"] == "programs/acme/journal/actions.jsonl"
    assert first_payload["queued_action_count"] == 2
    assert first_payload["skipped_action_count"] == 0
    assert first_payload["review_command"] == "vertex actions review --program acme"

    assert second_result.exit_code == 0
    second_payload = json.loads(second_result.stdout)
    assert second_payload["queued_action_count"] == 0
    assert second_payload["skipped_action_count"] == 2

    actions = load_actions("acme", programs_root=repo_root / "programs")
    assert len(actions) == 2
    assert {action.id for action in actions} == {"act-1", "act-2"}
    assert all(action.status is ActionStatus.PROPOSED for action in actions)


def test_meeting_close_disabled_mode_uses_deterministic_extractor_without_ai_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = _seed_repo(tmp_path)
    program_path = repo_root / "programs" / "acme" / "program.yaml"
    program_payload = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_payload, dict)
    program_payload["ai"] = {"enabled": True, "budget_usd_per_run": 0.25}
    program_path.write_text(yaml.safe_dump(program_payload, sort_keys=False), encoding="utf-8")

    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-disabled",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-disabled",
                content="Action: follow up with priya by 2026-05-14 on WI:101 to confirm the ramp packet.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = meeting_close_command.generate_meeting_close_artifacts(
            program_id="acme",
            meeting_id="lt-sync-disabled",
            title_override=None,
            emit_html=False,
            emit_teams=False,
            dry_run=False,
            programs_root=repo_root / "programs",
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert artifacts.extractor == "deterministic"
    assert artifacts.packet_path is not None and artifacts.packet_path.exists()
    assert artifacts.proposal_path is not None and artifacts.proposal_path.exists()
    assert not (repo_root / "programs" / "acme" / "publications" / "acme" / "ai").exists()


def test_meeting_close_dismiss_review_filters_action_from_outputs_and_queue(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())

    result = runner.invoke(
        cli.app,
        [
            "meeting-close",
            "--program",
            "acme",
            "--transcript",
            "lt-sync-123",
            "--promote-actions",
            "--dismiss-action",
            "2",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["review_plan_applied"] is True
    assert payload["approved_action_count"] == 0
    assert payload["dismissed_action_count"] == 1
    assert payload["pending_action_count"] == 1
    assert payload["queued_action_count"] == 1
    assert len(payload["mappings"]) == 1
    assert payload["mappings"][0]["action"]["id"] == "act-1"
    assert "broader launch list" not in payload["follow_up_message"]

    proposal_path = repo_root / "programs" / "acme" / "publications" / "acme" / "ado_proposals" / "meeting-action-lt-sync-123.json"
    proposal_payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert len(proposal_payload["entries"]) == 1

    actions = load_actions("acme", programs_root=repo_root / "programs")
    assert len(actions) == 1
    assert actions[0].id == "act-1"
    assert actions[0].status is ActionStatus.PROPOSED


def test_meeting_close_edit_review_updates_action_before_promotion(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())

    result = runner.invoke(
        cli.app,
        [
            "meeting-close",
            "--program",
            "acme",
            "--transcript",
            "lt-sync-123",
            "--promote-actions",
            "--approve-action",
            "1",
            "--edit-action",
            "2",
            "--edit-text",
            "Send revised readiness recap to launch leaders.",
            "--edit-owner",
            "bob",
            "--edit-due",
            "2026-06-03",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["review_plan_applied"] is True
    assert payload["approved_action_count"] == 2
    assert payload["dismissed_action_count"] == 0
    assert payload["pending_action_count"] == 0
    assert payload["queued_action_count"] == 2
    edited_mapping = next(mapping for mapping in payload["mappings"] if mapping["action"]["id"] == "act-2")
    assert edited_mapping["action"]["text"] == "Send revised readiness recap to launch leaders."
    assert edited_mapping["action"]["owner_alias"] == "bob"
    assert edited_mapping["action"]["due_date"] == "2026-06-03"
    assert "bob by 2026-06-03: Send revised readiness recap to launch leaders." in payload["follow_up_message"]

    actions = load_actions("acme", programs_root=repo_root / "programs")
    status_by_id = {action.id: action for action in actions}
    assert status_by_id["act-1"].status is ActionStatus.OPEN
    assert status_by_id["act-2"].status is ActionStatus.OPEN
    assert status_by_id["act-2"].text == "Send revised readiness recap to launch leaders."
    assert status_by_id["act-2"].owner_alias == "bob"
    assert status_by_id["act-2"].due_date == date(2026, 6, 3)


def test_meeting_close_can_apply_ado_after_all_actions_are_reviewed(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())
    apply_calls: list[str] = []
    def _stub_apply_ado_proposal(proposal_reference, *, programs_root=None, **kwargs):
        apply_calls.append(proposal_reference)
        return SimpleNamespace(
            manifest_path=Path(proposal_reference),
            applied_count=2,
            skipped_count=0,
            conflict_count=0,
            failed_count=0,
        )
    monkeypatch.setattr(
        meeting_close_command,
        "apply_ado_proposal",
        _stub_apply_ado_proposal,
    )

    result = runner.invoke(
        cli.app,
        [
            "meeting-close",
            "--program",
            "acme",
            "--transcript",
            "lt-sync-123",
            "--approve-action",
            "1",
            "--approve-action",
            "2",
            "--promote-actions",
            "--apply-ado",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(apply_calls) == 1
    assert payload["ado_apply_applied_count"] == 2
    assert payload["ado_apply_skipped_count"] == 0
    assert payload["ado_apply_conflict_count"] == 0
    assert payload["ado_apply_failed_count"] == 0
    assert payload["ado_apply_manifest_path"] == "programs/acme/publications/acme/ado_proposals/meeting-action-lt-sync-123.json"


def test_meeting_close_blocks_ado_apply_when_review_decisions_are_pending(tmp_path: Path, monkeypatch) -> None:
    repo_root = _seed_repo(tmp_path)
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(meeting_close_command, "PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        meeting_close_command,
        "_build_transcript_reader",
        lambda: _StubTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-123",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-123",
                content="Review WI:101 and confirm next steps.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_command, "_extract_actions_from_transcript", _stub_extract_actions)
    monkeypatch.setattr(meeting_close_command, "_build_ado_client", lambda program: _StubADOClient())

    result = runner.invoke(
        cli.app,
        [
            "meeting-close",
            "--program",
            "acme",
            "--transcript",
            "lt-sync-123",
            "--apply-ado",
        ],
    )

    assert result.exit_code == 1
    assert "--apply-ado requires all extracted actions to be resolved in-command" in result.stdout


class _StubTranscriptReader:
    def __init__(self, record: TranscriptRecord) -> None:
        self._record = record

    def get_transcript(self, *, meeting_id: str) -> TranscriptRecord | None:
        return self._record if meeting_id == self._record.meeting_id else None


class _StubADOClient:
    def query_work_items_batch(self, work_item_ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
        assert work_item_ids == [101]
        assert "System.AreaPath" in fields
        return [
            {
                "id": 101,
                "rev": 17,
                "fields": {
                    "System.Id": 101,
                    "System.Title": "UD chunking",
                    "System.AreaPath": "Acme\\UD",
                    "System.AssignedTo": {"uniqueName": "alice@contoso.com"},
                    "System.Rev": 17,
                },
            }
        ]


def _stub_extract_actions(program, signal):
    return (
        (
            ActionItem(
                id="act-1",
                program_id="acme",
                text="Confirm UD mitigation plan with leadership.",
                owner_alias="alice",
                due_date=date(2026, 6, 1),
                status=ActionStatus.PROPOSED,
                source_signal_id=signal.id,
                source_type=ActionSourceType.MEETING_TRANSCRIPT,
                linked_work_item_ids=(101,),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id=None,
                created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
            ActionItem(
                id="act-2",
                program_id="acme",
                text="Send readiness recap to the broader launch list.",
                owner_alias="unknown",
                due_date=None,
                status=ActionStatus.PROPOSED,
                source_signal_id=signal.id,
                source_type=ActionSourceType.MEETING_TRANSCRIPT,
                linked_work_item_ids=(),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id=None,
                created_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
        ),
        "stub",
    )


def _seed_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path
    (repo_root / "programs" / "acme").mkdir(parents=True, exist_ok=True)
    (repo_root / "output").mkdir(parents=True, exist_ok=True)

    (repo_root / "programs" / "acme" / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "3.0",
                "id": "acme",
                "name": "Acme",
                "ado": {
                    "organization": "org",
                    "project": "proj",
                    "area_paths": ["Acme\\UD"],
                    "work_item_types": ["Feature"],
                    "excluded_states": [],
                    "date_window_days": 30,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "workstreams": [
                    {"id": "ud", "name": "UD", "area_paths": ["Acme\\UD"]},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return repo_root
