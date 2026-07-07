from __future__ import annotations

from typer.testing import CliRunner

from src.commands import auth


def test_scheduled_pat_status_reports_configured(monkeypatch) -> None:
    monkeypatch.setattr(auth, "get_scheduled_ado_pat", lambda: "secret")

    result = CliRunner().invoke(auth.app, ["scheduled-pat", "--status"])

    assert result.exit_code == 0
    assert "configured" in result.output
    assert "secret" not in result.output


def test_scheduled_pat_status_reports_missing_without_secret(monkeypatch) -> None:
    monkeypatch.setattr(auth, "get_scheduled_ado_pat", lambda: (_ for _ in ()).throw(RuntimeError("missing credential")))

    result = CliRunner().invoke(auth.app, ["scheduled-pat", "--status"])

    assert result.exit_code == 2
    assert "not configured" in result.output


def test_scheduled_pat_prompt_stores_without_echoing_secret(monkeypatch) -> None:
    stored = []
    monkeypatch.setattr(auth, "set_scheduled_ado_pat", stored.append)

    result = CliRunner().invoke(
        auth.app, ["scheduled-pat"], input="synthetic-pat\nsynthetic-pat\n",
    )

    assert result.exit_code == 0
    assert stored == ["synthetic-pat"]
    assert "synthetic-pat" not in result.output
    assert "stored in Credential Manager" in result.output


def test_scheduled_pat_rejects_command_line_secret(monkeypatch) -> None:
    stored = []
    monkeypatch.setattr(auth, "set_scheduled_ado_pat", stored.append)

    result = CliRunner().invoke(auth.app, ["scheduled-pat", "--pat", "synthetic-pat"])

    assert result.exit_code == 2
    assert not stored
