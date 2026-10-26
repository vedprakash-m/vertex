from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
import typer

from src.commands import freshness
from src.core import program_fact_store
from src.core.exceptions import ConfigError, StateError
from tests.unit.test_commands_freshness import (
    AS_OF,
    EDITION_NAME,
    _sample_items,
    _seed_confirmed_snapshot,
    _stage_freshness_workspace,
)


def _forbidden(*args, **kwargs):
    raise AssertionError("Invalid workspace reached metadata, a reader, provider, or writer.")


@pytest.mark.parametrize(
    "selection",
    (
        "default-program-conflict",
        "default-report-conflict",
        "default-archive-conflict",
        "explicit-program-conflict",
        "explicit-report-conflict",
        "explicit-archive-conflict",
        "relative-program",
        "relative-report",
        "relative-archive",
        "legacy-relative-report",
    ),
)
def test_invalid_api_workspace_fails_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    roots = {
        "programs_root": tmp_path / "programs",
        "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    for name, root in roots.items():
        monkeypatch.setattr(freshness, name.upper(), root)
    kwargs = {}
    if selection.startswith("default-"):
        name = selection.removeprefix("default-").removesuffix("-conflict")
        monkeypatch.setattr(freshness, f"{name.upper()}S_ROOT" if name != "archive" else "ARCHIVE_ROOT", tmp_path / "other" / name)
    else:
        kwargs.update(roots)
        if selection.startswith("explicit-"):
            name = selection.removeprefix("explicit-").removesuffix("-conflict")
            key = {"program": "programs_root", "report": "reports_root", "archive": "archive_root"}[name]
            kwargs[key] = tmp_path / "other" / name
        elif selection == "legacy-relative-report":
            kwargs = {"reports_root": Path("reports"), "archive_root": roots["archive_root"]}
        else:
            name = selection.removeprefix("relative-")
            key = {"program": "programs_root", "report": "reports_root", "archive": "archive_root"}[name]
            kwargs[key] = Path(name)

    with monkeypatch.context() as guard:
        for name in (
            "load_bundle", "read_archive_index", "load_trusted_baseline_issue",
            "load_narratives", "load_program_facts", "_load_live_freshness_items",
            "CircuitBreaker", "get_program_output_dir", "_write_output_text",
            "load_latest_notification_state", "build_workstream_narrative_history",
        ):
            guard.setattr(freshness, name, _forbidden)
        for name in ("stat", "exists", "is_file", "is_dir", "open", "mkdir"):
            guard.setattr(Path, name, _forbidden)
        with pytest.raises(ConfigError, match="workspace roots"):
            freshness.generate_freshness_report(EDITION_NAME, notify=True, **kwargs)


@pytest.mark.parametrize("entrypoint", ("preflight", "command"))
@pytest.mark.parametrize("invalid", (False, True))
def test_direct_preflight_guards_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    entrypoint: str, invalid: bool,
) -> None:
    monkeypatch.setattr(freshness, "PROGRAMS_ROOT", tmp_path / ("other" if invalid else "programs"))
    monkeypatch.setattr(freshness, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(freshness, "ARCHIVE_ROOT", tmp_path / "archive")
    generated = Mock(side_effect=RuntimeError("generation reached"))
    monkeypatch.setattr(freshness, "generate_freshness_report", generated)

    def invoke() -> None:
        if entrypoint == "preflight":
            freshness._preflight_freshness_workspace()
        else:
            freshness.freshness_command(
                edition=EDITION_NAME, since=None, by="dri", teams_format=False,
                notify=False, allow_stale=False, dry_run=False, format="human",
            )

    if invalid:
        with pytest.raises(typer.Exit) as error:
            invoke()
        assert error.value.exit_code == 2
        assert "Conflicting report workspace roots" in capsys.readouterr().err
        generated.assert_not_called()
    elif entrypoint == "command":
        with pytest.raises(RuntimeError, match="generation reached"):
            invoke()
        generated.assert_called_once()
    else:
        invoke()
        generated.assert_not_called()
        assert capsys.readouterr().err == ""


def test_preflight_does_not_swallow_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(freshness, "validate_report_workspace", Mock(side_effect=RuntimeError("unexpected")))
    with pytest.raises(RuntimeError, match="unexpected"):
        freshness._preflight_freshness_workspace()


@pytest.mark.parametrize("failure", ("grouping", "issue-number", "unexpected"))
def test_coherent_api_preserves_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    reports_root, archive_root, _ = _stage_freshness_workspace(tmp_path, tmp_path)
    monkeypatch.setattr(freshness, "_load_live_freshness_items", _forbidden)
    kwargs = {}
    if failure == "grouping":
        kwargs["by"] = "other"
        expected_error = typer.BadParameter
    elif failure == "issue-number":
        kwargs["expected_issue_number"] = 9
        expected_error = StateError
    else:
        monkeypatch.setattr(freshness, "load_bundle", Mock(side_effect=RuntimeError("unexpected")))
        expected_error = RuntimeError
    with pytest.raises(expected_error):
        freshness.generate_freshness_report(
            EDITION_NAME, reports_root=reports_root, archive_root=archive_root, **kwargs,
        )


def test_optional_program_summaries_do_not_load_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(freshness, "load_program_facts", _forbidden)
    assert freshness._build_action_summary_lines(
        program_id=None, programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive", as_of=AS_OF,
    ) == ()
    assert freshness._build_milestone_summary_lines(
        program_id=None, programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive", freshness_report=Mock(),
    ) == ()


@pytest.mark.parametrize("selection", ("module-defaults", "explicit", "legacy-reports"))
@pytest.mark.parametrize("history", (False, True))
def test_coherent_context_reaches_consumers_and_actual_fact_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str, history: bool,
) -> None:
    reports_root, archive_root, _ = _stage_freshness_workspace(tmp_path, tmp_path)
    programs_root = tmp_path / "programs"
    if history:
        _seed_confirmed_snapshot(archive_root)
    monkeypatch.setattr(freshness, "PROGRAMS_ROOT", programs_root if selection == "module-defaults" else tmp_path / "unselected" / "programs")
    monkeypatch.setattr(freshness, "REPORTS_ROOT", reports_root)
    monkeypatch.setattr(freshness, "ARCHIVE_ROOT", archive_root)

    program_calls: dict[str, list[Path]] = {}
    for name in (
        "load_bundle", "load_trusted_baseline_issue", "resolve_edition",
        "load_latest_notification_state", "get_program_output_dir",
        "_build_action_summary_lines", "_build_milestone_summary_lines",
        "_build_ncfl_summary_line",
    ):
        original = getattr(freshness, name)

        def capture(*args, _name=name, _original=original, **kwargs):
            program_calls.setdefault(_name, []).append(kwargs["programs_root"])
            return _original(*args, **kwargs)

        monkeypatch.setattr(freshness, name, capture)
    fact_calls: list[dict] = []
    original_facts = freshness.load_program_facts

    def facts(*args, **kwargs):
        fact_calls.append(kwargs.copy())
        return original_facts(*args, **kwargs)

    monkeypatch.setattr(freshness, "load_program_facts", facts)
    history_calls: list[Path] = []
    original_history = program_fact_store.load_skipped_issues_for_program

    def skipped_history(*args, **kwargs):
        history_calls.append(kwargs["archive_root"])
        assert kwargs["programs_root"] == programs_root
        return original_history(*args, **kwargs)

    monkeypatch.setattr(program_fact_store, "load_skipped_issues_for_program", skipped_history)
    archive_calls: list[Path] = []
    original_narrative_history = freshness.build_workstream_narrative_history

    def narrative_history(*args, **kwargs):
        archive_calls.append(kwargs["archive_root"])
        return original_narrative_history(*args, **kwargs)

    monkeypatch.setattr(freshness, "build_workstream_narrative_history", narrative_history)
    kwargs = {}
    if selection != "module-defaults":
        kwargs.update(reports_root=reports_root, archive_root=archive_root)
    if selection == "explicit":
        kwargs["programs_root"] = programs_root
    artifacts = freshness.generate_freshness_report(
        EDITION_NAME, as_of=AS_OF, notify=True,
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
        **kwargs,
    )

    assert artifacts.issue_number == (2 if history else 1)
    assert artifacts.md_path.is_relative_to(programs_root)
    assert artifacts.html_path.exists()
    assert artifacts.notify_previews
    assert len(program_calls) == 8
    assert all(root == programs_root for calls in program_calls.values() for root in calls)
    assert len(fact_calls) == 2
    assert all(call["programs_root"] == programs_root and call["archive_root"] == archive_root for call in fact_calls)
    assert history_calls == [archive_root]
    assert archive_calls == [archive_root]


def test_action_summary_fallback_forwards_selected_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root, _ = _stage_freshness_workspace(tmp_path, tmp_path)
    original = program_fact_store.load_skipped_issues_for_program
    calls: list[Path] = []

    def history(*args, **kwargs):
        calls.append(kwargs["archive_root"])
        return original(*args, **kwargs)

    monkeypatch.setattr(program_fact_store, "load_skipped_issues_for_program", history)
    assert freshness._build_action_summary_lines(
        program_id="acme", programs_root=reports_root.parent / "programs",
        archive_root=archive_root, as_of=AS_OF,
    ) == ()
    assert calls == [archive_root]
