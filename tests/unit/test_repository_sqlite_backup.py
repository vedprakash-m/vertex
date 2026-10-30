from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.core.backup import BACKUP_MANIFEST_NAME, create_repository_backup, find_backups_referencing_paths, restore_repository_backup, verify_repository_backup
from src.core.checkpoint_store import _copy_sqlite, create_checkpoint_snapshot
from src.core.exceptions import StateError


@pytest.fixture(autouse=True)
def _backup_authority_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERTEX_PROGRAMS_ROOT", raising=False)
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)


def _canonical_source(root: Path) -> Path:
    program = root / "programs" / "synthetic"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text("id: synthetic\n", encoding="utf-8")
    return root / "vertex-db" / "synthetic" / "vertex.sqlite3"


def _manifest(root: Path) -> dict:
    return json.loads((root / BACKUP_MANIFEST_NAME).read_text(encoding="utf-8"))


def _save_manifest(root: Path, payload: dict) -> None:
    (root / BACKUP_MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("version", ["1.1", "1.0", "unversioned"])
@pytest.mark.parametrize("declarations", [
    '"files": [], "files": []',
    '"metadata": {"PRIVATE-DUPLICATE": 0, "PRIVATE-DUPLICATE": 1}',
    '"metadata": {"PRIVATE-DUPLICATE": 1, "PRIVATE-DUPLICATE": 1}',
    r'"metadata": {"PRIVATE-DUPLICATE": 0, "\u0050RIVATE-DUPLICATE": 1}',
    '"schema_version": "1.0", "schema_version": "1.0"',
    '"schema_version": "1.1", "schema_version": "1.0"',
])
def test_backup_manifest_refuses_duplicate_json_properties(
    version: str, declarations: str, tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "knowledge").mkdir(parents=True)
    (source / "knowledge" / "note.md").write_text("original", encoding="utf-8")
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    payload = _manifest(backup)
    if version != "1.1":
        payload.pop("declared_scope")
        payload.pop("schema_version")
        if version == "1.0":
            payload["schema_version"] = version
    unique = json.dumps(payload)
    assert verify_repository_backup(backup).is_valid
    hits = find_backups_referencing_paths(
        tmp_path, relative_paths={"knowledge/note.md"},
    )
    assert len(hits) == 1
    assert hits[0].matching_paths == ("knowledge/note.md",)
    (backup / "knowledge" / "note.md").write_text("tampered", encoding="utf-8")
    assert not verify_repository_backup(backup).is_valid
    (backup / BACKUP_MANIFEST_NAME).write_text(
        unique[:-1] + ", " + declarations + "}", encoding="utf-8",
    )
    for read in (
        lambda: verify_repository_backup(backup),
        lambda: find_backups_referencing_paths(tmp_path, relative_paths={"knowledge/note.md"}),
    ):
        with pytest.raises(StateError, match="duplicate") as caught:
            read()
        assert "PRIVATE-DUPLICATE" not in str(caught.value)
        assert "knowledge/note.md" not in str(caught.value)


@pytest.mark.parametrize("version", ["1.1", "1.0", "unversioned"])
def test_backup_manifest_distinct_objects_may_reuse_property_names(
    version: str, tmp_path: Path,
) -> None:
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=tmp_path / "absent")
    payload = _manifest(backup)
    if version != "1.1":
        payload.pop("declared_scope")
        payload.pop("schema_version")
        if version == "1.0":
            payload["schema_version"] = version
    payload["metadata"] = [{"same": 1}, {"same": 2}]
    _save_manifest(backup, payload)
    assert verify_repository_backup(backup).is_valid


