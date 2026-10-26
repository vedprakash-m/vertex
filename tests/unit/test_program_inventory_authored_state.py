from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import socket
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import program_inventory
from src.core.program_paths import ROOT_ENTRIES


def _program(root: Path) -> Path:
    program = root / "fixture"
    program.mkdir()
    (program / "program.yaml").write_bytes(b"synthetic program")
    (program / "decisions.yaml").write_bytes(b"synthetic decisions")
    (program / "knowledge").mkdir()
    return program


def _entries(report: dict) -> dict[Path, dict]:
    return {Path(entry["path"]): entry for entry in report["authored_entries"]}


def test_new_authored_root_file_is_preserved_but_not_recognized(tmp_path: Path) -> None:
    program = _program(tmp_path)
    new_file = program / "new_authored.yaml"
    new_file.write_bytes(b"synthetic new authored state")
    before = new_file.stat()

    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)

    entry = _entries(report)[Path("new_authored.yaml")]
    assert entry == {
        "path": "new_authored.yaml", "kind": "file",
        "classification": "unclassified", "tier": None, "size_bytes": before.st_size,
    }
    assert report["summary"]["authored_unclassified_count"] == 1
    assert report["summary"]["clean"] is False
    assert new_file.stat() == before
    assert new_file.read_bytes() == b"synthetic new authored state"


def test_recognized_authored_entries_come_from_registry_not_content(tmp_path: Path) -> None:
    program = _program(tmp_path)
    expected = [entry for entry in ROOT_ENTRIES if entry.tier in {"T-1", "T-2"}]
    for entry in expected:
        path = program / entry.name
        if entry.kind == "dir":
            path.mkdir(exist_ok=True)
        else:
            path.write_bytes(b"not parsed as YAML")
    (program / "journal").mkdir()
    (program / "narratives").mkdir()

    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)
    entries = _entries(report)

    assert set(entries) == {Path(entry.name) for entry in expected}
    for entry in expected:
        assert entries[Path(entry.name)]["classification"] == "recognized"
        assert entries[Path(entry.name)]["tier"] == entry.tier
        assert entries[Path(entry.name)]["kind"] == entry.kind
    assert report["scope"]["authored_registry_tiers"] == ["T-1", "T-2"]
    assert report["scope"]["review_currency"] is False
    assert report["summary"]["authored_recognized_count"] == len(expected)
    assert report["summary"]["authored_unclassified_count"] == 0
    assert report["summary"]["clean"] is True


def test_known_directory_does_not_recognize_nested_files_or_namesakes(tmp_path: Path) -> None:
    program = _program(tmp_path)
    paths = [
        Path("knowledge") / "new_authored.yaml",
        Path("knowledge") / "nested" / "program.yaml",
        Path("editions") / "weekly" / "edition.yaml",
    ]
    for relative in paths:
        target = program / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic unknown")

    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)
    entries = _entries(report)

    unknown = set(paths) | {Path("knowledge") / "nested", Path("editions") / "weekly"}
    for relative in unknown:
        assert entries[relative]["classification"] == "unclassified"
        assert entries[relative]["tier"] is None
    assert entries[Path("knowledge")]["classification"] == "recognized"
    assert report["summary"]["authored_unclassified_count"] == len(unknown)
    assert report["summary"]["clean"] is False
    assert report["scope"]["authored_state"] == "recursive-metadata"
    assert entries[paths[0]]["size_bytes"] == len(b"synthetic unknown")


def test_root_only_discloses_uninspected_descendants_without_enumerating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _program(tmp_path)
    (program / "knowledge" / "new_authored.yaml").write_bytes(b"preserve")
    original = program_inventory._scan
    original_stat = Path.stat
    scanned = []

    def scan(path: Path):
        scanned.append(path)
        if path != program:
            pytest.fail("Root-only must not enumerate any descendants.")
        return original(path)

    def stat_path(self: Path, *, follow_symlinks: bool = True):
        if self.is_relative_to(program / "knowledge") and self != program / "knowledge":
            pytest.fail("Root-only must not inspect authored descendant metadata.")
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(program_inventory, "_scan", scan)
    monkeypatch.setattr(Path, "stat", stat_path)
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)

    assert scanned == [program]
    assert report["scope"]["authored_state"] == "root-only"
    assert report["scope"]["recursive_sizes"] is False
    assert report["subdir_sizes"] == []
    assert set(_entries(report)) == {Path("program.yaml"), Path("decisions.yaml"), Path("knowledge")}
    assert report["summary"]["authored_unclassified_count"] == 0
    human = program_inventory._render_human(report)
    assert "authored descendants not inspected" in human
    assert "review currency not assessed" in human


