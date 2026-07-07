from __future__ import annotations

from pathlib import Path

from src.commands.doctor_checks.consistency_checks import latest_archived_review_issue


def test_latest_archived_review_issue_ignores_non_matching_files(tmp_path: Path) -> None:
    review_dir = tmp_path / "archive" / "demo_weekly" / "review"
    review_dir.mkdir(parents=True)
    (review_dir / "issue_002.review.yaml").write_text("issue_number: 2\n", encoding="utf-8")
    (review_dir / "issue_010.review.yaml").write_text("issue_number: 10\n", encoding="utf-8")
    (review_dir / "notes.yaml").write_text("ignored: true\n", encoding="utf-8")

    issue_number = latest_archived_review_issue("demo_weekly", archive_root=tmp_path / "archive")

    assert issue_number == 10