def test_canonical_fact_database_capture_is_standalone_and_scope_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.backup as backup_module

    source = tmp_path / "source"
    database = _canonical_source(source)
    connection = _open_wal(database)
    try:
        connection.execute("INSERT INTO sample VALUES ('uncommitted')")
        backup = tmp_path / "backup"
        create_repository_backup(backup, source_root=source)
        manifest = _manifest(backup)
        assert manifest["schema_version"] == "1.1"
        assert manifest["included_roots"] == ["programs"]
        assert manifest["declared_scope"] == {
            "kind": "repository_subset",
            "program_ids": ["synthetic"],
            "fact_reality_stores": [{
                "program_id": "synthetic",
                "relative_path": "vertex-db/synthetic/vertex.sqlite3",
                "source_state": "present",
            }],
        }
        assert _standalone_values(backup / database.relative_to(source)) == [("committed",)]
        assert not list((backup / "vertex-db").rglob("*-wal"))
        result = verify_repository_backup(backup)
        assert result.is_valid
        assert result.declared_scope_check == "passed"
        assert result.absent_unassessed_store_count == 0
        restored = tmp_path / "restored"
        original_hash = backup_module._hash_file
        hashed = []

        def hash_file(path):
            hashed.append(path)
            return original_hash(path)

        monkeypatch.setattr(backup_module, "_hash_file", hash_file)
        restore_repository_backup(backup, restored)
        assert restored / database.relative_to(source) in hashed
        assert _standalone_values(restored / database.relative_to(source)) == [("committed",)]
    finally:
        connection.close()


