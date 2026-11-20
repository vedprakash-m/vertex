from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.commands import confirm
import src.commands.confirm_stages.archive_transaction as archive_transaction
from src.core.exceptions import ConfigError
from src.core.manifest_writer import hash_jsonable
import src.core.gather_run_manifest as gather_run_manifest
from src.core.gather_run_manifest import (
    GatherRunStatus,
    compute_manifest_hash,
    get_committed_root,
    get_committed_run_dir,
    get_latest_full_pointer_path,
    get_staging_run_dir,
    write_manifest,
)
from tests.unit.test_commands_confirm import (
    EDITION_NAME,
    _isolate_fact_store_db,
    _prepare_confirmable_weekly_issue,
    repo_root,
)
from tests.unit.test_report_gather_run_lineage import _commit_gather_run
from tests.unit.test_gather_run_manifest import _make_manifest, _write_nested_manifest

pytestmark = pytest.mark.usefixtures(_isolate_fact_store_db.__name__)


def _rebind_program_verdict(
    report_manifest: dict[str, object], draft: dict[str, object], capture: dict[str, object],
) -> None:
    metadata = report_manifest["metadata"]
    assert isinstance(metadata, dict)
    observation = metadata["program_verdict_observation"]
    comparison = metadata["program_verdict_comparison"]
    assert isinstance(observation, dict) and isinstance(comparison, dict)
    inputs = observation["inputs"]
    verdict = observation["verdict"]
    assert isinstance(inputs, dict) and isinstance(verdict, dict)
    inputs["capture"] = capture
    verdict["snapshot_id"] = capture["sha256"]
    observation["inputs_sha256"] = hash_jsonable(inputs)
    observation["verdict_sha256"] = hash_jsonable(verdict)
    comparison["capture_sha256"] = capture["sha256"]
    comparison["observation_inputs_sha256"] = observation["inputs_sha256"]
    draft["program_verdict_observation_sha256"] = hash_jsonable(observation)


