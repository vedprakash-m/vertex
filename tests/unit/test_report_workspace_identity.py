from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import dotenv
from typer import Typer
from typer.testing import CliRunner

from src.commands import propose, report, report_history
from src.core import workspace_paths
from src.core.exceptions import ConfigError, QueryError, StateError
from src.core.models import ArchiveEntry, ArchiveIndex
from src.core.pipeline import StageContext
from src.core.stages import resolution_stage


def _files(root: Path) -> dict[Path, bytes | None]:
    return {path.relative_to(root): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def _isolate_resolution(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    dependencies = {
        "load_bundle": Mock(return_value=SimpleNamespace(config=SimpleNamespace(edition=SimpleNamespace(type="detailed")))),
        "find_edition_yaml": Mock(return_value=None),
        "read_archive_index": Mock(return_value=ArchiveIndex(edition="weekly", issues=())),
        "load_trusted_baseline_issue": Mock(return_value=None),
        "_resolve_previous_snapshot": Mock(return_value=(None, None)),
        "delete_narratives_dir": Mock(),
        "delete_seedable_overrides": Mock(),
        "seed_overrides_from_prior": Mock(),
        "ProgramFactStore": Mock(),
    }
    for name, dependency in dependencies.items():
        monkeypatch.setattr(resolution_stage, name, dependency)
    return dependencies


@pytest.mark.parametrize("relative", ["programs_root", "reports_root", "archive_root"])
def test_report_roots_reject_relative_paths_without_creating_storage(tmp_path: Path, relative: str) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    roots[relative] = Path(relative)
    before = _files(tmp_path)
    with pytest.raises(ConfigError, match="absolute"):
        workspace_paths.validate_report_workspace(**roots)
    assert _files(tmp_path) == before


@pytest.mark.parametrize("conflicting", ["reports_root", "archive_root"])
def test_report_roots_reject_independent_workspace_mappings(tmp_path: Path, conflicting: str) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    roots[conflicting] = tmp_path / "other workspace" / conflicting
    before = _files(tmp_path)
    with pytest.raises(ConfigError, match="Conflicting report workspace roots"):
        workspace_paths.validate_report_workspace(**roots)
    assert _files(tmp_path) == before


def test_report_roots_accept_coherent_nonexistent_workspace_without_initializing_it(tmp_path: Path) -> None:
    workspace = tmp_path / "not-created"
    workspace_paths.validate_report_workspace(
        programs_root=workspace / "programs", reports_root=workspace / "reports", archive_root=workspace / "archive",
    )
    assert not workspace.exists()


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("conflicting", ["reports", "archive"])
def test_resolution_rejects_conflicts_before_any_store_or_reseed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: bool, conflicting: str,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.setattr(resolution_stage, "PROGRAMS_ROOT", root, raising=False)
    dependencies = _isolate_resolution(monkeypatch)
    dependencies["load_bundle"].side_effect = AssertionError("Conflicting context must fail before configuration")
    ctx = StageContext(
        edition_name="weekly", issue_number=7, dry_run=True, reseed=True,
        programs_root=root if selected else None,
        reports_root=tmp_path / ("other" if conflicting == "reports" else "") / "reports",
        archive_root=tmp_path / ("other" if conflicting == "archive" else "") / "archive",
    )
    before = _files(tmp_path)

    with pytest.raises(ConfigError, match="Conflicting report workspace roots"):
        resolution_stage.ResolutionStage().execute(ctx)

    for dependency in dependencies.values():
        dependency.assert_not_called()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("selected", [False, True])
def test_coherent_resolution_preserves_selected_or_shared_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: bool,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.setattr(resolution_stage, "PROGRAMS_ROOT", root, raising=False)
    dependencies = _isolate_resolution(monkeypatch)
    ctx = StageContext(
        edition_name="weekly", issue_number=7,
        programs_root=root if selected else None,
        reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
    )
    before = _files(tmp_path)

    resolved = resolution_stage.ResolutionStage().execute(ctx)

    assert resolved.programs_root is root
    assert resolved.reports_root == ctx.reports_root
    assert resolved.archive_root == ctx.archive_root
    assert resolved.bundle is dependencies["load_bundle"].return_value
    assert resolved.resolved_issue_number == 7
    assert ctx.bundle is None
    assert ctx.programs_root is (root if selected else None)
    dependencies["load_bundle"].assert_called_once_with("weekly", reports_root=ctx.reports_root, programs_root=root)
    assert dependencies["load_trusted_baseline_issue"].call_args.kwargs["programs_root"] is root
    assert dependencies["_resolve_previous_snapshot"].call_args.kwargs["programs_root"] is root
    assert _files(tmp_path) == before


@pytest.mark.parametrize("baseline", ["empty", "trusted", "archive"])
def test_coherent_resolution_keeps_issue_baseline_and_reseed_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: str,
) -> None:
    root = tmp_path / "programs"
    dependencies = _isolate_resolution(monkeypatch)
    timestamp = datetime(2026, 10, 24, tzinfo=timezone.utc)
    issues = () if baseline == "empty" else (ArchiveEntry(issue_number=3, generated_at=timestamp),)
    dependencies["read_archive_index"].return_value = ArchiveIndex(edition="weekly", issues=issues)
    dependencies["load_trusted_baseline_issue"].return_value = 3 if baseline == "trusted" else None
    dependencies["find_edition_yaml"].return_value = root / "alpha" / "editions" / "weekly.yaml"
    typed_edition = Mock()
    typed_edition.return_value.program.id = "alpha"
    monkeypatch.setattr(resolution_stage, "resolve_edition", typed_edition)
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    ctx = StageContext(
        edition_name="weekly", programs_root=root, reports_root=tmp_path / "reports",
        archive_root=tmp_path / "archive", dry_run=True, reseed=True, started_at=timestamp,
        as_of=timestamp if baseline == "trusted" else None,
        data_as_of=timestamp if baseline == "archive" else None,
    )
    before = _files(tmp_path)
    stage = resolution_stage.ResolutionStage()

    resolved = stage.execute(ctx)

    assert stage.name() == "resolution"
    assert resolved.programs_root is root
    assert resolved.data_as_of == timestamp
    assert resolved.resolved_issue_number == (1 if baseline == "empty" else 4)
    assert resolved.trusted_baseline_issue_number == (None if baseline == "empty" else 3)
    assert resolved.resolved_v2 is typed_edition.return_value
    typed_edition.assert_called_once_with("weekly", programs_root=root)
    dependencies["ProgramFactStore"].assert_called_once_with("alpha", db_root=tmp_path)
    fact_store = dependencies["ProgramFactStore"].return_value
    fact_store.pin_snapshot.assert_called_once_with(
        metadata={"edition_name": "weekly", "issue_number": resolved.resolved_issue_number},
        created_at=timestamp,
    )
    fact_store.load_pinned_snapshot.assert_called_once_with(fact_store.pin_snapshot.return_value.snapshot_id)
    assert resolved.fact_snapshot_pin is fact_store.pin_snapshot.return_value
    assert resolved.fact_snapshot is fact_store.load_pinned_snapshot.return_value
    for name in ("delete_narratives_dir", "delete_seedable_overrides"):
        dependencies[name].assert_called_once_with(
            "weekly", resolved.resolved_issue_number, reports_root=ctx.reports_root,
        )
    if baseline == "empty":
        dependencies["seed_overrides_from_prior"].assert_not_called()
        assert resolved.overrides_seeding is None
    else:
        dependencies["seed_overrides_from_prior"].assert_called_once_with(
            "weekly", target_issue_number=4, source_issue_number=3,
            reports_root=ctx.reports_root, archive_root=ctx.archive_root,
        )
        assert resolved.overrides_seeding is dependencies["seed_overrides_from_prior"].return_value
    assert _files(tmp_path) == before


@pytest.mark.parametrize("generate", [report.generate_report_draft, propose.generate_section_revision_proposals])
def test_actual_report_and_proposal_entry_points_cannot_drop_selected_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generate: Callable[..., object],
) -> None:
    dependencies = _isolate_resolution(monkeypatch)
    dependencies["load_bundle"].side_effect = AssertionError("No configuration load from an incoherent request")
    before = _files(tmp_path)

    with pytest.raises(ConfigError, match="Conflicting report workspace roots"):
        generate(
            edition_name="weekly", programs_root=tmp_path / "selected" / "programs",
            reports_root=tmp_path / "other" / "reports", archive_root=tmp_path / "other" / "archive",
            dry_run=True, offline=True,
        )

    for dependency in dependencies.values():
        dependency.assert_not_called()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("conflicting", ["reports", "archive"])
