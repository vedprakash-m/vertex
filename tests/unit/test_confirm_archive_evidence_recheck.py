from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

import src.commands.confirm_stages.archive_transaction as archive_transaction
from src.core.exceptions import ConfigError
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
from tests.unit.test_commands_confirm import (
    _isolate_fact_store_db as _isolate_fact_store_db,
    test_confirm_issue_archives_outputs_and_resets_active_state as _exercise_actual_confirm,
)
from tests.unit.test_commands_report import repo_root as repo_root


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
        _exercise_actual_confirm(repo_root, tmp_path)

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
        _exercise_actual_confirm(repo_root, tmp_path)

    checkpoints.assert_not_called()
    assert not tuple((tmp_path / "archive").rglob("*.snapshot.json"))
