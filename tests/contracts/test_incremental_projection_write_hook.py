from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
import multiprocessing
import threading
import time

import pytest

from src.commands import ledger
from src.commands.ledger import _maybe_rebuild_program_projection, _persist_event, _persist_events
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence, build_event_envelope, read_events, write_event
import src.core.ledger.program_views as program_views
from src.core.ledger.program_views import canonical_projection_dump, get_current_projection_path, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=7)


@pytest.fixture(autouse=True)
def _disable_bridge(monkeypatch) -> None:
    monkeypatch.setattr("src.commands.ledger._maybe_bridge_event_to_fact_store", lambda *_args, **_kwargs: None)


def _risk_raised(
    program_id: str,
    risk_id: str,
    recorded_at: datetime,
    *,
    title: str | None = None,
    severity: str = "high",
) -> EventEnvelope:
    return build_event_envelope(
        program_id=program_id,
        event_type="risk.raised.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": risk_id, "title": title or risk_id, "severity": severity},
        source_ref=_deck_ref(),
    )


def _risk_status(program_id: str, risk_id: str, recorded_at: datetime, *, new_status: str = "blocked") -> EventEnvelope:
    return build_event_envelope(
        program_id=program_id,
        event_type="risk.status_changed.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": risk_id, "new_status": new_status},
        source_ref=_deck_ref(),
    )


def _workstream_created(program_id: str, workstream_id: str, recorded_at: datetime) -> object:
    return build_event_envelope(
        program_id=program_id,
        event_type="workstream.created.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={
            "workstream_id": workstream_id,
            "name": f"Workstream {workstream_id}",
            "owner_person_id": "person:owner",
        },
        source_ref=_deck_ref(),
    )


def _correction(program_id: str, target_event_id: str, recorded_at: datetime) -> object:
    return build_event_envelope(
        program_id=program_id,
        event_type="operator.correction.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={
            "corrects_event_id": target_event_id,
            "corrected_payload": {
                "risk_id": "risk:r1",
                "title": "risk:r1",
                "severity": "critical",
            },
            "reason": "correct severity",
        },
        source_ref=_deck_ref(),
    )


def _normalized_dump(path: Path) -> dict[str, list[dict[str, object]]]:
    dump = canonical_projection_dump(path)
    meta = dump["projection_meta"]
    if meta:
        meta[0].pop("built_at", None)
    return dump


def _assert_projection_matches_full_rebuild(program_id: str, *, programs_root: Path, expected_path: Path) -> None:
    events = read_events(program_id, programs_root=programs_root)
    current_path = get_current_projection_path(program_id, programs_root=programs_root)
    project_events_to_sqlite(
        program_id,
        events,
        projection_path=expected_path,
        programs_root=programs_root,
    )
    assert _normalized_dump(current_path) == _normalized_dump(expected_path)


def _track_full_rebuild_calls(monkeypatch) -> tuple[list[int], list[int]]:
    full_calls: list[int] = []
    incremental_calls: list[int] = []
    original_full = program_views.project_events_to_sqlite
    original_incremental = program_views._incremental_fold

    def _wrapped_full(program_id, events, *, projection_path, programs_root=program_views.PROGRAMS_ROOT, as_of=None, knowledge_as_of=None, triage_decisions=None):
        event_tuple = tuple(events)
        full_calls.append(len(event_tuple))
        return original_full(
            program_id,
            event_tuple,
            projection_path=projection_path,
            programs_root=programs_root,
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
            triage_decisions=triage_decisions,
        )

    def _wrapped_incremental(program_id, **kwargs):
        incremental_calls.append(len(kwargs["delta_events"]))
        return original_incremental(program_id, **kwargs)

    monkeypatch.setattr(program_views, "project_events_to_sqlite", _wrapped_full)
    monkeypatch.setattr(program_views, "_incremental_fold", _wrapped_incremental)
    return full_calls, incremental_calls


