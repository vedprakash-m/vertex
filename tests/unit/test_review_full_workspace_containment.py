from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from src.commands import review_full
from src.core.exceptions import ConfigError


def _forbidden(*args, **kwargs):
    raise AssertionError("Invalid workspace reached a reader, provider, or writer.")


@pytest.mark.parametrize("entrypoint", ("generate_review_full", "prepare_review_full_context"))
@pytest.mark.parametrize(
    "selection",
    (
        "default-program", "default-report", "default-archive",
        "explicit-program", "explicit-report", "explicit-archive",
        "relative-program", "relative-report", "relative-archive", "legacy-relative-report",
    ),
)
def test_invalid_review_full_workspace_fails_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, selection: str,
) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    for name, root in roots.items():
        monkeypatch.setattr(review_full, name.upper(), root)
    mode, name = selection.split("-", 1)
    key = {"program": "programs_root", "report": "reports_root", "archive": "archive_root"}.get(name)
    kwargs = {}
    if mode == "default":
        monkeypatch.setattr(review_full, key.upper(), tmp_path / "other" / name)
    elif mode == "legacy":
        kwargs = {"reports_root": Path("reports"), "archive_root": roots["archive_root"]}
    else:
        kwargs.update(roots)
        kwargs[key] = Path(name) if mode == "relative" else tmp_path / "other" / name
    with monkeypatch.context() as guard:
        for target in (
            "load_bundle", "_load_review_full_context", "_load_draft_state",
            "load_program_facts", "_build_default_anticipation_client", "_write_output_text",
            "ReviewerRenderer",
        ):
            guard.setattr(review_full, target, _forbidden)
        if entrypoint == "generate_review_full":
            guard.setattr(review_full, "prepare_review_full_context", _forbidden)
        for target in ("stat", "exists", "is_file", "is_dir", "open", "mkdir"):
            guard.setattr(Path, target, _forbidden)
        with pytest.raises(ConfigError, match="workspace roots"):
            getattr(review_full, entrypoint)(edition_name="demo_weekly", **kwargs)


@pytest.mark.parametrize("entrypoint", ("preflight", "command"))
@pytest.mark.parametrize("invalid", (False, True))
def test_review_full_preflight_guards_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entrypoint: str, invalid: bool,
) -> None:
    monkeypatch.setattr(review_full, "PROGRAMS_ROOT", tmp_path / ("other" if invalid else "programs"))
    monkeypatch.setattr(review_full, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(review_full, "ARCHIVE_ROOT", tmp_path / "archive")
    generator = Mock(side_effect=RuntimeError("generation reached"))
    monkeypatch.setattr(review_full, "generate_review_full", generator)

    def invoke() -> None:
        if entrypoint == "preflight":
            review_full._preflight_review_full_workspace()
        else:
            review_full.review_full_command(edition="demo_weekly", format="human")

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


def test_review_full_preflight_preserves_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_full, "validate_report_workspace", Mock(side_effect=RuntimeError("unexpected")), raising=False)
    with pytest.raises(RuntimeError, match="unexpected"):
        review_full._preflight_review_full_workspace()


@pytest.mark.parametrize("selection", ("defaults", "none", "explicit", "legacy-reports"))
def test_prepare_review_full_preserves_root_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    for name, root in roots.items():
        monkeypatch.setattr(review_full, name.upper(), root)
    kwargs = {}
    if selection == "none":
        kwargs = dict.fromkeys(roots)
    elif selection == "explicit":
        kwargs = roots
    elif selection == "legacy-reports":
        monkeypatch.setattr(review_full, "PROGRAMS_ROOT", tmp_path / "unselected")
        kwargs = {"reports_root": roots["reports_root"], "archive_root": roots["archive_root"]}
    loader = Mock(side_effect=RuntimeError("selected workspace"))
    monkeypatch.setattr(review_full, "load_bundle", loader)
    with pytest.raises(RuntimeError, match="selected workspace"):
        review_full.prepare_review_full_context(edition_name="demo_weekly", **kwargs)
    loader.assert_called_once_with(
        "demo_weekly", programs_root=roots["programs_root"], reports_root=roots["reports_root"],
    )


