from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time

import pytest

from src.commands import confirm as confirm_module
from src.commands.confirm_stages import archive_transaction
from src.core.fact_sor_state import get_fact_sor_state_path, load_fact_sor_state, save_fact_sor_state
from src.core.knowledge_candidate_store import load_triage_decisions
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.event_log import read_events
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from tests.unit import test_commands_confirm as confirm_tests
from tests.unit.test_commands_confirm import (
    EDITION_NAME,
    _isolate_fact_store_db as _isolate_fact_store_db,
    _prepare_confirmable_weekly_issue,
    test_confirm_issue_archives_outputs_and_resets_active_state as _exercise_actual_confirm,
)
from tests.unit.test_commands_report import repo_root as repo_root


_WRITER_CHILD = """
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
import portalocker
from src.core import workspace_lease
from src.core.fact_sor_state import save_fact_sor_state
from src.core.knowledge_candidate_store import KnowledgeCandidateDecisionRecord, append_triage_decision
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, write_event
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore

kind, db_root, programs_root, resource, attempted, committed = sys.argv[1:]
resource = Path(resource)
sidecar = resource.with_name(resource.name + ".capture.lock")
original_lock = portalocker.Lock
@contextmanager
def observe_lock(path, *args, **kwargs):
    if Path(path) == sidecar:
        try:
            with original_lock(path, timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB):
                pass
        except portalocker.exceptions.AlreadyLocked:
            Path(attempted).write_text("blocked", encoding="utf-8")
    with original_lock(path, *args, **kwargs) as handle:
        yield handle
workspace_lease.portalocker.Lock = observe_lock

if kind == "selector":
    save_fact_sor_state(
        "acme", mode="primary", recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
        programs_root=Path(programs_root),
    )
elif kind == "fact":
    result = ProgramFactStore("acme", db_root=Path(db_root)).append_fact(
        ProgramFactInput(
            fact_type="decision", entity_refs=("PROGRAM:acme",),
            payload={"decision": "later"}, natural_key="later",
        ),
        recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
    )
    if result.action != "created":
        raise RuntimeError("Expected the later fact to be created")
elif kind == "ledger":
    now = datetime.now(timezone.utc)
    event = build_event_envelope(
        program_id="acme", event_type="risk.raised.v1", occurred_at=now, recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT, confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="synthetic", payload={"risk_id": "risk:later", "title": "Later risk", "severity": "high"},
        source_ref=OperatorAssertionRef(asserted_by="synthetic", asserted_at=now),
    )
    write_event(event, programs_root=Path(programs_root))
else:
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id="synthetic-later", kind="skip", decided_at=datetime.now(timezone.utc),
            triage_actor="synthetic",
        ),
        programs_root=Path(programs_root),
    )
Path(committed).write_text("committed", encoding="utf-8")
"""


@pytest.mark.parametrize(("writer", "source_mode"), [
    ("selector", "legacy"),
    ("fact", "legacy"),
    ("ledger", "shadow"),
    ("knowledge", "shadow"),
    ("ledger", "legacy"),
    ("knowledge", "legacy"),
])
def test_actual_confirm_fences_cooperating_writer_in_another_process(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str,
    source_mode: str,
) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "fact-db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    if source_mode == "shadow":
        stage_workspace = confirm_tests.stage_v2_report_workspace

        def stage_with_shadow_workstreams(*args, **kwargs):
            reports_root = stage_workspace(*args, **kwargs)
            save_fact_sor_state(
                "acme", mode="legacy", family_modes={"workitem.state": "shadow"},
                recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc), programs_root=programs_root,
            )
            return reports_root

        monkeypatch.setattr(confirm_tests, "stage_v2_report_workspace", stage_with_shadow_workstreams)
    store = ProgramFactStore("acme", db_root=db_root)
    store.append_fact(
        ProgramFactInput(
            fact_type="decision", entity_refs=("PROGRAM:acme",),
            payload={"decision": "baseline"}, natural_key="baseline",
        ),
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    resource = {
        "selector": get_fact_sor_state_path("acme", programs_root=programs_root),
        "fact": store.db_path,
        "ledger": programs_root / "acme" / "ledger",
        "knowledge": get_shared_knowledge_root(programs_root),
    }[writer]
    attempted, committed = (tmp_path / name for name in ("attempted", "committed"))
    checkpoint = archive_transaction.create_checkpoint_snapshot
    write_archive = confirm_module.write_confirmed_issue
    process: subprocess.Popen[str] | None = None
    promoted: list[Path] = []

    def contend_before_checkpoint(*args, **kwargs):
        nonlocal process
        process = subprocess.Popen(
            [
                sys.executable, "-c", _WRITER_CHILD, writer, str(db_root), str(programs_root),
                str(resource), str(attempted), str(committed),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 20
        while not attempted.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not attempted.exists() and process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"Writer exited without lock contention (committed={committed.exists()}):\n{stdout}\n{stderr}"
            )
        assert attempted.exists(), "Writer did not encounter the held fence"
        assert not committed.exists()
        return checkpoint(*args, **kwargs)

    def observe_promotion(*args, **kwargs):
        assert not committed.exists()
        paths = write_archive(*args, **kwargs)
        assert paths.index_path.is_file()
        assert not committed.exists()
        promoted.append(paths.index_path)
        return paths

    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", contend_before_checkpoint)
    monkeypatch.setattr(confirm_module, "write_confirmed_issue", observe_promotion)
    try:
        if writer in {"ledger", "knowledge"}:
            reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
            result = confirm_module.confirm_issue(
                EDITION_NAME, 1, force=True, ack_stale_approval=True,
                reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
            )
            assert result.exit_code == 0, result.failures
        else:
            _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)
        assert process is not None
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, f"{stdout}\n{stderr}"
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)

    assert len(promoted) == 1
    assert committed.read_text(encoding="utf-8") == "committed"
    if writer == "selector":
        state = load_fact_sor_state("acme", programs_root=programs_root)
        assert state is not None and state.mode == "primary"
    elif writer == "fact":
        snapshot = store.snapshot(as_of=datetime(2026, 5, 7, tzinfo=timezone.utc))
        assert any(fact.natural_key == "later" for fact in snapshot.facts)
    elif writer == "ledger":
        assert any(event.payload.get("risk_id") == "risk:later" for event in read_events("acme", programs_root=programs_root))
    else:
        assert any(decision.candidate_id == "synthetic-later" for decision in load_triage_decisions(programs_root=programs_root))
