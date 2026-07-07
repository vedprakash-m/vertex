from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands import investigate


runner = CliRunner()


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: str | None = None
    stderr: str | None = None


def test_investigate_prefers_geneva_binary(monkeypatch) -> None:
    monkeypatch.setattr(investigate.shutil, "which", lambda name: "C:/tools/geneva.exe" if name == "geneva" else None)

    command = investigate._resolve_investigation_command(mode="icm", target="12345")

    assert command == ["C:/tools/geneva.exe", "/investigate", "12345"]


def test_investigate_falls_back_to_agency(monkeypatch) -> None:
    monkeypatch.setattr(
        investigate.shutil,
        "which",
        lambda name: "C:/tools/agency.exe" if name == "agency" else None,
    )

    command = investigate._resolve_investigation_command(mode="health", target="acme-account")

    assert command[:4] == ["C:/tools/agency.exe", "copilot", "--agent", "geneva-monitoring-agent"]
    assert "acme-account" in command[-1]


def test_investigate_writes_markdown_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(investigate.shutil, "which", lambda name: "C:/tools/geneva.exe" if name == "geneva" else None)

    artifacts = investigate.investigate(
        program_id="acme",
        icm_id="12345",
        programs_root=tmp_path,
        runner=lambda command: _Completed(returncode=0, stdout="# Investigation\nAll clear\n"),
        now=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert artifacts.output_path.exists()
    assert "All clear" in artifacts.output_path.read_text(encoding="utf-8")


def test_investigate_cli_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(investigate.shutil, "which", lambda name: "C:/tools/geneva.exe" if name == "geneva" else None)

    result = runner.invoke(app, ["investigate", "--program", "acme", "--icm", "12345", "--dry-run"])

    assert result.exit_code == 0
    assert "Resolved command:" in result.stdout
    assert "Planned output:" in result.stdout