def test_absent_store_is_not_inferred_unnecessary(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    backup = tmp_path / "backup"
    result = create_repository_backup(backup, source_root=source)
    assert result.absent_unassessed_store_count == 1
    assert _manifest(backup)["declared_scope"]["fact_reality_stores"][0]["source_state"] == "absent_unassessed"
    checked = verify_repository_backup(backup)
    assert checked.is_valid
    assert checked.absent_unassessed_store_count == 1


@pytest.mark.parametrize("damage", [
    "scope", "kind", "program_ids", "duplicate_program", "invalid_program", "stores",
    "missing_store", "duplicate_store", "foreign_store", "path", "state",
    "absent_with_file", "file_entry", "duplicate_file", "program_entry", "program_bytes",
    "database_bytes", "database_checksum", "version", "null_version", "bad_file", "bad_sha",
    "bad_files", "bad_store", "bad_relative", "absolute", "backslash", "traversal",
])
def test_scope_damage_refuses_default_restore_before_mutation(damage: str, tmp_path: Path) -> None:
    source = tmp_path / "source"
    database = _canonical_source(source)
    connection = _open_wal(database)
    connection.close()
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    payload = _manifest(backup)
    scope = payload.setdefault("declared_scope", {
        "kind": "repository_subset", "program_ids": ["synthetic"],
        "fact_reality_stores": [{"program_id": "synthetic", "relative_path": "vertex-db/synthetic/vertex.sqlite3", "source_state": "present"}],
    })
    store = scope["fact_reality_stores"][0]
    if damage == "scope":
        payload.pop("declared_scope")
    elif damage == "kind":
        scope["kind"] = "workspace"
    elif damage == "program_ids":
        scope["program_ids"] = None
    elif damage == "duplicate_program":
        scope["program_ids"].append("synthetic")
    elif damage == "invalid_program":
        scope["program_ids"] = ["../foreign"]
    elif damage == "stores":
        scope["fact_reality_stores"] = None
    elif damage == "missing_store":
        scope["fact_reality_stores"] = []
    elif damage == "duplicate_store":
        scope["fact_reality_stores"].append(dict(store))
    elif damage == "foreign_store":
        store["program_id"] = "foreign"
    elif damage == "path":
        store["relative_path"] = "../foreign.sqlite3"
    elif damage == "state":
        store["source_state"] = "optional"
    elif damage == "absent_with_file":
        store["source_state"] = "absent_unassessed"
    elif damage == "file_entry":
        payload["files"] = [entry for entry in payload["files"] if entry["relative_path"] != store["relative_path"]]
    elif damage == "duplicate_file":
        payload["files"].append(dict(payload["files"][0]))
    elif damage == "program_entry":
        payload["files"] = [entry for entry in payload["files"] if not entry["relative_path"].endswith("program.yaml")]
    elif damage == "program_bytes":
        (backup / "programs" / "synthetic" / "program.yaml").unlink()
    elif damage == "database_bytes":
        (backup / database.relative_to(source)).unlink(missing_ok=True)
    elif damage == "database_checksum":
        (backup / "vertex-db" / "synthetic").mkdir(parents=True, exist_ok=True)
        (backup / database.relative_to(source)).write_bytes(b"damaged")
    elif damage == "version":
        payload["schema_version"] = "9.0"
    elif damage == "null_version":
        payload["schema_version"] = None
    elif damage == "bad_file":
        payload["files"].append(None)
    elif damage == "bad_sha":
        payload["files"][0]["sha256"] = None
    elif damage == "bad_files":
        payload["files"] = None
    elif damage == "bad_store":
        scope["fact_reality_stores"] = [None]
    elif damage == "bad_relative":
        payload["files"][0]["relative_path"] = None
    elif damage in {"absolute", "backslash", "traversal"}:
        payload["files"][0]["relative_path"] = {
            "absolute": "C:/outside", "backslash": "programs\\synthetic\\program.yaml", "traversal": "../outside",
        }[damage]
    _save_manifest(backup, payload)
    destination = tmp_path / "restored"
    with pytest.raises(StateError):
        restore_repository_backup(backup, destination)
    assert not destination.exists()


@pytest.mark.parametrize("version", ["1.0", "unversioned"])
def test_historical_manifest_integrity_and_restore_remain_compatible(version: str, tmp_path: Path) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    payload = _manifest(backup)
    payload.pop("declared_scope", None)
    if version == "unversioned":
        payload.pop("schema_version")
    else:
        payload["schema_version"] = version
    _save_manifest(backup, payload)
    result = verify_repository_backup(backup)
    assert result.is_valid
    assert result.declared_scope_check == "unassessed"
    assert result.absent_unassessed_store_count is None
    restore_repository_backup(backup, tmp_path / "restored")


@pytest.mark.parametrize("damage", ["scope", "version", "file"])
def test_skip_preflight_still_requires_manifest_structure(damage: str, tmp_path: Path) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    payload = _manifest(backup)
    if damage == "scope":
        payload.pop("declared_scope", None)
    elif damage == "version":
        payload["schema_version"] = "future"
    else:
        payload["files"].append({"relative_path": ""})
    _save_manifest(backup, payload)
    with pytest.raises(StateError):
        restore_repository_backup(backup, tmp_path / "restored", skip_preflight=True)
    assert not (tmp_path / "restored").exists()


def test_skip_preflight_reports_not_checked(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    (backup / "programs" / "synthetic" / "program.yaml").write_text("modified", encoding="utf-8")
    result = restore_repository_backup(backup, tmp_path / "restored", skip_preflight=True)
    assert result.declared_scope_check == "not_checked"
    assert not result.preflight_verified


@pytest.mark.parametrize("setting,value", [
    ("VERTEX_PROGRAMS_ROOT", "external_programs"),
    ("VERTEX_DB_PATH", "external_db"),
    ("VERTEX_DB_PATH", "shared.sqlite3"),
    ("VERTEX_DB_PATH", "vertex-db/synthetic/vertex.sqlite3"),
    ("VERTEX_DB_PATH", ""),
    ("VERTEX_PROGRAMS_ROOT", "relative"),
])
def test_unsupported_authority_mapping_refuses_before_capture(
    setting: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    configured = "" if not value else "relative" if value == "relative" else str(source / value)
    monkeypatch.setenv(setting, configured)
    with pytest.raises(StateError, match="mapping"):
        create_repository_backup(tmp_path / "backup", source_root=source)
    assert not (tmp_path / "backup").exists()


def test_exact_canonical_mapping_and_nonprogram_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    (source / "programs" / "_templates").mkdir()
    (source / "programs" / "_templates" / "program.yaml").write_text("template", encoding="utf-8")
    (source / "programs" / "not_a_program").mkdir()
    (source / "programs" / "ordinary.txt").write_text("ordinary", encoding="utf-8")
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(source / "programs"))
    monkeypatch.setenv("VERTEX_DB_PATH", str(source / "vertex-db"))
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    assert _manifest(backup)["declared_scope"]["program_ids"] == ["synthetic"]
    assert verify_repository_backup(backup).is_valid


def test_observed_database_disappearing_cannot_publish_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.core.backup as module
    source = tmp_path / "source"
    database = _canonical_source(source)
    connection = _open_wal(database)
    connection.close()
    copy = module._copy_checkpoint_file

    def remove_database(src: Path, dst: Path) -> None:
        if src.name == "program.yaml":
            database.unlink()
        copy(src, dst)

    monkeypatch.setattr(module, "_copy_checkpoint_file", remove_database)
    with pytest.raises(StateError):
        create_repository_backup(tmp_path / "backup", source_root=source)
    assert not (tmp_path / "backup" / BACKUP_MANIFEST_NAME).exists()


@pytest.mark.parametrize("damage", ["programs_file", "metadata_dir", "database_dir", "enumeration", "metadata", "link", "reparse", "special", "ancestor", "helper_mapping"])
def test_capture_inventory_failures_never_publish_success(
    damage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stat
    from types import SimpleNamespace
    import src.core.backup as module
    source = tmp_path / "source"
    database = _canonical_source(source)
    if damage == "programs_file":
        (source / "programs" / "synthetic" / "program.yaml").unlink()
        (source / "programs" / "synthetic").rmdir()
        (source / "programs").rmdir()
        (source / "programs").write_text("wrong kind", encoding="utf-8")
    elif damage == "metadata_dir":
        metadata = source / "programs" / "synthetic" / "program.yaml"
        metadata.unlink()
        metadata.mkdir()
    elif damage == "database_dir":
        database.mkdir(parents=True)
    elif damage == "enumeration":
        original_iterdir = Path.iterdir

        def fail_enumeration(path: Path):
            if path == source / "programs":
                raise PermissionError("synthetic")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_enumeration)
    elif damage == "helper_mapping":
        monkeypatch.setattr(module, "get_program_reality_db_path", lambda *args, **kwargs: tmp_path / "outside")
    else:
        original_lstat = Path.lstat
        target = source / "programs" / "synthetic"
        if damage == "ancestor":
            target = source

        def fail_metadata(path: Path):
            if path == target:
                if damage == "metadata":
                    raise PermissionError("synthetic")
                mode = stat.S_IFLNK if damage == "link" else stat.S_IFIFO if damage == "special" else stat.S_IFREG if damage == "ancestor" else stat.S_IFDIR
                return SimpleNamespace(st_mode=mode, st_file_attributes=1024 if damage == "reparse" else 0)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_metadata)
    with pytest.raises(StateError):
        create_repository_backup(tmp_path / "backup", source_root=source)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("root_name", ["programs", "knowledge", "editions"])
@pytest.mark.parametrize("damage", ["file_link", "directory_link", "reparse", "unsupported"])
def test_recursive_capture_rejects_unsupported_descendants(
    root_name: str, damage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stat
    from types import SimpleNamespace

    source = tmp_path / "source"
    _canonical_source(source)
    root = source / root_name
    root.mkdir(exist_ok=True)
    capture_parent = root / "synthetic" if root_name == "programs" else root
    capture_parent.mkdir(exist_ok=True)
    target = capture_parent / ("linked-directory" if damage == "directory_link" else "linked-file.yaml")
    if damage == "directory_link":
        target.mkdir()
        (target / "contents.yaml").write_text("outside scope\n", encoding="utf-8")
        mode = stat.S_IFLNK
    else:
        target.write_text("outside scope\n", encoding="utf-8")
        mode = stat.S_IFIFO if damage == "unsupported" else stat.S_IFREG
        attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) if damage == "reparse" else 0
        original_lstat = Path.lstat

        def fake_lstat(path: Path):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFLNK if damage == "file_link" else mode,
                                      st_file_attributes=attributes)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fake_lstat)
    if damage == "directory_link":
        attributes = 0
        original_lstat = Path.lstat

        def fake_lstat(path: Path):
            if path == target:
                return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fake_lstat)

    backup = tmp_path / "backup"
    with pytest.raises(StateError):
        create_repository_backup(backup, source_root=source)
    assert not (backup / BACKUP_MANIFEST_NAME).exists()
    assert not (backup / target.relative_to(source)).exists()
    assert not backup.exists() or not any(backup.iterdir())


