"""Local-import ``processed/`` directory rotation (P2-14 / RK-12 / OA-4).

The 3-directory atomicity model (inbox → claimed → processed) is unbounded on
the ``processed/`` side: every successfully hydrated file lands there and stays
forever. OA-4 / RK-12 require raw ``.eml`` content to be purged after 90 days
(or once evidence excerpts are vaulted, whichever is later). This module
implements the retention rotation: files older than ``max_age_days`` **or** a
surplus beyond ``max_count`` (oldest first) are moved to ``processed/archive/``.

Zone A — pure filesystem operations, no AI or M365 imports. Surface-agnostic:
works for any local-import inbox (EML, ICS, Teams-export, docs) because it only
cares about a ``processed/`` directory of files.

Rotation is **best-effort**: a per-file move failure logs and continues. The
``archive/`` subtree is itself unbounded but is intended to be operator-purged
(e.g. via the OS or a scheduled task) once the retention window fully expires —
moving to ``archive/`` is the pipeline-side fence that gets raw content out of
the hot ``processed/`` path without deleting anything the operator may still
want to re-hydrate during a debug session.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PROCESSED_MAX_AGE_DAYS = 90
DEFAULT_PROCESSED_MAX_COUNT = 500
_ARCHIVE_DIRNAME = "archive"


def rotate_processed_dir(
    processed_dir: Path,
    *,
    max_age_days: int = DEFAULT_PROCESSED_MAX_AGE_DAYS,
    max_count: int = DEFAULT_PROCESSED_MAX_COUNT,
    now_epoch: float | None = None,
) -> int:
    """Rotate stale/surplus files from ``processed/`` to ``processed/archive/``.

    A file is rotated when **either**:
    * its mtime is older than ``max_age_days`` from ``now_epoch`` (default: real
      time), **or**
    * the file count in ``processed/`` exceeds ``max_count`` (oldest first).

    Returns the number of files moved. Best-effort: per-file move failures log
    a warning and continue; a missing ``processed/`` dir returns 0.
    """
    if not processed_dir.exists() or not processed_dir.is_dir():
        return 0
    if max_age_days <= 0:
        raise ValueError("max_age_days must be a positive integer.")
    if max_count <= 0:
        raise ValueError("max_count must be positive.")

    import time

    ref_now = now_epoch if now_epoch is not None else time.time()
    age_threshold = ref_now - (max_age_days * 86400)

    # Only direct files (the archive/ subdir is excluded from the count/move).
    files = [p for p in processed_dir.iterdir() if p.is_file()]
    # Oldest first (mtime) — stable by name as a tiebreaker.
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))

    # Identify files to rotate by age.
    to_rotate: list[Path] = [p for p in files if p.stat().st_mtime < age_threshold]

    # Identify surplus files (count > max_count) — oldest surplus first, but
    # do not double-count files already selected by age.
    surplus_count = max(0, len(files) - max_count)
    if surplus_count:
        # ``files`` is oldest-first; the oldest surplus are the first entries not
        # already in the age-selected set.
        already = set(to_rotate)
        for p in files:
            if surplus_count <= 0:
                break
            if p in already:
                continue
            to_rotate.append(p)
            already.add(p)
            surplus_count -= 1

    if not to_rotate:
        return 0

    archive_dir = processed_dir / _ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for src in to_rotate:
        dst = archive_dir / src.name
        # Name collision (a previously-rotated file with the same name) — timestamp
        # the new arrival so we never overwrite an archived file.
        if dst.exists():
            import time as _t

            ts = _t.strftime("%Y%m%dT%H%M%SZ", _t.gmtime(ref_now))
            dst = archive_dir / f"{src.stem}.{ts}{src.suffix}"
        try:
            shutil.move(str(src), str(dst))
            moved += 1
        except OSError as exc:  # pragma: no cover - defensive filesystem error
            log.warning("rotate_processed_dir: could not move %s to archive: %s", src, exc)
    return moved


__all__ = [
    "rotate_processed_dir",
    "DEFAULT_PROCESSED_MAX_AGE_DAYS",
    "DEFAULT_PROCESSED_MAX_COUNT",
]