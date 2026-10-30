from pathlib import Path
from contextlib import contextmanager
import json
from unittest.mock import Mock

import pytest
from typer import BadParameter

import src.commands.confirm as confirm_module
import src.commands.confirm_stages.archive_transaction as archive_transaction
from src.core.exceptions import ConfigError
from src.core.program_fact_store import ProgramFactStore
from src.core.edition_resolver import get_program_output_dir
from tests.unit.test_commands_confirm import (
    _isolate_fact_store_db as _isolate_fact_store_db,
    test_confirm_issue_archives_outputs_and_resets_active_state as _exercise_actual_confirm,
)
from tests.unit.test_commands_report import repo_root as repo_root


@pytest.mark.parametrize("scope", ["edition_name", "issue_number"])
def test_forced_confirm_rejects_foreign_fact_pin_scope(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    load_draft = confirm_module._load_draft_state
    archive = Mock(wraps=confirm_module._execute_archive_transaction_impl)
    monkeypatch.setattr(confirm_module, "_execute_archive_transaction_impl", archive)

    def foreign_pin(*args, **kwargs):
        draft = load_draft(*args, **kwargs)
        path = get_program_output_dir(args[0], programs_root=kwargs["programs_root"]) / f"issue_{args[1]:03d}" / f"issue_{args[1]:03d}.manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["metadata"].pop("run_evidence_snapshot")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        draft.pop("run_evidence_snapshot_sha256")
        metadata = {"edition_name": "acme_weekly", "issue_number": 1}
        metadata[scope] = "foreign_edition" if scope == "edition_name" else 2
        pin = ProgramFactStore("acme").pin_snapshot(metadata=metadata)
        return {
            **draft,
            "program_fact_snapshot": {
                **draft["program_fact_snapshot"],
                "snapshot_id": pin.snapshot_id,
            },
        }

    monkeypatch.setattr(confirm_module, "_load_draft_state", foreign_pin)
    with pytest.raises(AssertionError, match="(?i)scope"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)
    archive.assert_not_called()


def test_confirm_rechecks_fact_scope_after_archive_lock(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = archive_transaction.ArchiveLock
    checkpoint = Mock(wraps=archive_transaction.create_checkpoint_snapshot)
    monkeypatch.setattr(archive_transaction, "create_checkpoint_snapshot", checkpoint)

    @contextmanager
    def change_scope(path):
        with lock(path):
            store = ProgramFactStore("acme")
            with store._connect() as connection:
                connection.execute(
                    "UPDATE program_fact_snapshot_pins SET metadata_json = ?",
                    (json.dumps({"edition_name": "foreign", "issue_number": 1}),),
                )
            yield

    monkeypatch.setattr(archive_transaction, "ArchiveLock", change_scope)
    with pytest.raises(ConfigError, match="scope"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)
    checkpoint.assert_not_called()
    assert not tuple((tmp_path / "archive").rglob("*.snapshot.json"))


def test_confirm_preserves_unscoped_legacy_pin_compatibility(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_draft = confirm_module._load_draft_state

    def legacy_pin(*args, **kwargs):
        draft = load_draft(*args, **kwargs)
        path = get_program_output_dir(args[0], programs_root=kwargs["programs_root"]) / f"issue_{args[1]:03d}" / f"issue_{args[1]:03d}.manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["metadata"].pop("run_evidence_snapshot")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        draft.pop("run_evidence_snapshot_sha256")
        pin = ProgramFactStore("acme").pin_snapshot()
        return {
            **draft,
            "program_fact_snapshot": {
                **draft["program_fact_snapshot"],
                "snapshot_id": pin.snapshot_id,
            },
        }

    monkeypatch.setattr(confirm_module, "_load_draft_state", legacy_pin)
    _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)


def test_modern_capture_mismatch_refuses_before_artifacts_without_writes(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_draft = confirm_module._load_draft_state
    before: dict[Path, bytes] = {}
    artifacts = Mock(side_effect=AssertionError("Artifact construction must not run."))
    monkeypatch.setattr(confirm_module, "_build_confirm_artifacts", artifacts)

    def mismatched_reference(*args, **kwargs):
        draft = load_draft(*args, **kwargs)
        assert draft["run_evidence_snapshot_sha256"]
        before.update({path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()})
        return {**draft, "run_evidence_snapshot_sha256": "mismatched-reference"}

    monkeypatch.setattr(confirm_module, "_load_draft_state", mismatched_reference)
    with pytest.raises(BadParameter, match="capture does not match"):
        _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)
    artifacts.assert_not_called()
    assert before
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