@pytest.mark.parametrize("selection", ("defaults", "none", "explicit", "legacy-reports"))
def test_generate_review_full_passes_selected_roots_to_context_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    for name, root in roots.items():
        monkeypatch.setattr(review_full, name.upper(), root)
    kwargs = {}
    if selection == "none":
        kwargs = dict.fromkeys(roots)
    elif selection == "explicit":
        kwargs = roots
    elif selection == "legacy-reports":
        monkeypatch.setattr(review_full, "PROGRAMS_ROOT", tmp_path / "unselected")
        kwargs = {"reports_root": roots["reports_root"], "archive_root": roots["archive_root"]}
    context = Mock(return_value=SimpleNamespace(issue_number=1, bundle=object(), reviewer_context=SimpleNamespace(sections=())))
    monkeypatch.setattr(review_full, "prepare_review_full_context", context)
    monkeypatch.setattr(review_full, "ReviewerRenderer", Mock())
    output_dir = Mock(return_value=roots["programs_root"] / "demo" / "publications")
    monkeypatch.setattr(review_full, "get_program_output_dir", output_dir)
    monkeypatch.setattr(review_full, "_write_output_text", lambda path, text: path)
    cards = Mock(return_value=())
    monkeypatch.setattr(review_full, "_write_section_review_adaptive_cards", cards)
    review_full.generate_review_full(edition_name="demo_weekly", **kwargs)
    for name, root in roots.items():
        assert context.call_args.kwargs[name] == root
    output_dir.assert_called_once_with("demo_weekly", programs_root=roots["programs_root"])
    assert cards.call_args.kwargs["programs_root"] == roots["programs_root"]


@pytest.mark.parametrize("consumer", ("milestones", "assumptions"))
def test_nested_review_full_fact_readers_keep_selected_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: str,
) -> None:
    facts = Mock(return_value=())
    monkeypatch.setattr(review_full, "load_program_facts", facts)
    monkeypatch.setattr(review_full, "project_milestones", lambda snapshot: ())
    monkeypatch.setattr(review_full, "project_assumptions", lambda snapshot: ())
    kwargs = {
        "programs_root": tmp_path / "programs", "archive_root": tmp_path / "history",
        "as_of": datetime(2026, 5, 5, tzinfo=timezone.utc),
    }
    if consumer == "milestones":
        review_full._build_milestone_assessments("demo", dependencies=(), items=(), **kwargs)
    else:
        review_full._build_assumption_rows("demo", **kwargs)
    facts.assert_called_once()
    assert facts.call_args.kwargs["archive_root"] == tmp_path / "history"
    assert facts.call_args.kwargs["programs_root"] == tmp_path / "programs"
    assert facts.call_args.kwargs["db_root"] == tmp_path


@pytest.mark.parametrize("consumer", ("milestones", "assumptions"))
def test_review_full_without_program_does_not_read_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: str,
) -> None:
    monkeypatch.setattr(review_full, "load_program_facts", _forbidden)
    kwargs = {
        "programs_root": tmp_path / "programs", "archive_root": tmp_path / "archive",
        "as_of": datetime(2026, 5, 5, tzinfo=timezone.utc),
    }
    if consumer == "milestones":
        assert review_full._build_milestone_assessments(None, dependencies=(), items=(), **kwargs) == ()
    else:
        assert review_full._build_assumption_rows(None, **kwargs) == ()


def test_review_full_direct_command_preserves_generation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("programs", "reports", "archive"):
        monkeypatch.setattr(review_full, f"{name.upper()}_ROOT", tmp_path / name)
    monkeypatch.setattr(review_full, "generate_review_full", Mock(side_effect=typer.BadParameter("missing draft")))
    with pytest.raises(typer.Exit) as error:
        review_full.review_full_command(edition="demo_weekly", format="human")
    assert error.value.exit_code == 2
    assert "missing draft" in capsys.readouterr().out
