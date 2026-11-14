from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from src.core.exceptions import StateError
from src.core.snapshot_store import ArchiveLock


def _metadata(root: Path, *, age_minutes: int = 0) -> bytes:
    payload = {
        "pid": 123,
        "started_at": (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat(),
    }
    data = json.dumps(payload).encode("utf-8")
    (root / ".lock").write_bytes(data)
    return data


def test_entrant_before_metadata_write_cannot_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_text = Path.write_text
    interleaved = False

    def contend(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        nonlocal interleaved
        if path == tmp_path / ".lock" and not interleaved:
            interleaved = True
            with pytest.raises(StateError, match="locked"):
                with ArchiveLock(tmp_path):
                    pytest.fail("second entrant reached the critical section")
        return write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", contend)
    with ArchiveLock(tmp_path):
        assert interleaved
        assert (tmp_path / ".lock").is_file()
    assert not (tmp_path / ".lock").exists()


def test_old_holder_cannot_be_displaced_or_remove_successor_lock(tmp_path: Path) -> None:
    with ArchiveLock(tmp_path):
        original = _metadata(tmp_path, age_minutes=31)
        with pytest.raises(StateError, match="locked"):
            with ArchiveLock(tmp_path):
                pytest.fail("age displaced an active holder")
        assert (tmp_path / ".lock").read_bytes() == original
    with ArchiveLock(tmp_path):
        assert (tmp_path / ".lock").is_file()


@pytest.mark.parametrize("old_metadata", [
    b"invalid json", b"[]", b'{"started_at": "invalid"}',
    b'{"started_at": "2020-01-01T00:00:00"}', b'{"started_at": 1}',
])
def test_malformed_legacy_metadata_can_be_replaced(tmp_path: Path, old_metadata: bytes) -> None:
    (tmp_path / ".lock").write_bytes(old_metadata)
    with ArchiveLock(tmp_path):
        assert json.loads((tmp_path / ".lock").read_text())["pid"] == os.getpid()
    assert not (tmp_path / ".lock").exists()


def test_recent_legacy_metadata_refuses_without_leaking_guard(tmp_path: Path) -> None:
    original = _metadata(tmp_path)
    for _ in range(2):
        with pytest.raises(StateError, match="locked"):
            with ArchiveLock(tmp_path):
                pytest.fail("recent abandoned metadata must retain its age rule")
        assert (tmp_path / ".lock").read_bytes() == original
    _metadata(tmp_path, age_minutes=31)
    with ArchiveLock(tmp_path):
        assert (tmp_path / ".lock").is_file()


def test_guard_persists_empty_after_normal_and_exceptional_exit(tmp_path: Path) -> None:
    lock = ArchiveLock(tmp_path)
    with lock:
        assert (tmp_path / ".archive.guard").stat().st_size == 0
    guard_identity = (tmp_path / ".archive.guard").stat().st_ino
    with pytest.raises(ValueError, match="caller"):
        with lock:
            assert (tmp_path / ".archive.guard").stat().st_ino == guard_identity
            raise ValueError("caller")
    assert not (tmp_path / ".lock").exists()
    assert (tmp_path / ".archive.guard").read_bytes() == b""


def test_same_context_reentry_refuses_without_releasing_owner(tmp_path: Path) -> None:
    lock = ArchiveLock(tmp_path)
    with lock:
        original = (tmp_path / ".lock").read_bytes()
        with pytest.raises(StateError, match="locked"):
            with lock:
                pytest.fail("context reentry must not drop its outer guard")
        with pytest.raises(StateError, match="locked"):
            with ArchiveLock(tmp_path):
                pytest.fail("outer guard was released")
        assert (tmp_path / ".lock").read_bytes() == original
    with lock:
        pass


@pytest.mark.parametrize("failure", ["write", "unlink"])
def test_metadata_failure_releases_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    method = "write_text" if failure == "write" else "unlink"
    original = getattr(Path, method)

    def refuse(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == tmp_path / ".lock":
            raise PermissionError("metadata unavailable")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, refuse)
        with pytest.raises(PermissionError, match="metadata unavailable"):
            with ArchiveLock(tmp_path):
                pass
    if (tmp_path / ".lock").exists():
        _metadata(tmp_path, age_minutes=31)
    with ArchiveLock(tmp_path):
        assert (tmp_path / ".lock").is_file()


def test_acquisition_error_does_not_modify_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import portalocker

    original = _metadata(tmp_path, age_minutes=31)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise portalocker.exceptions.LockException("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(portalocker.Lock, "_get_lock", refuse)
        with pytest.raises(StateError, match="locked"):
            with ArchiveLock(tmp_path):
                pytest.fail("lock acquisition failed")
    assert (tmp_path / ".lock").read_bytes() == original
    with ArchiveLock(tmp_path):
        pass


def test_guard_open_failure_remains_visible_and_preserves_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import portalocker

    original = _metadata(tmp_path, age_minutes=31)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("guard unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(portalocker.Lock, "_get_fh", refuse)
        with pytest.raises(PermissionError, match="guard unavailable"):
            with ArchiveLock(tmp_path):
                pytest.fail("guard could not be opened")
    assert (tmp_path / ".lock").read_bytes() == original
    with ArchiveLock(tmp_path):
        pass


def test_stale_metadata_removal_failure_releases_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _metadata(tmp_path, age_minutes=31)
    unlink = Path.unlink

    def refuse(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name == ".lock":
            raise PermissionError("stale metadata unavailable")
        unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", refuse)
        with pytest.raises(PermissionError, match="stale metadata unavailable"):
            with ArchiveLock(tmp_path):
                pytest.fail("stale metadata could not be removed")
    assert (tmp_path / ".lock").read_bytes() == original
    with ArchiveLock(tmp_path):
        pass


def test_metadata_cleanup_still_holds_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlink = Path.unlink
    attempted = False

    def contend(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal attempted
        if path == tmp_path / ".lock":
            attempted = True
            with pytest.raises(StateError, match="locked"):
                with ArchiveLock(tmp_path):
                    pytest.fail("guard released before cleanup")
        unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", contend)
        with ArchiveLock(tmp_path):
            pass
    assert attempted
    with ArchiveLock(tmp_path):
        pass


def test_exit_without_metadata_or_ownership_releases_guard(tmp_path: Path) -> None:
    lock = ArchiveLock(tmp_path)
    lock.__exit__(None, None, None)
    assert list(tmp_path.iterdir()) == []
    with lock:
        (tmp_path / ".lock").unlink()
    with ArchiveLock(tmp_path):
        pass


@pytest.mark.parametrize("age_minutes", [0, 31])
def test_competing_process_refuses_held_guard(tmp_path: Path, age_minutes: int) -> None:
    code = (
        "import sys; from pathlib import Path; "
        "from src.core.snapshot_store import ArchiveLock; "
        "from src.core.exceptions import StateError\n"
        "try:\n"
        " with ArchiveLock(Path(sys.argv[1])): sys.exit(9)\n"
        "except StateError as error:\n"
        " print(error); sys.exit(0)\n"
    )
    with ArchiveLock(tmp_path):
        original = _metadata(tmp_path, age_minutes=age_minutes)
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert "locked" in result.stdout
        assert (tmp_path / ".lock").read_bytes() == original


def test_abrupt_exit_releases_os_guard_but_retains_metadata_age_rule(tmp_path: Path) -> None:
    code = (
        "import os,sys; from pathlib import Path; "
        "from src.core.snapshot_store import ArchiveLock; "
        "lock=ArchiveLock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".archive.guard").is_file()
    original = (tmp_path / ".lock").read_bytes()
    with pytest.raises(StateError, match="locked"):
        with ArchiveLock(tmp_path):
            pytest.fail("recent metadata must still refuse")
    assert (tmp_path / ".lock").read_bytes() == original
    _metadata(tmp_path, age_minutes=31)
    with ArchiveLock(tmp_path):
        pass


def test_archive_directory_failure_remains_visible(tmp_path: Path) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("file", encoding="utf-8")
    with pytest.raises(FileExistsError):
        with ArchiveLock(root):
            pass
