from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.core.models import NotificationRecord, RiskLevel, WorkItem
from src.core.notification_state_store import load_latest_notification_state
from tests.support.report_test_setup import reset_overrides_to_seed_state, stage_v2_report_workspace


runner = CliRunner()
AS_OF = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
EDITION_NAME = "acme_weekly"


def test_notify_cli_dry_run_previews_email_bodies(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "1", "--dry-run"])

    assert result.exit_code == 0
    assert "NOTIFY PREVIEW" in result.stdout
    assert "To: jordan@example.com" in result.stdout
    assert "Deployment safety remediation" in result.stdout
    assert "Dry run: no notifications sent." in result.stdout
    assert not (tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "notifications").exists()


def test_notify_cli_rejects_issue_mismatch(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "2", "--dry-run"])

    assert result.exit_code == 2
    assert "pending issue 001" in result.stdout


def test_notify_cli_surfaces_unavailable_transport(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "1", "--channel", "email"], input="y\n")

    assert result.exit_code == 2
    assert "Missing GRAPH_TENANT_ID" in result.stdout
    assert not (tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "notifications").exists()


def test_notify_cli_records_successful_send(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    def _successful_sender(preview):
        return NotificationRecord(
            sent_at=datetime.now(timezone.utc),
            channel="email",
            to=preview.to,
            subject=preview.subject,
            message_id=f"msg-{preview.to[0]}",
            success=True,
            error=None,
        )

    monkeypatch.setattr("src.commands.notify._build_notify_email_sender", lambda: _successful_sender)

    result = runner.invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "1", "--channel", "email"], input="y\n")

    assert result.exit_code == 0
    assert "Notification log:" in result.stdout
    assert "Sent 2 notification email(s)." in result.stdout
    state = load_latest_notification_state(edition=EDITION_NAME, programs_root=(tmp_path / "programs"))
    assert state is not None
    assert {item.work_item_id for item in state.items} == {901001, 901002}


def test_notify_cli_writes_eml_drafts_by_default(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "1"], input="y\n")

    eml_paths = sorted((tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "notifications").glob("*.eml"))

    assert result.exit_code == 0
    assert len(eml_paths) == 2
    assert "Notification log:" in result.stdout
    assert "EML:" in result.stdout
    assert "Send manually via Outlook." in result.stdout
    state = load_latest_notification_state(edition=EDITION_NAME, programs_root=(tmp_path / "programs"))
    assert state is not None
    assert {item.work_item_id for item in state.items} == {901001, 901002}


def test_notify_cli_writes_adaptive_card_drafts(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "1", "--channel", "adaptive-card"], input="y\n")

    card_paths = sorted((tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "adaptive_cards").glob("*.json"))

    assert result.exit_code == 0
    assert len(card_paths) == 2
    assert "Notification log:" in result.stdout
    assert "CARD:" in result.stdout
    assert "Post manually to Teams." in result.stdout

    payload = json.loads(card_paths[0].read_text(encoding="utf-8"))
    assert payload["type"] == "AdaptiveCard"
    assert payload["version"] == "1.5"
    assert payload["body"][0]["text"] == f"{EDITION_NAME} freshness alert"

    state = load_latest_notification_state(edition=EDITION_NAME, programs_root=(tmp_path / "programs"))
    assert state is not None
    assert {item.work_item_id for item in state.items} == {901001, 901002}


def test_notify_cli_posts_adaptive_cards_to_configured_webhook(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    _set_notify_webhook_url(reports_root.parent / "programs", webhook_url="https://contoso.example/webhook")

    monkeypatch.setattr("src.commands.notify.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.notify.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.notify.PROGRAMS_ROOT", (tmp_path / "programs"))
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    sent_cards: list[tuple[str, str, dict]] = []

    def _build_fake_sender(webhook_url: str):
        assert webhook_url == "https://contoso.example/webhook"

        def _sender(recipient_email: str, subject: str, payload: dict) -> NotificationRecord:
            sent_cards.append((recipient_email, subject, payload))
            return NotificationRecord(
                sent_at=datetime.now(timezone.utc),
                channel="teams",
                to=(recipient_email,),
                subject=subject,
                message_id=None,
                success=True,
                error=None,
            )

        return _sender

    monkeypatch.setattr("src.commands.notify._build_notify_teams_sender", _build_fake_sender)

    result = runner.invoke(app, ["notify", "--edition", EDITION_NAME, "--issue", "1", "--channel", "adaptive-card"], input="y\n")

    card_paths = sorted((tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "adaptive_cards").glob("*.json"))

    assert result.exit_code == 0
    assert len(card_paths) == 2
    assert len(sent_cards) == 2
    assert "Notification log:" in result.stdout
    assert "CARD:" in result.stdout
    assert "Sent 2 adaptive card notification(s) to Teams." in result.stdout

    state = load_latest_notification_state(edition=EDITION_NAME, programs_root=(tmp_path / "programs"))
    assert state is not None
    assert {item.work_item_id for item in state.items} == {901001, 901002}


def _sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=901001,
            type="Feature",
            title="Deployment safety remediation",
            state="Active",
            assigned_to="Jordan Rivera",
            assigned_to_email="jordan@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 1),
            risk_level=RiskLevel.LOW,
            tags=["Safety"],
            custom_fields={"changed_date": (as_of - timedelta(days=18)).isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=901002,
            type="Risk",
            title="Networking readiness gap",
            state="At Risk",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme\\Networking",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={
                "changed_date": (as_of - timedelta(days=1)).isoformat(),
                "description": "WIP, updating soon",
            },
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _set_notify_webhook_url(programs_root: Path, *, webhook_url: str) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    m365_block = program_document.setdefault("m365", {})
    assert isinstance(m365_block, dict)
    m365_block["teams_incoming_webhook_url"] = webhook_url
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