def test_write_hook_keeps_projection_fresh_without_manual_replay(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"

    _persist_event(
        _risk_raised(program_id, "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        programs_root=programs_root,
    )
    persisted = _persist_event(
        _risk_status(program_id, "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc), new_status="mitigated"),
        programs_root=programs_root,
    )

    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    projection = canonical_projection_dump(projection_path)

    assert projection["proj_risk"][0]["risk_id"] == "risk:r1"
    assert projection["proj_risk"][0]["status"] == "mitigated"
    assert projection["projection_meta"][0]["event_watermark"] == persisted.event_id
    _assert_projection_matches_full_rebuild(
        program_id,
        programs_root=programs_root,
        expected_path=tmp_path / "expected.sqlite3",
    )


def test_write_hook_full_rebuilds_on_projector_version_bump(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"
    _persist_event(_risk_raised(program_id, "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=programs_root)

    full_calls, incremental_calls = _track_full_rebuild_calls(monkeypatch)
    monkeypatch.setattr(program_views, "_PROJECTOR_VERSION", "test-version-bump")

    _persist_event(
        _risk_status(program_id, "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc)),
        programs_root=programs_root,
    )

    assert full_calls == [2]
    assert incremental_calls == []
    _assert_projection_matches_full_rebuild(program_id, programs_root=programs_root, expected_path=tmp_path / "version.sqlite3")


def test_write_hook_full_rebuilds_when_delta_window_is_exceeded(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"
    _persist_event(_risk_raised(program_id, "risk:r0", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=programs_root)

    full_calls, incremental_calls = _track_full_rebuild_calls(monkeypatch)
    monkeypatch.setattr(program_views, "_MAX_INCREMENTAL_DELTA", 1)

    _persist_events(
        (
            _risk_raised(program_id, "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc)),
            _risk_raised(program_id, "risk:r2", datetime(2025, 1, 3, tzinfo=timezone.utc)),
        ),
        programs_root=programs_root,
    )

    assert full_calls == [3]
    assert incremental_calls == []
    _assert_projection_matches_full_rebuild(program_id, programs_root=programs_root, expected_path=tmp_path / "delta.sqlite3")


def test_write_hook_full_rebuilds_for_structural_events(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"
    _persist_event(_risk_raised(program_id, "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=programs_root)

    full_calls, incremental_calls = _track_full_rebuild_calls(monkeypatch)
    _persist_event(
        _workstream_created(program_id, "workstream:w1", datetime(2025, 1, 2, tzinfo=timezone.utc)),
        programs_root=programs_root,
    )

    assert full_calls == [2]
    assert incremental_calls == []
    _assert_projection_matches_full_rebuild(program_id, programs_root=programs_root, expected_path=tmp_path / "structural.sqlite3")


def test_write_hook_full_rebuilds_for_correction_events(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"
    original = _persist_event(
        _risk_raised(program_id, "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc), severity="low"),
        programs_root=programs_root,
    )

    full_calls, incremental_calls = _track_full_rebuild_calls(monkeypatch)
    _persist_event(
        _correction(program_id, original.event_id, datetime(2025, 1, 2, tzinfo=timezone.utc)),
        programs_root=programs_root,
    )

    assert full_calls == [2]
    assert incremental_calls == []
    _assert_projection_matches_full_rebuild(program_id, programs_root=programs_root, expected_path=tmp_path / "correction.sqlite3")


def test_write_hook_allows_concurrent_append_and_read(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"
    _persist_event(_risk_raised(program_id, "risk:r0", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=programs_root)

    errors: list[BaseException] = []
    writer_done = threading.Event()
    projection_path = get_current_projection_path(program_id, programs_root=programs_root)

    def _writer() -> None:
        try:
            for index in range(1, 11):
                _persist_event(
                    _risk_raised(
                        program_id,
                        f"risk:r{index}",
                        datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index),
                    ),
                    programs_root=programs_root,
                )
                time.sleep(0.01)
        except BaseException as exc:  # pragma: no cover - test captures exact exception
            errors.append(exc)
        finally:
            writer_done.set()

    def _reader() -> None:
        try:
            while not writer_done.is_set():
                dump = canonical_projection_dump(projection_path)
                risk_ids = [row["risk_id"] for row in dump["proj_risk"]]
                assert len(risk_ids) == len(set(risk_ids))
                assert dump["projection_meta"]
                time.sleep(0.005)
        except BaseException as exc:  # pragma: no cover - test captures exact exception
            errors.append(exc)
            writer_done.set()

    writer = threading.Thread(target=_writer)
    reader = threading.Thread(target=_reader)
    writer.start()
    reader.start()
    writer.join(timeout=15)
    reader.join(timeout=15)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert not errors
    assert len(canonical_projection_dump(projection_path)["proj_risk"]) == 11


def test_wal_mode_is_sufficient_for_concurrent_rebuilds_on_same_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"
    _persist_event(_risk_raised(program_id, "risk:r0", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=programs_root)

    errors: list[BaseException] = []
    trials = 25

    for trial in range(trials):
        write_event(
            _risk_raised(
                program_id,
                f"risk:r{trial}-0",
                datetime(2025, 1, 2, tzinfo=timezone.utc) + timedelta(seconds=trial * 2),
            ),
            programs_root=programs_root,
        )
        write_event(
            _risk_raised(
                program_id,
                f"risk:r{trial}-1",
                datetime(2025, 1, 2, tzinfo=timezone.utc) + timedelta(seconds=(trial * 2) + 1),
            ),
            programs_root=programs_root,
        )
        barrier = threading.Barrier(2)

        def _worker() -> None:
            try:
                barrier.wait()
                _maybe_rebuild_program_projection(program_id, programs_root=programs_root)
            except BaseException as exc:  # pragma: no cover - test captures exact exception
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert all(not thread.is_alive() for thread in threads)

    assert not errors
    _assert_projection_matches_full_rebuild(program_id, programs_root=programs_root, expected_path=tmp_path / "wal.sqlite3")


def test_auto_projection_rebuild_can_be_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VERTEX_DISABLE_AUTO_PROJECTION_REBUILD", "1")
    monkeypatch.setattr(
        "src.commands.ledger.project_events_incremental_to_sqlite",
        lambda *_args, **_kwargs: pytest.fail("auto rebuild should be disabled"),
    )

    programs_root = tmp_path / "programs"
    _persist_event(
        _risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        programs_root=programs_root,
    )

    assert not get_current_projection_path("acme", programs_root=programs_root).exists()


def test_auto_projection_rebuild_failure_does_not_block_writes(monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("projection boom")

    monkeypatch.setattr("src.commands.ledger.project_events_incremental_to_sqlite", _boom)

    programs_root = tmp_path / "programs"
    event = _risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc))

    with caplog.at_level(logging.ERROR):
        persisted = _persist_event(event, programs_root=programs_root)

    events = read_events("acme", programs_root=programs_root)
    assert events[-1].event_id == persisted.event_id
    assert any("projection: incremental rebuild raised for program=acme" in record.message for record in caplog.records)


@pytest.mark.parametrize("first_commit", ["older", "newer"])
def test_competing_incremental_hooks_match_full_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, first_commit: str,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_DISABLE_AUTO_PROJECTION_REBUILD", raising=False)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    _persist_event(_risk_raised("acme", "risk:r1", start), programs_root=root)
    ready = {name: threading.Event() for name in ("older", "newer")}
    release = {name: threading.Event() for name in ("older", "newer")}
    errors: list[BaseException] = []
    fold = program_views._incremental_fold

    def paused_fold(program_id: str, **kwargs):
        name = threading.current_thread().name
        if name in ready and not ready[name].is_set():
            ready[name].set()
            if not release[name].wait(timeout=30):
                raise TimeoutError("projection interleaving did not complete")
        return fold(program_id, **kwargs)

    monkeypatch.setattr(program_views, "_incremental_fold", paused_fold)

    def write(offset: int, status: str) -> None:
        try:
            _persist_event(
                _risk_status("acme", "risk:r1", start + timedelta(seconds=offset), new_status=status),
                programs_root=root,
            )
        except BaseException as error:
            errors.append(error)

    older = threading.Thread(target=write, args=(1, "blocked"), name="older")
    newer = threading.Thread(target=write, args=(2, "mitigated"), name="newer")
    with caplog.at_level(logging.ERROR, logger="src.commands.ledger"):
        older.start()
        try:
            assert ready["older"].wait(timeout=30)
            newer.start()
            assert ready["newer"].wait(timeout=30)
            release[first_commit].set()
            first = older if first_commit == "older" else newer
            first.join(timeout=30)
            assert not first.is_alive()
        finally:
            release["older"].set()
            release["newer"].set()
            older.join(timeout=30)
            if newer.ident is not None:
                newer.join(timeout=30)

    assert not older.is_alive() and not newer.is_alive()
    assert not errors
    assert not any("projection: incremental rebuild raised" in record.message for record in caplog.records)
    _assert_projection_matches_full_rebuild("acme", programs_root=root, expected_path=tmp_path / "expected.sqlite3")


def _paused_process_writer(root: Path, ready, release, queue) -> None:
    ledger._maybe_bridge_event_to_fact_store = lambda *_args, **_kwargs: None
    fold = program_views._incremental_fold
    messages: list[str] = []

    class CaptureErrors(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = CaptureErrors(level=logging.ERROR)
    ledger.log.addHandler(handler)

    def paused_fold(program_id: str, **kwargs):
        if not ready.is_set():
            ready.set()
            if not release.wait(timeout=30):
                raise TimeoutError("projection interleaving did not complete")
        return fold(program_id, **kwargs)

    program_views._incremental_fold = paused_fold
    try:
        _persist_event(
            _risk_status("acme", "risk:r1", datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc)),
            programs_root=root,
        )
        queue.put(messages)
    except BaseException as error:
        queue.put([repr(error)])
    finally:
        ledger.log.removeHandler(handler)


def test_competing_incremental_hooks_match_full_projection_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_DISABLE_AUTO_PROJECTION_REBUILD", raising=False)
    _persist_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root)
    context = multiprocessing.get_context("spawn")
    ready, release, queue = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_paused_process_writer, args=(root, ready, release, queue))
    process.start()
    try:
        assert ready.wait(timeout=30)
        with caplog.at_level(logging.ERROR, logger="src.commands.ledger"):
            _persist_event(
                _risk_status("acme", "risk:r1", datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc), new_status="mitigated"),
                programs_root=root,
            )
    finally:
        release.set()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=30)
    assert process.exitcode == 0
    assert queue.get(timeout=5) == []
    assert not any("projection: incremental rebuild raised" in record.message for record in caplog.records)
    _assert_projection_matches_full_rebuild("acme", programs_root=root, expected_path=tmp_path / "expected.sqlite3")


def test_hook_retries_conflict_with_fresh_authoritative_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_DISABLE_AUTO_PROJECTION_REBUILD", raising=False)
    _persist_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root)
    original = ledger.project_events_incremental_to_sqlite
    seen: list[int] = []

    def conflict_once(program_id: str, events, **kwargs):
        seen.append(len(events))
        if len(seen) == 1:
            write_event(
                _risk_status("acme", "risk:r1", datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc), new_status="mitigated"),
                programs_root=root,
            )
            raise program_views.ProjectionConflictError("synthetic conflict")
        return original(program_id, events, **kwargs)

    monkeypatch.setattr(ledger, "project_events_incremental_to_sqlite", conflict_once)
    _persist_event(
        _risk_status("acme", "risk:r1", datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc)),
        programs_root=root,
    )
    assert seen == [2, 3]
    _assert_projection_matches_full_rebuild("acme", programs_root=root, expected_path=tmp_path / "expected.sqlite3")


def test_hook_logs_exhausted_conflict_and_preserves_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_DISABLE_AUTO_PROJECTION_REBUILD", raising=False)
    _persist_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root)
    path = get_current_projection_path("acme", programs_root=root)
    before = canonical_projection_dump(path)
    attempts: list[int] = []

    def conflict(*_args, **_kwargs):
        attempts.append(1)
        raise program_views.ProjectionConflictError("synthetic conflict")

    monkeypatch.setattr(ledger, "project_events_incremental_to_sqlite", conflict)
    with caplog.at_level(logging.ERROR, logger="src.commands.ledger"):
        persisted = _persist_event(
            _risk_status("acme", "risk:r1", datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc)),
            programs_root=root,
        )
    assert len(attempts) == 2
    assert read_events("acme", programs_root=root)[-1].event_id == persisted.event_id
    assert canonical_projection_dump(path) == before
    assert any("projection: incremental rebuild raised" in record.message for record in caplog.records)


@pytest.mark.parametrize("fail_replace", [False, True])
def test_write_hook_full_rebuild_preserves_projection_through_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fail_replace: bool,
) -> None:
    programs_root = tmp_path / "programs"
    program_id = "acme"
    _persist_event(
        _risk_raised(program_id, "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        programs_root=programs_root,
    )
    path = get_current_projection_path(program_id, programs_root=programs_root)
    old_bytes = path.read_bytes()
    old_projection = canonical_projection_dump(path)
    original_replace = Path.replace
    observed: list[tuple[bool, bool]] = []

    def replacement(source: Path, target: Path) -> Path:
        if source == path.with_suffix(".sqlite3.tmp") and target == path:
            exists = path.exists()
            observed.append((exists, path.read_bytes() == old_bytes if exists else False))
            if fail_replace:
                raise OSError("Synthetic hook replacement failure")
        return original_replace(source, target)

    with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
        patch.setattr(Path, "replace", replacement)
        persisted = _persist_event(
            _workstream_created(program_id, "workstream:w1", datetime(2025, 1, 2, tzinfo=timezone.utc)),
            programs_root=programs_root,
        )

    assert observed == [(True, True)]
    assert read_events(program_id, programs_root=programs_root)[-1].event_id == persisted.event_id
    if fail_replace:
        assert path.read_bytes() == old_bytes
        assert canonical_projection_dump(path) == old_projection
        assert any("projection: incremental rebuild raised" in record.message for record in caplog.records)
        _maybe_rebuild_program_projection(program_id, programs_root=programs_root)
    else:
        assert not any("projection: incremental rebuild raised" in record.message for record in caplog.records)
    _assert_projection_matches_full_rebuild(
        program_id, programs_root=programs_root, expected_path=tmp_path / "expected.sqlite3",
    )
