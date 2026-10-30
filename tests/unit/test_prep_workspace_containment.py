from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from src.commands import prep
from src.core.exceptions import ConfigError


@pytest.mark.parametrize("selection", ("external", "legacy", "checkout", "installation"))
def test_prep_preflight_admits_only_safe_external_or_unset_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    from src.core.workspace_paths import REPO_ROOT

    root = tmp_path / "programs"
    if selection == "checkout":
        (tmp_path / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    elif selection in ("legacy", "installation"):
        root = REPO_ROOT / "programs"
    for name in ("reports", "archive"):
        monkeypatch.setattr(prep, name.upper() + "_ROOT", REPO_ROOT / name)
    monkeypatch.setattr(prep, "PROGRAMS_ROOT", root)
    monkeypatch.setattr("src.core.workspace_paths._PROGRAMS_ROOT_CONFIGURED", selection == "installation")
    if selection in ("checkout", "installation"):
        with pytest.raises(typer.Exit) as error:
            prep._preflight_prep_workspace()
        assert error.value.exit_code == 2
    else:
        prep._preflight_prep_workspace()


def _forbidden(*args, **kwargs):
    raise AssertionError("Invalid workspace reached metadata, a reader, provider, or writer.")


@pytest.mark.parametrize(
    "selection",
    (
        "default-program", "default-report", "default-archive",
        "explicit-program", "explicit-report", "explicit-archive",
        "relative-program", "relative-report", "relative-archive", "legacy-relative-report",
    ),
)
def test_invalid_prep_workspace_fails_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    for name, root in roots.items():
        monkeypatch.setattr(prep, name.upper(), root, raising=False)
    mode, name = selection.split("-", 1)
    key = {"program": "programs_root", "report": "reports_root", "archive": "archive_root"}.get(name)
    kwargs = {}
    if mode == "default":
        monkeypatch.setattr(prep, key.upper(), tmp_path / "other" / name)
    elif mode == "legacy":
        kwargs = {"reports_root": Path("reports"), "archive_root": roots["archive_root"]}
    else:
        kwargs.update(roots)
        kwargs[key] = Path(name) if mode == "relative" else tmp_path / "other" / name

    with monkeypatch.context() as guard:
        for target in (
            "resolve_edition", "load_bundle", "_load_review_full_context", "_load_draft_state",
            "load_program_facts", "_build_anticipation_client", "_write_output_text",
        ):
            guard.setattr(prep, target, _forbidden)
        for target in ("open", "mkdir"):
            guard.setattr(Path, target, _forbidden)
        with pytest.raises(ConfigError, match="workspace roots"):
            prep.generate_prep_brief(edition_name="demo_lt", **kwargs)


@pytest.mark.parametrize("selection", ("defaults", "none", "explicit", "legacy-reports"))
def test_coherent_prep_workspace_preserves_root_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    for name, root in roots.items():
        monkeypatch.setattr(prep, name.upper(), root, raising=False)
    kwargs = {}
    if selection == "none":
        kwargs = dict.fromkeys(roots)
    elif selection == "explicit":
        kwargs = roots
    elif selection == "legacy-reports":
        monkeypatch.setattr(prep, "PROGRAMS_ROOT", tmp_path / "unselected")
        kwargs = {"reports_root": roots["reports_root"], "archive_root": roots["archive_root"]}
    resolver = Mock(side_effect=RuntimeError("selected workspace"))
    monkeypatch.setattr(prep, "resolve_edition", resolver)
    with pytest.raises(RuntimeError, match="selected workspace"):
        prep.generate_prep_brief(edition_name="demo_lt", **kwargs)
    resolver.assert_called_once_with("demo_lt", programs_root=roots["programs_root"])


@pytest.mark.parametrize("entrypoint", ("preflight", "command"))
@pytest.mark.parametrize("invalid", (False, True))
def test_prep_preflight_guards_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entrypoint: str, invalid: bool,
) -> None:
    monkeypatch.setattr(prep, "PROGRAMS_ROOT", tmp_path / ("other" if invalid else "programs"), raising=False)
    monkeypatch.setattr(prep, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(prep, "ARCHIVE_ROOT", tmp_path / "archive")
    generator = Mock(side_effect=RuntimeError("generation reached"))
    monkeypatch.setattr(prep, "generate_prep_brief", generator)

    def invoke() -> None:
        if entrypoint == "preflight":
            prep._preflight_prep_workspace()
        else:
            prep.prep_command(edition="demo_lt", format="human")

    if invalid:
        with pytest.raises(typer.Exit) as error:
            invoke()
        assert error.value.exit_code == 2
        assert "Conflicting report workspace roots" in capsys.readouterr().err
        generator.assert_not_called()
    elif entrypoint == "command":
        with pytest.raises(RuntimeError, match="generation reached"):
            invoke()
        generator.assert_called_once()
    else:
        invoke()
        generator.assert_not_called()
        assert capsys.readouterr().err == ""


def test_prep_preflight_preserves_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prep, "resolve_report_workspace_roots", Mock(side_effect=RuntimeError("unexpected")))
    with pytest.raises(RuntimeError, match="unexpected"):
        prep._preflight_prep_workspace()


