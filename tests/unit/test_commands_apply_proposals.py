from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.commands.apply_proposals as apply_proposals
from cli import app
from src.commands.apply_proposals import apply_section_revision_proposals, preview_section_revision_proposals_undo, prompt_for_section_revision_actions, undo_section_revision_proposals
from src.commands.propose import generate_section_revision_proposals
from src.core.narrative_store import get_narratives_dir, load_narratives, narrative_filename_for_section
from src.core.section_proposal_store import append_proposal, load_proposals
from src.core.models_v2 import SectionRevisionStatus
from tests.unit.test_commands_propose import _empty_work_item_loader
from tests.unit.test_commands_report import _seed_v2_report_layout


runner = CliRunner()


def test_apply_section_revision_proposals_accepts_no_ai_without_writing_narrative(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    narratives = load_narratives("acme_weekly", proposal_artifacts.issue_number, reports_root=reports_root)

    artifacts = apply_section_revision_proposals(
        edition_name="acme_weekly",
        accept=("exec_summary",),
        reports_root=reports_root,
        archive_root=archive_root,
    )

    updated_proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    exec_summary = next(proposal for proposal in updated_proposals if proposal.section_id == "exec_summary")
    current_narratives = load_narratives("acme_weekly", proposal_artifacts.issue_number, reports_root=reports_root)

    assert artifacts.accepted_count == 1
    assert artifacts.backup_path is None
    assert exec_summary.status == SectionRevisionStatus.ACCEPTED
    assert current_narratives == narratives


def test_apply_section_revision_proposals_writes_ai_text_and_creates_backup(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)
    exec_summary = next(proposal for proposal in proposals if proposal.section_id == "exec_summary")
    revised = replace(exec_summary, proposed_text="Revised proposed summary.")
    append_proposal(revised, "acme", proposal_artifacts.issue_number, programs_root=programs_root)

    artifacts = apply_section_revision_proposals(
        edition_name="acme_weekly",
        accept=("exec_summary",),
        reports_root=reports_root,
        archive_root=archive_root,
    )

    narratives_dir = get_narratives_dir("acme_weekly", proposal_artifacts.issue_number, reports_root=reports_root)
    narrative_path = narratives_dir / narrative_filename_for_section("exec_summary")
    updated_proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)
    updated_exec_summary = next(proposal for proposal in updated_proposals if proposal.section_id == "exec_summary")

    assert artifacts.accepted_count == 1
    assert artifacts.backup_path is not None
    assert artifacts.backup_path.exists()
    assert (artifacts.backup_path / "exec_summary.md").exists()
    assert narrative_path.read_text(encoding="utf-8").strip() == "Revised proposed summary."
    assert updated_exec_summary.status == SectionRevisionStatus.ACCEPTED


def test_apply_section_revision_proposals_accepts_modified_text(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    artifacts = apply_section_revision_proposals(
        edition_name="acme_weekly",
        accept_modified=(("exec_summary", "Edited summary after author review."),),
        reports_root=reports_root,
        archive_root=archive_root,
    )

    narratives_dir = get_narratives_dir("acme_weekly", proposal_artifacts.issue_number, reports_root=reports_root)
    narrative_path = narratives_dir / narrative_filename_for_section("exec_summary")
    updated_proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    updated_exec_summary = next(proposal for proposal in updated_proposals if proposal.section_id == "exec_summary")

    assert artifacts.accepted_count == 1
    assert artifacts.backup_path is not None
    assert artifacts.accepted_sections == ()
    assert artifacts.accepted_modified_sections == ("exec_summary",)
    assert artifacts.applied_sections == ("exec_summary",)
    assert narrative_path.read_text(encoding="utf-8").strip() == "Edited summary after author review."
    assert updated_exec_summary.status == SectionRevisionStatus.ACCEPTED_MODIFIED
    assert updated_exec_summary.accepted_text == "Edited summary after author review."


def test_apply_section_revision_proposals_returns_decision_breakdown(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)
    if len(proposals) == 1:
        append_proposal(
            replace(
                proposals[0],
                proposal_id="apply-breakdown-rejected",
                section_id="ws_networking",
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=programs_root,
        )
        proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)

    artifacts = apply_section_revision_proposals(
        edition_name="acme_weekly",
        accept_modified=((proposals[0].section_id, "Edited summary after author review."),),
        reject=(proposals[1].section_id,),
        reports_root=reports_root,
        archive_root=archive_root,
    )

    assert artifacts.accepted_sections == ()
    assert artifacts.accepted_modified_sections == (proposals[0].section_id,)
    assert artifacts.rejected_sections == (proposals[1].section_id,)
    assert artifacts.applied_sections == (proposals[0].section_id,)


def test_apply_section_revision_proposals_rejects_accept_and_accept_modified_overlap(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    with pytest.raises(Exception, match="both accepted and accepted-modified"):
        apply_section_revision_proposals(
            edition_name="acme_weekly",
            accept=("exec_summary",),
            accept_modified=(("exec_summary", "Edited summary after author review."),),
            reports_root=reports_root,
            archive_root=archive_root,
        )


def test_apply_section_revision_proposals_dry_run_skips_mutations(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    artifacts = apply_section_revision_proposals(
        edition_name="acme_weekly",
        accept=("exec_summary",),
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
    )
    unchanged = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    exec_summary = next(proposal for proposal in unchanged if proposal.section_id == "exec_summary")

    assert artifacts.accepted_count == 1
    assert artifacts.backup_path is None
    assert exec_summary.status == SectionRevisionStatus.PENDING


def test_apply_section_revision_proposals_refuses_hash_mismatch(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    narratives_dir = get_narratives_dir("acme_weekly", proposal_artifacts.issue_number, reports_root=reports_root)
    narrative_path = narratives_dir / "exec_summary.md"
    narrative_path.write_text("Manually changed summary.\n", encoding="utf-8")

    with pytest.raises(Exception, match="narrative has changed since proposal was generated"):
        apply_section_revision_proposals(
            edition_name="acme_weekly",
            accept=("exec_summary",),
            reports_root=reports_root,
            archive_root=archive_root,
        )


def test_apply_proposals_cli_accept_all_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    monkeypatch.setattr("src.commands.apply_proposals.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.apply_proposals.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["apply-proposals", "--edition", "acme_weekly", "--accept-all"], input="n\n")

    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")

    assert result.exit_code == 1
    assert "Accept all" in result.output
    assert "Confidence mix:" in result.output
    assert "Apply cancelled." in result.output
    assert all(proposal.status == SectionRevisionStatus.PENDING for proposal in proposals)


def test_apply_proposals_cli_accept_all_yes_skips_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    monkeypatch.setattr("src.commands.apply_proposals.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.apply_proposals.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        apply_proposals.typer,
        "confirm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("confirm should not be called")),
    )

    result = runner.invoke(app, ["apply-proposals", "--edition", "acme_weekly", "--accept-all", "--yes"])

    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")

    assert result.exit_code == 0
    assert "Applied " in result.output
    assert "accept:" in result.output
    assert all(proposal.status == SectionRevisionStatus.ACCEPTED for proposal in proposals)


def test_apply_proposals_cli_prints_decision_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)
    if len(proposals) == 1:
        append_proposal(
            replace(
                proposals[0],
                proposal_id="cli-breakdown-rejected",
                section_id="ws_networking",
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=programs_root,
        )
        proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)

    monkeypatch.setattr("src.commands.apply_proposals.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.apply_proposals.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(
        app,
        [
            "apply-proposals",
            "--edition",
            "acme_weekly",
            "--accept-modified",
            f"{proposals[0].section_id}=Edited summary after author review.",
            "--reject",
            proposals[1].section_id,
        ],
    )

    assert result.exit_code == 0
    assert "accept-modified: " in result.output
    assert proposals[0].section_id in result.output
    assert "reject: " in result.output
    assert proposals[1].section_id in result.output


def test_apply_section_revision_proposals_rejects_ai_text_that_violates_ban_list(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)
    exec_summary = next(proposal for proposal in proposals if proposal.section_id == "exec_summary")
    append_proposal(
        replace(exec_summary, proposed_text="This week we improved coverage due to better follow-up."),
        "acme",
        proposal_artifacts.issue_number,
        programs_root=programs_root,
    )

    with pytest.raises(Exception, match="violates the editorial ban-list"):
        apply_section_revision_proposals(
            edition_name="acme_weekly",
            accept=("exec_summary",),
            reports_root=reports_root,
            archive_root=archive_root,
        )


def test_apply_section_revision_proposals_rejects_modified_text_that_violates_ban_list(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    with pytest.raises(Exception, match="violates the editorial ban-list"):
        apply_section_revision_proposals(
            edition_name="acme_weekly",
            accept_modified=(("exec_summary", "This week we escalated because of missing telemetry."),),
            reports_root=reports_root,
            archive_root=archive_root,
        )

    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    exec_summary = next(proposal for proposal in proposals if proposal.section_id == "exec_summary")
    assert exec_summary.status == SectionRevisionStatus.PENDING


def test_prompt_for_section_revision_actions_collects_accept_reject_and_skip(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    if len(proposals) == 1:
        append_proposal(
            replace(
                proposals[0],
                proposal_id="interactive-second-proposal",
                section_id="ws_networking",
                source_hash=proposals[0].source_hash,
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=reports_root.parent / "programs",
        )
        proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    decisions = iter(("a", "r", "s"))
    echoed: list[str] = []

    artifacts = prompt_for_section_revision_actions(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        prompt_fn=lambda *_args, **_kwargs: next(decisions),
        echo_fn=echoed.append,
    )

    assert artifacts.issue_number == proposal_artifacts.issue_number
    assert artifacts.accept == (proposals[0].section_id,)
    assert artifacts.accept_modified == ()
    assert artifacts.reject == (proposals[1].section_id,)
    assert any(line.startswith("Section: ") for line in echoed)
    assert any(line.startswith("Proposed: ") for line in echoed)
    assert any(line.startswith("Confidence: ") for line in echoed)
    assert any(line.startswith("ADO Delta: ") for line in echoed)


def test_prompt_for_section_revision_actions_reprompts_on_invalid_choice(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    decisions = iter(("bad", "a", "s", "s"))
    echoed: list[str] = []

    artifacts = prompt_for_section_revision_actions(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        prompt_fn=lambda *_args, **_kwargs: next(decisions),
        echo_fn=echoed.append,
    )

    assert len(artifacts.accept) == 1
    assert artifacts.accept_modified == ()
    assert artifacts.reject == ()
    assert "Enter 'a', 'm', 'r', or 's'." in echoed


def test_prompt_for_section_revision_actions_collects_modify_decision(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    decisions = iter(("m", "Interactive edited summary.", "s"))

    artifacts = prompt_for_section_revision_actions(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        prompt_fn=lambda *_args, **_kwargs: next(decisions),
        echo_fn=lambda *_args, **_kwargs: None,
    )

    assert artifacts.accept == ()
    assert artifacts.accept_modified == ((proposals[0].section_id, "Interactive edited summary."),)
    assert artifacts.reject == ()


def test_prompt_for_section_revision_actions_surfaces_signal_context(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)
    append_proposal(
        replace(
            proposals[0],
            proposal_id="interactive-evidence-context",
            section_id="ws_networking",
            evidence_brief=replace(
                proposals[0].evidence_brief,
                top_signals=("sig-001", "sig-002"),
                kpi_summary="Healthy fleet is 97%.",
                stale_claims=("Prior latency claim is stale.",),
            ),
        ),
        "acme",
        proposal_artifacts.issue_number,
        programs_root=programs_root,
    )
    echoed: list[str] = []
    decisions = iter(("s", "s"))

    prompt_for_section_revision_actions(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        prompt_fn=lambda *_args, **_kwargs: next(decisions),
        echo_fn=echoed.append,
    )

    assert any(line == "Top Signals: sig-001, sig-002" for line in echoed)
    assert any(line == "KPI Summary: Healthy fleet is 97%." for line in echoed)
    assert any(line == "Stale Claims: Prior latency claim is stale." for line in echoed)


def test_preview_section_revision_proposals_undo_picks_latest_backup(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    backup_root = reports_root.parent / "programs" / "acme" / "backups" / "narratives" / f"issue_{proposal_artifacts.issue_number:03d}"
    older_backup = backup_root / "20260517T010000Z"
    newer_backup = backup_root / "20260517T020000Z"
    older_backup.mkdir(parents=True, exist_ok=True)
    newer_backup.mkdir(parents=True, exist_ok=True)
    (older_backup / "exec_summary.md").write_text("Older backup.\n", encoding="utf-8")
    (newer_backup / "exec_summary.md").write_text("Newer backup.\n", encoding="utf-8")

    artifacts = preview_section_revision_proposals_undo(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
    )

    assert artifacts.issue_number == proposal_artifacts.issue_number
    assert artifacts.restored_backup_path == newer_backup
    assert artifacts.available_backups == (older_backup, newer_backup)


def test_undo_section_revision_proposals_restores_latest_backup(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    narratives_dir = get_narratives_dir("acme_weekly", proposal_artifacts.issue_number, reports_root=reports_root)
    backup_root = reports_root.parent / "programs" / "acme" / "backups" / "narratives" / f"issue_{proposal_artifacts.issue_number:03d}"
    latest_backup = backup_root / "20260517T020000Z"
    latest_backup.mkdir(parents=True, exist_ok=True)
    (latest_backup / "exec_summary.md").write_text("Recovered summary.\n", encoding="utf-8")
    (latest_backup / "ws_networking.md").write_text("Recovered workstream.\n", encoding="utf-8")
    (narratives_dir / "exec_summary.md").write_text("Current changed summary.\n", encoding="utf-8")
    (narratives_dir / "ws_networking.md").write_text("Current changed workstream.\n", encoding="utf-8")

    artifacts = undo_section_revision_proposals(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
    )

    assert artifacts.restored_backup_path == latest_backup
    assert (narratives_dir / "exec_summary.md").read_text(encoding="utf-8") == "Recovered summary.\n"
    assert (narratives_dir / "ws_networking.md").read_text(encoding="utf-8") == "Recovered workstream.\n"

