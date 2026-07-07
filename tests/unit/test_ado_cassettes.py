from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.commands.report import generate_report_draft
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.support.ado_cassettes import load_cassette_payload, load_cassette_work_items


FROZEN_NOW = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
EDITION_NAME = "acme_weekly"


def test_cold_start_cassette_generates_draft(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, _ = _copy_temp_roots(repo_root, tmp_path)
    payload = load_cassette_payload("cold_start")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert len(artifacts.report.items) == len(payload["work_items"])
    assert "acme-adventure-xio-100-ramp-readiness" in artifacts.html_body
    assert artifacts.title == "Program Hygiene | Issue 1 | 2026-05-05"



def test_partial_empty_cassette_surfaces_zero_match_dimensions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, _ = _copy_temp_roots(repo_root, tmp_path)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("partial_empty", timestamp),
        open_browser=False,
    )

    assert len(artifacts.report.items) == 2
    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert artifacts.quality_matrix_json_path is not None and artifacts.quality_matrix_json_path.exists()

    quality_matrix = json.loads(artifacts.quality_matrix_json_path.read_text(encoding="utf-8"))

    assert any(
        any("No items matched the current slice contract" in issue for issue in slice_row.get("issues", []))
        for slice_row in quality_matrix["slices"]
    )



def test_large_cassette_handles_500_plus_items(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, _ = _copy_temp_roots(repo_root, tmp_path)
    items, ado_calls = load_cassette_work_items("large", FROZEN_NOW)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: (items, ado_calls),
        open_browser=False,
    )

    assert len(items) >= 500
    assert len(artifacts.snapshot.items) >= 500
    assert artifacts.manifest.ado_calls == 1
    assert artifacts.html_path is not None and artifacts.html_path.exists()



def _copy_temp_roots(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    return reports_root, archive_root, (tmp_path / "programs" / "acme" / "publications")

