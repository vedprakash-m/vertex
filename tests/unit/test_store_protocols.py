from __future__ import annotations

from dataclasses import replace
import json
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.file_stores import FileArchiveStore, FileSignalStore, FileTrajectoryStore
from src.core.models import ArchiveIndex, Confidence, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.models_v2 import Signal, SignalReviewDecision, SignalThreadLink, SignalUsageMarker, TrajectoryPoint
from src.core.signal_classification import classify_signal
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.store_protocols import ArchiveStore, SignalStore, TrajectoryStore


class InMemorySignalStore:
    def __init__(self) -> None:
        self._signals: list[Signal] = []
        self._reviews: dict[str, SignalReviewDecision] = {}
        self._usage_markers: list[SignalUsageMarker] = []
        self._threads: dict[str, SignalThreadLink] = {}

    def append(self, signal: Signal) -> None:
        self._signals.append(signal)

    def read(
        self,
        program_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        **filters: object,
    ) -> tuple[Signal, ...]:
        workstream_id = filters.get("workstream_id")
        return tuple(
            signal
            for signal in self._signals
            if signal.program_id == program_id
            and (start is None or signal.timestamp >= start)
            and (end is None or signal.timestamp <= end)
            and (workstream_id is None or signal.workstream_id == workstream_id)
        )

    def read_reviews(self, program_id: str) -> dict[str, SignalReviewDecision]:
        del program_id
        return dict(self._reviews)

    def append_review(self, program_id: str, decision: SignalReviewDecision) -> None:
        del program_id
        self._reviews[decision.signal_id] = decision

    def read_usage_markers(self, program_id: str) -> tuple[SignalUsageMarker, ...]:
        del program_id
        return tuple(self._usage_markers)

    def append_usage_marker(self, program_id: str, marker: SignalUsageMarker) -> None:
        del program_id
        self._usage_markers.append(marker)

    def read_threads(self, program_id: str) -> dict[str, SignalThreadLink]:
        del program_id
        return dict(self._threads)

    def append_thread(self, program_id: str, link: SignalThreadLink) -> None:
        del program_id
        self._threads[link.signal_id] = link


class InMemoryTrajectoryStore:
    def __init__(self) -> None:
        self._points: dict[tuple[str, int], list[TrajectoryPoint]] = {}

    def append(self, program_id: str, work_item_id: int, point: TrajectoryPoint) -> bool:
        key = (program_id, work_item_id)
        current = self._points.setdefault(key, [])
        if current and current[-1] == point:
            return False
        current.append(point)
        return True

    def list_work_item_ids(self, program_id: str) -> tuple[int, ...]:
        return tuple(
            sorted(
                work_item_id
                for stored_program_id, work_item_id in self._points
                if stored_program_id == program_id
            )
        )

    def read(
        self,
        program_id: str,
        work_item_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[TrajectoryPoint, ...]:
        entries = self._points.get((program_id, work_item_id), [])
        return tuple(
            point
            for point in entries
            if (start is None or point.date >= start)
            and (end is None or point.date <= end)
        )


class InMemoryArchiveStore:
    def __init__(self) -> None:
        self._index = ArchiveIndex(edition="acme_weekly", issues=())
        self._snapshots: dict[tuple[str, int], Snapshot] = {}
        self._scorecard_history: dict[str, tuple[dict[str, object], ...]] = {}

    def read_index(self, edition: str) -> ArchiveIndex:
        del edition
        return self._index

    def write_confirmed(
        self,
        edition: str,
        issue_number: int,
        snapshot: Snapshot,
        acquire_lock: bool = True,
    ) -> None:
        del acquire_lock
        self._snapshots[(edition, issue_number)] = snapshot

    def read_scorecard_history(self, edition: str) -> tuple[dict[str, object], ...]:
        return self._scorecard_history.get(edition, ())


def _build_signal() -> Signal:
    return Signal(
        id="sig-1",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="ado",
        program_id="acme",
        workstream_id="core",
        entity_refs=("WI:101",),
        text="Risk moved to active.",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )


def _build_review() -> SignalReviewDecision:
    return SignalReviewDecision(
        signal_id="sig-1",
        decision="approved",
        reviewed_at=datetime(2026, 5, 10, 9, 5, tzinfo=timezone.utc),
        reviewed_by="operator",
    )


def _build_thread(thread_id: str = "thread-1") -> SignalThreadLink:
    return SignalThreadLink(
        signal_id="sig-1",
        thread_id=thread_id,
        linked_at=datetime(2026, 5, 10, 9, 6, tzinfo=timezone.utc),
        linked_by="operator",
    )


def _build_usage_marker() -> SignalUsageMarker:
    return SignalUsageMarker(
        signal_id="sig-1",
        issue_number=7,
        edition_id="acme_weekly",
        manifest_id="manifest-007",
        used_at=datetime(2026, 5, 10, 9, 9, tzinfo=timezone.utc),
    )


def _build_point() -> TrajectoryPoint:
    return TrajectoryPoint(
        date=date(2026, 5, 10),
        state="Active",
        assigned_to="operator",
        target_date=date(2026, 5, 20),
        risk_level=RiskLevel.MEDIUM,
        area_path="One\\Adventure\\Acme",
        tags=("acme",),
    )


def _build_snapshot() -> Snapshot:
    return Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 10, 8, 55, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=101,
                type="Feature",
                title="Acme rollout",
                state="Active",
                assigned_to="operator",
                area_path="One\\Adventure\\Acme",
                target_date=date(2026, 5, 20),
                risk_level=RiskLevel.MEDIUM,
                tags=["acme"],
            ),
        ),
        scorecards=(),
    )


