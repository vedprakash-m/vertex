from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import MagicMock, Mock

import portalocker
import pytest
import yaml

from src.commands import confirm as confirm_module
import src.commands.confirm_stages.archive_transaction as archive_transaction
from src.core.draft_artifact_store import load_draft_state
from src.core.edition_resolver import get_program_output_dir
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import get_fact_sor_state_path, save_fact_sor_state
from src.core import fact_sor_state, workspace_lease
from src.core.milestone_engine import assess_milestone_health, save_milestones
from src.core.models import RiskLevel
from src.core.models_v2 import Milestone, MilestoneStatus, TrajectoryPoint
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from src.core.quality_gates.fact_drift import evaluate_program_fact_drift_from_draft
from src.core.quality_gates import QualityGateReport, fact_drift
from src.core.snapshot_store import get_archive_root
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.stages.milestone_stage import MilestoneStage
from src.core.trajectory import get_trajectory_path
from tests.unit import test_commands_confirm as confirm_tests
from tests.unit.test_commands_confirm import (
    _isolate_fact_store_db as _isolate_fact_store_db,
    test_confirm_issue_archives_outputs_and_resets_active_state as _exercise_actual_confirm,
)
from tests.unit.test_commands_report import (
    repo_root as repo_root,
    test_generate_report_draft_builds_lookback_from_confirmed_archive as _build_lookback,
)


def test_source_selector_drift_blocks_before_fact_store_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    save_fact_sor_state(
        "synthetic", mode="legacy", recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    monkeypatch.setattr(
        fact_drift, "ProgramFactStore",
        lambda *_args, **_kwargs: pytest.fail("Selector drift must block before fact-store access."),
    )

    report = evaluate_program_fact_drift_from_draft(
        draft_state={
            "program_fact_snapshot": {"snapshot_id": "pfs_synthetic", "program_id": "synthetic"},
        },
        program_id="synthetic",
        programs_root=programs_root,
        effective_family_modes=(("judgment", "primary"), ("workitem.state", "legacy")),
    )

    assert len(report.failing_results) == 1
    assert report.failing_results[0].gate_id == "QG-SG-20"
    assert report.failing_results[0].forceable is False
    assert "selectors changed" in report.failing_results[0].message


def test_lookback_retains_its_separate_unbound_selector_scope(tmp_path: Path) -> None:
    report = evaluate_program_fact_drift_from_draft(
        draft_state={"edition_type": "lookback"},
        program_id="synthetic",
        programs_root=tmp_path / "programs",
        effective_family_modes=(),
    )
    assert report.failing_results == ()


@pytest.mark.parametrize("modes", [
    (),
    (("judgment", "legacy"),),
    (("judgment", "invalid"), ("workitem.state", "legacy")),
    (("workitem.state", "legacy"), ("judgment", "legacy")),
    (("judgment", "legacy"), ("judgment", "legacy"), ("workitem.state", "legacy")),
    (("judgment", "legacy"), ("judgment", "primary"), ("workitem.state", "legacy")),
    (("judgment", "legacy"), ("workitem.state", "legacy"), ("workitem.state", "shadow")),
])
def test_invalid_source_selector_identity_refuses_without_force(
    tmp_path: Path, modes: tuple[tuple[str, str], ...],
) -> None:
    report = evaluate_program_fact_drift_from_draft(
        draft_state={}, program_id="synthetic", programs_root=tmp_path / "programs",
        effective_family_modes=modes,
    )
    assert len(report.failing_results) == 1
    assert report.failing_results[0].forceable is False


@pytest.mark.parametrize("modes", [
    (("judgment", "legacy"), ("judgment", "primary"), ("workitem.state", "legacy")),
    (("judgment", "legacy"), ("workitem.state", "legacy"), ("workitem.state", "shadow")),
])
def test_conflicting_selectors_refuse_before_current_policy_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modes: tuple[tuple[str, str], ...],
) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("Conflicting captured selectors must fail before reading current policy.")

    monkeypatch.setattr(fact_sor_state, "load_fact_sor_state", forbidden)
    assert fact_sor_state.family_sor_modes_match(
        "synthetic", modes, programs_root=tmp_path / "programs",
    ) is False


