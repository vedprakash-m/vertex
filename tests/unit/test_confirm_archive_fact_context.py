from pathlib import Path
from unittest.mock import Mock

import pytest

import src.commands.confirm_stages.archive_transaction as archive_transaction
from tests.unit.test_commands_confirm import (
    _isolate_fact_store_db as _isolate_fact_store_db,
    test_confirm_issue_archives_outputs_and_resets_active_state as _exercise_actual_confirm,
)
from tests.unit.test_commands_report import repo_root as repo_root


def test_actual_confirm_archive_fact_capture_preserves_selected_archive(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_facts = Mock(wraps=archive_transaction.load_program_facts)
    monkeypatch.setattr(archive_transaction, "load_program_facts", load_facts)

    _exercise_actual_confirm(repo_root, tmp_path, monkeypatch)

    load_facts.assert_called_once_with(
        "acme",
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )
