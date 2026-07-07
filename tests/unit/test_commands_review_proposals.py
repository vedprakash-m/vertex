from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

import pytest
from typer.testing import CliRunner

from dataclasses import replace
import src.commands.review_proposals as review_proposals
from cli import app
from src.commands.propose import generate_section_revision_proposals
from src.commands.review_proposals import generate_review_proposals
from src.core.dependency_graph import DependencyType
from src.core.dependency_scout import DependencyProposal, save_dependency_proposals
from src.core.journal import append_signal
from src.core.section_proposal_store import append_proposal
from src.core.section_proposal_store import load_proposals, update_proposal_status
from src.core.models import Confidence
from src.core.models_v2 import SectionRevisionStatus, Signal
from tests.unit.test_commands_propose import _empty_work_item_loader
from tests.unit.test_commands_report import _seed_v2_report_layout


runner = CliRunner()


def test_generate_review_proposals_writes_pending_review_html(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert artifacts.html_path.exists()
    assert artifacts.proposal_count >= 1
    assert "No AI proposal (evidence brief below)" in html
    assert "Pending Review" in html
    assert "Rendered Pending Proposals" in html
    assert "Pending Proposals" in html
    assert "Generated" in html
    assert "Pending Confidence Mix" in html
    assert "high=" in html or "medium=" in html or "low=" in html
    assert "vertex apply-proposals --edition acme_weekly --accept-all" in html
    assert "vertex apply-proposals --edition acme_weekly --interactive" in html
    assert "vertex apply-proposals --edition acme_weekly --accept " in html
    assert "vertex apply-proposals --edition acme_weekly --accept-modified " in html
    assert "<h3>Confidence</h3>" in html
    assert "Executive Summary" in html


def test_generate_review_proposals_links_top_signals_to_detail_cards(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    append_signal(
        Signal(
            id="sig-review-001",
            timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=(),
            text="Approved signal detail for the proposal review pane.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata={"author": "test"},
        ),
        programs_root=programs_root,
        partition_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
    )
    generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", 1, programs_root=programs_root)
    exec_summary = next(proposal for proposal in proposals if proposal.section_id == "exec_summary")
    update_proposal_status(
        exec_summary.proposal_id,
        SectionRevisionStatus.REJECTED,
        rejection_reason="replace evidence for test",
        program_id="acme",
        issue_number=1,
        programs_root=programs_root,
    )

    from dataclasses import replace
    from src.core.section_proposal_store import append_proposal

    append_proposal(
        replace(
            exec_summary,
            proposal_id="review-signal-linked-proposal",
            status=SectionRevisionStatus.PENDING,
            evidence_brief=replace(exec_summary.evidence_brief, top_signals=("sig-review-001",)),
        ),
        "acme",
        1,
        programs_root=programs_root,
    )

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert 'href="#signal-sig-review-001"' in html
    assert 'id="signal-sig-review-001"' in html
    assert "Approved signal detail for the proposal review pane." in html
    assert "Confidence: high" in html
    assert "sig-review-001</a> <span class=\"confidence-chip\">high</span>" in html


def test_generate_review_proposals_surfaces_resolved_decision_summary(repo_root: Path, tmp_path: Path) -> None:
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
    if len(proposals) < 4:
        append_proposal(
            replace(
                proposals[0],
                proposal_id="review-summary-accepted-modified",
                section_id="ws_storage",
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=reports_root.parent / "programs",
        )
        append_proposal(
            replace(
                proposals[0],
                proposal_id="review-summary-rejected",
                section_id="ws_platform",
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=reports_root.parent / "programs",
        )
        append_proposal(
            replace(
                proposals[0],
                proposal_id="review-summary-pending",
                section_id="ws_customer",
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=reports_root.parent / "programs",
        )
        proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    update_proposal_status(
        proposals[0].proposal_id,
        SectionRevisionStatus.ACCEPTED,
        resolved_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
        program_id="acme",
        issue_number=proposal_artifacts.issue_number,
        programs_root=reports_root.parent / "programs",
    )
    update_proposal_status(
        proposals[1].proposal_id,
        SectionRevisionStatus.ACCEPTED_MODIFIED,
        accepted_text="Edited after reviewer pass.",
        resolved_at=datetime(2026, 5, 17, 10, 5, tzinfo=timezone.utc),
        program_id="acme",
        issue_number=proposal_artifacts.issue_number,
        programs_root=reports_root.parent / "programs",
    )
    update_proposal_status(
        proposals[2].proposal_id,
        SectionRevisionStatus.REJECTED,
        rejection_reason="Not needed after discussion.",
        resolved_at=datetime(2026, 5, 17, 10, 10, tzinfo=timezone.utc),
        program_id="acme",
        issue_number=proposal_artifacts.issue_number,
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Accepted" in html
    assert "Accepted With Edits" in html
    assert "Recent Decisions" in html
    assert "Edited after reviewer pass." in html
    assert "Status: Accepted With Edits" in html


def test_generate_review_proposals_surfaces_pending_dependency_proposals(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    append_signal(
        Signal(
            id="sig-review-dependency-001",
            timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI#101", "WI#202"),
            text="Approved dependency signal for the proposal review pane.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata={"author": "test"},
        ),
        programs_root=programs_root,
        partition_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
    )
    generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_empty_work_item_loader,
    )
    save_dependency_proposals(
        "acme",
        (
            DependencyProposal(
                id="dep-proposal-review-001",
                program_id="acme",
                from_workstream_id="acme",
                to_workstream_id="fabrikam",
                from_item_id=101,
                to_item_id=202,
                from_item_title="UD chunking",
                to_item_title="Fabrikam buildouts",
                suggested_dependency_type=DependencyType.SHARES_RESOURCE,
                rationale="UD chunking and Fabrikam buildouts co-moved in approved evidence.",
                evidence_refs=("sig-review-dependency-001",),
                detection_method="co_mention",
                occurrence_count=3,
                first_seen_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
                confidence=Confidence.HIGH,
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert "Dependency Proposals" in html
    assert "dep-proposal-review-001" in html
    assert "vertex dependencies accept --program acme --id dep-proposal-review-001" in html
    assert "vertex dependencies dismiss --program acme --id dep-proposal-review-001" in html
    assert "Approved dependency signal for the proposal review pane." in html
    assert "Confidence: high" in html
    assert "sig-review-dependency-001</a> <span class=\"confidence-chip\">high</span>" in html


def test_generate_review_proposals_renders_dependency_only_review_when_section_proposals_are_absent(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    append_signal(
        Signal(
            id="sig-review-dependency-only-001",
            timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI#301", "WI#401"),
            text="Dependency-only review signal.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"author": "test"},
        ),
        programs_root=programs_root,
        partition_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
    )
    save_dependency_proposals(
        "acme",
        (
            DependencyProposal(
                id="dep-proposal-review-only-001",
                program_id="acme",
                from_workstream_id="acme",
                to_workstream_id="fabrikam",
                from_item_id=301,
                to_item_id=401,
                from_item_title="Platform readiness",
                to_item_title="Fleet rollout",
                suggested_dependency_type=DependencyType.INFORMS,
                rationale="Platform readiness and fleet rollout share repeated blocker language.",
                evidence_refs=("sig-review-dependency-only-001",),
                detection_method="comment_language",
                occurrence_count=2,
                first_seen_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
                confidence=Confidence.MEDIUM,
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert artifacts.proposal_count == 1
    assert "Dependency Proposals" in html
    assert "dep-proposal-review-only-001" in html
    assert "Platform readiness" in html
    assert "Fleet rollout" in html
    assert "vertex apply-proposals --edition acme_weekly --accept-all" not in html


def test_generate_review_proposals_filters_to_single_section(repo_root: Path, tmp_path: Path) -> None:
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
    target_section = proposals[0].section_id
    other_sections = {proposal.section_id for proposal in proposals if proposal.section_id != target_section}

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        section_id=target_section,
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert artifacts.proposal_count == 1
    assert f"filtered to {target_section}" in html
    assert target_section in html
    for other_section in other_sections:
        assert other_section not in html


def test_generate_review_proposals_rejects_unknown_section_filter(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    with pytest.raises(Exception, match="No pending proposal found for section 'missing_section'"):
        generate_review_proposals(
            edition_name="acme_weekly",
            section_id="missing_section",
            reports_root=reports_root,
            archive_root=archive_root,
            open_browser=False,
        )


def test_generate_review_proposals_requires_pending_proposals(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    proposal_artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    for proposal in load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs"):
        update_proposal_status(
            proposal.proposal_id,
            SectionRevisionStatus.REJECTED,
            rejection_reason="already reviewed",
            program_id="acme",
            issue_number=proposal_artifacts.issue_number,
            programs_root=reports_root.parent / "programs",
        )

    with pytest.raises(Exception, match="No pending proposals found"):
        generate_review_proposals(
            edition_name="acme_weekly",
            reports_root=reports_root,
            archive_root=archive_root,
            open_browser=False,
        )


def test_generate_review_proposals_resolved_only_renders_history_without_pending(repo_root: Path, tmp_path: Path) -> None:
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
                proposal_id="review-history-rejected",
                section_id="ws_storage",
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=reports_root.parent / "programs",
        )
        proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    for index, proposal in enumerate(proposals):
        update_proposal_status(
            proposal.proposal_id,
            SectionRevisionStatus.ACCEPTED_MODIFIED if index == 0 else SectionRevisionStatus.REJECTED,
            accepted_text="Final edited history text." if index == 0 else None,
            rejection_reason="already reviewed" if index != 0 else None,
            resolved_at=datetime(2026, 5, 17, 11, index, tzinfo=timezone.utc),
            program_id="acme",
            issue_number=proposal_artifacts.issue_number,
            programs_root=reports_root.parent / "programs",
        )

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        resolved_only=True,
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert artifacts.proposal_count == len(proposals)
    assert "proposal history pane" in html
    assert "Resolved History" in html
    assert "Rendered Resolved Proposals" in html
    assert "Remaining Pending Proposals" in html
    assert "Resolved Decision Mix" in html
    assert "accepted_modified=1" in html
    assert "rejected=1" in html
    assert "All resolved sections" in html
    assert "Final edited history text." in html
    assert "Rejection Reason: already reviewed" in html
    assert "Accepted With Edits" in html
    assert "vertex apply-proposals --edition acme_weekly --accept-all" not in html


def test_generate_review_proposals_resolved_only_section_filter_scopes_recent_decisions(repo_root: Path, tmp_path: Path) -> None:
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
                proposal_id="review-filtered-history-other",
                section_id="ws_storage",
            ),
            "acme",
            proposal_artifacts.issue_number,
            programs_root=programs_root,
        )
        proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=programs_root)

    update_proposal_status(
        proposals[0].proposal_id,
        SectionRevisionStatus.ACCEPTED_MODIFIED,
        accepted_text="Filtered section decision.",
        resolved_at=datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
        program_id="acme",
        issue_number=proposal_artifacts.issue_number,
        programs_root=programs_root,
    )
    update_proposal_status(
        proposals[1].proposal_id,
        SectionRevisionStatus.REJECTED,
        rejection_reason="Other section decision.",
        resolved_at=datetime(2026, 5, 17, 12, 5, tzinfo=timezone.utc),
        program_id="acme",
        issue_number=proposal_artifacts.issue_number,
        programs_root=programs_root,
    )

    artifacts = generate_review_proposals(
        edition_name="acme_weekly",
        resolved_only=True,
        section_id=proposals[0].section_id,
        reports_root=reports_root,
        archive_root=archive_root,
        open_browser=False,
    )

    html = artifacts.html_path.read_text(encoding="utf-8")

    assert artifacts.proposal_count == 1
    assert "Filtered section decision." in html
    assert "Other section decision." not in html
    assert re.search(r"<h2>Accepted With Edits</h2>\s*<p>1</p>", html)
    assert re.search(r"<h2>Rejected</h2>\s*<p>0</p>", html)


def test_review_proposals_cli_reports_pending_mode_and_section_filter(
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
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")

    monkeypatch.setattr("src.commands.review_proposals.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_proposals.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(
        app,
        [
            "review-proposals",
            "--edition",
            "acme_weekly",
            "--section",
            proposals[0].section_id,
            "--no-open",
        ],
    )

    assert result.exit_code == 0
    assert "Mode: pending review" in result.output
    assert f"Section filter: {proposals[0].section_id}" in result.output
    assert "Rendered pending proposals: 1" in result.output


def test_review_proposals_cli_reports_resolved_history_mode(
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
    proposals = load_proposals("acme", proposal_artifacts.issue_number, programs_root=reports_root.parent / "programs")
    update_proposal_status(
        proposals[0].proposal_id,
        SectionRevisionStatus.ACCEPTED_MODIFIED,
        accepted_text="Resolved history text.",
        program_id="acme",
        issue_number=proposal_artifacts.issue_number,
        programs_root=reports_root.parent / "programs",
    )

    monkeypatch.setattr("src.commands.review_proposals.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_proposals.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(
        app,
        [
            "review-proposals",
            "--edition",
            "acme_weekly",
            "--resolved-only",
            "--no-open",
        ],
    )

    assert result.exit_code == 0
    assert "Mode: resolved history" in result.output
    assert "Rendered resolved proposal history entries: 1" in result.output

