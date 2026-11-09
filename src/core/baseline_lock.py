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
from typing import NoReturn

import yaml

from src.core.yaml_utils import UniqueKeySafeLoader


class BaselineLockError(RuntimeError):
    """Raised when an artifact is hardlocked or its lock state cannot be determined."""


_BASELINE_FILENAME = "trusted_baseline.yaml"
_MAX_WALK_DEPTH = 6


def _raise_invalid_lock_state() -> NoReturn:
    try:
        raise BaselineLockError(
            "Cannot determine trusted baseline lock state. Preserve and inspect "
            "trusted_baseline.yaml before retrying."
        )
    except BaselineLockError as error:
        # Also remove an active exception in the caller from the exposed chain.
        error.__context__ = None
        raise


def _baseline_exists(baseline_path: Path) -> bool:
    try:
        baseline_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        _raise_invalid_lock_state()
    return True


def _read_lock_state(baseline_path: Path) -> tuple[int | None, frozenset[int]]:
    try:
        text = baseline_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        invalid = True
        payload = None
    else:
        try:
            payload = yaml.load(text, Loader=UniqueKeySafeLoader)
        except (yaml.YAMLError, ValueError):
            invalid = True
            payload = None
        else:
            invalid = False
    if not invalid:
        if payload is None:
            payload = {}
        if isinstance(payload, dict):
            raw_trusted = payload.get("trusted_issue_number")
            raw_locked = payload.get("locked_issues")
            if raw_locked is None:
                raw_locked = []
            if (
                isinstance(raw_locked, list)
                and (raw_trusted is None or type(raw_trusted) in (int, str))
                and all(type(value) in (int, str) for value in raw_locked)
            ):
                try:
                    trusted = int(raw_trusted) if raw_trusted is not None else None
                    locked = frozenset(int(value) for value in raw_locked)
                except ValueError:
                    pass
                else:
                    return trusted, locked
    _raise_invalid_lock_state()


def _read_locked_issues(baseline_path: Path) -> frozenset[int]:
    trusted, locked = _read_lock_state(baseline_path)
    return locked if trusted is None else locked | {trusted}


def locked_issues_for_baseline(baseline_path: Path) -> frozenset[int]:
    """Return locked issues, raising when baseline lock state cannot be determined."""

    if not _baseline_exists(baseline_path):
        return frozenset()
    return _read_locked_issues(baseline_path)


def find_baseline_path(start: Path) -> Path | None:
    """Walk up from an artifact path to locate the owning ``trusted_baseline.yaml``."""

    current = start if start.is_dir() else start.parent
    for _ in range(_MAX_WALK_DEPTH):
        candidate = current / _BASELINE_FILENAME
        if _baseline_exists(candidate):
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def is_issue_locked(issue_number: int | None, *, baseline_path: Path | None) -> bool:
    if issue_number is None or baseline_path is None:
        return False
    return issue_number in locked_issues_for_baseline(baseline_path)


def assert_issue_unlocked(issue_number: int | None, *, target_path: Path, artifact: str) -> None:
    """Raise ``BaselineLockError`` if the issue is hardlocked or its lock state is invalid.

    ``target_path`` is the file about to be written; the owning baseline is discovered by
    walking up from it. A no-op when no baseline is found or the issue is unlocked.
    """

    if issue_number is None:
        return
    baseline_path = find_baseline_path(target_path)
    if baseline_path is None:
        return
    if issue_number in _read_locked_issues(baseline_path):
        raise BaselineLockError(
            f"Refusing to overwrite {artifact} for issue {issue_number}: it is a hardlocked "
            f"trusted baseline (see {baseline_path}). If you genuinely intend to modify it, "
            f"unlock first with `vertex admin baseline --edition <edition> --unlock {issue_number}`."
        )
