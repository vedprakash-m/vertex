from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from threading import Event, current_thread

import portalocker
import pytest

import src.core.context_gap_reply_import as intake
import src.core.ncfl_proposal_store as store
from tests.unit.test_context_gap_reply_import import _bound_intake_workspace, _write_reply_eml
from tests.unit.test_ncfl_flow import _proposal


def _pending(proposal_id: str = "first"):
    return _proposal(proposal_id, issue_number=1, conflict_key="shared", source_value="high")


def _pause_first_read(monkeypatch: pytest.MonkeyPatch, module):
    captured, resume = Event(), Event()
    original = module.load_proposals
    paused = False

    def read(*args, **kwargs):
        nonlocal paused
        proposals = original(*args, **kwargs)
        if current_thread().name.startswith("stale") and not paused:
            paused = True
            captured.set()
            assert resume.wait(15), "competing mutation did not complete"
        return proposals

    monkeypatch.setattr(module, "load_proposals", read)
    return captured, resume


@pytest.mark.parametrize("operation", ["dismiss", "stage", "two-stagers"])
def test_mutation_rechecks_authoritative_queue_after_competing_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    first, second = _pending(), _pending("second")
    if operation != "two-stagers":
        store.save_proposals("acme", 1, (first,), programs_root=tmp_path)
    captured, resume = _pause_first_read(monkeypatch, store)

    def stale_mutation():
        if operation == "dismiss":
            return store.update_proposal_status(
                "acme", proposal_id=first.proposal_id, new_status="dismissed",
                actor="stale", issue_number=1, programs_root=tmp_path,
            )
        return store.stage_extracted_proposals("acme", 1, (second,), programs_root=tmp_path)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale") as pool:
        future = pool.submit(stale_mutation)
        try:
            assert captured.wait(15)
            if operation == "two-stagers":
                store.stage_extracted_proposals("acme", 1, (first,), programs_root=tmp_path)
            else:
                store.update_proposal_status(
                    "acme", proposal_id=first.proposal_id, new_status="accepted",
                    actor="winner", issue_number=1, programs_root=tmp_path,
                )
        finally:
            resume.set()
        if operation == "dismiss":
            with pytest.raises(ValueError, match="not pending"):
                future.result(timeout=15)
        else:
            future.result(timeout=15)
    rows = {p.proposal_id: p for p in store.load_proposals("acme", programs_root=tmp_path)}
    if operation == "two-stagers":
        assert set(rows) == {"first", "second"}
        assert rows["first"].status == "superseded"
        assert rows["first"].superseded_by == "second"
    else:
        assert rows["first"].status == "accepted"
        assert [(d.from_status, d.to_status) for d in rows["first"].decision_history] == [
            ("pending", "accepted"),
        ]
        if operation == "stage":
            assert rows["second"].status == "pending"
    index = store.load_conflict_index("acme", programs_root=tmp_path)
    assert {entry["proposal_id"]: entry["status"] for entry in index["shared"]} == {
        key: proposal.status for key, proposal in rows.items()
    }


@pytest.mark.parametrize("competing_issue", [1, 2])
def test_actual_reply_intake_rechecks_replay_after_competing_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, competing_issue: int,
) -> None:
    programs, arguments, _ = _bound_intake_workspace(tmp_path)
    captured, resume = _pause_first_read(monkeypatch, intake)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale") as pool:
        future = pool.submit(intake.import_context_gap_reply, **arguments)
        try:
            assert captured.wait(15)
            retained = intake.import_context_gap_reply(**dict(arguments, issue_number=competing_issue))
            decided = store.update_proposal_status(
                "sample", proposal_id=retained.proposal_id, new_status="dismissed",
                actor="reviewer", programs_root=programs,
            )
            before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        finally:
            resume.set()
        assert future.result(timeout=15) == decided
    assert store.load_proposals("sample", programs_root=programs) == (decided,)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_actual_reply_intake_preserves_competing_reply_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, arguments, packet = _bound_intake_workspace(tmp_path)
    captured, resume = _pause_first_read(monkeypatch, intake)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale") as pool:
        future = pool.submit(intake.import_context_gap_reply, **arguments)
        try:
            assert captured.wait(15)
            other_source = tmp_path / "other-reply.eml"
            _write_reply_eml(
                other_source, from_addr="other@example.com", subject="Re: gap",
                body=f"Another answer.\n\n> Reference: {packet.id}\n",
            )
            other = intake.import_context_gap_reply(**dict(arguments, source=other_source))
        finally:
            resume.set()
        first = future.result(timeout=15)
    proposals = store.load_proposals("sample", programs_root=programs)
    assert {p.proposal_id for p in proposals} == {first.proposal_id, other.proposal_id}
    assert len(proposals) == 2
    assert all(p.status == "pending" and not p.decision_history for p in proposals)
    assert all(not p.batch_eligible for p in proposals)