def test_in_memory_stores_satisfy_protocols() -> None:
    assert isinstance(InMemorySignalStore(), SignalStore)
    assert isinstance(InMemoryTrajectoryStore(), TrajectoryStore)
    assert isinstance(InMemoryArchiveStore(), ArchiveStore)


def test_file_signal_store_round_trips_signals_and_reviews(tmp_path: Path) -> None:
    store = FileSignalStore(programs_root=tmp_path)
    signal = _build_signal()
    review = _build_review()
    usage_marker = _build_usage_marker()
    thread = _build_thread()
    # FileSignalStore classifies signals on append (journal write path), so the
    # round-tripped signal carries metadata.signal_class. Mirror that in the
    # expectation. (SQLiteSignalStore does not classify; call sites do.)
    threaded_signal = replace(classify_signal(signal), thread_id=thread.thread_id)

    store.append(signal)
    store.append_review(signal.program_id, review)
    store.append_usage_marker(signal.program_id, usage_marker)
    store.append_thread(signal.program_id, thread)

    assert store.read(signal.program_id) == (threaded_signal,)
    assert store.read(signal.program_id, workstream_id="core") == (threaded_signal,)
    assert store.read_reviews(signal.program_id) == {review.signal_id: review}
    assert store.read_usage_markers(signal.program_id) == (usage_marker,)
    assert store.read_threads(signal.program_id) == {thread.signal_id: thread}


def test_file_trajectory_store_round_trips_points(tmp_path: Path) -> None:
    store = FileTrajectoryStore(programs_root=tmp_path)
    point = _build_point()

    assert store.append("acme", 101, point) is True
    assert store.append("acme", 101, point) is False
    assert store.list_work_item_ids("acme") == (101,)
    assert store.read("acme", 101) == (point,)


def test_sqlite_signal_store_round_trips_signals_reviews_and_threads(tmp_path: Path) -> None:
    store = SQLiteSignalStore(programs_root=tmp_path)
    signal = _build_signal()
    first_review = _build_review()
    usage_marker = _build_usage_marker()
    latest_review = SignalReviewDecision(
        signal_id="sig-1",
        decision="dismissed",
        reviewed_at=datetime(2026, 5, 10, 9, 7, tzinfo=timezone.utc),
        reviewed_by="operator",
        note="Not needed.",
    )
    first_thread = _build_thread("thread-1")
    latest_thread = SignalThreadLink(
        signal_id="sig-1",
        thread_id="thread-2",
        linked_at=datetime(2026, 5, 10, 9, 8, tzinfo=timezone.utc),
        linked_by="operator",
    )
    threaded_signal = replace(signal, thread_id=latest_thread.thread_id)

    store.append(signal)
    store.append_review(signal.program_id, first_review)
    store.append_review(signal.program_id, latest_review)
    store.append_usage_marker(signal.program_id, usage_marker)
    store.append_thread(signal.program_id, first_thread)
    store.append_thread(signal.program_id, latest_thread)

    assert store.read(signal.program_id) == (threaded_signal,)
    assert store.read(signal.program_id, workstream_id="core") == (threaded_signal,)
    assert store.read_reviews(signal.program_id) == {latest_review.signal_id: latest_review}
    assert store.read_usage_markers(signal.program_id) == (usage_marker,)
    assert store.read_threads(signal.program_id) == {latest_thread.signal_id: latest_thread}


def test_sqlite_trajectory_store_round_trips_points(tmp_path: Path) -> None:
    store = SQLiteTrajectoryStore(programs_root=tmp_path)
    point = _build_point()
    changed_point = TrajectoryPoint(
        date=date(2026, 5, 11),
        state="Blocked",
        assigned_to="operator",
        target_date=date(2026, 5, 21),
        risk_level=RiskLevel.HIGH,
        area_path="One\\Adventure\\Acme",
        tags=("acme", "blocked"),
    )

    assert store.append("acme", 101, point) is True
    assert store.append("acme", 101, point) is False
    assert store.append("acme", 101, changed_point) is True
    assert store.list_work_item_ids("acme") == (101,)
    assert store.read("acme", 101) == (point, changed_point)
    assert store.read("acme", 101, start=date(2026, 5, 11)) == (changed_point,)


def test_file_archive_store_writes_snapshot_and_reads_history(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    store = FileArchiveStore(archive_root=archive_root)
    snapshot = _build_snapshot()

    assert store.read_index("acme_weekly") == ArchiveIndex(edition="acme_weekly", issues=())

    store.write_confirmed("acme_weekly", 1, snapshot)

    snapshot_path = archive_root / "acme_weekly" / "snapshots" / "issue_001.snapshot.json"
    assert snapshot_path.exists()

    scorecard_path = archive_root / "acme_weekly" / "scorecards.json"
    scorecard_path.parent.mkdir(parents=True, exist_ok=True)
    scorecard_path.write_text(
        json.dumps({"schema_version": "1.0", "entries": [{"issue_number": 1, "risk": "medium"}]}),
        encoding="utf-8",
    )

    assert store.read_scorecard_history("acme_weekly") == ({"issue_number": 1, "risk": "medium"},)