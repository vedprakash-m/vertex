from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.commands import confirm
import src.commands.confirm_stages.archive_transaction as archive_transaction
from src.core.exceptions import ConfigError
from src.core.gather_run_manifest import (
    GatherRunStatus,
    compute_manifest_hash,
    get_committed_run_dir,
    write_manifest,
)
from tests.unit.test_commands_confirm import (
    EDITION_NAME,
    _isolate_fact_store_db,
    _prepare_confirmable_weekly_issue,
    repo_root,
)
from tests.unit.test_report_gather_run_lineage import _commit_gather_run

pytestmark = pytest.mark.usefixtures(_isolate_fact_store_db.__name__)


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
])
@pytest.mark.parametrize("dry_run", [False, True])
def test_confirm_gather_pin_identity_gates_archive(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    field: str, value: str | bool | float | None, dry_run: bool,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    committed = _commit_gather_run(programs_root)
    invalid = replace(committed, **{field: value}) if value is not None else committed
    invalid = replace(invalid, manifest_hash=compute_manifest_hash(invalid))
    manifest_path = write_manifest(
        get_committed_run_dir("acme", committed.run_id, programs_root=programs_root), invalid,
    )
    if value is None:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        del payload[field]
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    draft_path = (
        programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    )
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft.update(gather_run_id=committed.run_id, gather_run_hash=invalid.manifest_hash)
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    archive_before = {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()}
    writers = [Mock(wraps=getattr(confirm, name)) for name in ("write_confirmed", "write_confirmed_issue")]
    for name, writer in zip(("write_confirmed", "write_confirmed_issue"), writers, strict=True):
        monkeypatch.setattr(confirm, name, writer)

    result = confirm.confirm_issue(
        EDITION_NAME, 1, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, force=True, dry_run=dry_run,
    )

    if value == "acme":
        assert result.exit_code == 0, result.failures
        assert not result.failures
        if dry_run:
            assert result.archive_paths is None
            for writer in writers:
                writer.assert_not_called()
            assert {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()} == archive_before
        else:
            assert result.archive_paths is not None
            assert result.archive_paths.snapshot_path.exists()
            for writer in writers:
                writer.assert_called_once()
        return

    assert result.exit_code != 0
    assert any(
        ("invalid: no readable" if value is None else "program/run/status/schema mismatch") in failure
        for failure in result.failures
    )
    assert result.archive_paths is None
    for writer in writers:
        writer.assert_not_called()
    assert {path: path.read_bytes() for path in archive_root.rglob("*") if path.is_file()} == archive_before
    assert json.loads(draft_path.read_text(encoding="utf-8")) == draft


@pytest.mark.parametrize("change", ["corruption", "newer_full"])
def test_forced_confirm_rechecks_gather_pin_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    committed = _commit_gather_run(programs_root)
    draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft.update(gather_run_id=committed.run_id, gather_run_hash=committed.manifest_hash)
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
            else:
                newer = _commit_gather_run(programs_root)
                assert newer.run_id != committed.run_id
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
