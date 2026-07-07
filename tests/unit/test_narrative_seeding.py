from __future__ import annotations

import json
from datetime import datetime, timezone

from src.commands.report import generate_report_draft
from src.core.trusted_baseline_store import advance_trusted_baseline
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_report import _sample_items


EDITION_NAME = "acme_weekly"


def test_generate_report_draft_seeds_narratives_from_trusted_prior_issue(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    prior_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_001"
    prior_dir.mkdir(parents=True, exist_ok=True)
    (prior_dir / "exec_summary.md").write_text("Prior exec summary text.", encoding="utf-8")
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    seeded_exec_summary = (
        reports_root.parent / "programs" / "acme" / "narratives" / "issue_002" / "exec_summary.md"
    ).read_text(encoding="utf-8")
    manifest_payload = json.loads(
        (
            reports_root.parent / "programs" / "acme" / "narratives" / "issue_002" / ".seeding_manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert "<!-- SEEDED from Issue 001" in seeded_exec_summary
    assert "Prior exec summary text." in seeded_exec_summary
    assert artifacts.report.exec_summary_text == "Prior exec summary text."
    assert manifest_payload["source_issue"] == 1
    assert manifest_payload["source_path"] == "program_local"
    assert manifest_payload["files"]["exec_summary.md"]["source_hash"].startswith("sha256:")


def test_generate_report_draft_reseed_replaces_seedable_narratives(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    prior_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_001"
    prior_dir.mkdir(parents=True, exist_ok=True)
    (prior_dir / "exec_summary.md").write_text("Updated seeded exec summary.", encoding="utf-8")
    target_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_002"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "exec_summary.md").write_text("<!-- SCAFFOLD -->\n\nOld scaffold body.", encoding="utf-8")
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        reseed=True,
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    seeded_exec_summary = (target_dir / "exec_summary.md").read_text(encoding="utf-8")

    assert "Updated seeded exec summary." in seeded_exec_summary
    assert "Old scaffold body." not in seeded_exec_summary
    assert artifacts.report.exec_summary_text == "Updated seeded exec summary."


def test_generate_report_draft_no_seed_skips_trusted_baseline_seeding(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    prior_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_001"
    prior_dir.mkdir(parents=True, exist_ok=True)
    (prior_dir / "exec_summary.md").write_text("Prior exec summary text.", encoding="utf-8")
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        no_seed=True,
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    target_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_002"
    rendered_exec_summary = (target_dir / "exec_summary.md").read_text(encoding="utf-8")

    assert "<!-- SEEDED from Issue 001" not in rendered_exec_summary
    assert "Prior exec summary text." not in rendered_exec_summary
    assert not (target_dir / ".seeding_manifest.json").exists()
    assert artifacts.report.exec_summary_text != "Prior exec summary text."


def test_generate_report_draft_respects_no_seed_sentinel(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    prior_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_001"
    prior_dir.mkdir(parents=True, exist_ok=True)
    (prior_dir / "exec_summary.md").write_text("Prior exec summary text.", encoding="utf-8")
    target_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_002"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / ".no-seed").write_text("", encoding="utf-8")
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    rendered_exec_summary = (target_dir / "exec_summary.md").read_text(encoding="utf-8")

    assert "<!-- SEEDED from Issue 001" not in rendered_exec_summary
    assert not (target_dir / ".seeding_manifest.json").exists()


def test_generate_report_draft_reconciles_existing_next_issue_from_published_sidecar(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    program_archive_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME
    source_archive_dir = program_archive_dir / "narratives" / "issue_001"
    source_archive_dir.mkdir(parents=True, exist_ok=True)
    (source_archive_dir / "exec_summary.md").write_text("Confirmed summary text.\n", encoding="utf-8")
    published_dir = program_archive_dir / "published_narratives" / "issue_001"
    published_dir.mkdir(parents=True, exist_ok=True)
    (published_dir / "exec_summary.md").write_text("Published summary text.\n", encoding="utf-8")

    target_dir = reports_root.parent / "programs" / "acme" / "narratives" / "issue_002"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "exec_summary.md").write_text("Confirmed summary text.\n", encoding="utf-8")

    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    seeded_exec_summary = (target_dir / "exec_summary.md").read_text(encoding="utf-8")
    manifest_payload = json.loads((target_dir / ".seeding_manifest.json").read_text(encoding="utf-8"))

    assert "Published summary text." in seeded_exec_summary
    assert artifacts.report.exec_summary_text == "Published summary text."
    assert manifest_payload["source_path"] == "published_archive_preferred"
    assert manifest_payload["files"]["exec_summary.md"]["source_path"] == "published_archive"

