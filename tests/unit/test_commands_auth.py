from __future__ import annotations

from typer.testing import CliRunner

from src.commands import auth


def test_armada_scheduled_pat_status_reports_configured(monkeypatch) -> None:
    monkeypatch.setattr(auth, "get_scheduled_ado_pat", lambda: "secret")

    result = CliRunner().invoke(auth.app, ["armada-scheduled-pat", "--status"])

    assert result.exit_code == 0
    assert "configured" in result.output
    assert "secret" not in result.output


def test_armada_scheduled_pat_status_reports_missing_without_secret(monkeypatch) -> None:
    monkeypatch.setattr(auth, "get_scheduled_ado_pat", lambda: (_ for _ in ()).throw(RuntimeError("missing credential")))

    result = CliRunner().invoke(auth.app, ["armada-scheduled-pat", "--status"])

    assert result.exit_code == 2
    assert "not configured" in result.output
