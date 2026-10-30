from __future__ import annotations

import json
import csv
from io import StringIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.core.backup import BACKUP_MANIFEST_NAME, create_repository_backup, verify_repository_backup


runner = CliRunner()


@pytest.mark.parametrize("version", ["1.1", "1.0", "unversioned"])
@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
@pytest.mark.parametrize("declarations", [
    '"files": [], "files": []',
    '"metadata": {"PRIVATE-DUPLICATE": 0, "PRIVATE-DUPLICATE": 1}',
    '"metadata": {"PRIVATE-DUPLICATE": 1, "PRIVATE-DUPLICATE": 1}',
    r'"metadata": {"PRIVATE-DUPLICATE": 0, "\u0050RIVATE-DUPLICATE": 1}',
    '"schema_version": "1.0", "schema_version": "1.0"',
    '"schema_version": "1.1", "schema_version": "1.0"',
])
def test_verify_cli_refuses_duplicate_json_properties(
    version: str, output_format: str, declarations: str, tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "knowledge").mkdir(parents=True)
    (source / "knowledge" / "note.md").write_text("original", encoding="utf-8")
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    manifest = backup / BACKUP_MANIFEST_NAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if version != "1.1":
        payload.pop("declared_scope")
        payload.pop("schema_version")
        if version == "1.0":
            payload["schema_version"] = version
    unique = json.dumps(payload)
    manifest.write_text(unique, encoding="utf-8")
    arguments = ["backup", "--verify", str(backup), "--format", output_format]
    assert runner.invoke(app, arguments).exit_code == 0
    (backup / "knowledge" / "note.md").write_text("tampered", encoding="utf-8")
    assert runner.invoke(app, arguments).exit_code == 2
    manifest.write_text(unique[:-1] + ", " + declarations + "}", encoding="utf-8")
    result = runner.invoke(app, arguments)
    assert result.exit_code == 2, result.stdout
    assert "duplicate" in result.stdout
    assert "PRIVATE-DUPLICATE" not in result.stdout
    assert "Listed-file integrity verified" not in result.stdout
    if output_format != "human":
        error = json.loads(result.stdout) if output_format == "json" else next(csv.DictReader(StringIO(result.stdout)))
        assert error["error"]
        assert error["is_valid"] in (False, "False")
        assert "declared_scope_check" not in error


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
@pytest.mark.parametrize("fault", ["source", "copy", "copy_io", "hash_io"])
def test_restore_cli_refuses_post_preflight_failures(
    output_format: str, fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.backup as backup

    source = _seed_backup_workspace(tmp_path / "source")
    captured = tmp_path / "backup"
    destination = tmp_path / "restored"
    create_repository_backup(captured, source_root=source)
    original_verify = backup._verify_manifest
    original_copy = backup.shutil.copy2
    original_hash = backup._hash_file
    relative = Path("editions") / "acme_weekly.yaml"

    def verify(root, manifest):
        result = original_verify(root, manifest)
        if fault == "source":
            (captured / relative).write_text("changed\n", encoding="utf-8")
        return result

    def copy(src, dst):
        if dst == destination / relative:
            if fault == "copy_io":
                raise OSError("synthetic copy failure")
            if fault == "copy":
                dst.write_text("damaged\n", encoding="utf-8")
                return dst
        return original_copy(src, dst)

    def hash_file(path):
        if fault == "hash_io" and path == destination / relative:
            raise OSError("synthetic hash failure")
        return original_hash(path)

    monkeypatch.setattr(backup, "_verify_manifest", verify)
    monkeypatch.setattr(backup.shutil, "copy2", copy)
    monkeypatch.setattr(backup, "_hash_file", hash_file)
    result = runner.invoke(app, ["backup", "--restore", str(destination), "--from", str(captured), "--format", output_format])
    assert result.exit_code == 2, result.stdout
    assert "editions/acme_weekly.yaml" in result.stdout
    if output_format == "human":
        assert "Restore failed:" in result.stdout
        assert "Restore copied" not in result.stdout
    else:
        payload = json.loads(result.stdout) if output_format == "json" else next(csv.DictReader(StringIO(result.stdout)))
        assert payload["error"]
        assert "file_count" not in payload


@pytest.fixture(autouse=True)
def _backup_authority_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERTEX_PROGRAMS_ROOT", raising=False)
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
@pytest.mark.parametrize("mode", ["create", "verify", "restore", "skip", "legacy", "invalid", "mapping"])
def test_declared_scope_cli_contract(output_format: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _seed_backup_workspace(tmp_path / "source")
    backup = tmp_path / "backup"
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", source)
    if mode != "mapping":
        create_repository_backup(backup, source_root=source)
    args = ["backup", "--format", output_format]
    if mode in {"create", "mapping"}:
        args += ["--to", str(tmp_path / "new_backup")]
        if mode == "mapping":
            monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "external"))
    elif mode in {"restore", "skip"}:
        args += ["--restore", str(tmp_path / "restored"), "--from", str(backup)]
        if mode == "skip":
            args += ["--skip-preflight"]
    else:
        args += ["--verify", str(backup)]
        if mode in {"legacy", "invalid"}:
            manifest_path = backup / BACKUP_MANIFEST_NAME
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload.pop("declared_scope", None)
            payload["schema_version"] = "1.0" if mode == "legacy" else "1.1"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, args)
    assert result.exit_code == (2 if mode in {"invalid", "mapping"} else 0), result.stdout
    if mode in {"invalid", "mapping"}:
        assert "failed" in result.stdout.lower() if output_format == "human" else "error" in result.stdout
        return
    expected = "unassessed" if mode == "legacy" else "not_checked" if mode in {"create", "skip"} else "passed"
    if output_format == "human":
        assert "recovery not assessed" in result.stdout.lower()
        assert "complete" not in result.stdout.lower()
        assert expected.replace("_", " ") in result.stdout.lower()
    else:
        payload = json.loads(result.stdout) if output_format == "json" else next(csv.DictReader(StringIO(result.stdout)))
        assert payload["capture_scope"] == ("legacy_unspecified" if mode == "legacy" else "repository_subset")
        assert payload["declared_scope_check"] == expected
        assert payload["recovery_qualification"] == "not_assessed"
        assert payload["absent_unassessed_store_count"] == (
            None if output_format == "json" and mode == "legacy" else
            "" if mode == "legacy" else 1 if output_format == "json" else "1"
        )


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
    assert manifest["schema_version"] == "1.1"
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
    assert "Listed-file integrity verified: 9 files checked" in verify_result.stdout


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


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_backup_cli_rejects_linked_capture_scope(
    output_format: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import stat
    from types import SimpleNamespace

    repo_root = _seed_backup_workspace(tmp_path / "repo")
    linked_file = repo_root / "editions" / "acme_weekly.yaml"
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", repo_root)
    original_lstat = Path.lstat

    def fake_lstat(path: Path):
        if path == linked_file:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    backup_root = tmp_path / "backup"
    result = runner.invoke(app, ["backup", "--to", str(backup_root), "--format", output_format])

    assert result.exit_code == 2, result.stdout
    assert not (backup_root / BACKUP_MANIFEST_NAME).exists()
    assert not (backup_root / "editions" / "acme_weekly.yaml").exists()
    assert not backup_root.exists() or not any(backup_root.iterdir())
    if output_format == "human":
        assert "Backup failed:" in result.stdout
    else:
        payload = json.loads(result.stdout) if output_format == "json" else next(csv.DictReader(StringIO(result.stdout)))
        assert payload["error"]
        assert "file_count" not in payload


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_backup_cli_preserves_valid_capture_controls(
    output_format: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo_root = _seed_backup_workspace(tmp_path / "repo")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", repo_root)

    result = runner.invoke(app, ["backup", "--to", str(backup_root), "--format", output_format])

    assert result.exit_code == 0, result.stdout
    assert verify_repository_backup(backup_root).is_valid
    manifest = json.loads((backup_root / BACKUP_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["included_roots"] == ["programs", "knowledge", "editions"]
    if output_format == "human":
        assert "Repository subset captured: 9 files copied" in result.stdout
    else:
        payload = json.loads(result.stdout) if output_format == "json" else next(csv.DictReader(StringIO(result.stdout)))
        assert payload["file_count"] == (9 if output_format == "json" else "9")


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
    assert lines[0] == "mode,backup_root,is_valid,checked_file_count,missing_paths,mismatched_paths,manifest_path,error,capture_scope,declared_scope_check,absent_unassessed_store_count,recovery_qualification"
    assert lines[1] == f"verify,{backup_root.resolve()},True,9,,,{backup_root.resolve() / BACKUP_MANIFEST_NAME},,repository_subset,passed,1,not_assessed"


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