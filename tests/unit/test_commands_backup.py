from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.backup import BACKUP_MANIFEST_NAME, create_repository_backup, verify_repository_backup


runner = CliRunner()


def test_create_repository_backup_copies_expected_roots_and_writes_manifest(tmp_path: Path) -> None:
    repo_root = _seed_backup_workspace(tmp_path / "repo")
    backup_root = tmp_path / "backup"

    result = create_repository_backup(backup_root, source_root=repo_root)

    assert result.file_count == 9
    assert (backup_root / "programs" / "acme" / "program.yaml").exists()
    assert (backup_root / "programs" / "acme" / "journal" / "2026-W19.jsonl").exists()
    assert (backup_root / "programs" / "acme" / "knowledge" / "entities.yaml").exists()
    assert (backup_root / "programs" / "acme" / "ledger" / "events" / "2026-06.jsonl").exists()
    assert (backup_root / "programs" / "acme" / "ledger" / "evidence" / "ab" / "ab1234artifact").exists()
    assert (backup_root / "programs" / "acme" / "ledger" / "evidence" / "ab" / "ab1234artifact.meta.json").exists()
    assert (backup_root / "knowledge" / "people_directory.yaml").exists()
    assert (backup_root / "knowledge" / "domains" / "storage-platform" / "sources.yaml").exists()
    assert (backup_root / "editions" / "acme_weekly.yaml").exists()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0"
    assert manifest["included_roots"] == ["programs", "knowledge", "editions"]
    assert [entry["relative_path"] for entry in manifest["files"]] == [
        "programs/acme/journal/2026-W19.jsonl",
        "programs/acme/knowledge/entities.yaml",
        "programs/acme/ledger/events/2026-06.jsonl",
        "programs/acme/ledger/evidence/ab/ab1234artifact",
        "programs/acme/ledger/evidence/ab/ab1234artifact.meta.json",
        "programs/acme/program.yaml",
        "knowledge/domains/storage-platform/sources.yaml",
        "knowledge/people_directory.yaml",
        "editions/acme_weekly.yaml",
    ]


def test_verify_repository_backup_detects_checksum_mismatch(tmp_path: Path) -> None:
    repo_root = _seed_backup_workspace(tmp_path / "repo")
    backup_root = tmp_path / "backup"
    create_repository_backup(backup_root, source_root=repo_root)

    tampered_path = backup_root / "knowledge" / "people_directory.yaml"
    tampered_path.write_text("alias: tampered\n", encoding="utf-8")

    result = verify_repository_backup(backup_root)

    assert result.checked_file_count == 9
    assert result.missing_paths == ()
    assert result.mismatched_paths == ("knowledge/people_directory.yaml",)


def test_backup_cli_creates_and_verifies_backup(monkeypatch, tmp_path: Path) -> None:
    repo_root = _seed_backup_workspace(tmp_path / "repo")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", repo_root)

    create_result = runner.invoke(app, ["backup", "--to", str(backup_root)])
    verify_result = runner.invoke(app, ["backup", "--verify", str(backup_root)])

    assert create_result.exit_code == 0
    assert f"Manifest: {backup_root / BACKUP_MANIFEST_NAME}" in create_result.stdout
    assert verify_result.exit_code == 0
    assert "Backup verified: 9 files checked" in verify_result.stdout


def test_backup_cli_reports_verification_failures(monkeypatch, tmp_path: Path) -> None:
    repo_root = _seed_backup_workspace(tmp_path / "repo")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", repo_root)

    runner.invoke(app, ["backup", "--to", str(backup_root)])
    (backup_root / "programs" / "acme" / "program.yaml").unlink()

    result = runner.invoke(app, ["backup", "--verify", str(backup_root)])

    assert result.exit_code == 2
    assert "Backup verification failed: 1 missing, 0 checksum mismatches" in result.stdout
    assert "Missing: programs/acme/program.yaml" in result.stdout


def test_backup_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    repo_root = _seed_backup_workspace(tmp_path / "repo")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", repo_root)

    create_json = runner.invoke(app, ["backup", "--to", str(backup_root), "--format", "json"])

    assert create_json.exit_code == 0
    create_payload = json.loads(create_json.stdout)
    assert create_payload["mode"] == "create"
    assert create_payload["file_count"] == 9
    assert create_payload["destination_root"] == str(backup_root)
    assert create_payload["manifest_path"] == str(backup_root / BACKUP_MANIFEST_NAME)

    verify_csv = runner.invoke(app, ["backup", "--verify", str(backup_root), "--format", "csv"])

    assert verify_csv.exit_code == 0
    lines = verify_csv.stdout.strip().splitlines()
    assert lines[0] == "mode,backup_root,is_valid,checked_file_count,missing_paths,mismatched_paths,manifest_path,error"
    assert lines[1] == f"verify,{backup_root.resolve()},True,9,,,{backup_root.resolve() / BACKUP_MANIFEST_NAME},"


def _seed_backup_workspace(repo_root: Path) -> Path:
    (repo_root / "programs" / "acme" / "journal").mkdir(parents=True, exist_ok=True)
    (repo_root / "programs" / "acme" / "knowledge").mkdir(parents=True, exist_ok=True)
    (repo_root / "programs" / "acme" / "ledger" / "events").mkdir(parents=True, exist_ok=True)
    (repo_root / "programs" / "acme" / "ledger" / "evidence" / "ab").mkdir(parents=True, exist_ok=True)
    (repo_root / "knowledge" / "domains" / "storage-platform").mkdir(parents=True, exist_ok=True)
    (repo_root / "knowledge").mkdir(parents=True, exist_ok=True)
    (repo_root / "editions").mkdir(parents=True, exist_ok=True)

    (repo_root / "programs" / "acme" / "program.yaml").write_text("name: Acme\n", encoding="utf-8")
    (repo_root / "programs" / "acme" / "journal" / "2026-W19.jsonl").write_text(
        '{"id":"signal-1"}\n',
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "knowledge" / "entities.yaml").write_text("entities: []\n", encoding="utf-8")
    (repo_root / "programs" / "acme" / "ledger" / "events" / "2026-06.jsonl").write_text(
        '{"event_id":"evt-1"}\n',
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "ledger" / "evidence" / "ab" / "ab1234artifact").write_text(
        "captured evidence\n",
        encoding="utf-8",
    )
    (repo_root / "programs" / "acme" / "ledger" / "evidence" / "ab" / "ab1234artifact.meta.json").write_text(
        '{"vault_hash":"sha256:ab1234artifact"}\n',
        encoding="utf-8",
    )
    (repo_root / "knowledge" / "people_directory.yaml").write_text("people: []\n", encoding="utf-8")
    (repo_root / "knowledge" / "domains" / "storage-platform" / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (repo_root / "editions" / "acme_weekly.yaml").write_text("program: acme\n", encoding="utf-8")
    return repo_root