@pytest.mark.parametrize("error", [ConfigError("synthetic invalid policy"), OSError("synthetic unreadable policy")])
def test_unavailable_source_selectors_fail_visibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setattr(fact_sor_state, "load_fact_sor_state", Mock(side_effect=error))
    report = evaluate_program_fact_drift_from_draft(
        draft_state={}, program_id="synthetic", programs_root=tmp_path / "programs",
        effective_family_modes=(("judgment", "legacy"), ("workitem.state", "legacy")),
    )
    assert report.failing_results[0].forceable is False
    assert "selectors are unavailable" in report.failing_results[0].message


def test_captured_source_selectors_require_program_identity(tmp_path: Path) -> None:
    report = evaluate_program_fact_drift_from_draft(
        draft_state={}, program_id=None, programs_root=tmp_path / "programs",
        effective_family_modes=(("judgment", "legacy"), ("workitem.state", "legacy")),
    )
    assert report.failing_results[0].forceable is False
    assert "selectors are missing or invalid" in report.failing_results[0].message


def test_source_selector_comparison_uses_effective_family_override(
    tmp_path: Path,
) -> None:
    programs_root = tmp_path / "programs"
    modes = (("judgment", "primary"), ("workitem.state", "legacy"))
    save_fact_sor_state(
        "synthetic", mode="legacy", family_modes={"judgment": "primary"},
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc), recorded_by="first",
        programs_root=programs_root,
    )
    unchanged = evaluate_program_fact_drift_from_draft(
        draft_state={}, program_id="synthetic", programs_root=programs_root,
        effective_family_modes=modes,
    )
    assert unchanged.failing_results == ()

    save_fact_sor_state(
        "synthetic", mode="legacy", family_modes={"judgment": "primary"},
        recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc), recorded_by="different-author",
        programs_root=programs_root,
    )
    cosmetic_change = evaluate_program_fact_drift_from_draft(
        draft_state={}, program_id="synthetic", programs_root=programs_root,
        effective_family_modes=modes,
    )
    assert cosmetic_change.failing_results == ()

    save_fact_sor_state(
        "synthetic", mode="legacy", family_modes={"judgment": "shadow"},
        recorded_at=datetime(2026, 5, 7, tzinfo=timezone.utc), recorded_by="second",
        programs_root=programs_root,
    )
    changed = evaluate_program_fact_drift_from_draft(
        draft_state={}, program_id="synthetic", programs_root=programs_root,
        effective_family_modes=modes,
    )
    assert changed.failing_results[0].gate_id == "QG-SG-20"
    assert "selectors changed" in changed.failing_results[0].message


