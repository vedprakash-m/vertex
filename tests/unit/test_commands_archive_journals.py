from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from cli import app
from src.core.journal import append_signal
from src.core.models import Confidence
from src.core.models_v2 import Signal


runner = CliRunner()


def test_archive_journals_cli_moves_weekly_files(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)
    older_timestamp = datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc)
    newer_timestamp = datetime(2025, 1, 21, 12, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="sig-old",
            timestamp=older_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Older signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=older_timestamp,
    )
    append_signal(
        Signal(
            id="sig-new",
            timestamp=newer_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Newer signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=newer_timestamp,
    )

    monkeypatch.setattr("src.commands.archive_journals.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["archive-journals", "--program", "acme", "--before", "2025-W03"])

    assert result.exit_code == 0
    assert "Archived 1 weekly journal file(s) for acme." in result.stdout
    assert (programs_root / "acme" / "journal_archive" / "2025-W02.jsonl").exists()
    assert not (programs_root / "acme" / "journal" / "2025-W02.jsonl").exists()
    assert (programs_root / "acme" / "journal" / "2025-W04.jsonl").exists()


def test_archive_journals_cli_uses_retention_policy(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme",
                "name": "Acme",
                "retention_days": {
                    "default": 365,
                    "manual": 30,
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    eligible_timestamp = datetime(2025, 1, 14, 12, 0, tzinfo=timezone.utc)
    mixed_manual_timestamp = datetime(2025, 1, 7, 12, 0, tzinfo=timezone.utc)
    mixed_ado_timestamp = datetime(2025, 1, 8, 12, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="sig-eligible",
            timestamp=eligible_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Eligible manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=eligible_timestamp,
    )
    append_signal(
        Signal(
            id="sig-mixed-manual",
            timestamp=mixed_manual_timestamp,
            source="manual",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Mixed manual signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=mixed_manual_timestamp,
    )
    append_signal(
        Signal(
            id="sig-mixed-ado",
            timestamp=mixed_ado_timestamp,
            source="ado/revision",
            program_id="acme",
            workstream_id=None,
            entity_refs=(),
            text="Mixed ADO signal.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
        programs_root=programs_root,
        partition_at=mixed_ado_timestamp,
    )

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 3, 15, 12, 0, tzinfo=tz or timezone.utc)

    monkeypatch.setattr("src.commands.archive_journals.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.archive_journals.datetime", _FixedDateTime)

    result = runner.invoke(app, ["archive-journals", "--program", "acme", "--retention"])

    assert result.exit_code == 0
    assert "Archived 1 weekly journal file(s) for acme using retention policy." in result.stdout
    assert (programs_root / "acme" / "journal_archive" / "2025-W03.jsonl").exists()
    assert (programs_root / "acme" / "journal" / "2025-W02.jsonl").exists()


def test_archive_journals_cli_requires_before_or_retention(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)
    monkeypatch.setattr("src.commands.archive_journals.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["archive-journals", "--program", "acme"])

    assert result.exit_code == 2


def test_archive_journals_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    archived_path = tmp_path / "programs" / "acme" / "journal_archive" / "2025-W02.jsonl"
    (programs_root / "acme").mkdir(parents=True)
    monkeypatch.setattr("src.commands.archive_journals.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.archive_journals.archive_weekly_journal_files",
        lambda program_id, before_week, programs_root: [archived_path],
    )

    json_result = runner.invoke(
        app,
        ["archive-journals", "--program", "acme", "--before", "2025-W03", "--format", "json"],
    )

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "acme"
    assert payload["mode"] == "before_week"
    assert payload["before_week"] == "2025-W03"
    assert payload["archived_count"] == 1
    assert payload["moved_paths"] == [str(archived_path)]

    csv_result = runner.invoke(
        app,
        ["archive-journals", "--program", "acme", "--before", "2025-W03", "--format", "csv"],
    )

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "row_type,program_id,mode,before_week,archived_count,path"
    assert lines[1] == "summary,acme,before_week,2025-W03,1,"
    assert lines[2] == f"path,acme,before_week,2025-W03,,{archived_path}"