@pytest.mark.parametrize("consumer", ("diagram", "reachable", "all", "assumptions"))
def test_nested_prep_fact_readers_keep_selected_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: str,
) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("id: demo\n", encoding="utf-8")
    archive_root = tmp_path / "history"
    facts = Mock(return_value=())
    monkeypatch.setattr(prep, "load_program_facts", facts)
    monkeypatch.setattr(prep, "project_dependencies", lambda snapshot: ())
    monkeypatch.setattr(prep, "project_assumptions", lambda snapshot: ())
    kwargs = {"programs_root": programs_root, "archive_root": archive_root}
    if consumer == "diagram":
        prep._build_prep_dependency_diagram_lines(program_id="demo", **kwargs)
    elif consumer == "reachable":
        prep._load_reachable_cross_program_dependencies(program_id="demo", max_hops=3, **kwargs)
    elif consumer == "all":
        prep._load_all_cross_program_dependencies(**kwargs)
    else:
        prep._build_prep_assumption_lines(
            program_id="demo", as_of=datetime(2026, 5, 7, tzinfo=timezone.utc), **kwargs,
        )
    facts.assert_called_once()
    assert facts.call_args.kwargs["archive_root"] == archive_root
    assert facts.call_args.kwargs["programs_root"] == programs_root
    assert facts.call_args.kwargs["db_root"] == tmp_path


def test_direct_prep_command_preserves_human_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("programs", "reports", "archive"):
        monkeypatch.setattr(prep, f"{name.upper()}_ROOT", tmp_path / name)
    artifacts = prep.PrepBriefArtifacts(issue_number=7, markdown_path=tmp_path / "prep_brief.md")
    monkeypatch.setattr(prep, "generate_prep_brief", Mock(return_value=artifacts))
    with pytest.raises(typer.Exit) as error:
        prep.prep_command(edition="demo_lt", format="human")
    assert error.value.exit_code == 0
    output = capsys.readouterr()
    assert "Prep brief generated for Issue 007." in output.out
    assert str(artifacts.markdown_path) in output.out
    assert output.err == ""


def _dependency(identifier: str, source: str, target: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier, from_program_id=source, to_program_id=target,
        from_workstream_id="source", to_workstream_id="target",
        from_item_id=None, to_item_id=None, from_milestone_id=None, to_milestone_id=None,
        dependency_type=SimpleNamespace(value="blocks"), status=SimpleNamespace(value="active"),
    )


@pytest.mark.parametrize("max_hops", (0, 1, 2, 3))
def test_reachable_dependencies_keep_hop_and_cycle_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_hops: int,
) -> None:
    first = _dependency("first", "demo", "other")
    second = _dependency("second", "other", "demo")
    loader = Mock(return_value=(first, first, second))
    monkeypatch.setattr(prep, "_load_all_cross_program_dependencies", loader)
    dependencies = prep._load_reachable_cross_program_dependencies(
        program_id="demo", programs_root=tmp_path / "programs", max_hops=max_hops,
    )
    assert dependencies == (() if max_hops == 0 else (first,) if max_hops == 1 else (first, second))
    if max_hops == 0:
        loader.assert_not_called()
    else:
        loader.assert_called_once_with(programs_root=tmp_path / "programs", archive_root=None)


def test_dependency_diagram_preserves_node_and_edge_deduplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _dependency("first", "demo", "other")
    second = _dependency("second", "demo", "other")
    loader = Mock(return_value=(first, first, second))
    monkeypatch.setattr(prep, "_load_reachable_cross_program_dependencies", loader)
    lines = prep._build_prep_dependency_diagram_lines(
        program_id="demo", programs_root=tmp_path / "programs",
    )
    assert lines.count('  source["source"]') == 1
    assert lines.count('  other_target["other:target"]') == 1
    assert lines.count("  source -->|ACTIVE blocks| other_target") == 2
    loader.assert_called_once_with(
        program_id="demo", programs_root=tmp_path / "programs", max_hops=3, archive_root=None,
    )


@pytest.mark.parametrize("unexpected", (False, True))
def test_all_dependency_reader_preserves_invalid_program_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unexpected: bool,
) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("id: demo\n", encoding="utf-8")
    (programs_root / "stray").write_text("not a program", encoding="utf-8")
    (programs_root / "empty").mkdir()
    error = RuntimeError("unexpected") if unexpected else ConfigError("invalid authored dependencies")
    facts = Mock(side_effect=error)
    monkeypatch.setattr(prep, "load_program_facts", facts)
    if unexpected:
        with pytest.raises(RuntimeError, match="unexpected"):
            prep._load_all_cross_program_dependencies(programs_root=programs_root)
    else:
        assert prep._load_all_cross_program_dependencies(programs_root=programs_root) == ()
    facts.assert_called_once_with(
        "demo", db_root=tmp_path, programs_root=programs_root, archive_root=None,
    )
