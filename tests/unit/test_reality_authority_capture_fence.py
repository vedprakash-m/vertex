from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest
import yaml

from src.core import fact_sor_state, program_fact_store
from src.core.fact_sor_state import load_fact_sor_state, save_fact_sor_state
from src.core.program_fact_store import ProgramFactStore
from src.core.program_reality import ProgramReality


NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    programs = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "database"))
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    save_fact_sor_state("sample", mode="primary", recorded_at=NOW, programs_root=programs)
    ProgramFactStore("sample").initialize()
    return programs


def test_actual_assessment_excludes_sor_writer_after_mode_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs = _workspace(tmp_path, monkeypatch)
    selected, release, attempted, committed = Event(), Event(), Event(), Event()
    original = program_fact_store.resolve_fact_sor_mode

    def pause_after_selection(*args, **kwargs):
        result = original(*args, **kwargs)
        selected.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", pause_after_selection)

    def change_mode():
        attempted.set()
        save_fact_sor_state("sample", mode="legacy", recorded_at=NOW, programs_root=programs)
        committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(ProgramReality.load, "sample", programs_root=programs, assessment_as_of=NOW)
        try:
            assert selected.wait(10)
            writing = pool.submit(change_mode)
            assert attempted.wait(5)
            assert not committed.wait(0.2), "SoR writer committed during assessment capture"
        finally:
            release.set()
        result = reading.result(timeout=10)
        writing.result(timeout=10)

    assert result.sor_mode == "primary"
    assert load_fact_sor_state("sample", programs_root=programs).mode == "legacy"


def test_actual_assessment_waits_for_sor_writer_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs = _workspace(tmp_path, monkeypatch)
    writing, release, attempted, resolved = Event(), Event(), Event(), Event()
    original_dump = fact_sor_state.yaml.safe_dump
    original_resolve = program_fact_store.resolve_fact_sor_mode

    def pause_dump(*args, **kwargs):
        writing.set()
        assert release.wait(10)
        return original_dump(*args, **kwargs)

    def observe_resolution(*args, **kwargs):
        result = original_resolve(*args, **kwargs)
        resolved.set()
        return result

    monkeypatch.setattr(fact_sor_state.yaml, "safe_dump", pause_dump)
    monkeypatch.setattr(program_fact_store, "resolve_fact_sor_mode", observe_resolution)

    def change_mode():
        attempted.set()
        return save_fact_sor_state("sample", mode="legacy", recorded_at=NOW, programs_root=programs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(change_mode)
        try:
            assert writing.wait(10)
            reading = pool.submit(ProgramReality.load, "sample", programs_root=programs, assessment_as_of=NOW)
            assert attempted.wait(5)
            assert not resolved.wait(0.2), "assessment read SoR state during an unfinished writer"
        finally:
            release.set()
        writer.result(timeout=10)
        result = reading.result(timeout=10)

    assert result.sor_mode == "legacy"


def test_sor_writer_failure_propagates_and_releases_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs = _workspace(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic authority write failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(fact_sor_state.yaml, "safe_dump", fail)
        with pytest.raises(RuntimeError, match="synthetic authority write failure"):
            save_fact_sor_state("sample", mode="legacy", recorded_at=NOW, programs_root=programs)

    result = ProgramReality.load("sample", programs_root=programs, assessment_as_of=NOW)
    assert result.sor_mode == "primary"