@pytest.mark.parametrize("dry_run", [False, True])
def test_confirm_path_shaped_gather_pin_blocks_before_archive(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    get_committed_root("acme", programs_root=programs_root).mkdir(parents=True)
    staged_dir = get_staging_run_dir("acme", "gather-outside", programs_root=programs_root)
    staged = replace(
        _make_manifest(program_id="acme", run_id="../staging/gather-outside"),
        status=GatherRunStatus.COMMITTED,
    )
    staged = replace(staged, manifest_hash=compute_manifest_hash(staged))
    staged_path = write_manifest(staged_dir, staged)
    staged_before = staged_path.read_bytes()
    draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft.update(gather_run_id=staged.run_id, gather_run_hash=staged.manifest_hash)
    report_manifest_path = draft_path.with_name("issue_001.manifest.json")
    report_manifest = json.loads(report_manifest_path.read_text(encoding="utf-8"))
    capture = report_manifest["metadata"]["run_evidence_snapshot"]
    for payload in (report_manifest, capture):
        payload.update(gather_run_id=draft["gather_run_id"], gather_run_hash=draft["gather_run_hash"])
    capture.pop("sha256")
    capture["sha256"] = hash_jsonable(capture)
    draft["run_evidence_snapshot_sha256"] = capture["sha256"]
    _rebind_program_verdict(report_manifest, draft, capture)
    report_manifest_path.write_text(json.dumps(report_manifest), encoding="utf-8")
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    archive_before = {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()}
    writers = [Mock(wraps=getattr(confirm, name)) for name in ("write_confirmed", "write_confirmed_issue")]
    for name, writer in zip(("write_confirmed", "write_confirmed_issue"), writers, strict=True):
        monkeypatch.setattr(confirm, name, writer)
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    result = confirm.confirm_issue(
        EDITION_NAME, 1, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, force=True, dry_run=dry_run,
    )

    assert result.exit_code != 0
    assert any("invalid identifier" in failure for failure in result.failures)
    assert result.archive_paths is None
    checkpoints.assert_not_called()
    for writer in writers:
        writer.assert_not_called()
    assert staged_path.read_bytes() == staged_before
    assert {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()} == archive_before
    assert json.loads(draft_path.read_text(encoding="utf-8")) == draft


@pytest.mark.parametrize(("field", "value"), [
    ("program_id", "acme"),
    ("program_id", "other"),
    ("run_id", "gather-other"),
    ("status", GatherRunStatus.RUNNING),
    ("schema_version", "gather-run.unsupported"),
    ("schema_version", True),
    ("schema_version", 1.0),
    ("program_id", None),
    ("run_id", None),
    ("status", None),
    ("schema_version", None),
    ("query_results", "malformed"),
    ("alert_ids", "malformed"),
    ("query_results", "deep-decode"),
    ("actor_identity_type", "deep-hash"),
])
@pytest.mark.parametrize("dry_run", [False, True])
def test_confirm_gather_pin_identity_gates_archive(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    field: str, value: str | bool | float | None, dry_run: bool,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    committed = _commit_gather_run(programs_root)
    invalid = (
        replace(committed, **{field: value})
        if value is not None and field not in {"query_results", "alert_ids"} else committed
    )
    invalid = replace(invalid, manifest_hash=compute_manifest_hash(invalid))
    manifest_path = write_manifest(
        get_committed_run_dir("acme", committed.run_id, programs_root=programs_root), invalid,
    )
    if value in {"deep-decode", "deep-hash"}:
        _write_nested_manifest(manifest_path, "decode" if value == "deep-decode" else "hash")
    elif value is None or field in {"query_results", "alert_ids"}:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if field == "query_results":
            payload[field] = None
        elif field == "alert_ids":
            payload[field] = ["alert", 1]
        else:
            del payload[field]
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    draft_path = (
        programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    )
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft.update(gather_run_id=committed.run_id, gather_run_hash=invalid.manifest_hash)
    report_manifest_path = draft_path.with_name("issue_001.manifest.json")
    report_manifest = json.loads(report_manifest_path.read_text(encoding="utf-8"))
    capture = report_manifest["metadata"]["run_evidence_snapshot"]
    for payload in (report_manifest, capture):
        payload.update(gather_run_id=draft["gather_run_id"], gather_run_hash=draft["gather_run_hash"])
    capture.pop("sha256")
    capture["sha256"] = hash_jsonable(capture)
    draft["run_evidence_snapshot_sha256"] = capture["sha256"]
    _rebind_program_verdict(report_manifest, draft, capture)
    report_manifest_path.write_text(json.dumps(report_manifest), encoding="utf-8")
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    archive_before = {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()}
    writers = [Mock(wraps=getattr(confirm, name)) for name in ("write_confirmed", "write_confirmed_issue")]
    for name, writer in zip(("write_confirmed", "write_confirmed_issue"), writers, strict=True):
        monkeypatch.setattr(confirm, name, writer)
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)

    result = confirm.confirm_issue(
        EDITION_NAME, 1, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, force=True, dry_run=dry_run,
    )

    if value == "acme":
        assert result.exit_code == 0, result.failures
        assert not result.failures
        if dry_run:
            assert result.archive_paths is None
            checkpoints.assert_not_called()
            for writer in writers:
                writer.assert_not_called()
            assert {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()} == archive_before
        else:
            assert result.archive_paths is not None
            assert result.archive_paths.snapshot_path.exists()
            checkpoints.assert_called_once()
            for writer in writers:
                writer.assert_called_once()
        return

    assert result.exit_code != 0
    assert any(
        (
            "invalid: no readable" if value is None or field == "query_results"
            else "hash verification" if field == "alert_ids" or value == "deep-hash"
            else "program/run/status/schema mismatch"
        ) in failure
        for failure in result.failures
    )
    assert result.archive_paths is None
    checkpoints.assert_not_called()
    for writer in writers:
        writer.assert_not_called()
    assert {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()} == archive_before
    assert json.loads(draft_path.read_text(encoding="utf-8")) == draft


@pytest.mark.parametrize("change", [
    "corruption", "newer_full", "malformed_structure", "malformed_hash", "deep_decode", "deep_hash",
])
def test_forced_confirm_rechecks_gather_pin_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    committed = _commit_gather_run(programs_root)
    draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft.update(gather_run_id=committed.run_id, gather_run_hash=committed.manifest_hash)
    report_manifest_path = draft_path.with_name("issue_001.manifest.json")
    report_manifest = json.loads(report_manifest_path.read_text(encoding="utf-8"))
    capture = report_manifest["metadata"]["run_evidence_snapshot"]
    for payload in (report_manifest, capture):
        payload.update(gather_run_id=draft["gather_run_id"], gather_run_hash=draft["gather_run_hash"])
    capture.pop("sha256")
    capture["sha256"] = hash_jsonable(capture)
    draft["run_evidence_snapshot_sha256"] = capture["sha256"]
    _rebind_program_verdict(report_manifest, draft, capture)
    report_manifest_path.write_text(json.dumps(report_manifest), encoding="utf-8")
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    archive_before = {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()}
    lock = archive_transaction.ArchiveLock
    checkpoints = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoints)
    writers = [Mock(wraps=getattr(confirm, name)) for name in ("write_confirmed", "write_confirmed_issue")]
    for name, writer in zip(("write_confirmed", "write_confirmed_issue"), writers, strict=True):
        monkeypatch.setattr(confirm, name, writer)
    entered = []

    @contextmanager
    def change_gather_after_lock(path: Path):
        with lock(path):
            entered.append(path)
            if change == "corruption":
                invalid = replace(committed, program_id="foreign")
                invalid = replace(invalid, manifest_hash=compute_manifest_hash(invalid))
                write_manifest(
                    get_committed_run_dir("acme", committed.run_id, programs_root=programs_root), invalid,
                )
            elif change == "newer_full":
                newer = _commit_gather_run(programs_root)
                assert newer.run_id != committed.run_id
            else:
                path = get_committed_run_dir("acme", committed.run_id, programs_root=programs_root) / "manifest.json"
                if change in {"deep_decode", "deep_hash"}:
                    _write_nested_manifest(path, "decode" if change == "deep_decode" else "hash")
                else:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if change == "malformed_structure":
                        payload["query_results"] = None
                    else:
                        payload["alert_ids"] = ["alert", 1]
                    path.write_text(json.dumps(payload), encoding="utf-8")
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_gather_after_lock)
    with pytest.raises(ConfigError, match="(?i)(gather|stale|mismatch)"):
        confirm.confirm_issue(
            EDITION_NAME, 1, reports_root=reports_root, archive_root=archive_root,
            programs_root=programs_root, force=True,
        )

    assert len(entered) == 1
    checkpoints.assert_not_called()
    for writer in writers:
        writer.assert_not_called()
    assert {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()} == archive_before
    assert json.loads(draft_path.read_text(encoding="utf-8")) == draft