def test_actual_reply_intake_rechecks_foreign_record_before_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, arguments, _ = _bound_intake_workspace(tmp_path)
    captured, resume = _pause_first_read(monkeypatch, intake)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale") as pool:
        future = pool.submit(intake.import_context_gap_reply, **arguments)
        try:
            assert captured.wait(15)
            foreign = replace(_pending(), program_id="foreign")
            store.save_proposals("sample", 1, (foreign,), programs_root=programs)
            before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        finally:
            resume.set()
        with pytest.raises(intake.ContextGapReplyImportError, match="Reply intake unavailable"):
            future.result(timeout=15)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_bound_reply_intake_refuses_escaped_lock_sidecar_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs, arguments, _ = _bound_intake_workspace(tmp_path)
    queue = programs / "sample" / "context_proposals"
    queue.mkdir(parents=True, exist_ok=True)
    lock_path = queue / "_queue.lock"
    outside = tmp_path / "outside-queue.lock"
    outside.write_bytes(b"synthetic external lock content\n")
    try:
        lock_path.symlink_to(outside)
    except OSError:
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == lock_path:
                return original_resolve(outside, *args, **kwargs)
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(intake.ContextGapReplyImportError, match="leaves the selected program"):
        intake.import_context_gap_reply(**arguments)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_repeated_extractor_staging_preserves_decision_and_history(tmp_path: Path) -> None:
    pending = _pending()
    store.stage_extracted_proposals("acme", 1, (pending,), programs_root=tmp_path)
    decided = store.update_proposal_status(
        "acme", proposal_id=pending.proposal_id, new_status="dismissed", actor="reviewer",
        programs_root=tmp_path,
    )
    assert store.stage_extracted_proposals("acme", 1, (pending,), programs_root=tmp_path) == (decided,)
    assert store.load_proposals("acme", programs_root=tmp_path) == (decided,)


@pytest.mark.parametrize("operation", ["stage", "dismiss", "insert", "rebuild"])
def test_mutation_revalidates_corruption_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    store.save_proposals("acme", 1, (_pending(),), programs_root=tmp_path)
    captured, resume = _pause_first_read(monkeypatch, store)

    def mutate():
        if operation == "stage":
            return store.stage_extracted_proposals("acme", 1, (_pending("second"),), programs_root=tmp_path)
        if operation == "dismiss":
            return store.update_proposal_status(
                "acme", proposal_id="first", new_status="dismissed", actor="operator",
                programs_root=tmp_path,
            )
        if operation == "insert":
            return store.insert_proposal("acme", _pending("second"), programs_root=tmp_path)
        return store.rebuild_conflict_index("acme", programs_root=tmp_path)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale") as pool:
        future = pool.submit(mutate)
        try:
            assert captured.wait(15)
            other = store.get_proposals_path("acme", 2, programs_root=tmp_path)
            other.write_text("[null]\n", encoding="utf-8")
            before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        finally:
            resume.set()
        with pytest.raises(ValueError, match="expected proposal objects"):
            future.result(timeout=15)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_explicit_save_repairs_malformed_target_without_loading_it(tmp_path: Path) -> None:
    path = store.get_proposals_path("acme", 1, programs_root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"damaged": true}\n', encoding="utf-8")
    assert store.save_proposals("acme", 1, (_pending(),), programs_root=tmp_path) == path
    assert store.load_proposals("acme", programs_root=tmp_path) == (_pending(),)