def test_actual_confirm_refuses_source_selector_drift_before_archive_transaction(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    original_loader = confirm_module._load_confirm_draft_manifest
    lock = archive_transaction.ArchiveLock
    lock_entries = []
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    def load_then_change_selector(*args, **kwargs):
        manifest = original_loader(*args, **kwargs)
        save_fact_sor_state(
            "acme", mode="primary", recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
            programs_root=programs_root,
        )
        return manifest

    @contextmanager
    def track_archive_lock(path: Path):
        lock_entries.append(path)
        with lock(path):
            yield

    monkeypatch.setattr(confirm_module, "_load_confirm_draft_manifest", load_then_change_selector)
    monkeypatch.setattr(archive_transaction, "ArchiveLock", track_archive_lock)
    with pytest.raises(AssertionError, match="selectors changed"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert lock_entries == []
    checkpoints.assert_not_called()
    assert not (tmp_path / "archive" / "acme_weekly" / "index.json").exists()


def test_actual_confirm_rechecks_source_selectors_under_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    original_lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)
    entered = []

    @contextmanager
    def change_selector_after_lock(path: Path):
        with original_lock(path):
            entered.append(path)
            save_fact_sor_state(
                "acme", mode="primary", recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
                programs_root=programs_root,
            )
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_selector_after_lock)
    with pytest.raises(ConfigError, match="selectors changed"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert len(entered) == 1
    checkpoints.assert_not_called()
    assert not (archive_root / "acme_weekly" / "index.json").exists()
    assert not tuple((archive_root / "acme_weekly" / "snapshots").glob("*.snapshot.json"))
    with workspace_lease.cooperative_capture_fence(
        ProgramFactStore("acme").db_path,
        get_fact_sor_state_path("acme", programs_root=programs_root),
    ):
        pass


@pytest.mark.parametrize(
    ("writer", "db_root_kind"),
    [("selector", "directory"), ("fact", "directory"), ("fact", "file"), ("fact", "implicit")],
)
def test_actual_confirm_excludes_cooperating_writers_until_archive_promotion(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    writer: str, db_root_kind: str,
) -> None:
    programs_root = tmp_path / "programs"
    if db_root_kind == "implicit":
        monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
        store = ProgramFactStore("acme", db_root=tmp_path)
    else:
        db_root = tmp_path / ("facts.sqlite3" if db_root_kind == "file" else "fact-db")
        monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
        store = ProgramFactStore("acme")
    store.append_fact(
        ProgramFactInput(
            fact_type="decision", entity_refs=("PROGRAM:acme",),
            payload={"decision": "baseline"}, natural_key="baseline",
        ),
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    checkpoint = archive_transaction.create_checkpoint_snapshot
    write_archive = confirm_module.write_confirmed_issue
    original_lock = portalocker.Lock
    attempted, committed = Event(), Event()
    writer_thread: list[int] = []
    archive_written: list[Path] = []

    def observe_lock(*args, **kwargs):
        if writer_thread and get_ident() == writer_thread[0]:
            attempted.set()
        return original_lock(*args, **kwargs)

    monkeypatch.setattr(workspace_lease.portalocker, "Lock", observe_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None

        def mutate() -> None:
            writer_thread.append(get_ident())
            if writer == "selector":
                save_fact_sor_state(
                    "acme", mode="primary", recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
                    programs_root=programs_root,
                )
            else:
                store.append_fact(
                    ProgramFactInput(
                        fact_type="decision", entity_refs=("PROGRAM:acme",),
                        payload={"decision": "later"}, natural_key="later",
                    ),
                    recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
                )
            committed.set()

        def contend_before_checkpoint(*args, **kwargs):
            nonlocal future
            future = pool.submit(mutate)
            assert attempted.wait(10)
            assert not committed.wait(1), "writer committed before archive promotion"
            assert not future.done()
            return checkpoint(*args, **kwargs)

        def observe_promotion(*args, **kwargs):
            assert not committed.is_set()
            result = write_archive(*args, **kwargs)
            assert result.index_path.is_file()
            assert not committed.is_set()
            archive_written.append(result.index_path)
            return result

        monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", contend_before_checkpoint)
        monkeypatch.setattr(confirm_module, "write_confirmed_issue", observe_promotion)
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)
        assert future is not None
        future.result(timeout=10)

    assert committed.is_set()
    assert len(archive_written) == 1


@pytest.mark.parametrize("writer", ["selector", "fact"])
def test_actual_confirm_allows_independent_program_writer_during_promotion(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    checkpoint = archive_transaction.create_checkpoint_snapshot
    written = Event()

    def mutate() -> None:
        if writer == "selector":
            save_fact_sor_state(
                "other", mode="primary", recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
                programs_root=programs_root,
            )
        else:
            ProgramFactStore("other").append_fact(
                ProgramFactInput(
                    fact_type="decision", entity_refs=("PROGRAM:other",),
                    payload={"decision": "independent"}, natural_key="independent",
                ),
                recorded_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
            )
        written.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        def write_before_checkpoint(*args, **kwargs):
            pool.submit(mutate).result(timeout=10)
            assert written.is_set()
            return checkpoint(*args, **kwargs)

        monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", write_before_checkpoint)
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert written.is_set()


@pytest.mark.parametrize("resource", ["selector", "fact"])
def test_actual_confirm_refuses_contended_evidence_fence_before_checkpoint(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme")
    store.append_fact(
        ProgramFactInput(
            fact_type="decision", entity_refs=("PROGRAM:acme",),
            payload={"decision": "baseline"}, natural_key="baseline",
        ),
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    target = (
        get_fact_sor_state_path("acme", programs_root=programs_root)
        if resource == "selector" else store.db_path
    )
    sidecar = target.with_name(target.name + ".capture.lock")
    original_archive_lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    @contextmanager
    def contend_after_archive_lock(path: Path):
        with original_archive_lock(path):
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            with portalocker.Lock(
                sidecar, timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            ):
                yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", contend_after_archive_lock)
    with pytest.raises(ConfigError, match="Capture fence unavailable") as error:
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert str(tmp_path) not in str(error.value)
    checkpoints.assert_not_called()
    root = get_archive_root("acme_weekly", tmp_path / "archive", programs_root=programs_root)
    assert not (root / "index.json").exists()
    assert not (root / ".lock").exists()
    assert not tuple(root.rglob("*.snapshot.json"))


def test_confirm_cli_reports_evidence_fence_contention_before_archive_writes(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root, _ = confirm_tests._prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    monkeypatch.setattr(confirm_module, "REPORTS_ROOT", reports_root)
    monkeypatch.setattr(confirm_module, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(confirm_module, "_CONFIRM_PROGRAMS_ROOT", programs_root)
    sidecar = get_fact_sor_state_path("acme", programs_root=programs_root).with_name(
        "fact_store_sor.yaml.capture.lock"
    )
    original_archive_lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    @contextmanager
    def contend_after_archive_lock(path: Path):
        with original_archive_lock(path):
            with portalocker.Lock(
                sidecar, timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
            ):
                yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", contend_after_archive_lock)
    result = confirm_tests.runner.invoke(
        confirm_tests.app, ["confirm", "--edition", "acme_weekly", "--issue", "1", "--force"],
    )

    assert result.exit_code == 2, result.output
    assert "Capture fence unavailable" in result.output
    assert str(tmp_path) not in result.output
    checkpoints.assert_not_called()
    root = get_archive_root("acme_weekly", archive_root, programs_root=programs_root)
    assert not (root / "index.json").exists()
    assert not tuple(root.rglob("*.snapshot.json"))


def test_actual_confirm_releases_evidence_fence_after_archive_failure(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme")

    def fail_archive(*args, **kwargs):
        raise RuntimeError("synthetic archive failure")

    monkeypatch.setattr(confirm_module, "write_confirmed_issue", fail_archive)
    with pytest.raises(RuntimeError, match="synthetic archive failure"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    def acquire_from_another_thread() -> None:
        with workspace_lease.cooperative_capture_fence(
            store.db_path, get_fact_sor_state_path("acme", programs_root=programs_root),
        ):
            pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(acquire_from_another_thread).result(timeout=10)


def test_actual_confirm_releases_evidence_fence_before_followthrough(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme")
    write_context = confirm_module._write_context_snapshot_for_issue
    acquired = Event()

    def acquire_from_another_thread() -> None:
        with workspace_lease.cooperative_capture_fence(
            store.db_path, get_fact_sor_state_path("acme", programs_root=programs_root),
        ):
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        def verify_before_context(*args, **kwargs):
            pool.submit(acquire_from_another_thread).result(timeout=10)
            assert acquired.is_set()
            return write_context(*args, **kwargs)

        monkeypatch.setattr(confirm_module, "_write_context_snapshot_for_issue", verify_before_context)
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert acquired.is_set()


def test_dry_run_does_not_enter_archive_evidence_fence(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("A dry-run must not enter the archive evidence fence.")

    monkeypatch.setattr(archive_transaction, "cooperative_capture_fence", forbidden)
    confirm_tests.test_confirm_issue_force_surfaces_source_health_gate_as_warning(monkeypatch, repo_root, tmp_path)


@pytest.mark.parametrize("pinned", [True, False])
def test_lookback_confirm_fences_only_declared_fact_pin(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool,
) -> None:
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    monkeypatch.setenv("VERTEX_AUTHOR", "Synthetic reviewer")
    _build_lookback(repo_root, tmp_path)
    reports = tmp_path / "reports"
    programs = tmp_path / "programs"
    archive = tmp_path / "archive"
    if not pinned:
        draft_path = get_program_output_dir("acme_weekly", programs_root=programs) / "issue_004" / "issue_004.draft.json"
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        del draft["program_fact_snapshot"]
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
    overrides = confirm_module.get_overrides_path("acme_weekly", reports, issue_number=4)
    payload = yaml.safe_load((overrides.parent / "issue_000.yaml").read_text(encoding="utf-8"))
    payload["issue_number"] = 4
    for scorecard in payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    confirm_tests._ack_decision_strip(payload)
    overrides.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    confirm_tests._write_authored_exec_summary(reports, 4, edition_name="acme_weekly")
    confirm_tests._write_authored_workstream_narratives(reports, 4, edition_name="acme_weekly")
    confirm_tests._seed_high_risk_signal_coverage(
        programs, captured_at=datetime(2026, 5, 5, 18, tzinfo=timezone.utc), work_item_id=900001,
    )
    # This fixture's continuity-layout validation is unrelated to the fence.
    monkeypatch.setattr(
        confirm_module, "evaluate_continuity_gates", lambda **_kwargs: QualityGateReport(results=()),
    )

    fence = archive_transaction.cooperative_capture_fence
    observed: list[tuple[Path, ...]] = []

    @contextmanager
    def observe(*resources: Path):
        observed.append(resources)
        with fence(*resources):
            yield

    monkeypatch.setattr(archive_transaction, "cooperative_capture_fence", observe)
    selector = get_fact_sor_state_path("acme", programs_root=programs)
    sidecar = selector.with_name(selector.name + ".capture.lock")
    with portalocker.Lock(sidecar, timeout=0, flags=portalocker.LOCK_EX | portalocker.LOCK_NB):
        result = confirm_module.confirm_issue(
            edition_name="acme_weekly", issue_number=4, force=True,
            reports_root=reports, archive_root=archive, programs_root=programs,
        )
    assert result.exit_code == 0, "\n".join(result.failures)
    assert observed == [((ProgramFactStore("acme").db_path,) if pinned else ())]


@pytest.mark.parametrize("change", ["addition", "replacement", "content", "missing_pin"])
def test_forced_confirm_rechecks_fact_pin_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    store = ProgramFactStore("acme", db_root=tmp_path / "fact-db")
    original = store.append_fact(
        ProgramFactInput(
            fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
            payload={"decision": "original"}, natural_key="baseline",
        ),
        recorded_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)
    entered = []

    @contextmanager
    def change_facts_after_lock(path: Path):
        with lock(path):
            entered.append(path)
            if change == "missing_pin":
                with store._connect() as connection:
                    connection.execute("DELETE FROM program_fact_snapshot_pins")
            elif change == "content":
                with store._connect() as connection:
                    connection.execute(
                        "UPDATE program_fact_revisions SET payload_json = ? WHERE revision_id = ?",
                        ('{"decision":"late change"}', original.revision.revision_id),
                    )
            else:
                store.append_fact(
                    ProgramFactInput(
                        fact_type="decision", scope="program", entity_refs=("PROGRAM:acme",),
                        payload={"decision": "late change"},
                        natural_key="baseline" if change == "replacement" else "late-decision",
                    )
                )
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_facts_after_lock)
    with pytest.raises(ConfigError, match="(?i)(drift|changed|content|unavailable)"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    assert len(entered) == 1
    checkpoints.assert_not_called()
    assert not (tmp_path / "archive" / "acme_weekly" / "index.json").exists()
    assert not tuple((tmp_path / "archive").rglob("*.snapshot.json"))


def test_archive_lock_failure_cannot_write_checkpoint(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    lock = MagicMock()
    lock.__enter__.side_effect = ConfigError("archive lock unavailable")
    monkeypatch.setattr(archive_transaction, "ArchiveLock", lambda path: lock)
    with pytest.raises(ConfigError, match="archive lock unavailable"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    checkpoints.assert_not_called()
    assert not tuple((tmp_path / "archive").rglob("*.snapshot.json"))


def test_actual_confirm_rejects_assessed_trajectory_drift_under_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_contexts = []
    lock_entries = []
    milestone = Milestone(
        id="synthetic-milestone",
        program_id="acme",
        name="Synthetic milestone",
        target_date=date(2026, 6, 30),
        owner_alias="maintainer",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=("Synthetic validation",),
        linked_workstream_ids=("deployment_readiness",),
        linked_work_item_ids=(900001,),
    )
    initial = TrajectoryPoint(
        date=date(2026, 5, 4), state="Active", assigned_to="Maintainer",
        target_date=date(2026, 5, 20), risk_level=RiskLevel.LOW,
        area_path=r"ExampleProject\Acme\Deployment",
    )
    changed = TrajectoryPoint(
        date=date(2026, 5, 5), state="Active", assigned_to="Maintainer",
        target_date=date(2026, 7, 15), risk_level=RiskLevel.LOW,
        area_path=r"ExampleProject\Acme\Deployment",
    )
    original_generate = confirm_tests.generate_report_draft

    def generate_with_milestone(*args, **kwargs):
        import yaml

        programs_root = kwargs["programs_root"]
        program_path = programs_root / "acme" / "program.yaml"
        program_data = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_data["storage_backend"] = "file"
        program_path.write_text(yaml.safe_dump(program_data, sort_keys=False), encoding="utf-8")
        save_milestones("acme", (milestone,), programs_root=programs_root)
        store = build_trajectory_store_for_program_id("acme", programs_root=programs_root)
        assert store.append("acme", 900001, initial)
        return original_generate(*args, **kwargs)

    original_execute = MilestoneStage.execute

    def capture_report_assessment(self, ctx):
        result = original_execute(self, ctx)
        if result.milestone_assessments:
            report_contexts.append((ctx, result))
        return result

    monkeypatch.setattr(confirm_tests, "generate_report_draft", generate_with_milestone)
    monkeypatch.setattr(MilestoneStage, "execute", capture_report_assessment)
    reports_root, archive_root, _ = confirm_tests._prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    assert report_contexts
    before_ctx, before_result = report_contexts[0]
    assert before_result.milestone_assessments[0].computed_health is MilestoneStatus.ON_TRACK

    draft = load_draft_state("acme_weekly", 1, programs_root=programs_root)
    assert draft is not None
    issue_dir = get_program_output_dir("acme_weekly", programs_root=programs_root) / "issue_001"
    draft_path = issue_dir / "issue_001.draft.json"
    original_lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    @contextmanager
    def change_after_lock(path: Path):
        with original_lock(path):
            lock_entries.append(path)
            store = build_trajectory_store_for_program_id("acme", programs_root=programs_root)
            assert store.append("acme", 900001, changed)
            current = store.read("acme", 900001)
            after = assess_milestone_health(
                milestone, before_ctx.items, {900001: current}, before_ctx.data_as_of,
            )
            assert after.computed_health is MilestoneStatus.AT_RISK
            fact_gate = evaluate_program_fact_drift_from_draft(
                draft_state=draft, program_id="acme", edition_name="acme_weekly", issue_number=1,
            )
            assert fact_gate.passed is True
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_after_lock)
    with pytest.raises(ConfigError, match="(?i)(trajectory|milestone|evidence changed)"):
        confirm_module.confirm_issue(
            edition_name="acme_weekly",
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            force=True,
        )

    assert len(lock_entries) == 1
    checkpoints.assert_not_called()
    assert not (archive_root / "acme_weekly" / "index.json").exists()
    assert not tuple(archive_root.rglob("*.snapshot.json"))
    assert draft_path.exists()
    assert get_trajectory_path("acme", 900001, programs_root).is_relative_to(programs_root)