@pytest.mark.parametrize("root_name", ["programs", "knowledge", "editions"])
@pytest.mark.parametrize("damage", ["file", "link", "dangling_link"])
def test_capture_rejects_unsupported_roots_before_following_them(
    root_name: str, damage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stat
    from types import SimpleNamespace

    source = tmp_path / "source"
    _canonical_source(source)
    root = source / root_name
    if damage == "dangling_link":
        if root.exists():
            import shutil
            shutil.rmtree(root)
        mode = stat.S_IFLNK
        actual_exists = False
    elif damage == "file":
        if root.is_dir():
            import shutil
            shutil.rmtree(root)
        root.write_text("not a supported root\n", encoding="utf-8")
        mode = stat.S_IFREG
        actual_exists = True
    else:
        root.mkdir(exist_ok=True)
        (root / "inside.yaml").write_text("linked root\n", encoding="utf-8")
        mode = stat.S_IFLNK
        actual_exists = True

    original_lstat = Path.lstat

    def fake_lstat(path: Path):
        if path == root:
            return SimpleNamespace(st_mode=mode, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    if damage == "dangling_link":
        original_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            return False if path == root else original_exists(path)

        monkeypatch.setattr(Path, "exists", fake_exists)
    backup = tmp_path / "backup"
    with pytest.raises(StateError):
        create_repository_backup(backup, source_root=source)
    assert not (backup / BACKUP_MANIFEST_NAME).exists()
    if actual_exists and damage != "file":
        assert not (backup / root_name / "inside.yaml").exists()
    assert not backup.exists() or not any(backup.iterdir())


def test_recursive_capture_preserves_empty_supported_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    (source / "knowledge").mkdir()

    backup = tmp_path / "backup"
    result = create_repository_backup(backup, source_root=source)

    assert result.file_count == 1
    assert _manifest(backup)["included_roots"] == ["programs", "knowledge"]
    assert verify_repository_backup(backup).is_valid


def test_recursive_capture_enumeration_error_refuses_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    root = source / "knowledge"
    root.mkdir()
    original_iterdir = Path.iterdir

    def fail_enumeration(path: Path):
        if path == root:
            raise PermissionError("synthetic")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_enumeration)
    backup = tmp_path / "backup"
    with pytest.raises(StateError, match="Backup scope enumeration failed"):
        create_repository_backup(backup, source_root=source)
    assert not (backup / BACKUP_MANIFEST_NAME).exists()
    assert not backup.exists() or not any(backup.iterdir())


def test_recursive_capture_refuses_entry_disappearing_during_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    root = source / "knowledge"
    root.mkdir()
    vanished = root / "vanished.yaml"
    vanished.write_text("enumerated before disappearance\n", encoding="utf-8")
    original_lstat = Path.lstat

    def disappear(path: Path):
        if path == vanished:
            raise FileNotFoundError(path)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", disappear)
    backup = tmp_path / "backup"
    with pytest.raises(StateError, match="entry disappeared during enumeration"):
        create_repository_backup(backup, source_root=source)
    assert not (backup / BACKUP_MANIFEST_NAME).exists()
    assert not backup.exists() or not any(backup.iterdir())


def test_capture_reconciles_destination_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.core.backup as module
    source = tmp_path / "source"
    _canonical_source(source)
    original_hash = module._hash_file
    calls = 0

    def damage_after_inventory(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("changed after capture record", encoding="utf-8")
        return original_hash(path)

    monkeypatch.setattr(module, "_hash_file", damage_after_inventory)
    with pytest.raises(StateError, match="obligations"):
        create_repository_backup(tmp_path / "backup", source_root=source)
    assert not (tmp_path / "backup" / BACKUP_MANIFEST_NAME).exists()


def test_offline_verification_never_reopens_source_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    database = _canonical_source(source)
    connection = _open_wal(database)
    connection.close()
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "unavailable_authority"))
    monkeypatch.setenv("VERTEX_PROGRAMS_ROOT", str(tmp_path / "unavailable_programs"))
    original_lstat = Path.lstat

    def refuse_original(path: Path):
        if path == source or source in path.parents:
            raise AssertionError("offline verification accessed source")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", refuse_original)
    assert verify_repository_backup(backup).is_valid
    restore_repository_backup(backup, tmp_path / "restored")


