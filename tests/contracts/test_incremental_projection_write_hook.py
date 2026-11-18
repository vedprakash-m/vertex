from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
import multiprocessing
import threading
import time

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import ledger
from src.commands.ledger import _maybe_rebuild_program_projection, _persist_event, _persist_events
from src.core.exceptions import ConfigError
from src.core.ledger.candidate_store import CandidateDecisionRecord, append_triage_decision
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence, build_event_envelope, read_events, write_event
import src.core.ledger.program_views as program_views
from src.core.ledger.program_views import canonical_projection_dump, get_current_projection_path, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef
from src.core.workspace_lease import cooperative_capture_fence


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
    original_full = program_views._project_events_to_sqlite_locked
    original_incremental = program_views._incremental_fold

    def _wrapped_full(program_id, events, *, projection_path, as_of=None, knowledge_as_of=None, triage_decisions):
        event_tuple = tuple(events)
        full_calls.append(len(event_tuple))
        return original_full(
            program_id,
            event_tuple,
            projection_path=projection_path,
            as_of=as_of,
            knowledge_as_of=knowledge_as_of,
            triage_decisions=triage_decisions,
        )

    def _wrapped_incremental(program_id, **kwargs):
        incremental_calls.append(len(kwargs["delta_events"]))
        return original_incremental(program_id, **kwargs)

    monkeypatch.setattr(program_views, "_project_events_to_sqlite_locked", _wrapped_full)
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


def test_competing_incremental_hooks_match_full_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_DISABLE_AUTO_PROJECTION_REBUILD", raising=False)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    _persist_event(_risk_raised("acme", "risk:r1", start), programs_root=root)
    ready = threading.Event()
    release = threading.Event()
    newer_done = threading.Event()
    errors: list[BaseException] = []
    fold = program_views._incremental_fold

    def paused_fold(program_id: str, **kwargs):
        if threading.current_thread().name == "older" and not ready.is_set():
            ready.set()
            if not release.wait(timeout=10):
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
        finally:
            if threading.current_thread().name == "newer":
                newer_done.set()

    older = threading.Thread(target=write, args=(1, "blocked"), name="older")
    newer = threading.Thread(target=write, args=(2, "mitigated"), name="newer")
    with caplog.at_level(logging.ERROR, logger="src.commands.ledger"):
        older.start()
        try:
            assert ready.wait(timeout=10)
            newer.start()
            assert not newer_done.wait(timeout=0.5)
        finally:
            release.set()
            older.join(timeout=10)
            if newer.ident is not None:
                newer.join(timeout=10)

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
    errors: list[BaseException] = []
    done = threading.Event()

    def write() -> None:
        try:
            _persist_event(
                _risk_status("acme", "risk:r1", datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc), new_status="mitigated"),
                programs_root=root,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    writer = threading.Thread(target=write)
    try:
        assert ready.wait(timeout=30)
        with caplog.at_level(logging.ERROR, logger="src.commands.ledger"):
            writer.start()
            assert not done.wait(timeout=0.5)
    finally:
        release.set()
        if writer.ident is not None:
            writer.join(timeout=30)
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=30)
    assert process.exitcode == 0
    assert not writer.is_alive() and not errors
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
        if source.parent == path.parent and source.name.startswith(path.name + ".") and source.suffix == ".tmp" and target == path:
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


@pytest.mark.parametrize("incremental", [False, True])
def test_current_projection_rejects_stale_supplied_events(tmp_path: Path, incremental: bool) -> None:
    root = tmp_path / "programs"
    first = write_event(
        _risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        programs_root=root,
    ).envelope
    stale = read_events("acme", programs_root=root)
    latest = write_event(
        _risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc), new_status="mitigated"),
        programs_root=root,
    ).envelope
    path = get_current_projection_path("acme", programs_root=root)
    program_views.project_program_events("acme", programs_root=root)
    before = canonical_projection_dump(path)
    builder = program_views.project_events_incremental_to_sqlite if incremental else program_views.project_events_to_sqlite

    with pytest.raises(program_views.ProjectionConflictError):
        builder("acme", stale, projection_path=path, programs_root=root)

    assert first.event_id != latest.event_id
    assert canonical_projection_dump(path) == before
    assert before["projection_meta"][0]["event_watermark"] == latest.event_id


