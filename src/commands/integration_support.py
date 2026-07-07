"""Registry storage path/backup utilities for the integration command (D-13).

Leaf helpers extracted from the ``integration.py`` god module (§28.4 strangler
fig): pure path resolution and SQLite backup/restore file mechanics with no
command/CLI logic. ``integration.py`` re-imports these so existing call sites
and the ``integration.<name>`` attribute surface are unchanged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.core.program_paths import resolve_channel_registry_path_for_read


def _registry_path(program: str, programs_root: Path) -> Path:
    """Resolve the channel-registry db path for read/inspection callers.

    Delegates to the transitional read resolver (canonical-first, legacy
    fallback during the Phase 1 compatibility window) per specs/declutter.md
    R-14. Write-capable callers must use ``get_channel_registry_path`` (the
    canonical write getter, no fallback) instead of this helper.
    """
    return resolve_channel_registry_path_for_read(program, programs_root=programs_root)


def _backup_path(program: str, programs_root: Path, *, prefix: str = "channel_registry") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return programs_root / program / "registry_backups" / f"{prefix}-{stamp}.sqlite3"


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Copy a SQLite database using the online backup API.

    Unlike shutil.copy2, this correctly handles WAL-mode databases where
    committed data may reside in the -wal sidecar file until a checkpoint.
    """
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _resolve_backup(backup_dir: Path, backup: str) -> Path | None:
    direct = backup_dir / backup
    if direct.exists():
        return direct
    with_prefix = backup_dir / f"channel_registry-{backup}.sqlite3"
    if with_prefix.exists():
        return with_prefix
    matches = sorted(backup_dir.glob(f"*{backup}*.sqlite3"))
    return matches[-1] if matches else None
