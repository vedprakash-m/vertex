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
    assert report["scope"]["authored_registry_tiers"] == ["T-1", "T-2", "T-6"]
    assert report["scope"]["review_currency"] is False
    assert report["summary"]["authored_recognized_count"] == len(expected)
    assert report["summary"]["authored_unclassified_count"] == 0
    assert report["summary"]["clean"] is True


def test_canonical_operator_override_is_included_without_dirtying_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    program = _program(tmp_path)
    overrides = program / "overrides"
    overrides.mkdir()
    override = overrides / "issue_999.yaml"
    override.write_bytes(b"synthetic override")

    assert program_inventory.main([
        "--program", "fixture", "--programs-root", str(tmp_path), "--format", "json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)

    assert _entries(report)[Path("overrides") / "issue_999.yaml"] == {
        "path": str(Path("overrides") / "issue_999.yaml"),
        "kind": "file",
        "classification": "recognized",
        "tier": "T-6",
        "size_bytes": len(b"synthetic override"),
    }
    assert report["scope"]["authored_registry_tiers"] == ["T-1", "T-2", "T-6"]
    assert report["summary"]["authored_recognized_count"] == 4
    assert report["summary"]["authored_unclassified_count"] == 0
    assert report["summary"]["clean"] is True
    assert "T-6 overrides" in program_inventory._render_human(report)


def test_override_filename_matches_minimum_width_issue_number_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    program = _program(tmp_path)
    overrides = program / "overrides"
    overrides.mkdir()
    issue_number = 1000
    canonical_name = f"issue_{issue_number:03d}.yaml"
    (overrides / canonical_name).write_bytes(b"synthetic override")
    (overrides / "issue_0001.yaml").write_bytes(b"synthetic noncanonical")

    assert program_inventory.main([
        "--program", "fixture", "--programs-root", str(tmp_path), "--format", "json",
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    entries = _entries(report)

    assert entries[Path("overrides") / canonical_name]["classification"] == "recognized"
    assert entries[Path("overrides") / canonical_name]["tier"] == "T-6"
    assert entries[Path("overrides") / "issue_0001.yaml"]["classification"] == "unclassified"
    assert report["summary"]["authored_unclassified_count"] == 1


def test_noncanonical_override_names_and_subdirectories_remain_unclassified(
    tmp_path: Path,
) -> None:
    program = _program(tmp_path)
    overrides = program / "overrides"
    (overrides / "nested").mkdir(parents=True)
    (overrides / "issue_1000.yaml").mkdir()
    for relative in (
        Path("custom.yaml"),
        Path("issue_12.yaml"),
        Path("issue_notes.yaml"),
        Path("nested") / "issue_123.yaml",
    ):
        (overrides / relative).write_bytes(b"synthetic unknown")

    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)
    entries = _entries(report)

    for relative in (
        Path("overrides") / "custom.yaml",
        Path("overrides") / "issue_12.yaml",
        Path("overrides") / "issue_notes.yaml",
        Path("overrides") / "issue_1000.yaml",
        Path("overrides") / "nested",
        Path("overrides") / "nested" / "issue_123.yaml",
    ):
        assert entries[relative]["classification"] == "unclassified"
        assert entries[relative]["tier"] is None
    assert report["summary"]["authored_unclassified_count"] == 6
    assert report["summary"]["clean"] is False


def test_root_only_does_not_inspect_operator_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _program(tmp_path)
    overrides = program / "overrides"
    overrides.mkdir()
    (overrides / "issue_999.yaml").write_bytes(b"preserve")
    original = program_inventory._scan
    scanned = []

    def scan(path: Path):
        scanned.append(path)
        if path != program:
            pytest.fail("Root-only must not enumerate authored descendants.")
        return original(path)

    monkeypatch.setattr(program_inventory, "_scan", scan)
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)

    assert scanned == [program]
    assert report["scope"]["authored_state"] == "root-only"
    assert Path("overrides") / "issue_999.yaml" not in _entries(report)
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


@pytest.mark.parametrize("directory", ["knowledge", "overrides"])
@pytest.mark.parametrize("failure", ["stat", "enumeration", "late-enumeration", "disappeared"])
def test_authored_access_failures_never_emit_partial_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], failure: str, directory: str,
) -> None:
    program = _program(tmp_path)
    authored_dir = program / directory
    authored_dir.mkdir(exist_ok=True)
    target = authored_dir / "new_authored.yaml"
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


@pytest.mark.parametrize("directory", ["knowledge", "overrides"])
@pytest.mark.parametrize("entry_type", ["symlink", "reparse", "special"])
def test_authored_descendant_links_reparse_and_special_entries_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_type: str, directory: str,
) -> None:
    program = _program(tmp_path)
    authored_dir = program / directory
    authored_dir.mkdir(exist_ok=True)
    target = authored_dir / "new_authored.yaml"
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
    (program / "overrides").mkdir()
    (program / "overrides" / "issue_999.yaml").write_bytes(b"synthetic override")
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


