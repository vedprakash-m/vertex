from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.baseline_lock import (
    BaselineLockError,
    assert_issue_unlocked,
    find_baseline_path,
    is_issue_locked,
    locked_issues_for_baseline,
)
from src.core.trusted_baseline_store import (
    TrustedBaseline,
    load_trusted_baseline,
    save_trusted_baseline,
)


def _write_baseline(program_dir: Path, *, trusted: int | None, locked: list[int]) -> Path:
    program_dir.mkdir(parents=True, exist_ok=True)
    path = program_dir / "trusted_baseline.yaml"
    path.write_text(
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
    return path


def test_trusted_issue_is_always_locked_plus_explicit_locks(tmp_path: Path) -> None:
    bp = _write_baseline(tmp_path / "programs" / "demo", trusted=77, locked=[78])
    assert locked_issues_for_baseline(bp) == frozenset({77, 78})
    assert is_issue_locked(77, baseline_path=bp) is True   # trusted -> auto-locked
    assert is_issue_locked(78, baseline_path=bp) is True   # explicit lock
    assert is_issue_locked(79, baseline_path=bp) is False  # active draft -> writable


def test_find_baseline_walks_up_from_artifact_path(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "demo"
    bp = _write_baseline(program_dir, trusted=77, locked=[78])
    overrides_path = program_dir / "overrides" / "issue_078.yaml"
    snapshot_path = program_dir / "archive" / "demo_weekly" / "snapshots" / "issue_078.snapshot.json"
    assert find_baseline_path(overrides_path) == bp
    assert find_baseline_path(snapshot_path) == bp


def test_assert_issue_unlocked_blocks_locked_and_allows_unlocked(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "demo"
    _write_baseline(program_dir, trusted=77, locked=[78])
    overrides_dir = program_dir / "overrides"

    with pytest.raises(BaselineLockError, match="hardlocked"):
        assert_issue_unlocked(78, target_path=overrides_dir / "issue_078.yaml", artifact="overrides")
    with pytest.raises(BaselineLockError):
        assert_issue_unlocked(77, target_path=overrides_dir / "issue_077.yaml", artifact="overrides")
    # Unlocked active issue and a path with no baseline above it are both no-ops.
    assert_issue_unlocked(79, target_path=overrides_dir / "issue_079.yaml", artifact="overrides")
    assert_issue_unlocked(78, target_path=tmp_path / "elsewhere" / "x.yaml", artifact="overrides")
    assert_issue_unlocked(None, target_path=overrides_dir / "x.yaml", artifact="overrides")


def test_trusted_baseline_roundtrips_locked_issues(tmp_path: Path) -> None:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    editions_root.mkdir()
    (editions_root / "demo_weekly.yaml").write_text(
        yaml.safe_dump({"schema_version": "2.0", "id": "demo_weekly", "program_id": "demo"}),
        encoding="utf-8",
    )
    doc = TrustedBaseline(
        schema_version="1.0",
        edition="demo_weekly",
        trusted_issue_number=77,
        established_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        established_by="tester",
        locked_issues=(77, 78),
    )
    save_trusted_baseline(doc.edition, doc, editions_root=editions_root, programs_root=programs_root)
    loaded = load_trusted_baseline("demo_weekly", editions_root=editions_root, programs_root=programs_root)
    assert loaded is not None
    assert loaded.locked_issues == (77, 78)
