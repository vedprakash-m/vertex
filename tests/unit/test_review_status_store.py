from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.models import ReviewSection, ReviewState, ReviewStatus
from src.core.review_status_store import archive_review_status, load_review_status, reset_review_status, save_review_status


EDITION_NAME = "acme_weekly"


def test_save_and_load_review_status_round_trip(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    review_status = ReviewStatus(
        issue_number=78,
        sections=(
            ReviewSection(
                section_id="exec_summary",
                state=ReviewState.SENT,
                reviewer="lead@example.com",
                note="Please verify wording",
                updated_at=datetime(2026, 5, 5, 9, 30, tzinfo=timezone.utc),
            ),
        ),
    )

    save_review_status(EDITION_NAME, review_status, reports_root)
    loaded = load_review_status(EDITION_NAME, reports_root)

    assert loaded == review_status


def test_reset_and_archive_review_status(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    review_status = reset_review_status(
        edition=EDITION_NAME,
        issue_number=79,
        section_ids=("exec_summary", "ws:deployment"),
        reports_root=reports_root,
    )
    archive_path = archive_review_status(
        edition=EDITION_NAME,
        issue_number=79,
        review_status=review_status,
        archive_root=archive_root,
    )

    assert all(section.state == ReviewState.PENDING for section in review_status.sections)
    assert archive_path == archive_root / EDITION_NAME / "review" / "issue_079.review.yaml"
    assert "state: pending" in archive_path.read_text(encoding="utf-8")


def test_load_review_status_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: '78'\nsections: []\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_missing_issue_number(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "sections: []\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_non_string_updated_at(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: exec_summary\n    state: pending\n    reviewer: lead@example.com\n    note: ok\n    updated_at: 123\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="updated_at must be a string"):
        load_review_status(EDITION_NAME, reports_root)


def test_save_review_status_rejects_naive_updated_at(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    review_status = ReviewStatus(
        issue_number=78,
        sections=(
            ReviewSection(
                section_id="exec_summary",
                state=ReviewState.SENT,
                reviewer="lead@example.com",
                note="Please verify wording",
                updated_at=datetime(2026, 5, 5, 9, 30),
            ),
        ),
    )

    with pytest.raises(ValueError, match="updated_at must include timezone information"):
        save_review_status(EDITION_NAME, review_status, reports_root)


def test_load_review_status_rejects_naive_updated_at(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: exec_summary\n    state: pending\n    reviewer: lead@example.com\n    note: ok\n    updated_at: 2026-05-05T09:30:00\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="updated_at must include timezone information"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_non_string_reviewer(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: exec_summary\n    state: pending\n    reviewer: 42\n    note: ok\n    updated_at: 2026-05-05T09:30:00+00:00\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="reviewer must be a string"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_non_string_note(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: exec_summary\n    state: pending\n    reviewer: lead@example.com\n    note: 42\n    updated_at: 2026-05-05T09:30:00+00:00\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="note must be a string"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_non_string_manifest_id(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: exec_summary\n    state: pending\n    reviewer: lead@example.com\n    note: ok\n    updated_at: '2026-05-05T09:30:00+00:00'\n    manifest_id: 123\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="manifest_id must be a string"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_non_list_sections(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections: exec_summary\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="sections must be a list"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_missing_sections(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="sections must be a list"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_non_string_section_id(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: 123\n    state: pending\n    reviewer: lead@example.com\n    note: ok\n    updated_at: 2026-05-05T09:30:00+00:00\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="section_id must be a string"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_non_string_state(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: exec_summary\n    state: 1\n    reviewer: lead@example.com\n    note: ok\n    updated_at: 2026-05-05T09:30:00+00:00\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="state must be a string"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_missing_section_id(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - state: pending\n    reviewer: lead@example.com\n    note: ok\n    updated_at: 2026-05-05T09:30:00+00:00\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="section_id must be a string"):
        load_review_status(EDITION_NAME, reports_root)


def test_load_review_status_rejects_missing_state(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    status_path = reports_root / EDITION_NAME / "review_status.yaml"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        "issue_number: 78\nsections:\n  - section_id: exec_summary\n    reviewer: lead@example.com\n    note: ok\n    updated_at: 2026-05-05T09:30:00+00:00\n    manifest_id: manifest-1\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="state must be a string"):
        load_review_status(EDITION_NAME, reports_root)
