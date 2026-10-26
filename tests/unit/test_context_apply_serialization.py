from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, current_thread

import portalocker
import pytest

from src.core import ncfl_apply
from src.core.ncfl_proposal_store import load_proposals, stage_extracted_proposals, update_proposal_status
from tests.unit.test_context_approval_binding import _pending, _workspace


def test_competing_apply_rechecks_completion_before_authored_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, _ = _workspace(tmp_path)
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    writing, contender_reached, release = Event(), Event(), Event()
    original_write = ncfl_apply._write_to_store
    original_acquire = portalocker.Lock.acquire
    writes: list[str] = []

    def acquire(lock, *args, **kwargs):
        if current_thread().name.startswith("contender"):
            contender_reached.set()
        return original_acquire(lock, *args, **kwargs)

    def write(*args, **kwargs):
        writes.append(current_thread().name)
        if current_thread().name.startswith("winner"):
            writing.set()
        else:
            contender_reached.set()
        assert release.wait(15)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(portalocker.Lock, "acquire", acquire)
    monkeypatch.setattr(ncfl_apply, "_write_to_store", write)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="winner") as first:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="contender") as second:
            winner = first.submit(ncfl_apply.apply_proposal, proposal, actor="one", programs_root=programs)
            try:
                assert writing.wait(15)
                contender = second.submit(ncfl_apply.apply_proposal, proposal, actor="two", programs_root=programs)
                assert contender_reached.wait(15)
            finally:
                release.set()
            results = (winner.result(timeout=15), contender.result(timeout=15))

    assert len(writes) == 1
    assert [result.action for result in results] == ["applied", "skipped_already_applied"]


def test_apply_lock_failure_precedes_journal_and_authored_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, authored = _workspace(tmp_path)
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    before = authored.read_bytes()

    def refuse(*args, **kwargs):
        raise portalocker.exceptions.LockException("synthetic contention")

    monkeypatch.setattr(portalocker.Lock, "acquire", refuse)
    result = ncfl_apply.apply_proposal(proposal, actor="one", programs_root=programs)
    assert result.action == "needs_repair"
    assert "busy or unavailable" in result.note
    assert authored.read_bytes() == before
    assert not ncfl_apply._journal_path("sample", "reviewed", programs_root=programs).exists()


def test_queue_lock_failure_precedes_journal_and_authored_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, authored = _workspace(tmp_path)
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    before = authored.read_bytes()

    def refuse(*args, **kwargs):
        raise ValueError("synthetic queue contention")

    monkeypatch.setattr(ncfl_apply, "_queue_lock", refuse)
    result = ncfl_apply.apply_proposal(proposal, actor="one", programs_root=programs)
    assert result.action == "needs_repair"
    assert "queue is busy or unavailable" in result.note
    assert authored.read_bytes() == before
    assert not ncfl_apply._journal_path("sample", "reviewed", programs_root=programs).exists()


def test_dry_run_does_not_create_apply_lock(tmp_path: Path) -> None:
    programs, _ = _workspace(tmp_path)
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    before = {path: path.read_bytes() for path in programs.rglob("*") if path.is_file()}
    result = ncfl_apply.apply_proposal(proposal, actor="one", programs_root=programs, dry_run=True)
    assert result.action == "applied"
    assert {path: path.read_bytes() for path in programs.rglob("*") if path.is_file()} == before


def test_apply_holds_authoritative_queue_lock_through_authored_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, _ = _workspace(tmp_path)
    proposal = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    writing, release = Event(), Event()
    original = ncfl_apply._write_to_store

    def write(*args, **kwargs):
        writing.set()
        assert release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(ncfl_apply, "_write_to_store", write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ncfl_apply.apply_proposal, proposal, actor="one", programs_root=programs)
        try:
            assert writing.wait(15)
            with pytest.raises(portalocker.exceptions.LockException):
                with portalocker.Lock(
                    programs / "sample" / "context_proposals" / "_queue.lock", mode="a+", timeout=0,
                ):
                    pass
        finally:
            release.set()
        assert future.result(timeout=15).action == "applied"


def test_other_program_apply_does_not_wait_for_active_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, authored = _workspace(tmp_path)
    first = load_proposals("sample", issue_number=1, programs_root=programs)[0]
    other_dir = programs / "other"
    other_dir.mkdir()
    (other_dir / "assumptions.yaml").write_bytes(authored.read_bytes())
    pending = replace(_pending(), program_id="other", edition_id="other_weekly")
    stage_extracted_proposals("other", 1, (pending,), programs_root=programs)
    other = update_proposal_status(
        "other", proposal_id=pending.proposal_id, new_status="accepted",
        actor="reviewer", issue_number=1, programs_root=programs,
    )
    writing, release = Event(), Event()
    original = ncfl_apply._write_to_store

    def write(proposal, **kwargs):
        if proposal.program_id == "sample":
            writing.set()
            assert release.wait(15)
        return original(proposal, **kwargs)

    monkeypatch.setattr(ncfl_apply, "_write_to_store", write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ncfl_apply.apply_proposal, first, actor="one", programs_root=programs)
        try:
            assert writing.wait(15)
            result = ncfl_apply.apply_proposal(other, actor="two", programs_root=programs)
            assert result.action == "applied"
        finally:
            release.set()
        assert future.result(timeout=15).action == "applied"
