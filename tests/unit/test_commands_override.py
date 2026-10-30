from __future__ import annotations
from tests.support.synthetic_report_source import synthetic_report_root as repo_root

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import override as override_module
from src.core.ai_proposal_store import append_ai_proposal, build_ai_proposal_id, load_ai_proposals
from src.core.edition_resolver import resolve_edition
from src.core.models import Confidence, RiskLevel
from src.core.exceptions import ConfigError
from src.core.models_v2 import AIProposal, AIProposalStatus, WorkstreamSynthesis
from src.core.overrides_store import OverridesDocument, get_overrides_path, load_overrides
from src.commands.report import generate_report_draft
from tests.unit.test_commands_report import _sample_items, _seed_v2_report_layout


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_cli_help_lists_confirm_and_override() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "confirm" in result.stdout
    assert "override" in result.stdout


def test_override_cli_updates_single_dimension_and_creates_backup(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.override.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.override.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(override_module, "PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "Deployment Velocity"],
        input="H\nLT aligned on High.\nN\n",
    )

    updated = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=1)
    overrides_path = get_overrides_path(EDITION_NAME, reports_root=reports_root, issue_number=1)

    assert result.exit_code == 0, result.stdout
    assert "Evidence:" in result.stdout
    assert "Prior confirmed risk:" in result.stdout
    assert overrides_path.with_suffix(overrides_path.suffix + ".bak").exists()
    assert updated is not None
    deployment = next(
        dimension
        for scorecard in updated.scorecards
        for dimension in scorecard.dimensions
        if dimension.name == "Deployment Velocity"
    )
    assert deployment.risk is not None and deployment.risk.value == "high"
    assert deployment.summary == "LT aligned on High."


