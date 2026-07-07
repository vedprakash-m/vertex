from __future__ import annotations

import difflib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.commands.freshness import generate_freshness_report
from src.core.circuit_breaker import CircuitBreaker
from src.core.models import EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.snapshot_store import get_archive_root, write_confirmed
from tests.support.report_test_setup import reset_overrides_to_seed_state, stage_v2_report_workspace


GOLDEN_DIR = Path(__file__).resolve().parent / "snapshots"
FROZEN_NOW = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
EDITION_NAME = "acme_weekly"


class GoldenFileMismatchError(AssertionError):
    pass


def _load_golden(name: str) -> str | None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if golden_path.exists():
        return golden_path.read_text(encoding="utf-8")
    return None


def _save_golden(name: str, content: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    (GOLDEN_DIR / f"{name}.golden").write_text(content, encoding="utf-8")


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden = _load_golden(name)
    if update or golden is None:
        _save_golden(name, actual)
        if golden is None:
            pytest.skip(f"Created new golden file: {name}.golden")
        return

    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise GoldenFileMismatchError(
            f"Output does not match golden file: {name}.golden\n\nDiff:\n{diff}"
        )


def test_freshness_stale_snapshot_markdown(update_golden: bool, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    _seed_confirmed_snapshot(archive_root)

    breaker = CircuitBreaker(state_path=(tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / ".ado_breaker.json")
    breaker.record_failure(now=FROZEN_NOW - timedelta(minutes=3))
    breaker.record_failure(now=FROZEN_NOW - timedelta(minutes=2))
    breaker.record_failure(now=FROZEN_NOW - timedelta(minutes=1))

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=FROZEN_NOW,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_unexpected_loader,
        allow_stale=True,
    )

    assert artifacts.stale_banner is not None
    assert artifacts.exit_code == 0
    _compare_with_golden("freshness_stale_snapshot_issue_002_md", artifacts.markdown_body, update_golden)


def _unexpected_loader(bundle, timestamp, since):
    del bundle, timestamp, since
    raise AssertionError("Live ADO loader should not run when the circuit breaker is open.")


def _seed_confirmed_snapshot(archive_root: Path) -> None:
    snapshot = Snapshot(
        issue_number=1,
        generated_at=FROZEN_NOW - timedelta(days=1),
        ado_data_as_of=FROZEN_NOW - timedelta(days=1),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=990001,
                type="Feature",
                title="Confirmed fallback item",
                state="Active",
                assigned_to="Casey Howard",
                area_path="One\\Adventure\\Acme\\Deployment",
                target_date=FROZEN_NOW.date() + timedelta(days=10),
                risk_level=RiskLevel.LOW,
                tags=[],
            ),
        ),
        scorecards=(),
        schema_version="1.0",
    )
    write_confirmed(EDITION_NAME, 1, snapshot, archive_root=archive_root)

    edition_root = get_archive_root(EDITION_NAME, archive_root)
    edition_root.mkdir(parents=True, exist_ok=True)
    index_path = edition_root / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": snapshot.generated_at.isoformat(),
                        "html_path": str(edition_root / "html" / "issue_001.html"),
                        "md_path": str(edition_root / "md" / "issue_001.md"),
                        "snapshot_path": str(edition_root / "snapshots" / "issue_001.snapshot.json"),
                        "manifest_path": str(edition_root / "manifests" / "issue_001.json"),
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