def test_full_rebuild_serializes_event_write_and_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    write_event(
        _risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)),
        programs_root=root,
    )
    program_views.project_program_events("acme", programs_root=root)
    entered, release, write_started, write_done = (threading.Event() for _ in range(4))
    errors: list[BaseException] = []
    original_fold = program_views._fold_events

    def paused_fold(*args, **kwargs):
        if threading.current_thread().name == "full":
            entered.set()
            if not release.wait(10):
                raise TimeoutError("full rebuild not released")
        return original_fold(*args, **kwargs)

    monkeypatch.setattr(program_views, "_fold_events", paused_fold)

    def full() -> None:
        try:
            program_views.project_program_events("acme", programs_root=root)
        except BaseException as error:
            errors.append(error)

    def write_and_project() -> None:
        try:
            write_started.set()
            _persist_event(
                _risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc), new_status="mitigated"),
                programs_root=root,
            )
        except BaseException as error:
            errors.append(error)
        finally:
            write_done.set()

    full_thread = threading.Thread(target=full, name="full")
    writer = threading.Thread(target=write_and_project)
    full_thread.start()
    try:
        assert entered.wait(10)
        writer.start()
        assert write_started.wait(10)
        assert not write_done.wait(0.5)
    finally:
        release.set()
        full_thread.join(10)
        if writer.ident is not None:
            writer.join(10)

    assert not full_thread.is_alive() and not writer.is_alive()
    assert not errors
    _assert_projection_matches_full_rebuild("acme", programs_root=root, expected_path=tmp_path / "expected.sqlite3")
    assert canonical_projection_dump(get_current_projection_path("acme", programs_root=root))["proj_risk"][0]["status"] == "mitigated"


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("cutoff", ["as_of", "knowledge_as_of"])
@pytest.mark.parametrize("second_historical", [False, True])
def test_competing_full_and_supplied_builds_preserve_requested_cutoffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, incremental: bool, cutoff: str, second_historical: bool,
) -> None:
    root = tmp_path / "programs"
    first_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    first = write_event(_risk_raised("acme", "risk:r1", first_time), programs_root=root).envelope
    latest = write_event(
        _risk_status("acme", "risk:r1", first_time + timedelta(days=1), new_status="mitigated"),
        programs_root=root,
    ).envelope
    events = read_events("acme", programs_root=root)
    path = get_current_projection_path("acme", programs_root=root)
    entered, release, started, done = (threading.Event() for _ in range(4))
    errors: list[BaseException] = []
    original_fold = program_views._fold_events

    def paused_fold(*args, **kwargs):
        if threading.current_thread().name == "first-full":
            entered.set()
            if not release.wait(10):
                raise TimeoutError("first projection not released")
        return original_fold(*args, **kwargs)

    monkeypatch.setattr(program_views, "_fold_events", paused_fold)
    first_options = {cutoff: first_time} if not second_historical else {}
    second_options = {cutoff: first_time} if second_historical else {}
    builder = program_views.project_events_incremental_to_sqlite if incremental else program_views.project_events_to_sqlite

    def first_build() -> None:
        try:
            program_views.project_program_events("acme", programs_root=root, **first_options)
        except BaseException as error:
            errors.append(error)

    def second_build() -> None:
        started.set()
        try:
            builder("acme", events, projection_path=path, programs_root=root, **second_options)
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    first_thread = threading.Thread(target=first_build, name="first-full")
    second_thread = threading.Thread(target=second_build)
    first_thread.start()
    try:
        assert entered.wait(10)
        second_thread.start()
        assert started.wait(10)
        assert not done.wait(0.25)
    finally:
        release.set()
        first_thread.join(10)
        if second_thread.ident is not None:
            second_thread.join(10)

    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert not errors
    expected = tmp_path / "expected.sqlite3"
    program_views.project_program_events("acme", programs_root=root, projection_path=expected, **second_options)
    actual = _normalized_dump(path)
    assert actual == _normalized_dump(expected)
    assert actual["projection_meta"][0]["event_watermark"] == (first.event_id if second_historical else latest.event_id)
    assert actual["proj_risk"][0]["status"] == ("open" if second_historical else "mitigated")