def test_override_cli_uses_trusted_baseline_for_previous_snapshot(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = override_module._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(override_module, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(override_module, "_load_previous_snapshot", _capturing_load_previous_snapshot)
    monkeypatch.setattr("src.commands.override.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.override.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(override_module, "PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "Deployment Velocity"],
        input="H\nTrusted baseline still points here.\nN\n",
    )

    assert result.exit_code == 0, result.stdout
    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_override_cli_accepts_pending_ai_proposal(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    proposal = _append_pending_ai_proposal(reports_root, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.override.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.override.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(override_module, "PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setenv("USERNAME", "operator")

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "Deployment Velocity"],
        input="A\nAI aligned on High.\nN\n",
    )

    updated = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=1)
    latest_proposal = {entry.id: entry for entry in load_ai_proposals("acme", programs_root=reports_root.parent / "programs")}[proposal.id]

    assert result.exit_code == 0, result.stdout
    assert "[A]I-proposed" in result.stdout
    assert "AI proposal: High" in result.stdout
    assert updated is not None
    deployment = next(
        dimension
        for scorecard in updated.scorecards
        for dimension in scorecard.dimensions
        if dimension.name == "Deployment Velocity"
    )
    assert deployment.risk is RiskLevel.HIGH
    assert deployment.summary == "AI aligned on High."
    assert latest_proposal.status is AIProposalStatus.ACCEPTED
    assert latest_proposal.resolved_by == "operator"


def test_override_cli_rejects_pending_ai_proposal_when_author_keeps_current(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    proposal = _append_pending_ai_proposal(reports_root, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.override.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.override.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(override_module, "PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setenv("USERNAME", "operator")

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "Deployment Velocity"],
        input="K\n\nN\n",
    )

    updated = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=1)
    latest_proposal = {entry.id: entry for entry in load_ai_proposals("acme", programs_root=reports_root.parent / "programs")}[proposal.id]

    assert result.exit_code == 0, result.stdout
    assert "AI proposal rejected" in result.stdout
    assert updated is not None
    deployment = next(
        dimension
        for scorecard in updated.scorecards
        for dimension in scorecard.dimensions
        if dimension.name == "Deployment Velocity"
    )
    assert deployment.risk is None
    assert deployment.summary is None
    assert latest_proposal.status is AIProposalStatus.REJECTED
    assert latest_proposal.resolved_by == "operator"


@pytest.mark.parametrize("selection", ["explicit", "none", "omitted"])
@pytest.mark.parametrize("program_backed", [True, False])
@pytest.mark.parametrize("state", ["draft", "missing", "confirmed"])
def test_override_retains_invocation_context(monkeypatch, tmp_path: Path, selection: str, program_backed: bool, state: str) -> None:
    reports_root = tmp_path / "reports"
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    programs_root.mkdir()
    overrides_path = tmp_path / "authored.yaml"
    overrides_path.write_text("original", encoding="utf-8")
    document = OverridesDocument(issue_number=1, top_3_now=(), scorecards=())
    bundle = SimpleNamespace(config=SimpleNamespace(edition=SimpleNamespace(title="Synthetic")))
    resolved = SimpleNamespace(paths=SimpleNamespace(program_id="demo")) if program_backed else None
    readers = {
        "load_bundle": Mock(return_value=bundle),
        "resolve_edition": Mock(return_value=resolved),
        "load_overrides": Mock(return_value=None if state == "missing" else document),
        "_load_draft_state": Mock(return_value={"items": []}),
        "load_trusted_baseline_issue": Mock(return_value=None),
        "_load_pending_ai_proposals_by_dimension": Mock(return_value={}),
        "get_overrides_path": Mock(return_value=overrides_path),
        "save_overrides": Mock(return_value=overrides_path),
    }
    for name, reader in readers.items():
        monkeypatch.setattr(override_module, name, reader)
    monkeypatch.setattr(override_module, "is_issue_confirmed", Mock(return_value=state == "confirmed"))
    monkeypatch.setattr(override_module, "_load_previous_snapshot", Mock(return_value=(None, None)))
    monkeypatch.setattr(override_module, "_build_scorecard_packets", Mock(return_value={}))
    options = {} if selection == "omitted" else {"programs_root": programs_root if selection == "explicit" else None}

    if state != "draft":
        from typer import BadParameter

        with pytest.raises(BadParameter, match="missing" if state == "missing" else "already confirmed"):
            override_module.run_override(
                EDITION_NAME, reports_root=reports_root, archive_root=archive_root, **options,
            )
        for name in ("_load_draft_state", "_load_pending_ai_proposals_by_dimension", "save_overrides"):
            readers[name].assert_not_called()
        assert not overrides_path.with_suffix(".yaml.bak").exists()
        return
    result = override_module.run_override(
        EDITION_NAME, reports_root=reports_root, archive_root=archive_root, **options,
    )

    assert result.saved_path == overrides_path
    assert result.backup_path.read_text(encoding="utf-8") == "original"
    for name in ("load_bundle", "resolve_edition", "_load_draft_state", "load_trusted_baseline_issue"):
        assert readers[name].call_args.kwargs["programs_root"] == programs_root, name
    artifact_root = programs_root if program_backed else None
    for name in ("load_overrides", "get_overrides_path", "save_overrides"):
        assert readers[name].call_args.kwargs["programs_root"] == artifact_root, name
    assert readers["_load_pending_ai_proposals_by_dimension"].call_args.kwargs["programs_root"] == artifact_root


@pytest.mark.parametrize("invalid", ["relative", "conflict"])
def test_override_invalid_workspace_precedes_reads_and_prompts(monkeypatch, tmp_path: Path, invalid: str) -> None:
    forbidden = Mock(side_effect=AssertionError("must fail before access"))
    for name in ("load_bundle", "load_overrides", "_load_draft_state", "_prompt_choice"):
        monkeypatch.setattr(override_module, name, forbidden)
    programs_root = Path("relative") if invalid == "relative" else tmp_path / "other"

    with pytest.raises(ConfigError):
        override_module.run_override(
            EDITION_NAME, reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
            programs_root=programs_root,
        )
    forbidden.assert_not_called()


def test_pending_override_proposals_use_selected_root(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, _ = _seed_v2_report_layout(repo_root, tmp_path)
    proposal = _append_pending_ai_proposal(reports_root, dimension_name="Deployment Velocity")
    programs_root = tmp_path / "programs"
    unrelated_reports = tmp_path / "decoy" / "reports"

    selected = override_module._load_pending_ai_proposals_by_dimension(
        EDITION_NAME, reports_root=unrelated_reports, programs_root=programs_root,
    )

    assert proposal in selected.values()
    assert override_module._load_pending_ai_proposals_by_dimension(
        EDITION_NAME, reports_root=unrelated_reports,
    ) == {}
    assert override_module._load_pending_ai_proposals_by_dimension(
        EDITION_NAME, reports_root=unrelated_reports, programs_root=None,
    ) == {}
    forbidden = Mock(side_effect=AssertionError("must validate selected edition first"))
    monkeypatch.setattr(override_module, "load_ai_proposals", forbidden)
    with pytest.raises(ConfigError):
        override_module._load_pending_ai_proposals_by_dimension(
            EDITION_NAME, reports_root=reports_root, programs_root=tmp_path / "missing",
        )
    forbidden.assert_not_called()


def _append_pending_ai_proposal(reports_root: Path, *, dimension_name: str) -> AIProposal:
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    resolved = resolve_edition(EDITION_NAME, editions_root=editions_root, programs_root=programs_root)
    assert resolved is not None
    workstream_id = next(
        dimension.workstream_id
        for scorecard in resolved.scorecards
        for dimension in scorecard.dimensions
        if dimension.name == dimension_name
    )
    created_at = datetime(2026, 5, 5, 19, 0, tzinfo=timezone.utc)
    proposal = AIProposal(
        id=build_ai_proposal_id("acme", workstream_id=workstream_id, created_at=created_at),
        workstream_id=workstream_id,
        synthesis=WorkstreamSynthesis(
            workstream_id=workstream_id,
            overall_assessment="AI sees the lane as the current gating risk.",
            proposed_risk=RiskLevel.HIGH,
            confidence=Confidence.MEDIUM,
            key_findings=("Cross-team dependency remains open.",),
            evidence_refs=("sig-1",),
            open_questions=("Who owns the unblock plan?",),
            recommended_actions=("Lock the unblock owner and date.",),
        ),
        status=AIProposalStatus.PENDING,
        created_at=created_at,
        resolved_at=None,
        resolved_by=None,
    )
    append_ai_proposal("acme", proposal, programs_root=programs_root)
    return proposal
