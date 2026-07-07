from __future__ import annotations

from src.core.file_stores import FileArchiveStore, FileSignalStore, FileTrajectoryStore
from src.core.store_protocols import ArchiveStore, SignalStore, TrajectoryStore


def test_file_signal_store_implements_protocol(tmp_path) -> None:
    assert isinstance(FileSignalStore(programs_root=tmp_path), SignalStore)


def test_file_trajectory_store_implements_protocol(tmp_path) -> None:
    assert isinstance(FileTrajectoryStore(programs_root=tmp_path), TrajectoryStore)


def test_file_archive_store_implements_protocol(tmp_path) -> None:
    assert isinstance(FileArchiveStore(archive_root=tmp_path / "archive"), ArchiveStore)