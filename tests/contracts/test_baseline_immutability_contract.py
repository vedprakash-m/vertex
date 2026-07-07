"""Contract: confirmed/locked baseline artifacts are immutable.

Complements the unit tests in test_baseline_lock.py by exercising the REAL write paths
(snapshot_store.write_confirmed + overrides_store.save_overrides) end-to-end against a
locked baseline, and by asserting the hardlock guard cannot be silently removed from
either write path.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from src.core import overrides_store, snapshot_store
from src.core.baseline_lock import BaselineLockError
from src.core.overrides_store import OverridesDocument, save_overrides
from src.core.snapshot_store import write_confirmed


def _setup(tmp_path: Path, *, trusted: int, locked: list[int]) -> None:
    program_dir = tmp_path / "programs" / "demo"
    (program_dir / "editions").mkdir(parents=True)
    (program_dir / "editions" / "demo_weekly.yaml").write_text(
        yaml.safe_dump({"schema_version": "2.0", "id": "demo_weekly", "program_id": "demo"}),
        encoding="utf-8",
    )
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump({"schema_version": "3.0", "id": "demo", "name": "Demo"}),
        encoding="utf-8",
    )
    (program_dir / "trusted_baseline.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "edition": "demo_weekly",
                "trusted_issue_number": trusted,
                "locked_issues": locked,
            }
        ),
        encoding="utf-8",
    )


def test_write_confirmed_refuses_locked_and_trusted_snapshots(tmp_path: Path) -> None:
    _setup(tmp_path, trusted=77, locked=[78])
    archive_root = tmp_path / "archive"
    # The hardlock fires before the snapshot is serialized, so a placeholder object proves
    # the guard without constructing a full Snapshot.
    with pytest.raises(BaselineLockError):
        write_confirmed("demo_weekly", 78, object(), archive_root=archive_root)  # type: ignore[arg-type]
    with pytest.raises(BaselineLockError):
        write_confirmed("demo_weekly", 77, object(), archive_root=archive_root)  # type: ignore[arg-type]


def test_save_overrides_refuses_locked_issue_but_allows_active(tmp_path: Path) -> None:
    _setup(tmp_path, trusted=77, locked=[78])
    reports_root = tmp_path / "output"
    with pytest.raises(BaselineLockError):
        save_overrides(
            "demo_weekly",
            OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
            reports_root=reports_root,
        )
    # The active (unlocked) issue still writes normally.
    written = save_overrides(
        "demo_weekly",
        OverridesDocument(issue_number=79, top_3_now=(), scorecards=()),
        reports_root=reports_root,
    )
    assert written.exists()


def test_hardlock_guard_is_wired_into_both_write_paths() -> None:
    # Defense-in-depth: the guard must not be silently removed from the write paths.
    assert "assert_issue_unlocked" in inspect.getsource(snapshot_store.write_confirmed)
    assert "assert_issue_unlocked" in inspect.getsource(overrides_store.save_overrides)
