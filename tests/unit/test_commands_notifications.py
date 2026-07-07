from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.notification_state_store import ConfirmedNotification, append_confirmed_notify_run


runner = CliRunner()


def test_admin_notifications_command_aggregates_program_editions_since_date(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    output_root = tmp_path / "output"
    (programs_root / "acme" / "editions").mkdir(parents=True)
    (programs_root / "fabrikam" / "editions").mkdir(parents=True)
    (programs_root / "acme" / "editions" / "acme_weekly.yaml").write_text("id: acme_weekly\nprogram_id: acme\n", encoding="utf-8")
    (programs_root / "acme" / "editions" / "nova_daily.yaml").write_text("id: nova_daily\nprogram_id: acme\n", encoding="utf-8")
    (programs_root / "fabrikam" / "editions" / "fabrikam_weekly.yaml").write_text("id: fabrikam_weekly\nprogram_id: fabrikam\n", encoding="utf-8")

    append_confirmed_notify_run(
        edition="acme_weekly",
        issue_number=1,
        confirmed_at=datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc),
        notifications=(
            ConfirmedNotification(
                dri_email="jordan@example.com",
                to=("jordan@example.com",),
                cc=(),
                subject="Deployment safety remediation",
                work_item_ids=(901001,),
            ),
        ),
        programs_root=programs_root,
    )
    append_confirmed_notify_run(
        edition="nova_daily",
        issue_number=2,
        confirmed_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
        notifications=(
            ConfirmedNotification(
                dri_email="brenda@example.com",
                to=("brenda@example.com",),
                cc=(),
                subject="Missing roll-forward guardrails",
                work_item_ids=(901002,),
            ),
        ),
        programs_root=programs_root,
    )
    append_confirmed_notify_run(
        edition="fabrikam_weekly",
        issue_number=3,
        confirmed_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        notifications=(
            ConfirmedNotification(
                dri_email="fabrikam@example.com",
                to=("fabrikam@example.com",),
                cc=(),
                subject="Ignore other program",
                work_item_ids=(800001,),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "notifications",
            "--program",
            "acme",
            "--since",
            "2026-05-05",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "Notification Log - acme" in result.output
    assert "edition=nova_daily" in result.output
    assert "Missing roll-forward guardrails" in result.output
    assert "Deployment safety remediation" not in result.output
    assert "fabrikam_weekly" not in result.output


def test_admin_notifications_command_emits_json_payload(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme" / "editions").mkdir(parents=True)
    (programs_root / "acme" / "editions" / "acme_weekly.yaml").write_text("id: acme_weekly\nprogram_id: acme\n", encoding="utf-8")

    append_confirmed_notify_run(
        edition="acme_weekly",
        issue_number=7,
        confirmed_at=datetime(2026, 5, 7, 9, 30, tzinfo=timezone.utc),
        notifications=(
            ConfirmedNotification(
                dri_email="jordan@example.com",
                to=("jordan@example.com",),
                cc=("lead@example.com",),
                subject="Escalate missing rollout evidence",
                work_item_ids=(901010, 901011),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "notifications",
            "--program",
            "acme",
            "--since",
            "2026-05-01",
            "--format",
            "json",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["program_id"] == "acme"
    assert len(payload["events"]) == 1
    event = payload["events"][0]
    assert event["edition"] == "acme_weekly"
    assert event["issue_number"] == 7
    assert event["notification_count"] == 1
    assert event["notifications"][0]["work_item_ids"] == [901010, 901011]

