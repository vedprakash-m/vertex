from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands import decision_brief
from src.commands.decision_brief import _enrich_with_ai
from src.core.decision_brief_engine import DecisionBrief
from src.core.exceptions import ConfigError


def test_enrich_with_ai_returns_original_brief_when_invocation_ai_disabled() -> None:
    brief = DecisionBrief(
        issue_number=42,
        edition_name="acme_weekly",
        generated_at="2026-06-06 12:00",
        items=(),
        total_pending=0,
        ai_enriched=False,
    )
    set_ai_mode(AIMode.DISABLED)
    try:
        enriched = _enrich_with_ai(
            brief=brief,
            bundle=object(),
            program_id="acme",
            create_ai_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("AI client should not be created when AIMode.DISABLED")),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert enriched is brief


@pytest.mark.parametrize("entrypoint", ("generate", "pending"))
@pytest.mark.parametrize("invalid", ("program", "report", "archive", "relative", "checkout"))
def test_decision_brief_invalid_workspace_refuses_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, invalid: str,
) -> None:
    roots = dict(
        programs_root=tmp_path / "programs", reports_root=tmp_path / "reports",
        archive_root=tmp_path / "archive",
    )
    if invalid == "checkout":
        (tmp_path / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    elif invalid == "relative":
        roots["programs_root"] = Path("programs")
    else:
        roots[f"{invalid}s_root" if invalid != "archive" else "archive_root"] = tmp_path / "other" / invalid
    for name in ("load_bundle", "resolve_edition_paths", "load_proposals", "build_signal_store_for_program_id",
                 "_enrich_with_ai", "_write_output_text"):
        monkeypatch.setattr(decision_brief, name, Mock(side_effect=AssertionError("access before admission")))
    function = (decision_brief.generate_decision_brief if entrypoint == "generate"
                else decision_brief.load_pending_decision_brief)
    with pytest.raises(ConfigError):
        function(edition_name="acme_weekly", issue_number=1, **roots)


@pytest.mark.parametrize("selection", ("explicit", "defaults", "reports-only", "none"))
def test_decision_brief_forwards_selected_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    roots = dict(programs_root=tmp_path / "programs", reports_root=tmp_path / "reports",
                 archive_root=tmp_path / "archive")
    for key, path in roots.items():
        monkeypatch.setattr(decision_brief, key.upper(), path, raising=False)
    kwargs = roots if selection == "explicit" else {}
    if selection == "none":
        kwargs = dict.fromkeys(roots)
    elif selection == "reports-only":
        monkeypatch.setattr(decision_brief, "PROGRAMS_ROOT", tmp_path / "unselected", raising=False)
        kwargs = {"reports_root": roots["reports_root"], "archive_root": roots["archive_root"]}
    brief = DecisionBrief(1, "acme_weekly", "2026-01-01", (), 0, False)
    bundle = Mock(return_value=object())
    pending = Mock(return_value=(brief, "acme"))
    enrich = Mock(return_value=brief)
    output = Mock(return_value=roots["programs_root"] / "acme" / "output")
    monkeypatch.setattr(decision_brief, "load_bundle", bundle)
    monkeypatch.setattr(decision_brief, "load_pending_decision_brief", pending)
    monkeypatch.setattr(decision_brief, "_enrich_with_ai", enrich)
    monkeypatch.setattr(decision_brief, "get_program_output_dir", output)
    monkeypatch.setattr(decision_brief, "_render_decision_brief_html", lambda **kwargs: "brief")
    monkeypatch.setattr(decision_brief, "_write_output_text", lambda path, text: path)
    result = decision_brief.generate_decision_brief(edition_name="acme_weekly", ai=True, **kwargs)
    assert bundle.call_args.kwargs["programs_root"] == roots["programs_root"]
    assert pending.call_args.kwargs == {
        "edition_name": "acme_weekly", "issue_number": None,
        **{key: kwargs.get(key) for key in roots},
    }
    assert enrich.call_args.kwargs["programs_root"] == roots["programs_root"]
    assert output.call_args.kwargs["programs_root"] == roots["programs_root"]
    assert result.html_path.is_relative_to(roots["programs_root"])


def test_pending_decision_brief_keeps_selected_store_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = dict(programs_root=tmp_path / "programs", reports_root=tmp_path / "reports",
                 archive_root=tmp_path / "archive")
    resolver = Mock(return_value=SimpleNamespace(program_id="acme"))
    issue = Mock(return_value=4)
    proposals = Mock(return_value=(SimpleNamespace(status=decision_brief.SectionRevisionStatus.PENDING),))
    store = Mock()
    store.read.return_value = ()
    signal_store = Mock(return_value=store)
    monkeypatch.setattr(decision_brief, "resolve_edition_paths", resolver)
    monkeypatch.setattr(decision_brief, "_resolve_default_issue_number", issue)
    monkeypatch.setattr(decision_brief, "load_proposals", proposals)
    monkeypatch.setattr(decision_brief, "build_signal_store_for_program_id", signal_store)
    monkeypatch.setattr(decision_brief, "build_decision_brief", lambda **kwargs: "brief")
    assert decision_brief.load_pending_decision_brief(
        edition_name="acme_weekly", issue_number=None, **roots,
    ) == ("brief", "acme")
    resolver.assert_called_once_with("acme_weekly", programs_root=roots["programs_root"])
    assert issue.call_args.kwargs["archive_root"] == roots["archive_root"]
    assert proposals.call_args.kwargs["programs_root"] == roots["programs_root"]
    signal_store.assert_called_once_with("acme", programs_root=roots["programs_root"])


def test_decision_brief_ai_receives_selected_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brief = DecisionBrief(1, "acme_weekly", "2026-01-01", (), 0, False)
    advisor = Mock(return_value=brief)
    monkeypatch.setattr("src.ai.decision_brief_advisor.advise_on_decision_brief", advisor)
    previous_mode = decision_brief.get_ai_mode()
    set_ai_mode(AIMode.ACTIVE)
    try:
        decision_brief._enrich_with_ai(
            brief=brief, bundle=object(), program_id="acme", create_ai_client=lambda bundle: object(),
            programs_root=tmp_path / "programs",
        )
    finally:
        set_ai_mode(previous_mode)
    assert advisor.call_args.kwargs["programs_root"] == tmp_path / "programs"


@pytest.mark.parametrize("entrypoint", ("command", "cli", "pilot-cli"))
def test_decision_brief_cli_refuses_unsafe_workspace_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    from typer.testing import CliRunner
    import cli

    (tmp_path / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    monkeypatch.setattr(decision_brief, "PROGRAMS_ROOT", tmp_path / "programs", raising=False)
    monkeypatch.setattr(decision_brief, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(decision_brief, "ARCHIVE_ROOT", tmp_path / "archive")
    generator = Mock(side_effect=AssertionError("unsafe generation"))
    monkeypatch.setattr(decision_brief, "generate_decision_brief", generator)
    for target in ("_maybe_register_tier_decision_sink", "_maybe_run_scheduled_db_maintenance",
                   "_maybe_run_session_catchup"):
        monkeypatch.setattr(cli, target, Mock(side_effect=AssertionError("startup before admission")))
    if entrypoint == "command":
        with pytest.raises(typer.Exit) as error:
            decision_brief.decision_brief_command(edition="acme_weekly", issue=1, ai=False, open_browser=False)
        assert error.value.exit_code == 2
    else:
        args = (["decision-brief", "--edition", "acme_weekly"] if entrypoint == "cli"
                else ["decision-brief-pilot", "summary", "--program", "acme"])
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 2, result.output
        assert "workspace" in result.output.lower()
    generator.assert_not_called()


@pytest.mark.parametrize("selection", ("programs-only", "reports-only", "defaults", "legacy"))
def test_actual_decision_brief_uses_selected_proposals_signals_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    from dataclasses import replace

    from src.core import workspace_paths
    from src.core.journal import append_signal
    from src.core.section_proposal_store import append_proposal
    from tests.unit.test_config_loader import _write_demo_v2_layout
    from tests.unit.test_decision_brief_engine import _make_proposal, _make_signal

    workspace = tmp_path / "selected"
    editions, programs = _write_demo_v2_layout(workspace)
    program_editions = programs / "demo" / "editions"
    program_editions.mkdir()
    (program_editions / "demo_weekly.yaml").write_bytes((editions / "demo_weekly.yaml").read_bytes())
    signal = replace(_make_signal("selected-evidence", "Selected workspace evidence."), program_id="demo")
    append_signal(signal, programs_root=programs)
    proposal = _make_proposal(current_text="Selected proposal.", proposed_text="Selected revision.")
    proposal = replace(
        proposal, edition_id="demo_weekly", issue_number=7,
        evidence_brief=replace(proposal.evidence_brief, top_signals=(signal.id,)),
    )
    append_proposal(proposal, "demo", 7, programs_root=programs)
    misleading = tmp_path / "misleading"
    misleading.mkdir()
    monkeypatch.chdir(misleading)
    kwargs = {"programs_root": programs} if selection == "programs-only" else {}
    if selection == "reports-only":
        kwargs = {"reports_root": workspace / "reports", "archive_root": workspace / "archive"}
    elif selection in ("defaults", "legacy"):
        for key in ("programs", "reports", "archive"):
            monkeypatch.setattr(decision_brief, key.upper() + "_ROOT", workspace / key)
        if selection == "legacy":
            monkeypatch.setattr(decision_brief, "REPO_ROOT", workspace)
            monkeypatch.setattr(workspace_paths, "REPO_ROOT", workspace)
            monkeypatch.setattr(workspace_paths, "PROGRAMS_ROOT", programs)
            monkeypatch.setattr(workspace_paths, "_PROGRAMS_ROOT_CONFIGURED", False)
    opened = Mock()
    monkeypatch.setattr(decision_brief.webbrowser, "open", opened)
    brief, program_id = decision_brief.load_pending_decision_brief(
        edition_name="demo_weekly", issue_number=7, **kwargs,
    )
    assert program_id == "demo"
    assert brief.items[0].proposed_text == "Selected revision."
    artifacts = decision_brief.generate_decision_brief(
        edition_name="demo_weekly", issue_number=7, ai=False, open_browser=True, **kwargs,
    )
    html = artifacts.html_path.read_text(encoding="utf-8")
    assert artifacts.item_count == 1
    assert artifacts.issue_number == 7
    assert artifacts.ai_enriched is False
    assert artifacts.html_path == programs / "demo" / "publications" / "demo_weekly" / "review" / "decision_brief.html"
    for text in ("Selected proposal.", "Selected workspace evidence."):
        assert text in html
    opened.assert_called_once_with(artifacts.html_path.resolve().as_uri())
    assert not (misleading / "programs").exists()
    assert not (workspace / "archive").exists()


def test_decision_brief_installed_root_configuration_is_not_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.core.workspace_paths._PROGRAMS_ROOT_CONFIGURED", True)
    with pytest.raises(typer.Exit) as error:
        decision_brief._preflight_decision_brief_workspace()
    assert error.value.exit_code == 2


def test_decision_brief_config_error_during_generation_is_safe_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_brief, "PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        decision_brief, "generate_decision_brief", Mock(side_effect=ConfigError("configuration changed")),
    )
    with pytest.raises(typer.Exit) as error:
        decision_brief.decision_brief_command(edition="demo_weekly", issue=1, ai=False, open_browser=False)
    assert error.value.exit_code == 2


@pytest.mark.parametrize("reason", ("unknown-edition", "no-pending"))
def test_missing_decision_brief_refuses_before_signal_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    resolver = Mock(return_value=None if reason == "unknown-edition" else SimpleNamespace(program_id="demo"))
    monkeypatch.setattr(decision_brief, "resolve_edition_paths", resolver)
    monkeypatch.setattr(decision_brief, "load_proposals", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        decision_brief, "build_signal_store_for_program_id",
        Mock(side_effect=AssertionError("signal read after missing brief")),
    )
    with pytest.raises(typer.BadParameter):
        decision_brief.load_pending_decision_brief(
            edition_name="demo_weekly", issue_number=1, programs_root=tmp_path / "programs",
        )


@pytest.mark.parametrize("latest", (None, SimpleNamespace(issue_number=7)))
def test_decision_brief_issue_selection_uses_selected_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, latest: object,
) -> None:
    reader = Mock(return_value=object())
    monkeypatch.setattr("src.core.archive_store.read_archive_index", reader)
    monkeypatch.setattr("src.core.archive_store.find_latest_confirmed_entry", lambda index: latest)
    kwargs = dict(edition_name="demo_weekly", program_id="demo", reports_root=tmp_path / "reports",
                  archive_root=tmp_path / "archive")
    if latest is None:
        with pytest.raises(typer.BadParameter, match="No confirmed issues"):
            decision_brief._resolve_default_issue_number(**kwargs)
    else:
        assert decision_brief._resolve_default_issue_number(**kwargs) == 8
    reader.assert_called_once_with("demo_weekly", archive_root=tmp_path / "archive")


def test_decision_brief_successful_direct_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_brief, "PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        decision_brief, "generate_decision_brief",
        Mock(return_value=decision_brief.DecisionBriefArtifacts(7, tmp_path / "brief.html", 1, False)),
    )
    with pytest.raises(typer.Exit) as error:
        decision_brief.decision_brief_command(edition="demo_weekly", issue=7, ai=False, open_browser=False)
    assert error.value.exit_code == 0


