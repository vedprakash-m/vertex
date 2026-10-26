from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from src.commands import triage
import src.core.metric_registry as metric_registry
from src.core.exceptions import ConfigError
from tests.unit.test_commands_triage import _seed_v2_triage_layout


def _bind_roots(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    monkeypatch.setattr(triage, "PROGRAMS_ROOT", workspace / "programs")
    monkeypatch.setattr(triage, "REPORTS_ROOT", workspace / "reports")
    monkeypatch.setattr(triage, "ARCHIVE_ROOT", workspace / "archive")


@pytest.mark.parametrize(
    ("selection", "conflict"),
    [
        (selection, conflict)
        for selection in ("omitted", "none", "explicit")
        for conflict in ("reports", "archive", "relative-programs", "relative-reports", "relative-archive")
        if (selection, conflict) != ("explicit", "relative-programs")
    ],
)
def test_invalid_triage_workspace_precedes_metadata_stores_providers_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str, conflict: str,
) -> None:
    _bind_roots(monkeypatch, tmp_path)
    roots = {"reports_root": tmp_path / "reports", "archive_root": tmp_path / "archive"}
    if conflict.startswith("relative-"):
        key = conflict.removeprefix("relative-") + "_root"
        if key == "programs_root":
            monkeypatch.setattr(triage, "PROGRAMS_ROOT", Path("programs"))
        else:
            roots[key] = Path(key)
    else:
        roots[conflict + "_root"] = tmp_path / "other" / conflict
    if selection != "explicit":
        monkeypatch.setattr(triage, "REPORTS_ROOT", roots["reports_root"])
    monkeypatch.setattr(triage, "ARCHIVE_ROOT", roots["archive_root"])
    kwargs = roots if selection == "explicit" else (
        {"reports_root": None, "archive_root": None} if selection == "none" else {}
    )
    forbidden = Mock(side_effect=AssertionError("Workspace validation must precede effects."))
    for name in (
        "load_bundle_with_mode", "resolve_edition", "read_archive_index", "load_program_knowledge",
        "build_signal_store_for_program_id", "build_trajectory_store_for_program_id",
    ):
        monkeypatch.setattr(triage, name, forbidden)
    monkeypatch.setattr(triage.ProgramReality, "load", forbidden)
    with monkeypatch.context() as metadata_guard:
        for name in ("exists", "is_file", "is_dir", "read_text", "write_text", "mkdir"):
            metadata_guard.setattr(Path, name, forbidden)
        with pytest.raises(ConfigError):
            triage.generate_triage_report(
                "fixture", work_item_loader=forbidden, vitality_loader=forbidden, **kwargs,
            )
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("selection", ["omitted", "none"])
def test_default_api_keeps_shared_program_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    _bind_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(triage, "PROGRAMS_ROOT", tmp_path / "selected" / "programs")
    forbidden = Mock(side_effect=AssertionError("Do not replace the shared program root."))
    monkeypatch.setattr(triage, "load_bundle_with_mode", forbidden)
    with pytest.raises(ConfigError, match="Conflicting report workspace roots"):
        triage.generate_triage_report("fixture", **({"reports_root": None} if selection == "none" else {}))
    forbidden.assert_not_called()