@pytest.mark.parametrize("operation", ["stage", "dismiss", "rebuild"])
def test_corrupt_preflight_creates_no_lock_or_other_files(tmp_path: Path, operation: str) -> None:
    path = store.get_proposals_path("acme", 1, programs_root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[null]\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        if operation == "stage":
            store.stage_extracted_proposals("acme", 1, (_pending(),), programs_root=tmp_path)
        elif operation == "dismiss":
            store.update_proposal_status(
                "acme", proposal_id="first", new_status="dismissed", actor="operator",
                programs_root=tmp_path,
            )
        else:
            store.rebuild_conflict_index("acme", programs_root=tmp_path)
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("populated", [False, True])
def test_readers_do_not_initialize_or_rewrite_queue_files(tmp_path: Path, populated: bool) -> None:
    if populated:
        store.save_proposals("acme", 1, (_pending(),), programs_root=tmp_path)
        store.save_proposals(
            "acme", 4, (replace(_pending("new"), issue_number=4),), programs_root=tmp_path,
        )
        (store.get_context_proposals_dir("acme", programs_root=tmp_path) / "_queue.lock").unlink()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert store.load_proposals("acme", issue_number=9, programs_root=tmp_path) == ()
    store.load_proposals("acme", status_filter={"pending"}, programs_root=tmp_path)
    store.load_conflict_index("acme", programs_root=tmp_path)
    store.conflicting_pending_proposals("acme", programs_root=tmp_path)
    stale = store.stale_pending_proposals("acme", programs_root=tmp_path)
    assert stale == ((_pending(),) if populated else ())
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


_LOCK_PROBE = """
import os
import sys
from pathlib import Path
guard = os.environ.get("RVX_GUARD_RUNNER")
if guard:
    source = Path(guard).read_text(encoding="utf-8")
    exec(compile(source.split("\\nimport pytest\\n", 1)[0], "<existing-rvx-guard>", "exec"))
import portalocker
try:
    with portalocker.Lock(sys.argv[1], mode="a+", timeout=0):
        print("acquired", flush=True)
except portalocker.exceptions.LockException:
    print("busy", flush=True)
if guard:
    assert not blocked, dict(blocked)
"""


def _probe_lock(path: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-c", _LOCK_PROBE, str(path)],
        capture_output=True, text=True, timeout=15, check=True,
    )
    return result.stdout.strip()


def test_program_lock_excludes_processes_and_releases_without_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, resume = Event(), Event()
    original = store._atomic_write_json
    lock_path = store.get_context_proposals_dir("acme", programs_root=tmp_path) / "_queue.lock"

    def write(path, payload):
        if current_thread().name.startswith("holder") and path.name.startswith("issue_"):
            entered.set()
            assert resume.wait(15)
        original(path, payload)

    monkeypatch.setattr(store, "_atomic_write_json", write)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="holder") as pool:
        future = pool.submit(store.save_proposals, "acme", 1, (_pending(),), programs_root=tmp_path)
        try:
            assert entered.wait(15)
            assert lock_path.exists()
            assert _probe_lock(lock_path) == "busy"
        finally:
            resume.set()
        future.result(timeout=15)
    assert lock_path.exists()
    assert _probe_lock(lock_path) == "acquired"


def test_failed_write_releases_program_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        def fail(*_args):
            raise OSError("synthetic failure")

        patch.setattr(store, "_atomic_write_json", fail)
        with pytest.raises(OSError, match="synthetic failure"):
            store.save_proposals("acme", 1, (_pending(),), programs_root=tmp_path)
    store.save_proposals("acme", 1, (_pending(),), programs_root=tmp_path)
    lock_path = store.get_context_proposals_dir("acme", programs_root=tmp_path) / "_queue.lock"
    assert lock_path.exists()
    assert _probe_lock(lock_path) == "acquired"


