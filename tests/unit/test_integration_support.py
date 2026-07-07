"""Direct coverage for the extracted integration registry support helpers (D-13)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.commands.integration_support import (
    _backup_path,
    _registry_path,
    _resolve_backup,
    _sqlite_copy,
)


def test_registry_path() -> None:
    root = Path("/tmp/programs")
    assert _registry_path("acme", root) == root / "acme" / "runtime" / "channel_registry.sqlite3"


def test_backup_path_uses_prefix_and_timestamp() -> None:
    root = Path("/tmp/programs")
    path = _backup_path("acme", root, prefix="reg")
    assert path.parent == root / "acme" / "registry_backups"
    assert path.name.startswith("reg-")
    assert path.suffix == ".sqlite3"


def test_sqlite_copy_roundtrips_data(tmp_path: Path) -> None:
    src = tmp_path / "src.sqlite3"
    dst = tmp_path / "dst.sqlite3"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (k TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()

    _sqlite_copy(src, dst)

    out = sqlite3.connect(dst)
    rows = out.execute("SELECT k FROM t").fetchall()
    out.close()
    assert rows == [("hello",)]


def test_resolve_backup_direct_prefix_and_glob(tmp_path: Path) -> None:
    # direct filename
    (tmp_path / "snap.sqlite3").write_text("x", encoding="utf-8")
    assert _resolve_backup(tmp_path, "snap.sqlite3") == tmp_path / "snap.sqlite3"

    # channel_registry-<token>.sqlite3 form
    (tmp_path / "channel_registry-20260101T000000Z.sqlite3").write_text("x", encoding="utf-8")
    assert _resolve_backup(tmp_path, "20260101T000000Z") == (
        tmp_path / "channel_registry-20260101T000000Z.sqlite3"
    )

    # glob fallback returns the last match
    assert _resolve_backup(tmp_path, "2026").name.endswith(".sqlite3")

    # no match
    assert _resolve_backup(tmp_path, "nope") is None