def test_separate_targets_preserve_supplied_inputs_during_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    first_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    first = write_event(_risk_raised("acme", "risk:r1", first_time), programs_root=root).envelope
    write_event(_risk_status("acme", "risk:r1", first_time + timedelta(days=1)), programs_root=root)
    events = read_events("acme", programs_root=root)
    first_path, second_path = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    entered, release, started, done = (threading.Event() for _ in range(4))
    errors: list[BaseException] = []
    original_fold = program_views._fold_events

    def paused_fold(*args, **kwargs):
        if threading.current_thread().name == "first-full":
            entered.set()
            if not release.wait(10):
                raise TimeoutError("first projection not released")
        return original_fold(*args, **kwargs)

    monkeypatch.setattr(program_views, "_fold_events", paused_fold)

    def build(target: Path, supplied: tuple[EventEnvelope, ...], incremental: bool) -> None:
        if incremental:
            started.set()
        try:
            builder = program_views.project_events_incremental_to_sqlite if incremental else program_views.project_events_to_sqlite
            builder("acme", supplied, projection_path=target, programs_root=root)
        except BaseException as error:
            errors.append(error)
        finally:
            if incremental:
                done.set()

    first_thread = threading.Thread(target=build, args=(first_path, (first,), False), name="first-full")
    second_thread = threading.Thread(target=build, args=(second_path, events, True))
    first_thread.start()
    try:
        assert entered.wait(10)
        second_thread.start()
        assert started.wait(10)
        assert not done.wait(0.25)
    finally:
        release.set()
        first_thread.join(10)
        if second_thread.ident is not None:
            second_thread.join(10)

    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert not errors
    for path, supplied in ((first_path, (first,)), (second_path, events)):
        expected = tmp_path / f"expected-{path.name}"
        project_events_to_sqlite("acme", supplied, projection_path=expected, programs_root=root)
        assert _normalized_dump(path) == _normalized_dump(expected)
    assert canonical_projection_dump(first_path)["proj_risk"][0]["status"] == "open"
    assert canonical_projection_dump(second_path)["proj_risk"][0]["status"] == "blocked"


@pytest.mark.parametrize("incremental", [False, True])
def test_current_inputs_changing_during_fence_wait_are_refused(tmp_path: Path, incremental: bool) -> None:
    root = tmp_path / "programs"
    first = write_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root).envelope
    program_views.project_program_events("acme", programs_root=root)
    path = get_current_projection_path("acme", programs_root=root)
    before = canonical_projection_dump(path)
    started, finished = threading.Event(), threading.Event()
    errors: list[BaseException] = []
    builder = program_views.project_events_incremental_to_sqlite if incremental else program_views.project_events_to_sqlite

    def stale_writer() -> None:
        started.set()
        try:
            builder("acme", (first,), projection_path=path, programs_root=root)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    writer = threading.Thread(target=stale_writer)
    with cooperative_capture_fence(root / "acme" / "ledger"):
        writer.start()
        assert started.wait(5)
        assert not finished.wait(0.25)
        latest = write_event(
            _risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc), new_status="mitigated"),
            programs_root=root,
        ).envelope
    writer.join(10)

    assert not writer.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], program_views.ProjectionConflictError)
    assert canonical_projection_dump(path) == before
    assert read_events("acme", programs_root=root)[-1].event_id == latest.event_id
    program_views.project_program_events("acme", programs_root=root)
    assert canonical_projection_dump(path)["proj_risk"][0]["status"] == "mitigated"


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("cutoff", ["as_of", "knowledge_as_of"])
def test_historical_current_projection_validates_selected_input_without_promoting_it(
    tmp_path: Path, incremental: bool, cutoff: str,
) -> None:
    root = tmp_path / "programs"
    first_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    first = write_event(_risk_raised("acme", "risk:r1", first_time), programs_root=root).envelope
    write_event(_risk_status("acme", "risk:r1", first_time + timedelta(days=1), new_status="mitigated"), programs_root=root)
    path = get_current_projection_path("acme", programs_root=root)
    program_views.project_program_events("acme", programs_root=root)
    builder = program_views.project_events_incremental_to_sqlite if incremental else program_views.project_events_to_sqlite
    options = {cutoff: first_time}

    builder("acme", (first,), projection_path=path, programs_root=root, **options)

    actual = _normalized_dump(path)
    assert actual["projection_meta"][0]["event_watermark"] == first.event_id
    assert actual["proj_risk"][0]["status"] == "open"
    expected = tmp_path / "historical.sqlite3"
    program_views.project_program_events("acme", programs_root=root, projection_path=expected, **options)
    assert actual == _normalized_dump(expected)