@pytest.mark.parametrize("failure", ["stat", "enumeration", "late-enumeration", "disappeared"])
def test_authored_access_failures_never_emit_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    program = _program(tmp_path)
    target = program / "knowledge" / "new_authored.yaml"
    target.write_bytes(b"preserve")
    original_stat = Path.stat
    original_scan = program_inventory._scan

    def stat_path(self: Path, *, follow_symlinks: bool = True):
        if self == target and failure in {"stat", "disappeared"}:
            error = FileNotFoundError if failure == "disappeared" else PermissionError
            raise error("PRIVATE-METADATA-DETAIL")
        return original_stat(self, follow_symlinks=follow_symlinks)

    def scan(path: Path):
        if path == target.parent and failure in {"enumeration", "late-enumeration"}:
            if failure == "late-enumeration":
                yield target
            raise PermissionError("PRIVATE-ENUMERATION-DETAIL")
        yield from original_scan(path)

    monkeypatch.setattr(Path, "stat", stat_path)
    monkeypatch.setattr(program_inventory, "_scan", scan)

    assert program_inventory.main([
        "--program", "fixture", "--programs-root", str(tmp_path), "--format", "json",
    ]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "no complete report" in output.err
    assert "PRIVATE" not in output.err
    assert target.is_relative_to(program)


@pytest.mark.parametrize("entry", ["program.yaml", "knowledge"])
def test_wrong_kind_authored_registry_entry_is_not_recognized(tmp_path: Path, entry: str) -> None:
    program = tmp_path / "fixture"
    program.mkdir()
    target = program / entry
    if entry == "program.yaml":
        target.mkdir()
    else:
        target.write_bytes(b"not a directory")

    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)

    assert _entries(report)[Path(entry)]["classification"] == "unclassified"
    assert report["summary"]["authored_unclassified_count"] == 1
    assert report["summary"]["clean"] is False


@pytest.mark.parametrize("entry_type", ["symlink", "reparse", "special"])
def test_authored_descendant_links_reparse_and_special_entries_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_type: str,
) -> None:
    program = _program(tmp_path)
    target = program / "knowledge" / "new_authored.yaml"
    target.write_bytes(b"preserve")
    original = Path.stat

    def stat_path(self: Path, *, follow_symlinks: bool = True):
        value = original(self, follow_symlinks=follow_symlinks)
        if self == target:
            return SimpleNamespace(
                st_mode={"symlink": stat.S_IFLNK, "reparse": value.st_mode, "special": stat.S_IFIFO}[entry_type],
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if entry_type == "reparse" else 0,
                st_size=value.st_size,
            )
        return value

    monkeypatch.setattr(Path, "stat", stat_path)
    with pytest.raises(ValueError):
        program_inventory.inventory_program("fixture", tmp_path, root_only=False)


def test_recursive_human_and_json_outputs_expose_unknown_authored_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    program = _program(tmp_path)
    (program / "knowledge" / "new_authored.yaml").write_bytes(b"preserve")
    args = ["--program", "fixture", "--programs-root", str(tmp_path)]

    assert program_inventory.main(args) == 1
    human = capsys.readouterr()
    assert "authored metadata" in human.out
    assert "unclassified" in human.out
    assert str(Path("knowledge") / "new_authored.yaml") in human.out
    assert "review currency not assessed" in human.out
    assert human.err == ""
    assert program_inventory.main([*args, "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["authored_unclassified_count"] == 1


@pytest.mark.parametrize("root_only", [True, False])
def test_collection_audit_forbids_content_hashes_databases_providers_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_only: bool,
) -> None:
    program = _program(tmp_path)
    (program / "knowledge" / "unknown.md").write_bytes(b"synthetic authored metadata")
    active = False

    def forbidden(*_args: object, **_kwargs: object):
        pytest.fail("Inventory attempted a content/hash/database/provider/write operation.")

    def audit(event: str, _args: tuple) -> None:
        if active and (
            event in {
                "open", "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.truncate",
                "os.chmod", "os.utime", "os.link", "os.symlink", "os.system", "subprocess.Popen",
            }
            or event.startswith(("sqlite3.", "socket.", "shutil."))
        ):
            forbidden()

    original_import = builtins.__import__

    def import_module(name: str, *args, **kwargs):
        fromlist = args[2] if len(args) > 2 else kwargs.get("fromlist", ())
        imported = [name, *(f"{name}.{child}" for child in fromlist or ())]
        for module in imported:
            if module.startswith(("src.ai", "src.m365", "src.core.models", "src.core.policy", "src.core.config_loader")):
                forbidden()
        return original_import(name, *args, **kwargs)

    sys.addaudithook(audit)
    with monkeypatch.context() as guards:
        guards.setattr(builtins, "__import__", import_module)
        for owner, names in (
            (builtins, ("open",)),
            (io, ("open",)),
            (os, ("open", "write", "mkdir", "remove", "rename", "replace", "rmdir")),
            (Path, ("open", "read_bytes", "read_text", "write_bytes", "write_text")),
            (hashlib, ("new", "file_digest", *(name for name in hashlib.algorithms_guaranteed if hasattr(hashlib, name)))),
            (sqlite3, ("connect",)),
            (socket, ("socket", "create_connection")),
        ):
            for name in names:
                guards.setattr(owner, name, forbidden)
        active = True
        try:
            report = program_inventory.inventory_program("fixture", tmp_path, root_only=root_only)
        finally:
            active = False
    assert report["scope"]["content_reads"] is False
    assert report["scope"]["cleanup_authority"] is False
    assert report["scope"]["review_currency"] is False


def test_arbitrary_unknown_data_is_not_declared_reviewed_or_authored(tmp_path: Path) -> None:
    program = _program(tmp_path)
    target = program / "opaque.bin"
    target.write_bytes(b"\x00\xffsynthetic")

    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)

    assert _entries(report)[Path("opaque.bin")] == {
        "path": "opaque.bin", "kind": "file", "classification": "unclassified",
        "tier": None, "size_bytes": len(b"\x00\xffsynthetic"),
    }
    assert report["scope"]["review_currency"] is False
    assert report["summary"]["clean"] is False