@pytest.mark.parametrize("stdout", [False, True])
def test_report_cli_blocks_conflicts_before_configuration_enrichment_or_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflicting: str, stdout: bool,
) -> None:
    monkeypatch.setattr(report, "PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(report, "REPORTS_ROOT", tmp_path / ("other" if conflicting == "reports" else "") / "reports")
    monkeypatch.setattr(report, "ARCHIVE_ROOT", tmp_path / ("other" if conflicting == "archive" else "") / "archive")
    dependencies = [
        Mock(side_effect=AssertionError("Preflight must precede configuration")),
        Mock(side_effect=AssertionError("Preflight must precede enrichment")),
        Mock(side_effect=AssertionError("Preflight must precede report generation")),
    ]
    for name, dependency in zip(
        ["load_bundle_with_mode", "_maybe_auto_run_workiq_enrich", "generate_report_draft"], dependencies, strict=True,
    ):
        monkeypatch.setattr(report, name, dependency)
    app = Typer()
    app.command()(report.report_command)
    before = _files(tmp_path)

    arguments = ["--edition", "weekly", "--dry-run", "--offline"]
    if stdout:
        arguments.append("--stdout")
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "Conflicting report workspace roots" in result.stderr
    assert not result.stdout
    for dependency in dependencies:
        dependency.assert_not_called()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("generate", [False, True])
def test_coherent_report_cli_passes_selected_root_to_configuration_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generate: bool,
) -> None:
    root = tmp_path / "programs"
    reports = tmp_path / "reports"
    monkeypatch.setattr(report, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(report, "REPORTS_ROOT", reports)
    monkeypatch.setattr(report, "ARCHIVE_ROOT", tmp_path / "archive")
    loader = Mock(return_value=SimpleNamespace(
        bundle=SimpleNamespace(config=SimpleNamespace(edition=SimpleNamespace(type="detailed"))),
    ))
    monkeypatch.setattr(report, "load_bundle_with_mode", loader)
    monkeypatch.setattr(report, "archive_integrity_waived", lambda: True)
    enrichment = Mock()
    generator = Mock(side_effect=QueryError("Synthetic stop before report effects"))
    monkeypatch.setattr(report, "_maybe_auto_run_workiq_enrich", enrichment)
    monkeypatch.setattr(report, "generate_report_draft", generator)
    app = Typer()
    app.command()(report.report_command)

    result = CliRunner().invoke(app, [
        "--edition", "weekly", "--issue", "80" if generate else "1", "--dry-run", "--offline",
    ])

    assert result.exit_code == (2 if generate else 0), result.output + repr(result.exception)
    if generate:
        assert "Synthetic stop before report effects" in result.output
        generator.assert_called_once()
        assert generator.call_args.kwargs["programs_root"] is root
        enrichment.assert_called_once()
    else:
        assert report._HISTORICAL_ISSUE_GUARD_MESSAGE in result.stdout
        generator.assert_not_called()
        enrichment.assert_not_called()
    loader.assert_called_once_with("weekly", reports_root=reports, programs_root=root)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("command", ["report", "draft"])
@pytest.mark.parametrize("conflicting", ["reports", "archive"])
def test_installed_cli_blocks_conflicts_before_any_startup_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, conflicting: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    monkeypatch.setattr(report, "PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(report, "REPORTS_ROOT", tmp_path / ("other" if conflicting == "reports" else "") / "reports")
    monkeypatch.setattr(report, "ARCHIVE_ROOT", tmp_path / ("other" if conflicting == "archive" else "") / "archive")
    hooks = [
        Mock(side_effect=AssertionError("Workspace conflict must precede startup hooks"))
        for _ in range(3)
    ]
    for name, hook in zip(
        ["_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"],
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    loader = Mock(side_effect=AssertionError("Workspace conflict must precede configuration"))
    monkeypatch.setattr(report, "load_bundle_with_mode", loader)
    before = _files(tmp_path)

    result = CliRunner().invoke(cli.app, [command, "--edition", "weekly", "--dry-run", "--offline", "--stdout"])

    assert result.exit_code == 2, result.output + repr(result.exception)
    assert "Conflicting report workspace roots" in result.stderr
    assert not result.stdout
    for hook in hooks:
        hook.assert_not_called()
    loader.assert_not_called()
    assert _files(tmp_path) == before


@pytest.mark.parametrize("command", ["report", "draft", "config"])
def test_installed_cli_preserves_coherent_reports_and_unrelated_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    cli = importlib.import_module("cli")
    root = tmp_path / "programs"
    monkeypatch.setattr(report, "PROGRAMS_ROOT", root)
    monkeypatch.setattr(report, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(report, "ARCHIVE_ROOT", tmp_path / "archive")
    if command == "config":
        monkeypatch.setattr(report, "REPORTS_ROOT", tmp_path / "other" / "reports")
    hooks = [Mock() for _ in range(3)]
    for name, hook in zip(
        ["_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance", "_maybe_run_session_catchup"],
        hooks, strict=True,
    ):
        monkeypatch.setattr(cli, name, hook)
    loader = Mock(return_value=SimpleNamespace(
        bundle=SimpleNamespace(config=SimpleNamespace(edition=SimpleNamespace(type="detailed"))),
    ))
    monkeypatch.setattr(report, "load_bundle_with_mode", loader)

    arguments = [command, "--help"] if command == "config" else [
        command, "--edition", "weekly", "--issue", "1", "--dry-run", "--offline",
    ]
    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 0, result.output + repr(result.exception)
    if command == "config":
        assert "Usage" in result.stdout
        loader.assert_not_called()
    else:
        assert report._HISTORICAL_ISSUE_GUARD_MESSAGE in result.stdout
        loader.assert_called_once_with("weekly", reports_root=tmp_path / "reports", programs_root=root)
    for hook in hooks:
        if command == "config":
            hook.assert_not_called()
        else:
            hook.assert_called_once()
    assert not list(tmp_path.iterdir())


def _draft(root: Path, marker: str) -> dict[str, str]:
    edition = root / "alpha" / "editions" / "weekly.yaml"
    edition.parent.mkdir(parents=True)
    edition.write_text("id: weekly\nprogram_id: alpha\n", encoding="utf-8")
    path = root / "alpha" / "publications" / "weekly" / "issue_007" / "issue_007.draft.json"
    path.parent.mkdir(parents=True)
    payload = {"marker": marker}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


@pytest.mark.parametrize("reader", [resolution_stage, report_history])
@pytest.mark.parametrize("selection", ["omitted", "none", "explicit"])
def test_draft_history_uses_shared_default_or_explicit_workspace_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: ModuleType, selection: str,
) -> None:
    default = tmp_path / "default" / "programs"
    explicit = tmp_path / "explicit" / "programs"
    default_payload = _draft(default, "default")
    explicit_payload = _draft(explicit, "explicit")
    monkeypatch.setattr(reader, "PROGRAMS_ROOT", default, raising=False)
    before = _files(tmp_path)
    arguments = {} if selection == "omitted" else {"programs_root": explicit if selection == "explicit" else None}

    payload = reader._load_previous_dry_run_state(edition_name="weekly", issue_number=7, **arguments)
    missing = reader._load_previous_dry_run_state(edition_name="weekly", issue_number=8, **arguments)

    assert payload == (explicit_payload if selection == "explicit" else default_payload)
    assert missing is None
    assert _files(tmp_path) == before


@pytest.mark.parametrize("reader", [resolution_stage, report_history])
def test_draft_history_rejects_non_mapping_payload(tmp_path: Path, reader: ModuleType) -> None:
    root = tmp_path / "programs"
    _draft(root, "fixture")
    path = root / "alpha" / "publications" / "weekly" / "issue_007" / "issue_007.draft.json"
    path.write_text("[]", encoding="utf-8")
    before = _files(tmp_path)

    with pytest.raises(StateError):
        reader._load_previous_dry_run_state(edition_name="weekly", issue_number=7, programs_root=root)

    assert _files(tmp_path) == before


@pytest.mark.parametrize("reader", [resolution_stage, report_history])
def test_draft_history_parse_errors_are_explicit(tmp_path: Path, reader: ModuleType) -> None:
    root = tmp_path / "programs"
    _draft(root, "fixture")
    path = root / "alpha" / "publications" / "weekly" / "issue_007" / "issue_007.draft.json"
    path.write_text("{", encoding="utf-8")
    before = _files(tmp_path)

    with pytest.raises(StateError):
        reader._load_previous_dry_run_state(edition_name="weekly", issue_number=7, programs_root=root)

    assert _files(tmp_path) == before
