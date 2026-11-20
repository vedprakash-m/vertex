from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from typer.testing import CliRunner

import src.commands.rev as command
from src.m365.graph_mail_client import MailSearchPage, MailRecord


def test_rev_run_workiq_uses_window_checkpoint_and_does_not_replay(
    tmp_path, monkeypatch,
) -> None:
    record = MailRecord(
        source_id="native-message",
        subject="Deployment completed",
        sender="fixture@example.net",
        recipients=(),
        received_at=datetime.now(timezone.utc).isoformat(),
        web_url="https://outlook.office.com/mail/native-message",
        preview="The rollout deployment completed successfully.",
        thread_id="native-thread",
        conversation_id="native-thread",
    )
    calls: list[tuple[str, str | None]] = []

    class Bridge:
        WORKIQ_TIMEOUT = 90

        def probe(self):
            return SimpleNamespace(available=True, has_workiq=True)

    class Client:
        def __init__(self, bridge):
            self.bridge = bridge

        def search_emails(self, *, query, limit, cursor, timeout_seconds):
            calls.append((query, cursor))
            if cursor is None:
                return MailSearchPage((record,), "cursor-2", "workiq")
            assert cursor == "cursor-2"
            return MailSearchPage((), None, "workiq")

    configured = SimpleNamespace(
        m365=SimpleNamespace(
            retrieval=SimpleNamespace(
                discovery_lookback_days=14,
                max_wall_clock_seconds=30,
            )
        )
    )
    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", Bridge)
    monkeypatch.setattr("src.m365.graph_mail_client.GraphMailClient", Client)
    monkeypatch.setattr(command, "load_program", lambda *args, **kwargs: configured)
    monkeypatch.setattr(
        command,
        "_resolve_profile",
        lambda *args, **kwargs: command.RevRetrievalProfile(
            profile=command.REV_PROFILE_SEARCH_HYDRATE
        ),
    )

    args = [
        "run",
        "--program", "synthetic",
        "--mailbox", "operator@example.net",
        "--workiq-query", "deployment status",
        "--programs-root", str(tmp_path),
    ]
    runner = CliRunner()

    first = runner.invoke(command.app, args)
    second = runner.invoke(command.app, args)
    third = runner.invoke(command.app, args)

    assert first.exit_code == 1, first.output
    assert second.exit_code == 0, second.output
    assert third.exit_code == 0, third.output
    assert calls == [
        ("deployment status", None),
        ("deployment status", "cursor-2"),
    ]
    assert '"enumerated": 1' in first.output
    assert '"enumerated": 0' in second.output
    assert '"enumerated": 0' in third.output


def test_rev_run_workiq_reports_unavailable_provider(
    tmp_path, monkeypatch,
) -> None:
    class Bridge:
        WORKIQ_TIMEOUT = 90

        def probe(self):
            return SimpleNamespace(available=False, has_workiq=False)

    monkeypatch.setattr("src.m365.agency_bridge.AgencyBridge", Bridge)
    monkeypatch.setattr(command, "load_program", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        command,
        "_resolve_profile",
        lambda *args, **kwargs: command.RevRetrievalProfile(
            profile=command.REV_PROFILE_SEARCH_HYDRATE
        ),
    )

    result = CliRunner().invoke(command.app, [
        "run",
        "--program", "synthetic",
        "--mailbox", "operator@example.net",
        "--workiq-query", "deployment status",
        "--programs-root", str(tmp_path),
    ])

    assert result.exit_code == 2
    assert "WorkIQ access unavailable" in result.output
