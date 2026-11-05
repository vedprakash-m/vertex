from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from threading import Event

import portalocker
import pytest

from src.core import program_reality, workspace_lease
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.ledger.candidate_store import CandidateDecisionRecord, append_triage_decision, get_candidate_db_dir
from src.core.ledger.event_index import rebuild_event_index
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, write_event, write_events_atomic
from src.core.ledger.source_refs import EmailRef
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.core.program_reality import ProgramReality


NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)
    store = ProgramFactStore("sample")
    store.initialize()
    get_candidate_db_dir("sample", programs_root=programs)
    rebuild_event_index("sample", programs_root=programs)
    return programs, store


def _event():
    return build_event_envelope(
        program_id="sample", event_type="milestone.completed.v1",
        occurred_at=NOW, recorded_at=NOW, temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED, actor="synthetic",
        payload={"milestone_id": "m1", "completed_on": "2026-11-04"},
        source_ref=EmailRef(
            subject="Synthetic milestone", sent_at=NOW, sender="owner@example.com",
            message_id="synthetic@example.com", vault_hash="sha256:synthetic",
        ),
    )


@pytest.mark.parametrize("writer", ["fact", "event", "batch", "decision", "index"])
def test_actual_assessment_excludes_covered_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str,
) -> None:
    programs, store = _workspace(tmp_path, monkeypatch)
    captured, release, attempted, committed = Event(), Event(), Event(), Event()
    original = program_reality.load_program_facts

    def pause_after_facts(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(program_reality, "load_program_facts", pause_after_facts)

    def mutate():
        attempted.set()
        if writer == "fact":
            store.append_fact(ProgramFactInput("signal.observation", ("signal:s1",), {}), recorded_at=NOW)
        elif writer == "event":
            write_event(_event(), programs_root=programs)
        elif writer == "batch":
            write_events_atomic((_event(),), programs_root=programs)
        elif writer == "decision":
            append_triage_decision(
                CandidateDecisionRecord("c1", "reject", NOW, "synthetic"),
                program_id="sample", programs_root=programs,
            )
        else:
            rebuild_event_index("sample", programs_root=programs)
        committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(ProgramReality.load, "sample", programs_root=programs, assessment_as_of=NOW)
        writing = None
        try:
            assert captured.wait(10)
            writing = pool.submit(mutate)
            assert attempted.wait(5)
            assert not committed.wait(0.2), "covered writer committed during assessment capture"
        finally:
            release.set()
        result = reading.result(timeout=10)
        assert writing is not None
        writing.result(timeout=10)
    assert committed.is_set()
    assert result.events_since(None)[0] == ()


def test_assessment_failure_releases_covered_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    programs, store = _workspace(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr(program_reality, "load_program_facts", fail)
    with pytest.raises(RuntimeError, match="synthetic capture failure"):
        ProgramReality.load("sample", programs_root=programs, assessment_as_of=NOW)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            store.append_fact, ProgramFactInput("signal.observation", ("signal:s1",), {}), recorded_at=NOW,
        ).result(timeout=10)
    assert result.action == "created"


def test_capture_does_not_block_independent_fact_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    programs, _ = _workspace(tmp_path, monkeypatch)
    other = ProgramFactStore("independent")
    other.initialize()
    captured, release = Event(), Event()
    original = program_reality.load_program_facts

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(program_reality, "load_program_facts", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(ProgramReality.load, "sample", programs_root=programs, assessment_as_of=NOW)
        try:
            assert captured.wait(10)
            result = pool.submit(
                other.append_fact, ProgramFactInput("signal.observation", ("signal:other",), {}),
                recorded_at=NOW,
            ).result(timeout=5)
            assert result.action == "created"
        finally:
            release.set()
        reading.result(timeout=10)


def test_fence_orders_resources_and_allows_nested_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = tmp_path / "a", tmp_path / "z"
    acquired = []
    lock = portalocker.Lock

    def record(path, *args, **kwargs):
        acquired.append(path)
        return lock(path, *args, **kwargs)

    monkeypatch.setattr(portalocker, "Lock", record)
    with workspace_lease.cooperative_capture_fence(second, first, first):
        with workspace_lease.cooperative_capture_fence(first):
            assert acquired == [first.with_name("a.capture.lock"), second.with_name("z.capture.lock")]
            with pytest.raises(portalocker.exceptions.LockException):
                with portalocker.Lock(
                    first.with_name("a.capture.lock"), timeout=0,
                    flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
                ):
                    pytest.fail("nested fence released its outer lock")
        with pytest.raises(ConfigError, match="expand"):
            with workspace_lease.cooperative_capture_fence(tmp_path / "another"):
                pytest.fail("expanded a held capture lock set")


def test_partial_acquisition_failure_releases_earlier_resource(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "z"
    with portalocker.Lock(
        second.with_name("z.capture.lock"), timeout=0,
        flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
    ):
        with pytest.raises(ConfigError, match="Capture fence unavailable"):
            with workspace_lease.cooperative_capture_fence(first, second):
                pytest.fail("entered a contended fence")
        with portalocker.Lock(
            first.with_name("a.capture.lock"), timeout=0,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        ):
            pass


@pytest.mark.parametrize("operation", ["mkdir", "resolve"])
def test_fence_acquisition_io_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    def fail(*args, **kwargs):
        raise OSError("synthetic-private-path")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, operation, fail)
        with pytest.raises(ConfigError, match="Capture fence unavailable") as error:
            with workspace_lease.cooperative_capture_fence(tmp_path / "a"):
                pytest.fail("entered an unavailable fence")
        assert "synthetic-private-path" not in str(error.value)
    with workspace_lease.cooperative_capture_fence(tmp_path / "a"):
        pass


def test_fence_excludes_another_process_and_releases_after_process_exit(tmp_path: Path) -> None:
    resource = tmp_path / "database"
    child = (
        "import sys; from pathlib import Path; "
        "from src.core.workspace_lease import cooperative_capture_fence; "
        "from src.core.exceptions import ConfigError\n"
        "try:\n"
        " with cooperative_capture_fence(Path(sys.argv[1])): pass\n"
        "except ConfigError:\n"
        " sys.exit(23)\n"
    )
    with workspace_lease.cooperative_capture_fence(resource):
        refused = subprocess.run(
            [sys.executable, "-c", child, str(resource)],
            capture_output=True, timeout=15, check=False,
        )
        assert refused.returncode == 23, refused.stderr.decode()
    acquired = subprocess.run(
        [sys.executable, "-c", child, str(resource)],
        capture_output=True, timeout=15, check=False,
    )
    assert acquired.returncode == 0, acquired.stderr.decode()
    abrupt = subprocess.run(
        [
            sys.executable, "-c",
            "import os, sys; from pathlib import Path; "
            "from src.core.workspace_lease import cooperative_capture_fence\n"
            "with cooperative_capture_fence(Path(sys.argv[1])): os._exit(0)\n",
            str(resource),
        ],
        capture_output=True, timeout=15, check=False,
    )
    assert abrupt.returncode == 0, abrupt.stderr.decode()
    with workspace_lease.cooperative_capture_fence(resource):
        pass
