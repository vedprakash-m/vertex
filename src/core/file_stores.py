from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.core import archive_store, journal, trajectory
from src.core.models import ArchiveIndex, Snapshot
from src.core.models_v2 import Signal, SignalReviewDecision, SignalThreadLink, SignalUsageMarker, TrajectoryPoint
from src.core.snapshot_store import ARCHIVE_ROOT, write_confirmed


class FileSignalStore:
    def __init__(self, programs_root: Path = journal.PROGRAMS_ROOT) -> None:
        self._programs_root = programs_root

    def append(self, signal: Signal) -> None:
        journal.append_signal(signal, programs_root=self._programs_root)

    def read(
        self,
        program_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        **filters: Any,
    ) -> tuple[Signal, ...]:
        unexpected_filters = sorted(key for key in filters if key not in {"workstream_id", "require_committed_gather_run"})
        if unexpected_filters:
            raise TypeError(f"Unsupported signal filters: {', '.join(unexpected_filters)}")
        workstream_id = filters.get("workstream_id")
        require_committed_gather_run = bool(filters.get("require_committed_gather_run", False))
        return journal.read_signals(
            program_id,
            start=start,
            end=end,
            workstream_id=workstream_id,
            require_committed_gather_run=require_committed_gather_run,
            programs_root=self._programs_root,
        )

    def read_reviews(self, program_id: str) -> dict[str, SignalReviewDecision]:
        return journal.load_latest_review_decisions(program_id, programs_root=self._programs_root)

    def append_review(self, program_id: str, decision: SignalReviewDecision) -> None:
        journal.append_review_decision(program_id, decision, programs_root=self._programs_root)

    def read_usage_markers(self, program_id: str) -> tuple[SignalUsageMarker, ...]:
        return tuple(
            entry
            for entry in journal.read_review_log(program_id, programs_root=self._programs_root)
            if isinstance(entry, SignalUsageMarker)
        )

    def append_usage_marker(self, program_id: str, marker: SignalUsageMarker) -> None:
        journal.append_usage_marker(program_id, marker, programs_root=self._programs_root)

    def read_threads(self, program_id: str) -> dict[str, SignalThreadLink]:
        return journal.load_latest_signal_threads(program_id, programs_root=self._programs_root)

    def append_thread(self, program_id: str, link: SignalThreadLink) -> None:
        journal.append_signal_thread_link(program_id, link, programs_root=self._programs_root)


class FileTrajectoryStore:
    def __init__(self, programs_root: Path = journal.PROGRAMS_ROOT) -> None:
        self._programs_root = programs_root

    def append(self, program_id: str, work_item_id: int, point: TrajectoryPoint) -> bool:
        return trajectory.append_trajectory_point(
            program_id,
            work_item_id,
            point,
            programs_root=self._programs_root,
        )

    def list_work_item_ids(self, program_id: str) -> tuple[int, ...]:
        trajectory_dir = trajectory.get_program_trajectory_dir(program_id, programs_root=self._programs_root)
        if not trajectory_dir.exists():
            return ()
        work_item_ids: list[int] = []
        for path in sorted(trajectory_dir.glob("*.jsonl"), key=lambda entry: entry.name.lower()):
            try:
                work_item_ids.append(int(path.stem))
            except ValueError:
                continue
        return tuple(work_item_ids)

    def read(
        self,
        program_id: str,
        work_item_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[TrajectoryPoint, ...]:
        return trajectory.read_trajectory(
            program_id,
            work_item_id,
            start=start,
            end=end,
            programs_root=self._programs_root,
        )


class FileArchiveStore:
    def __init__(self, archive_root: Path = ARCHIVE_ROOT) -> None:
        self._archive_root = archive_root

    def read_index(self, edition: str) -> ArchiveIndex:
        return archive_store.read_archive_index(edition, archive_root=self._archive_root)

    def write_confirmed(
        self,
        edition: str,
        issue_number: int,
        snapshot: Snapshot,
        acquire_lock: bool = True,
    ) -> None:
        write_confirmed(
            edition,
            issue_number,
            snapshot,
            archive_root=self._archive_root,
            acquire_lock=acquire_lock,
        )

    def read_scorecard_history(self, edition: str) -> tuple[dict[str, Any], ...]:
        return archive_store.read_scorecard_history(edition, archive_root=self._archive_root)


def build_file_signal_store(*, programs_root: Path = journal.PROGRAMS_ROOT) -> FileSignalStore:
    return FileSignalStore(programs_root=programs_root)


def build_file_trajectory_store(*, programs_root: Path = journal.PROGRAMS_ROOT) -> FileTrajectoryStore:
    return FileTrajectoryStore(programs_root=programs_root)
