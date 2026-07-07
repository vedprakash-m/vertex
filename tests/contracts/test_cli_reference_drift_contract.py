"""WS-9 step 3: CLI reference drift contract.

The `tests/contracts/cli_reference_snapshot.md` file is **generated** (not hand-written)
from the live Typer command tree. The drift guard is::

    python scripts/generate_cli_reference.py --check

which exits 0 iff the on-disk snapshot matches the live regeneration. This
contract invokes that guard on every CI run and fails the build on any drift.

Rules enforced:
  1. `tests/contracts/cli_reference_snapshot.md` MUST exist (it's the drift target).
  2. `python scripts/generate_cli_reference.py --check` MUST exit 0.
  3. The snapshot must not leak operator-local filesystem paths.
  4. The legacy `specs/cli-reference.md` (now gitignored) must not be tracked.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_legacy_cli_reference_md_is_gitignored() -> None:
    """specs/cli-reference.md is gitignored; only cli_reference_snapshot.md is tracked."""
    legacy = REPO_ROOT / "specs" / "cli-reference.md"
    if legacy.exists():
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(legacy)],
            cwd=str(REPO_ROOT),
            capture_output=True,
        )
        assert result.returncode != 0, (
            f"{legacy.relative_to(REPO_ROOT)} is still git-tracked. "
            f"Run `git rm --cached specs/cli-reference.md` to untrack it."
        )


def test_cli_reference_snapshot_exists() -> None:
    """The tracked `tests/contracts/cli_reference_snapshot.md` must exist so the drift
    check has a baseline to compare against."""
    snapshot = REPO_ROOT / "tests" / "contracts" / "cli_reference_snapshot.md"
    assert snapshot.exists(), (
        f"Snapshot {snapshot.relative_to(REPO_ROOT)} missing. Run "
        f"`python scripts/generate_cli_reference.py` to seed it."
    )


def test_cli_reference_drift_check() -> None:
    """The live regeneration must match the tracked snapshot.
    Exits non-zero (the contract fails) on any drift.
    """
    completed = subprocess.run(
        [sys.executable, "scripts/generate_cli_reference.py", "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        f"CLI reference drift detected (exit={completed.returncode}). "
        f"Regenerate with `python scripts/generate_cli_reference.py` and commit. "
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )


def test_cli_reference_avoids_local_absolute_paths() -> None:
    """The tracked CLI reference snapshot must not leak operator-local filesystem paths."""
    snapshot = REPO_ROOT / "tests" / "contracts" / "cli_reference_snapshot.md"
    if not snapshot.exists():
        return  # covered by test_cli_reference_snapshot_exists
    text = snapshot.read_text(encoding="utf-8")
    forbidden_markers = (
        str(REPO_ROOT),
        str(REPO_ROOT / "programs"),
        "C:\\Users\\",
        "/home/runner/work/",
    )
    assert not any(marker in text for marker in forbidden_markers), (
        "tests/contracts/cli_reference_snapshot.md leaks a local absolute path. "
        "Regenerate after sanitizing defaults in scripts/generate_cli_reference.py."
    )
