from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

import src.commands.doctor as doctor
from src.commands.doctor_checks.models import DoctorReport
from src.core.exceptions import ConfigError


@pytest.mark.parametrize("selection", ["programs_root", "reports_root", "archive_root"])
def test_doctor_implicit_roots_follow_explicit_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    report = DoctorReport(edition="synthetic", checks=())
    auth = Mock(return_value=report)
    milestones = Mock(return_value=report)
    monkeypatch.setattr(doctor, "_run_auth_doctor", auth)
    monkeypatch.setattr(doctor, "_run_milestone_doctor", milestones)
    roots = {selection: tmp_path / selection.removesuffix("_root")}

    doctor.run_doctor(edition_name="synthetic", check_auth=True, **roots)
    doctor.run_doctor(edition_name="synthetic", milestones=True, **roots)

    assert auth.call_args.kwargs["reports_root"] == tmp_path / "reports"
    assert milestones.call_args.kwargs["archive_root"] == tmp_path / "archive"
    for consumer in (auth, milestones):
        assert consumer.call_args.kwargs["programs_root"] == tmp_path / "programs"
        assert consumer.call_args.kwargs["editions_root"] == tmp_path / "editions"


def test_doctor_keeps_explicit_sibling_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    milestones = Mock(return_value=DoctorReport(edition="synthetic", checks=()))
    monkeypatch.setattr(doctor, "_run_milestone_doctor", milestones)
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "selected-reports",
        "archive_root": tmp_path / "selected-archive",
        "editions_root": tmp_path / "selected-editions",
    }

    doctor.run_doctor(edition_name="synthetic", milestones=True, **roots)

    assert milestones.call_args.kwargs == {
        "edition_name": "synthetic",
        "editions_root": roots["editions_root"],
        "programs_root": roots["programs_root"],
        "archive_root": roots["archive_root"],
    }


def test_doctor_without_explicit_workspace_keeps_existing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    milestones = Mock(return_value=DoctorReport(edition="synthetic", checks=()))
    monkeypatch.setattr(doctor, "_run_milestone_doctor", milestones)

    doctor.run_doctor(edition_name="synthetic", milestones=True)

    assert milestones.call_args.kwargs == {
        "edition_name": "synthetic",
        "editions_root": doctor.EDITIONS_ROOT,
        "programs_root": doctor.REPORTS_ROOT.parent / "programs",
        "archive_root": doctor.ARCHIVE_ROOT,
    }


@pytest.mark.parametrize("edition_args", [[], ["--edition", ""]])
@pytest.mark.parametrize("platform_readiness", [False, True])
def test_doctor_optional_edition_is_not_an_empty_selector(
    monkeypatch: pytest.MonkeyPatch,
    edition_args: list[str],
    platform_readiness: bool,
) -> None:
    lookup = Mock(side_effect=ConfigError("Synthetic invalid edition selector"))
    discover = Mock(return_value=())
    run_doctor = Mock(return_value=DoctorReport(edition="fleet", checks=()))
    monkeypatch.setattr(doctor, "_resolve_edition_paths", lookup)
    monkeypatch.setattr(doctor, "discover_report_editions", discover)
    monkeypatch.setattr(doctor, "run_doctor", run_doctor)
    app = typer.Typer()
    app.command()(doctor.doctor_command)

    mode_args = ["--platform-readiness"] if platform_readiness else []
    result = CliRunner().invoke(app, [*mode_args, *edition_args])

    if edition_args:
        assert isinstance(result.exception, ConfigError)
        assert lookup.call_args.args[0] == ""
        discover.assert_not_called()
        run_doctor.assert_not_called()
    else:
        assert result.exit_code == 0
        lookup.assert_not_called()
        if platform_readiness:
            discover.assert_not_called()
        else:
            discover.assert_called_once_with(
                reports_root=doctor.REPORTS_ROOT, programs_root=doctor.PROGRAMS_ROOT,
            )
        assert run_doctor.call_args.kwargs["platform_readiness"] is platform_readiness
