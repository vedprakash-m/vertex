from __future__ import annotations

from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from cli import app
from src.commands.doctor_checks.models import DoctorReport
from src.core.models import ArchiveIndex


runner = CliRunner()


def _raise_exit(**_: object) -> None:
    raise typer.Exit(code=0)


def test_confirm_command_surfaces_alert_banner(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.commands.confirm._resolve_edition_paths",
        lambda *args, **kwargs: SimpleNamespace(program_id="demo"),
    )
    monkeypatch.setattr(
        "src.commands.confirm._surface_alert_banner",
        lambda *args, **kwargs: "ALERT BANNER",
    )
    monkeypatch.setattr(
        "src.commands.confirm.read_archive_index",
        lambda *args, **kwargs: ArchiveIndex(edition="demo", issues=()),
    )
    monkeypatch.setattr(
        "src.commands.confirm.verify_archive_integrity",
        lambda *args, **kwargs: SimpleNamespace(ok=True, inconsistencies=()),
    )
    monkeypatch.setattr("src.commands.confirm.confirm_issue", _raise_exit)

    result = runner.invoke(app, ["confirm", "--edition", "demo", "--issue", "1"])

    rendered = result.output + getattr(result, "stderr", "")
    assert result.exit_code == 0
    assert "ALERT BANNER" in rendered


def test_doctor_command_surfaces_alert_banner(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.commands.doctor._resolve_edition_paths",
        lambda *args, **kwargs: SimpleNamespace(program_id="demo"),
    )
    monkeypatch.setattr(
        "src.commands.doctor._surface_alert_banner",
        lambda *args, **kwargs: "ALERT BANNER",
    )
    monkeypatch.setattr(
        "src.commands.doctor.run_doctor",
        lambda *args, **kwargs: DoctorReport(edition="demo", checks=()),
    )

    result = runner.invoke(app, ["doctor", "--edition", "demo"])

    rendered = result.output + getattr(result, "stderr", "")
    assert result.exit_code == 0
    assert "ALERT BANNER" in rendered
    assert "Overall: HEALTHY" in rendered
