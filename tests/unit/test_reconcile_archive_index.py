from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.archive_store import verify_archive_integrity
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.snapshot_store import write_confirmed


def _load_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "reconcile_archive_index.py"
    spec = importlib.util.spec_from_file_location("reconcile_archive_index", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("reconcile_archive_index", module)
    spec.loader.exec_module(module)
    return module


def _build_snapshot(issue_number: int) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 6, issue_number, 8, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 6, issue_number, 7, 30, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=1000 + issue_number,
                type="Feature",
                title=f"Issue {issue_number:03d}",
                state="Active",
                assigned_to="owner",
                area_path="One\\Demo",
                target_date=date(2026, 6, 30),
                risk_level=RiskLevel.LOW,
                tags=["demo"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Demo",
                name="Execution",
                risk=RiskLevel.LOW,
                prior_risk=None,
                item_count=1,
                ado_query_url="https://example.test/query",
            ),
        ),
    )


def _seed_canonical_issue(program_root: Path, edition: str, issue_number: int) -> None:
    archive_root = program_root / "archive"
    edition_root = archive_root / edition
    write_confirmed(
        edition=edition,
        issue_number=issue_number,
        snapshot=_build_snapshot(issue_number),
        archive_root=archive_root,
        acquire_lock=False,
    )
    (edition_root / "html").mkdir(parents=True, exist_ok=True)
    (edition_root / "md").mkdir(parents=True, exist_ok=True)
    (edition_root / "manifests").mkdir(parents=True, exist_ok=True)
    (edition_root / "eml").mkdir(parents=True, exist_ok=True)
    (edition_root / "html" / f"issue_{issue_number:03d}.html").write_text("<html/>", encoding="utf-8")
    (edition_root / "md" / f"issue_{issue_number:03d}.md").write_text("# Demo", encoding="utf-8")
    (edition_root / "manifests" / f"issue_{issue_number:03d}.json").write_text(
        json.dumps(
            {
                "issue_number": issue_number,
                "edition": edition,
                "started_at": datetime(2026, 6, issue_number, 7, 0, tzinfo=timezone.utc).isoformat(),
                "ended_at": datetime(2026, 6, issue_number, 8, 0, tzinfo=timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (edition_root / "eml" / f"issue_{issue_number:03d}.eml").write_text("From: demo", encoding="utf-8")


def test_apply_readd_restores_missing_index_entry(tmp_path: Path) -> None:
    module = _load_module()
    module.REPO_ROOT = tmp_path
    program_root = tmp_path / "programs" / "demo"
    edition = "demo_weekly"
    issue_number = 12
    _seed_canonical_issue(program_root, edition, issue_number)

    index_path = program_root / "archive" / edition / "index.json"
    index_path.write_text(json.dumps({"schema_version": "1.0", "edition": edition, "issues": []}), encoding="utf-8")

    result = module.main(
        [
            "--program",
            "demo",
            "--edition",
            edition,
            "--issue",
            str(issue_number),
            "--strategy",
            "readd",
            "--apply",
        ]
    )

    assert result == 0
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert [entry["issue_number"] for entry in payload["issues"]] == [issue_number]
    assert payload["issues"][0]["snapshot_path"].endswith(f"issue_{issue_number:03d}.snapshot.json")


def test_apply_drop_with_wipe_removes_index_and_files(tmp_path: Path) -> None:
    module = _load_module()
    module.REPO_ROOT = tmp_path
    program_root = tmp_path / "programs" / "demo"
    edition = "demo_weekly"
    issue_number = 13
    _seed_canonical_issue(program_root, edition, issue_number)

    index_path = program_root / "archive" / edition / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": edition,
                "issues": [
                    {
                        "issue_number": issue_number,
                        "generated_at": datetime(2026, 6, issue_number, 8, 0, tzinfo=timezone.utc).isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(program_root / "archive" / edition / "snapshots" / f"issue_{issue_number:03d}.snapshot.json"),
                        "html_path": str(program_root / "archive" / edition / "html" / f"issue_{issue_number:03d}.html"),
                        "md_path": str(program_root / "archive" / edition / "md" / f"issue_{issue_number:03d}.md"),
                        "manifest_path": str(program_root / "archive" / edition / "manifests" / f"issue_{issue_number:03d}.json"),
                        "eml_path": str(program_root / "archive" / edition / "eml" / f"issue_{issue_number:03d}.eml"),
                        "metadata": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = module.main(
        [
            "--program",
            "demo",
            "--edition",
            edition,
            "--issue",
            str(issue_number),
            "--strategy",
            "drop",
            "--apply",
            "--wipe",
        ]
    )

    assert result == 0
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["issues"] == []
    assert not (program_root / "archive" / edition / "snapshots" / f"issue_{issue_number:03d}.snapshot.json").exists()
    assert verify_archive_integrity(edition, archive_root=program_root / "archive").ok
