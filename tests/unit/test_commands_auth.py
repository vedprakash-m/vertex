from __future__ import annotations

from typer.testing import CliRunner

from cli import app


runner = CliRunner()


def test_admin_auth_setup_runs_azure_cli_login(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("src.commands.auth._has_azure_cli", lambda: True)

    def _fake_run(command: list[str]) -> int:
        captured["command"] = tuple(command)
        return 0

    monkeypatch.setattr("src.commands.auth._run_azure_cli_login", _fake_run)

    result = runner.invoke(app, ["admin", "auth", "setup", "--tenant-id", "tenant-123", "--use-device-code"])

    assert result.exit_code == 0
    assert captured["command"] == ("az", "login", "--tenant", "tenant-123", "--use-device-code")
    assert "Azure CLI sign-in completed." in result.stdout
    assert "vertex doctor --check-auth" in result.stdout


def test_admin_auth_setup_reports_missing_azure_cli(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.auth._has_azure_cli", lambda: False)

    result = runner.invoke(app, ["admin", "auth", "setup"])

    assert result.exit_code == 2
    assert "Azure CLI is not installed" in result.stderr


def test_admin_auth_setup_reports_browser_fallback_on_login_failure(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.auth._has_azure_cli", lambda: True)
    monkeypatch.setattr("src.commands.auth._run_azure_cli_login", lambda _command: 1)

    result = runner.invoke(app, ["admin", "auth", "setup"])

    assert result.exit_code == 2
    assert "https://aka.ms/devicelogin" in result.stderr
    assert "--use-device-code" in result.stderr