def test_review_rule_coverage_joins_inventory_without_inheriting_rules(tmp_path: Path) -> None:
    program = _program(tmp_path)
    (program / "review.yaml").write_bytes(b"synthetic config")
    (program / "unknown.yaml").write_bytes(b"synthetic unknown")
    (program / "knowledge" / "nested").mkdir()
    (program / "knowledge" / "nested" / "program.yaml").write_bytes(b"synthetic descendant")
    (program / "overrides").mkdir()
    (program / "overrides" / "issue_999.yaml").write_bytes(b"synthetic override")

    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)

    coverage = report["review_rule_coverage"]
    rows = {Path(row["path"]): row for row in coverage["entries"]}
    assert list(rows) == list(_entries(report))
    assert rows[Path("program.yaml")]["rule_references"] == ["context_review.files.program.yaml"]
    assert rows[Path("decisions.yaml")]["rule_references"] == ["context_review.entries.decisions.yaml"]
    assert rows[Path("program.yaml")]["disposition"] == "existing_rule"
    assert rows[Path("review.yaml")]["disposition"] == "unassessed"
    assert rows[Path("unknown.yaml")]["disposition"] == "unclassified"
    assert rows[Path("overrides") / "issue_999.yaml"]["disposition"] == "unassessed"
    for path in (Path("knowledge"), Path("knowledge") / "nested"):
        assert rows[path]["disposition"] == "container"
        assert rows[path]["rule_references"] == []
    descendant = rows[Path("knowledge") / "nested" / "program.yaml"]
    assert descendant["disposition"] == "unclassified"
    assert descendant["rule_references"] == []
    assert coverage["totals"] == {
        "entries": 8, "files": 6, "containers": 2, "other_entries": 0,
        "existing_rule": 2, "unassessed": 2, "unclassified": 2,
    }
    assert all(row["review_state"] == "not_evaluated" for row in rows.values())
    assert coverage["rule_basis"] == "compatibility_defaults_only"
    assert coverage["policy_overrides_assessed"] is False
    assert coverage["unassessed_scopes"] == [
        "shared_knowledge", "entities_and_fields", "bindings_and_audiences", "people_validity",
    ]
    assert all(set(row) == {
        "path", "kind", "classification", "disposition", "rule_references", "review_state",
    } for row in rows.values())
    assert coverage["totals"]["entries"] == sum(
        coverage["totals"][key] for key in ("containers", "existing_rule", "unassessed", "unclassified")
    )


def test_review_rule_coverage_tracks_new_taxonomy_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from src.core import program_paths

    program = _program(tmp_path)
    (program / "future_authored.yaml").write_bytes(b"synthetic future schema")
    try:
        with monkeypatch.context() as declarations:
            declarations.setattr(program_paths, "ROOT_ENTRIES", (
                *program_paths.ROOT_ENTRIES, program_paths.RootEntry("future_authored.yaml", "T-1", "file"),
            ))
            importlib.reload(program_inventory)
            report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)
    finally:
        importlib.reload(program_inventory)

    row = next(row for row in report["review_rule_coverage"]["entries"] if row["path"] == "future_authored.yaml")
    assert row["classification"] == "recognized"
    assert row["disposition"] == "unassessed"
    assert row["rule_references"] == []
    assert report["summary"]["clean"] is True


@pytest.mark.parametrize("root_only", [False, True])
def test_review_rule_coverage_cli_is_deterministic_and_explicit_about_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], root_only: bool,
) -> None:
    program = _program(tmp_path)
    (program / "workstreams.yaml").write_bytes(b"not YAML; no date may be inferred")
    (program / "knowledge" / "program.yaml").write_bytes(b"synthetic descendant")
    args = ["--program", "fixture", "--programs-root", str(tmp_path)]
    if root_only:
        args.append("--root-only")

    expected_exit = 0 if root_only else 1
    assert program_inventory.main([*args, "--format", "json"]) == expected_exit
    first = capsys.readouterr()
    assert first.err == ""
    assert program_inventory.main([*args, "--format", "json"]) == expected_exit
    assert capsys.readouterr().out == first.out
    report = json.loads(first.out)
    coverage = report["review_rule_coverage"]
    rows = {row["path"]: row for row in coverage["entries"]}
    assert rows["workstreams.yaml"]["rule_references"] == [
        "context_review.files.workstreams.yaml", "context_review.entries.workstreams.yaml",
    ]
    assert ("authored_descendants" in coverage["unassessed_scopes"]) is root_only
    assert (str(Path("knowledge") / "program.yaml") in rows) is (not root_only)
    assert report["program_dir"] == str(program)
    assert program_inventory.main(args) == expected_exit
    human = capsys.readouterr()
    assert human.err == ""
    for label in (
        "review-rule coverage", "compatibility defaults only", "policy overrides not assessed",
        "review currency not assessed", "shared_knowledge", "entities_and_fields",
        "bindings_and_audiences", "people_validity",
        "existing_rule=3", "container", "context_review.entries.workstreams.yaml",
    ):
        assert label in human.out
    assert "complete coverage" not in human.out


@pytest.mark.parametrize("root_only", [True, False])
def test_review_rule_coverage_never_enumerates_shared_or_other_program_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_only: bool,
) -> None:
    root = tmp_path / "programs"
    root.mkdir()
    program = _program(root)
    sibling = root / "other"
    sibling.mkdir()
    (sibling / "unknown.yaml").write_bytes(b"synthetic other program")
    shared = tmp_path / "knowledge"
    shared.mkdir()
    (shared / "people_directory.yaml").write_bytes(b"synthetic shared knowledge")
    scan = program_inventory._scan

    def selected_only(path: Path):
        assert path.is_relative_to(program)
        yield from scan(path)

    monkeypatch.setattr(program_inventory, "_scan", selected_only)
    report = program_inventory.inventory_program("fixture", root, root_only=root_only)
    coverage = report["review_rule_coverage"]
    assert {row["path"] for row in coverage["entries"]} == {"program.yaml", "decisions.yaml", "knowledge"}
    assert "shared_knowledge" in coverage["unassessed_scopes"]
    assert report["program_dir"] == str(program)
