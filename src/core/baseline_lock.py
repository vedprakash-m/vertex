"""Hardlock for trusted-baseline issue artifacts.

A confirmed/trusted (or explicitly locked) issue's artifacts — its confirmed snapshot
and its overrides — must never be silently overwritten by a routine `gather`/`report`/
`doctor`/`confirm` run. This module is the single enforcement point: write paths call
``assert_issue_unlocked`` before clobbering an issue's artifact, and it fails loud with a
clear unlock instruction instead of destroying the baseline.

Lock state lives in each program's ``trusted_baseline.yaml``:
  - the ``trusted_issue_number`` is *always* treated as locked (the confirmed baseline);
  - any issue listed under ``locked_issues:`` is additionally locked.

The guard resolves the baseline file by walking up from the artifact path, so callers that
only have a file path (e.g. ``save_overrides``) need no extra wiring.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class BaselineLockError(RuntimeError):
    """Raised when a write targets a hardlocked baseline issue artifact."""


_BASELINE_FILENAME = "trusted_baseline.yaml"
_MAX_WALK_DEPTH = 6


def _read_lock_state(baseline_path: Path) -> tuple[int | None, frozenset[int]]:
    try:
        payload = yaml.safe_load(baseline_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None, frozenset()
    if not isinstance(payload, dict):
        return None, frozenset()
    raw_trusted = payload.get("trusted_issue_number")
    trusted = int(raw_trusted) if raw_trusted is not None else None
    locked = {int(value) for value in (payload.get("locked_issues") or []) if value is not None}
    return trusted, frozenset(locked)


def locked_issues_for_baseline(baseline_path: Path) -> frozenset[int]:
    """Return every locked issue for a baseline file (trusted issue + explicit locks)."""

    trusted, locked = _read_lock_state(baseline_path)
    issues = set(locked)
    if trusted is not None:
        issues.add(trusted)
    return frozenset(issues)


def find_baseline_path(start: Path) -> Path | None:
    """Walk up from an artifact path to locate the owning ``trusted_baseline.yaml``."""

    current = start if start.is_dir() else start.parent
    for _ in range(_MAX_WALK_DEPTH):
        candidate = current / _BASELINE_FILENAME
        if candidate.exists():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def is_issue_locked(issue_number: int | None, *, baseline_path: Path | None) -> bool:
    if issue_number is None or baseline_path is None or not baseline_path.exists():
        return False
    return issue_number in locked_issues_for_baseline(baseline_path)


def assert_issue_unlocked(issue_number: int | None, *, target_path: Path, artifact: str) -> None:
    """Raise ``BaselineLockError`` if ``issue_number`` is hardlocked.

    ``target_path`` is the file about to be written; the owning baseline is discovered by
    walking up from it. A no-op when no baseline is found or the issue is unlocked.
    """

    if issue_number is None:
        return
    baseline_path = find_baseline_path(target_path)
    if baseline_path is None:
        return
    if issue_number in locked_issues_for_baseline(baseline_path):
        raise BaselineLockError(
            f"Refusing to overwrite {artifact} for issue {issue_number}: it is a hardlocked "
            f"trusted baseline (see {baseline_path}). If you genuinely intend to modify it, "
            f"unlock first with `vertex admin baseline --edition <edition> --unlock {issue_number}`."
        )