@pytest.mark.parametrize("failure", ("factory", "none", "advisor"))
def test_decision_brief_ai_fallback_preserves_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    brief = DecisionBrief(1, "demo_weekly", "2026-01-01", (), 0, False)
    factory = Mock(side_effect=RuntimeError("unavailable")) if failure == "factory" else Mock(
        return_value=None if failure == "none" else object(),
    )
    monkeypatch.setattr(
        "src.ai.decision_brief_advisor.advise_on_decision_brief",
        Mock(side_effect=RuntimeError("unavailable")),
    )
    previous_mode = decision_brief.get_ai_mode()
    set_ai_mode(AIMode.ACTIVE)
    try:
        assert decision_brief._enrich_with_ai(
            brief=brief, bundle=object(), program_id="demo", create_ai_client=factory,
            programs_root=tmp_path / "programs",
        ) is brief
    finally:
        set_ai_mode(previous_mode)


def test_decision_brief_missing_template_reports_render_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.exceptions import RenderError

    monkeypatch.setattr(decision_brief, "TEMPLATES_ROOT", tmp_path)
    brief = DecisionBrief(1, "demo_weekly", "2026-01-01", (), 0, False)
    with pytest.raises(RenderError, match="Missing template"):
        decision_brief._render_decision_brief_html(brief=brief, edition_name="demo_weekly")
