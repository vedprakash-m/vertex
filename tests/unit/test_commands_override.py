from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands import override as override_module
from src.core.ai_proposal_store import append_ai_proposal, build_ai_proposal_id, load_ai_proposals
from src.core.edition_resolver import resolve_edition
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import AIProposal, AIProposalStatus, WorkstreamSynthesis
from src.core.overrides_store import get_overrides_path, load_overrides
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

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "SCHIE Gaps"],
        input="H\nLT aligned on High.\nN\n",
    )

    updated = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=1)
    overrides_path = get_overrides_path(EDITION_NAME, reports_root=reports_root, issue_number=1)

    assert result.exit_code == 0
    assert "Evidence:" in result.stdout
    assert "Prior confirmed risk:" in result.stdout
    assert overrides_path.with_suffix(overrides_path.suffix + ".bak").exists()
    assert updated is not None
    schie = next(
        dimension
        for scorecard in updated.scorecards
        for dimension in scorecard.dimensions
        if dimension.name == "SCHIE Gaps"
    )
    assert schie.risk is not None and schie.risk.value == "high"
    assert schie.summary == "LT aligned on High."


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

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "SCHIE Gaps"],
        input="H\nTrusted baseline still points here.\nN\n",
    )

    assert result.exit_code == 0
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

    proposal = _append_pending_ai_proposal(reports_root, dimension_name="SCHIE Gaps")

    monkeypatch.setattr("src.commands.override.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.override.ARCHIVE_ROOT", archive_root)
    monkeypatch.setenv("USERNAME", "operator")

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "SCHIE Gaps"],
        input="A\nAI aligned on High.\nN\n",
    )

    updated = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=1)
    latest_proposal = {entry.id: entry for entry in load_ai_proposals("acme", programs_root=reports_root.parent / "programs")}[proposal.id]

    assert result.exit_code == 0
    assert "[A]I-proposed" in result.stdout
    assert "AI proposal: High" in result.stdout
    assert updated is not None
    schie = next(
        dimension
        for scorecard in updated.scorecards
        for dimension in scorecard.dimensions
        if dimension.name == "SCHIE Gaps"
    )
    assert schie.risk is RiskLevel.HIGH
    assert schie.summary == "AI aligned on High."
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

    proposal = _append_pending_ai_proposal(reports_root, dimension_name="SCHIE Gaps")

    monkeypatch.setattr("src.commands.override.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.override.ARCHIVE_ROOT", archive_root)
    monkeypatch.setenv("USERNAME", "operator")

    result = runner.invoke(
        app,
        ["override", "--edition", EDITION_NAME, "--dimension", "SCHIE Gaps"],
        input="K\n\nN\n",
    )

    updated = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=1)
    latest_proposal = {entry.id: entry for entry in load_ai_proposals("acme", programs_root=reports_root.parent / "programs")}[proposal.id]

    assert result.exit_code == 0
    assert "AI proposal rejected" in result.stdout
    assert updated is not None
    schie = next(
        dimension
        for scorecard in updated.scorecards
        for dimension in scorecard.dimensions
        if dimension.name == "SCHIE Gaps"
    )
    assert schie.risk is None
    assert schie.summary is None
    assert latest_proposal.status is AIProposalStatus.REJECTED
    assert latest_proposal.resolved_by == "operator"


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