@pytest.mark.parametrize("operation", ["stage", "dismiss", "insert", "rebuild"])
@pytest.mark.parametrize("boundary", ["read", "index"])
def test_mutations_hold_process_lock_for_authoritative_read_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, boundary: str,
) -> None:
    store.save_proposals("acme", 1, (_pending(),), programs_root=tmp_path)
    lock_path = store.get_context_proposals_dir("acme", programs_root=tmp_path) / "_queue.lock"
    original_read, original_write = store.load_proposals, store._atomic_write_json
    reads, observed = 0, []

    def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if boundary == "read" and reads == 2:
            observed.append(_probe_lock(lock_path))
        return original_read(*args, **kwargs)

    def write(path, payload):
        if boundary == "index" and path.name == "_conflict_index.json":
            observed.append(_probe_lock(lock_path))
        return original_write(path, payload)

    monkeypatch.setattr(store, "load_proposals", read)
    monkeypatch.setattr(store, "_atomic_write_json", write)
    if operation == "stage":
        store.stage_extracted_proposals("acme", 1, (_pending("second"),), programs_root=tmp_path)
    elif operation == "dismiss":
        store.update_proposal_status(
            "acme", proposal_id="first", new_status="dismissed", actor="reviewer", programs_root=tmp_path,
        )
    elif operation == "insert":
        store.insert_proposal("acme", _pending("second"), programs_root=tmp_path)
    else:
        store.rebuild_conflict_index("acme", programs_root=tmp_path)
    assert observed == ["busy"]


@pytest.mark.parametrize("scope", ["program", "workspace"])
def test_program_lock_does_not_serialize_independent_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    entered, resume = Event(), Event()
    original = store._atomic_write_json

    def write(path, payload):
        if current_thread().name.startswith("holder") and path.name.startswith("issue_"):
            entered.set()
            assert resume.wait(15)
        original(path, payload)

    monkeypatch.setattr(store, "_atomic_write_json", write)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="holder") as pool:
        future = pool.submit(store.save_proposals, "acme", 1, (_pending(),), programs_root=tmp_path)
        try:
            assert entered.wait(15)
            root = tmp_path / "other" if scope == "workspace" else tmp_path
            program = "other" if scope == "program" else "acme"
            proposal = replace(_pending(), program_id=program)
            store.stage_extracted_proposals(program, 1, (proposal,), programs_root=root)
            assert store.load_proposals(program, programs_root=root) == (proposal,)
        finally:
            resume.set()
        future.result(timeout=15)


def test_lock_contention_uses_safe_existing_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise portalocker.exceptions.LockException("synthetic private lock detail")

    monkeypatch.setattr(portalocker.Lock, "acquire", unavailable)
    with pytest.raises(ValueError, match="Context proposal queue is busy; retry the operation.") as caught:
        store.stage_extracted_proposals("acme", 1, (_pending(),), programs_root=tmp_path)
    assert "synthetic private" not in str(caught.value)


@pytest.mark.parametrize("command", ["dismiss", "extract", "import-reply"])
def test_actual_cli_formats_queue_contention_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    from cli import app
    from typer.testing import CliRunner
    import src.commands.context as context_command

    programs, arguments, packet = _bound_intake_workspace(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise portalocker.exceptions.LockException("synthetic private lock detail")

    monkeypatch.setattr(portalocker.Lock, "acquire", unavailable)
    args = ["context", command, "--edition", "sample_weekly", "--issue", "1", "--programs-root", str(programs)]
    if command == "dismiss":
        args += ["--proposal-id", "first", "--reason", "synthetic"]
    elif command == "extract":
        monkeypatch.setattr(context_command, "extract_proposals", lambda *_args, **_kwargs: (_pending(),))
    else:
        args += [
            "--source", str(arguments["source"]), "--gap-fingerprint", arguments["gap_fingerprint"],
            "--solicitation-id", packet.id,
        ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output + repr(result.exception)
    assert isinstance(result.exception, SystemExit)
    assert "synthetic private" not in result.output and "Traceback" not in result.output
    expected = {
        "dismiss": "Context proposal queue is busy; retry the operation.",
        "extract": "Context proposal queue is invalid; repair it before retrying.",
        "import-reply": "Reply intake unavailable",
    }
    assert expected[command] in result.output