def test_explicit_stale_acknowledgement_is_refused_for_current_target(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    when = datetime(2025, 1, 1, tzinfo=timezone.utc)
    event = write_event(_risk_raised("acme", "risk:r1", when), programs_root=root).envelope
    path = get_current_projection_path("acme", programs_root=root)
    program_views.project_program_events("acme", programs_root=root)
    before = canonical_projection_dump(path)
    append_triage_decision(
        CandidateDecisionRecord(candidate_id="gap", kind="gap_acknowledged", decided_at=when,
                                triage_actor="operator", gap_event_id="synthetic-gap"),
        program_id="acme", programs_root=root,
    )

    with pytest.raises(program_views.ProjectionConflictError, match="decisions changed"):
        project_events_to_sqlite("acme", (event,), projection_path=path, programs_root=root, triage_decisions=())

    assert canonical_projection_dump(path) == before
    program_views.project_program_events("acme", programs_root=root)
    assert canonical_projection_dump(path)["projection_meta"][0]["acknowledged_gaps_digest"] != before["projection_meta"][0]["acknowledged_gaps_digest"]


@pytest.mark.parametrize("incremental", [False, True])
def test_projection_inside_existing_ledger_fence_and_noncanonical_snapshot(
    tmp_path: Path, incremental: bool,
) -> None:
    root = tmp_path / "programs"
    first = write_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root).envelope
    write_event(_risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc)), programs_root=root)
    target = tmp_path / "explicit.sqlite3"
    builder = program_views.project_events_incremental_to_sqlite if incremental else program_views.project_events_to_sqlite

    with cooperative_capture_fence(root / "acme" / "ledger"):
        builder("acme", (first,), projection_path=target, programs_root=root)
        program_views.project_program_events("acme", programs_root=root)

    assert canonical_projection_dump(target)["projection_meta"][0]["event_watermark"] == first.event_id
    assert canonical_projection_dump(target)["proj_risk"][0]["status"] == "open"
    assert canonical_projection_dump(get_current_projection_path("acme", programs_root=root))["proj_risk"][0]["status"] == "blocked"


def _paused_full_projection(root: Path, ready, release, queue) -> None:
    original_fold = program_views._fold_events

    def paused_fold(*args, **kwargs):
        ready.set()
        if not release.wait(30):
            raise TimeoutError("full projection was not released")
        return original_fold(*args, **kwargs)

    program_views._fold_events = paused_fold
    try:
        program_views.project_program_events("acme", programs_root=root)
        queue.put(None)
    except BaseException as error:
        queue.put(repr(error))


