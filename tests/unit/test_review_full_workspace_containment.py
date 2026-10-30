from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from src.commands import review_full
from src.commands import review_sections
from src.core.exceptions import ConfigError
from src.core.workspace_paths import REPO_ROOT


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
            "ReviewerRenderer", "resolve_edition", "M365Enricher", "load_overrides",
            "load_narratives", "load_review_status", "read_archive_index",
        ):
            guard.setattr(review_full, target, _forbidden)
        if entrypoint == "generate_review_full":
            guard.setattr(review_full, "prepare_review_full_context", _forbidden)
        with pytest.raises(ConfigError, match="workspace roots"):
            getattr(review_full, entrypoint)(edition_name="demo_weekly", **kwargs)


@pytest.mark.parametrize("selection", ("relative", "missing", "unknown-edition"))
def test_review_full_status_helper_rejects_explicit_root_before_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    selected_root = tmp_path / "selected"
    selected_root.mkdir()
    if selection == "unknown-edition":
        (tmp_path / "editions").mkdir()
        (tmp_path / "editions" / "unavailable_weekly.yaml").write_text("program_id: decoy", encoding="utf-8")
    root = {
        "relative": Path("selected"), "missing": tmp_path / "missing",
        "unknown-edition": selected_root,
    }[selection]
    monkeypatch.setattr(review_full, "read_archive_index", _forbidden)
    monkeypatch.setattr(review_full, "load_review_status", _forbidden)
    with pytest.raises(ConfigError, match="artifact programs root"):
        review_full._load_review_full_context(
            edition_name="unavailable_weekly", issue_number=None,
            reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
            programs_root=root,
        )
    assert list(selected_root.iterdir()) == []


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
    monkeypatch.setattr(review_full, "resolve_report_workspace_roots", Mock(side_effect=RuntimeError("unexpected")), raising=False)
    with pytest.raises(RuntimeError, match="unexpected"):
        review_full._preflight_review_full_workspace()


@pytest.mark.parametrize(
    "module,preflight",
    (
        (review_full, "_preflight_review_full_workspace"),
        (review_sections, "_preflight_review_sections_workspace"),
    ),
)
def test_review_preflight_accepts_approved_external_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module, preflight: str,
) -> None:
    monkeypatch.setattr(module, "PROGRAMS_ROOT", tmp_path / "selected" / "programs")
    getattr(module, preflight)()


@pytest.mark.parametrize(
    "module,preflight",
    (
        (review_full, "_preflight_review_full_workspace"),
        (review_sections, "_preflight_review_sections_workspace"),
    ),
)
def test_review_preflight_keeps_unset_legacy_roots(
    monkeypatch: pytest.MonkeyPatch, module, preflight: str,
) -> None:
    for name in ("PROGRAMS_ROOT", "REPORTS_ROOT", "ARCHIVE_ROOT"):
        suffix = {"PROGRAMS_ROOT": "programs", "REPORTS_ROOT": "reports", "ARCHIVE_ROOT": "archive"}[name]
        monkeypatch.setattr(module, name, REPO_ROOT / suffix)
    mapper = Mock(wraps=module.resolve_report_workspace_roots)
    monkeypatch.setattr(module, "resolve_report_workspace_roots", mapper)

    getattr(module, preflight)()

    mapper.assert_called_once_with(None, None, None)


@pytest.mark.parametrize(
    "module,entrypoint",
    (
        (review_full, "prepare_review_full_context"),
        (review_sections, "_load_review_context"),
    ),
)
def test_direct_review_context_keeps_unset_legacy_roots(
    monkeypatch: pytest.MonkeyPatch, module, entrypoint: str,
) -> None:
    for name in ("PROGRAMS_ROOT", "REPORTS_ROOT", "ARCHIVE_ROOT"):
        suffix = {"PROGRAMS_ROOT": "programs", "REPORTS_ROOT": "reports", "ARCHIVE_ROOT": "archive"}[name]
        monkeypatch.setattr(module, name, REPO_ROOT / suffix)
    mapper = Mock(wraps=module.resolve_report_workspace_roots)
    monkeypatch.setattr(module, "resolve_report_workspace_roots", mapper)
    monkeypatch.setattr(module, "load_bundle", Mock(side_effect=RuntimeError("after workspace resolution")))

    with pytest.raises(RuntimeError, match="after workspace resolution"):
        if entrypoint == "prepare_review_full_context":
            module.prepare_review_full_context(edition_name="demo_weekly")
        else:
            module._load_review_context("demo_weekly", None)

    mapper.assert_called_once_with(None, None, None)


@pytest.mark.parametrize(
    "module,entrypoint",
    (
        (review_full, "generate_review_full"),
        (review_sections, "export_review_section"),
    ),
)
def test_direct_review_generation_keeps_unset_legacy_roots(
    monkeypatch: pytest.MonkeyPatch, module, entrypoint: str,
) -> None:
    for name in ("PROGRAMS_ROOT", "REPORTS_ROOT", "ARCHIVE_ROOT"):
        suffix = {"PROGRAMS_ROOT": "programs", "REPORTS_ROOT": "reports", "ARCHIVE_ROOT": "archive"}[name]
        monkeypatch.setattr(module, name, REPO_ROOT / suffix)
    mapper = Mock(wraps=module.resolve_report_workspace_roots)
    monkeypatch.setattr(module, "resolve_report_workspace_roots", mapper)
    prepare = Mock(side_effect=RuntimeError("after workspace resolution"))
    monkeypatch.setattr(module, "prepare_review_full_context", prepare)

    with pytest.raises(RuntimeError, match="after workspace resolution"):
        if entrypoint == "generate_review_full":
            module.generate_review_full(edition_name="demo_weekly")
        else:
            module.export_review_section(
                edition_name="demo_weekly",
                section_id="ws:demo",
            )

    mapper.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("entrypoint", ("review-full", "review-sections-export"))