def test_confirm_does_not_mark_valid_pin_stale_for_newer_failed_full_run(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    pinned = _commit_gather_run(programs_root)
    newer = _commit_gather_run(programs_root)
    assert pinned.run_id != newer.run_id
    assert pinned.finished_at is not None
    failed = replace(
        newer, status=GatherRunStatus.FAILED,
        finished_at=pinned.finished_at + timedelta(minutes=5), manifest_hash=None,
    )
    failed = replace(failed, manifest_hash=compute_manifest_hash(failed))
    failed_path = write_manifest(
        get_committed_run_dir("acme", failed.run_id, programs_root=programs_root), failed,
    )
    gather_run_manifest._write_pointer(
        get_latest_full_pointer_path("acme", programs_root=programs_root),
        run_id=failed.run_id,
        manifest_hash=failed.manifest_hash,
    )

    draft_path = (
        programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    )
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft.update(gather_run_id=pinned.run_id, gather_run_hash=pinned.manifest_hash)
    report_manifest_path = draft_path.with_name("issue_001.manifest.json")
    report_manifest = json.loads(report_manifest_path.read_text(encoding="utf-8"))
    capture = report_manifest["metadata"]["run_evidence_snapshot"]
    for payload in (report_manifest, capture):
        payload.update(gather_run_id=draft["gather_run_id"], gather_run_hash=draft["gather_run_hash"])
    capture.pop("sha256")
    capture["sha256"] = hash_jsonable(capture)
    draft["run_evidence_snapshot_sha256"] = capture["sha256"]
    _rebind_program_verdict(report_manifest, draft, capture)
    report_manifest_path.write_text(json.dumps(report_manifest), encoding="utf-8")
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    archive_before = {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()}
    writers = [Mock(wraps=getattr(confirm, name)) for name in ("write_confirmed", "write_confirmed_issue")]
    for name, writer in zip(("write_confirmed", "write_confirmed_issue"), writers, strict=True):
        monkeypatch.setattr(confirm, name, writer)

    with pytest.raises(ValueError, match="program/run/status/schema mismatch"):
        confirm.confirm_issue(
            EDITION_NAME, 1, reports_root=reports_root, archive_root=archive_root,
            programs_root=programs_root, force=True,
        )

    assert failed_path.exists()
    for writer in writers:
        writer.assert_not_called()
    assert {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()} == archive_before
    assert json.loads(draft_path.read_text(encoding="utf-8")) == draft