@pytest.mark.parametrize("second", ["full", "event_hook"])
def test_full_build_serializes_other_writer_across_spawned_processes(tmp_path: Path, second: str) -> None:
    root = tmp_path / "programs"
    write_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root)
    context = multiprocessing.get_context("spawn")
    ready, release, queue = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_paused_full_projection, args=(root, ready, release, queue))
    done = threading.Event()
    errors: list[BaseException] = []

    def second_writer() -> None:
        try:
            if second == "full":
                program_views.project_program_events("acme", programs_root=root)
            else:
                _persist_event(
                    _risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc), new_status="mitigated"),
                    programs_root=root,
                )
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    writer = threading.Thread(target=second_writer)
    process.start()
    try:
        assert ready.wait(90)
        writer.start()
        assert not done.wait(0.5)
    finally:
        release.set()
        if writer.ident is not None:
            writer.join(30)
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(30)
    assert process.exitcode == 0 and queue.get(timeout=5) is None
    assert not writer.is_alive() and not errors
    _assert_projection_matches_full_rebuild("acme", programs_root=root, expected_path=tmp_path / "expected.sqlite3")
    if second == "event_hook":
        assert canonical_projection_dump(get_current_projection_path("acme", programs_root=root))["proj_risk"][0]["status"] == "mitigated"


def test_abrupt_full_writer_exit_releases_locks_and_allows_replay(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    write_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root)
    program_views.project_program_events("acme", programs_root=root)
    path = get_current_projection_path("acme", programs_root=root)
    before = canonical_projection_dump(path)
    context = multiprocessing.get_context("spawn")
    ready, release, queue = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_paused_full_projection, args=(root, ready, release, queue))
    process.start()
    try:
        assert ready.wait(90)
    finally:
        process.terminate()
        process.join(30)

    assert not process.is_alive()
    assert canonical_projection_dump(path) == before
    program_views.project_program_events("acme", programs_root=root)
    _assert_projection_matches_full_rebuild("acme", programs_root=root, expected_path=tmp_path / "expected.sqlite3")


@pytest.mark.parametrize("competitor", ["projection", "event"])
def test_full_writer_fence_timeout_preserves_projection_and_retry_succeeds(tmp_path: Path, competitor: str) -> None:
    root = tmp_path / "programs"
    write_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root)
    program_views.project_program_events("acme", programs_root=root)
    path = get_current_projection_path("acme", programs_root=root)
    before = canonical_projection_dump(path)
    context = multiprocessing.get_context("spawn")
    ready, release, queue = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_paused_full_projection, args=(root, ready, release, queue))
    process.start()
    try:
        assert ready.wait(30)
        with pytest.raises(ConfigError, match="Capture fence unavailable"):
            if competitor == "projection":
                program_views.project_program_events("acme", programs_root=root)
            else:
                write_event(
                    _risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc)),
                    programs_root=root,
                )
        assert canonical_projection_dump(path) == before
    finally:
        release.set()
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(30)

    assert process.exitcode == 0 and queue.get(timeout=5) is None
    assert len(read_events("acme", programs_root=root)) == 1
    if competitor == "event":
        _persist_event(
            _risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc)),
            programs_root=root,
        )
    program_views.project_program_events("acme", programs_root=root)
    _assert_projection_matches_full_rebuild("acme", programs_root=root, expected_path=tmp_path / "expected.sqlite3")


def test_cli_replay_refuses_snapshot_made_stale_before_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "programs"
    write_event(_risk_raised("acme", "risk:r1", datetime(2025, 1, 1, tzinfo=timezone.utc)), programs_root=root)
    program_views.project_program_events("acme", programs_root=root)
    path = get_current_projection_path("acme", programs_root=root)
    before = canonical_projection_dump(path)
    builder = ledger.project_events_incremental_to_sqlite

    def change_after_cli_read(program_id: str, events, **kwargs):
        write_event(
            _risk_status("acme", "risk:r1", datetime(2025, 1, 2, tzinfo=timezone.utc), new_status="mitigated"),
            programs_root=root,
        )
        return builder(program_id, events, **kwargs)

    monkeypatch.setattr("cli.maybe_run_scheduled_compaction", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ledger, "project_events_incremental_to_sqlite", change_after_cli_read)
    result = CliRunner().invoke(app, ["ledger", "replay", "--program", "acme", "--programs-root", str(root)])

    assert result.exit_code != 0
    assert isinstance(result.exception, program_views.ProjectionConflictError)
    assert canonical_projection_dump(path) == before
    assert len(read_events("acme", programs_root=root)) == 2
