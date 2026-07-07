from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from src.core.models import ArchiveIndex, Snapshot
from src.core.models_v2 import Signal, SignalReviewDecision, SignalThreadLink, SignalUsageMarker, TrajectoryPoint


@runtime_checkable
class SignalStore(Protocol):
    def append(self, signal: Signal) -> None: ...

    def read(
        self,
        program_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        **filters: Any,
    ) -> tuple[Signal, ...]: ...

    def read_reviews(self, program_id: str) -> dict[str, SignalReviewDecision]: ...

    def append_review(self, program_id: str, decision: SignalReviewDecision) -> None: ...

    def read_usage_markers(self, program_id: str) -> tuple[SignalUsageMarker, ...]: ...

    def append_usage_marker(self, program_id: str, marker: SignalUsageMarker) -> None: ...

    def read_threads(self, program_id: str) -> dict[str, SignalThreadLink]: ...

    def append_thread(self, program_id: str, link: SignalThreadLink) -> None: ...


@runtime_checkable
class TrajectoryStore(Protocol):
    def append(self, program_id: str, work_item_id: int, point: TrajectoryPoint) -> bool: ...

    def list_work_item_ids(self, program_id: str) -> tuple[int, ...]: ...

    def read(
        self,
        program_id: str,
        work_item_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[TrajectoryPoint, ...]: ...


@runtime_checkable
class ArchiveStore(Protocol):
    def read_index(self, edition: str) -> ArchiveIndex: ...

    def write_confirmed(
        self,
        edition: str,
        issue_number: int,
        snapshot: Snapshot,
        acquire_lock: bool = True,
    ) -> None: ...

    def read_scorecard_history(self, edition: str) -> tuple[dict[str, Any], ...]: ...