def test_legacy_review_generation_does_not_promote_resolved_roots_to_explicit(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    for module in (review_full, review_sections):
        for name in ("PROGRAMS_ROOT", "REPORTS_ROOT", "ARCHIVE_ROOT"):
            suffix = {"PROGRAMS_ROOT": "programs", "REPORTS_ROOT": "reports", "ARCHIVE_ROOT": "archive"}[name]
            monkeypatch.setattr(module, name, REPO_ROOT / suffix)
    loader = Mock(side_effect=RuntimeError("legacy context reached loader"))
    monkeypatch.setattr(review_full, "load_bundle", loader)

    with pytest.raises(RuntimeError, match="legacy context reached loader"):
        if entrypoint == "review-full":
            review_full.generate_review_full(edition_name="demo_weekly")
        else:
            review_sections.export_review_section(
                edition_name="demo_weekly",
                section_id="ws:demo",
            )

    loader.assert_called_once_with(
        "demo_weekly",
        programs_root=REPO_ROOT / "programs",
        reports_root=REPO_ROOT / "reports",
    )


@pytest.mark.parametrize("entrypoint", ("review-full", "review-sections"))
def test_review_context_rejects_checkout_root_before_readers(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    for module, target in (
        (review_full, "load_bundle"),
        (review_sections, "load_bundle"),
    ):
        monkeypatch.setattr(module, target, _forbidden)
    with pytest.raises(ConfigError, match="outside installation assets"):
        if entrypoint == "review-full":
            review_full.prepare_review_full_context(
                edition_name="demo_weekly",
                programs_root=REPO_ROOT / "programs",
                reports_root=REPO_ROOT / "reports",
                archive_root=REPO_ROOT / "archive",
            )
        else:
            review_sections._load_review_context(
                "demo_weekly",
                None,
                programs_root=REPO_ROOT / "programs",
                reports_root=REPO_ROOT / "reports",
                archive_root=REPO_ROOT / "archive",
            )


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


@pytest.mark.parametrize("entrypoint", ("context", "export", "show", "set-pending", "set-approved", "clear"))
@pytest.mark.parametrize("invalid", ("programs", "reports", "archive", "relative"))
def test_review_sections_invalid_workspace_fails_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, invalid: str,
) -> None:
    for name in ("programs", "reports", "archive"):
        root = tmp_path / name
        if name == invalid:
            root = tmp_path / "other" / name
        elif invalid == "relative" and name == "programs":
            root = Path("programs")
        monkeypatch.setattr(review_sections, f"{name.upper()}_ROOT", root)
    for target in (
        "load_bundle", "read_archive_index", "load_review_status", "save_review_status",
        "prepare_review_full_context", "_load_active_manifest_id", "ReviewerRenderer",
        "_write_section_export_html", "resolve_edition", "load_program_facts",
    ):
        monkeypatch.setattr(review_sections, target, _forbidden, raising=False)
    monkeypatch.setattr(review_sections.webbrowser, "open", _forbidden)
    with pytest.raises(ConfigError, match="workspace roots"):
        if entrypoint == "context":
            review_sections._load_review_context("demo_weekly", None)
        elif entrypoint == "export":
            review_sections.export_review_section(edition_name="demo_weekly", section_id="ws:demo", open_browser=True)
        elif entrypoint == "show":
            review_sections.show_review_sections_command(edition="demo_weekly", issue=None, format="human")
        elif entrypoint.startswith("set-"):
            review_sections.set_review_section_command(
                edition="demo_weekly", issue=None, section="exec_summary",
                state=entrypoint.removeprefix("set-"), note=None, reviewer=None,
            )
        else:
            review_sections.clear_review_section_command(edition="demo_weekly", issue=None, section="exec_summary")


@pytest.mark.parametrize("helper", ("_load_review_context", "export_review_section"))
@pytest.mark.parametrize("invalid", ("programs", "reports", "archive", "relative"))
def test_review_sections_explicit_workspace_rejects_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str, invalid: str,
) -> None:
    kwargs = {
        "programs_root": tmp_path / "programs", "reports_root": tmp_path / "reports",
        "archive_root": tmp_path / "archive",
    }
    key = f"{invalid}_root" if invalid != "relative" else "programs_root"
    kwargs[key] = tmp_path / "other" / invalid if invalid != "relative" else Path("programs")
    for target in ("load_bundle", "prepare_review_full_context", "load_review_status", "read_archive_index"):
        monkeypatch.setattr(review_sections, target, _forbidden)
    with pytest.raises(ConfigError, match="workspace roots"):
        if helper == "_load_review_context":
            review_sections._load_review_context("demo_weekly", None, **kwargs)
        else:
            review_sections.export_review_section(edition_name="demo_weekly", section_id="ws:demo", **kwargs)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("helper", ("_load_active_manifest_id", "_try_load_active_manifest_id"))
@pytest.mark.parametrize("invalid", ("relative", "missing", "unknown-edition"))
def test_review_sections_manifest_admission_failure_is_not_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str, invalid: str,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    if invalid == "unknown-edition":
        (tmp_path / "editions").mkdir()
        (tmp_path / "editions" / "unavailable_weekly.yaml").write_text("program_id: decoy", encoding="utf-8")
    root = Path("selected") if invalid == "relative" else tmp_path / "missing" if invalid == "missing" else selected
    monkeypatch.setattr(review_sections, "get_program_output_dir", _forbidden)
    with pytest.raises(ConfigError, match="artifact programs root"):
        getattr(review_sections, helper)("unavailable_weekly", 1, programs_root=root)
    assert list(selected.iterdir()) == []