def test_coordinated_inventory_removal_is_not_authenticated_completeness(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _canonical_source(source)
    backup = tmp_path / "backup"
    create_repository_backup(backup, source_root=source)
    payload = _manifest(backup)
    payload["declared_scope"]["program_ids"] = []
    payload["declared_scope"]["fact_reality_stores"] = []
    payload["files"] = []
    (backup / "programs" / "synthetic" / "program.yaml").unlink()
    _save_manifest(backup, payload)
    assert verify_repository_backup(backup).is_valid


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
def test_actual_cli_canonical_wal_capture_verify_restore(
    output_format: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    database = _canonical_source(source)
    connection = _open_wal(database)
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", source)
    backup = tmp_path / "backup"
    destination = tmp_path / "restored"
    runner = CliRunner()
    try:
        connection.execute("INSERT INTO sample VALUES ('uncommitted')")
        for operation in (["--to", str(backup)], ["--verify", str(backup)], ["--restore", str(destination), "--from", str(backup)]):
            result = runner.invoke(app, ["backup", *operation, "--format", output_format])
            assert result.exit_code == 0, result.stdout
            assert "not_assessed" in result.stdout if output_format != "human" else "recovery not assessed" in result.stdout
        assert _standalone_values(destination / database.relative_to(source)) == [("committed",)]
        for operation in (["--verify", str(backup)], ["--restore", str(tmp_path / "refused"), "--from", str(backup)]):
            (backup / database.relative_to(source)).write_bytes(b"damaged")
            result = runner.invoke(app, ["backup", *operation, "--format", output_format])
            assert result.exit_code == 2, result.stdout
        assert not (tmp_path / "refused").exists()
    finally:
        connection.close()


def _open_wal(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE sample(value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('committed')")
    connection.commit()
    return connection


def _standalone_values(path: Path) -> list[tuple[str]]:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return connection.execute("SELECT value FROM sample").fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize("suffix", [".sqlite3", ".sqlite", ".db"])
def test_repository_backup_captures_committed_wal_without_live_sidecars(suffix: str, tmp_path: Path) -> None:
    source = tmp_path / "source"
    database = source / "programs" / "synthetic" / f"state{suffix}"
    connection = _open_wal(database)
    try:
        original_database = database.read_bytes()
        original_wal = Path(f"{database}-wal").read_bytes()
        connection.execute("INSERT INTO sample VALUES ('uncommitted')")
        backup = tmp_path / "backup"
        result = create_repository_backup(backup, source_root=source)
        relative = database.relative_to(source)
        assert _standalone_values(backup / relative) == [("committed",)]
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert [entry["relative_path"] for entry in manifest["files"]] == [relative.as_posix()]
        assert result.file_count == 1
        assert not Path(f"{backup / relative}-wal").exists()
        assert not Path(f"{backup / relative}-shm").exists()
        assert database.read_bytes() == original_database
        assert Path(f"{database}-wal").read_bytes() == original_wal
        assert verify_repository_backup(backup).is_valid
        destination = tmp_path / "restored"
        restore_repository_backup(backup, destination)
        assert _standalone_values(destination / relative) == [("committed",)]
    finally:
        connection.close()


def test_checkpoint_captures_open_outbox_database(tmp_path: Path) -> None:
    database = tmp_path / "synthetic" / "runtime" / "actuation" / "outbox.db"
    connection = _open_wal(database)
    try:
        checkpoint = create_checkpoint_snapshot("synthetic", 1, programs_root=tmp_path)
        assert _standalone_values(checkpoint / "runtime" / "actuation" / "outbox.db") == [("committed",)]
    finally:
        connection.close()


def test_repository_backup_does_not_discard_unassociated_sidecar_named_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    program = source / "programs" / "synthetic"
    program.mkdir(parents=True)
    for name in ("notes-wal", "notes-shm", "notes-journal", "notes"):
        (program / name).write_text("ordinary text", encoding="utf-8")
    result = create_repository_backup(tmp_path / "backup", source_root=source)
    assert result.file_count == 4
    assert verify_repository_backup(result.destination_root).is_valid


def test_corrupt_database_refuses_successful_backup_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    database = source / "programs" / "synthetic" / "state.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a SQLite database")
    with pytest.raises(StateError, match="SQLite"):
        create_repository_backup(tmp_path / "backup", source_root=source)
    assert not (tmp_path / "backup" / BACKUP_MANIFEST_NAME).exists()
    assert database.read_bytes() == b"not a SQLite database"


def test_sqlite_capture_never_creates_missing_source(tmp_path: Path) -> None:
    source = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        _copy_sqlite(source, tmp_path / "destination.sqlite3")
    assert not source.exists()
    assert not (tmp_path / "destination.sqlite3").exists()


@pytest.mark.parametrize("format_name", ["human", "json", "csv"])
def test_backup_cli_reports_sqlite_capture_failure(
    format_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    database = source / "programs" / "synthetic" / "state.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a SQLite database")
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", source)
    result = CliRunner().invoke(
        app, ["backup", "--to", str(tmp_path / "backup"), "--format", format_name],
    )
    assert result.exit_code == 2
    assert "SQLite" in result.stdout
    if format_name == "json":
        payload = json.loads(result.stdout)
        assert payload["mode"] == "create"
        assert "error" in payload
    assert not (tmp_path / "backup" / BACKUP_MANIFEST_NAME).exists()


@pytest.mark.parametrize("format_name", ["human", "json", "csv"])
def test_backup_cli_round_trips_open_sqlite_capture(
    format_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    database = source / "programs" / "synthetic" / "state.db"
    connection = _open_wal(database)
    monkeypatch.setattr("src.commands.backup.REPO_ROOT", source)
    runner = CliRunner()
    backup = tmp_path / "backup"
    destination = tmp_path / "restored"
    try:
        created = runner.invoke(app, ["backup", "--to", str(backup), "--format", format_name])
        assert created.exit_code == 0, created.stdout
        verified = runner.invoke(app, ["backup", "--verify", str(backup), "--format", format_name])
        assert verified.exit_code == 0, verified.stdout
        restored = runner.invoke(
            app, ["backup", "--restore", str(destination), "--from", str(backup), "--format", format_name],
        )
        assert restored.exit_code == 0, restored.stdout
        assert _standalone_values(destination / database.relative_to(source)) == [("committed",)]
    finally:
        connection.close()


def test_backup_creation_failure_rejects_unsupported_output_format(tmp_path: Path) -> None:
    from src.commands.backup import render_backup_create_error_output
    import typer

    with pytest.raises(typer.BadParameter, match="format"):
        render_backup_create_error_output(tmp_path, "capture failed", format="xml")