@pytest.mark.parametrize("explicit_reports", [False, True])
@pytest.mark.parametrize("explicit_archive", [False, True])
def test_coherent_context_selects_default_or_scoped_program_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_reports: bool, explicit_archive: bool,
) -> None:
    _bind_roots(monkeypatch, tmp_path)
    if explicit_reports:
        monkeypatch.setattr(triage, "PROGRAMS_ROOT", tmp_path / "unselected" / "programs")
    boundary = Mock(side_effect=RuntimeError("selected configuration boundary"))
    monkeypatch.setattr(triage, "load_bundle_with_mode", boundary)
    kwargs = {"reports_root": tmp_path / "reports"} if explicit_reports else {}
    if explicit_archive:
        kwargs["archive_root"] = tmp_path / "archive"
    with pytest.raises(RuntimeError, match="selected configuration boundary"):
        triage.generate_triage_report("fixture", **kwargs)
    boundary.assert_called_once_with(
        "fixture", reports_root=tmp_path / "reports", programs_root=tmp_path / "programs",
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("conflict", ["programs", "reports", "archive", "relative-programs", "relative-reports", "relative-archive"])
def test_direct_command_preflight_rejects_invalid_module_roots_before_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], conflict: str,
) -> None:
    _bind_roots(monkeypatch, tmp_path)
    name = conflict.removeprefix("relative-").upper() + "_ROOT"
    monkeypatch.setattr(triage, name, Path("relative") if conflict.startswith("relative-") else tmp_path / "other" / conflict)
    generation = Mock(side_effect=AssertionError("Preflight must precede generation."))
    monkeypatch.setattr(triage, "generate_triage_report", generation)
    with pytest.raises(typer.Exit) as error:
        triage.triage_command("fixture", "human")
    assert error.value.exit_code == 2
    output = capsys.readouterr()
    assert not output.out
    assert "workspace roots" in output.err
    generation.assert_not_called()


def test_direct_cli_uses_triage_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(triage, "PROGRAMS_ROOT", tmp_path / "other" / "programs")
    generation = Mock(side_effect=AssertionError("Preflight must precede generation."))
    monkeypatch.setattr(triage, "generate_triage_report", generation)
    app = typer.Typer()
    app.command("triage")(triage.triage_command)
    result = CliRunner().invoke(app, ["--edition", "fixture"])
    assert result.exit_code == 2
    assert "Conflicting report workspace roots" in result.stderr
    assert not result.stdout
    generation.assert_not_called()


@pytest.mark.parametrize("selection", ["default", "explicit", "default-archive"])
def test_selected_archive_reaches_every_real_reality_load(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    _bind_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    monkeypatch.setattr(
        metric_registry, "load_metric_definition_map",
        partial(metric_registry.load_metric_definition_map, metrics_root=tmp_path / "knowledge" / "metrics"),
    )
    reality_load = Mock(wraps=triage.ProgramReality.load)
    monkeypatch.setattr(triage.ProgramReality, "load", reality_load)
    kwargs = {}
    if selection != "default":
        monkeypatch.setattr(triage, "PROGRAMS_ROOT", tmp_path / "unselected" / "programs")
        kwargs["reports_root"] = reports_root
    if selection == "explicit":
        monkeypatch.setattr(triage, "ARCHIVE_ROOT", tmp_path / "unselected" / "archive")
        kwargs["archive_root"] = archive_root
    artifacts = triage.generate_triage_report(
        "acme_weekly", issue_number=78, as_of=datetime(2026, 5, 18, tzinfo=timezone.utc),
        work_item_loader=lambda _bundle, _timestamp: ((), 0),
        vitality_loader=lambda _program, _workstreams, _timestamp: ((), 0),
        **kwargs,
    )
    assert artifacts.report.program_id == "acme"
    assert reality_load.call_count == 3
    for call in reality_load.call_args_list:
        assert call.args == ("acme",)
        assert call.kwargs == {"programs_root": programs_root, "archive_root": archive_root}


def test_milestone_config_failure_preserves_attention_with_selected_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reality_load = Mock(side_effect=ConfigError("invalid synthetic milestones"))
    monkeypatch.setattr(triage.ProgramReality, "load", reality_load)
    assert triage._build_milestone_triage_lines(
        program_id="fixture", programs_root=tmp_path / "programs", archive_root=tmp_path / "archive",
        items=(), dependencies=(), as_of=datetime(2026, 5, 18, tzinfo=timezone.utc),
    ) == ((), ("Milestones skipped: invalid synthetic milestones",))
    reality_load.assert_called_once_with(
        "fixture", programs_root=tmp_path / "programs", archive_root=tmp_path / "archive",
    )


def test_preflight_does_not_swallow_unexpected_validator_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(triage, "validate_report_workspace", Mock(side_effect=RuntimeError("unexpected failure")))
    with pytest.raises(RuntimeError, match="unexpected failure"):
        triage._preflight_triage_workspace()
