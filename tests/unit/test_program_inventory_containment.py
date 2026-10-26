from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import program_inventory


def test_cli_requires_explicit_program_without_reading_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_inventory(*args, **kwargs):
        pytest.fail("Missing program must not trigger an implicit inventory.")

    monkeypatch.setattr(program_inventory, "inventory_program", unexpected_inventory)
    with pytest.raises(SystemExit) as error:
        program_inventory.main([])
    assert error.value.code == 2


def _program(root: Path) -> Path:
    path = root / "fixture"
    path.mkdir()
    (path / "program.yaml").write_bytes(b"PRIVATE-CONTENT-DO-NOT-READ")
    return path


@pytest.mark.parametrize("program_id", [
    "", ".", "..", "../outside", "nested/child", "nested\\child", "Q:outside", "bad\nid", "fixture.", "fixture ",
])
def test_inventory_rejects_non_bare_program_selectors(tmp_path: Path, program_id: str) -> None:
    with pytest.raises(ValueError):
        program_inventory.inventory_program(program_id, tmp_path, root_only=True)


def test_canonical_only_runtime_is_not_omitted(tmp_path: Path) -> None:
    path = _program(tmp_path)
    (path / "runtime").mkdir()
    (path / "runtime" / "channel_registry.sqlite3").write_bytes(b"not-a-real-database")
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)
    entries = {entry["name"]: entry for entry in report["runtime_artifacts"]}
    assert entries["channel_registry.sqlite3"]["at_root"] is False
    assert entries["channel_registry.sqlite3"]["at_runtime"] is True
    assert entries["channel_registry.sqlite3"]["split_brain"] is False
    assert entries["channel_registry.sqlite3"]["canonical_path"] == str(path / "runtime" / "channel_registry.sqlite3")


def test_split_brain_is_not_clean_even_without_clutter(tmp_path: Path) -> None:
    path = _program(tmp_path)
    (path / "runtime").mkdir()
    (path / "gather_state.json").write_bytes(b"legacy")
    (path / "runtime" / "gather_state.json").write_bytes(b"canonical")
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)
    assert report["summary"]["runtime_split_brain_count"] == 1
    assert report["summary"]["clean"] is False


@pytest.mark.parametrize("root_only", [True, False])
def test_inventory_never_reads_contents_or_claims_cleanup_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_only: bool,
) -> None:
    _program(tmp_path)

    def forbidden_read(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Inventory must inspect metadata only.")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=root_only)
    assert report["scope"]["content_reads"] is False
    assert report["scope"]["cleanup_authority"] is False
    assert report["scope"]["snapshot_consistent"] is False
    assert report["scope"]["recursive_sizes"] is (not root_only)


def test_stat_failure_cannot_produce_a_successful_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path = _program(tmp_path) / "program.yaml"
    original = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True):
        if self == path:
            raise PermissionError("PRIVATE-STAT-DETAIL")
        return original(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    result = program_inventory.main(["--program", "fixture", "--programs-root", str(tmp_path), "--root-only", "--format", "json"])
    output = capsys.readouterr()
    assert result == 2
    assert output.out == ""
    assert "PermissionError" in output.err
    assert "PRIVATE-STAT-DETAIL" not in output.err


def test_recursive_enumeration_failure_is_not_a_partial_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _program(tmp_path) / "journal"
    path.mkdir()
    original = program_inventory._scan

    def fail_scan(current: Path):
        if current == path:
            raise PermissionError("PRIVATE-SCAN-DETAIL")
        return original(current)

    monkeypatch.setattr(program_inventory, "_scan", fail_scan, raising=False)
    with pytest.raises(PermissionError):
        program_inventory.inventory_program("fixture", tmp_path, root_only=False)


@pytest.mark.parametrize("entry", ["program.yaml", "gather_state.json"])
def test_known_file_name_as_directory_is_not_recognized_or_clean(tmp_path: Path, entry: str) -> None:
    path = tmp_path / "fixture"
    path.mkdir()
    (path / entry).mkdir()
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)
    assert report["summary"]["clean"] is False


def test_unrecognized_tool_marker_is_preserved_and_not_clean(tmp_path: Path) -> None:
    path = _program(tmp_path) / ".unknown-tool"
    path.write_bytes(b"preserve")
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)
    assert report["summary"]["clean"] is False
    assert path.read_bytes() == b"preserve"


def test_cli_json_reports_scope_and_keeps_missing_program_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _program(tmp_path)
    result = program_inventory.main(["--program", "fixture", "--programs-root", str(tmp_path), "--root-only", "--format", "json"])
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scope"]["registered_runtime_locations"] is True
    result = program_inventory.main(["--program", "absent", "--programs-root", str(tmp_path), "--format", "json"])
    assert result == 1
    assert json.loads(capsys.readouterr().out)["program_dir_exists"] is False


@pytest.mark.parametrize("location", ["ancestor", "program", "entry", "nested", "canonical"])
@pytest.mark.parametrize("link_type", ["symlink", "reparse"])
def test_links_are_rejected_before_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, link_type: str,
) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    program = _program(root)
    if location == "ancestor":
        target = root
    elif location == "program":
        target = program
    elif location == "entry":
        target = program / "journal"
        target.mkdir()
    elif location == "canonical":
        (program / "runtime").mkdir()
        target = program / "runtime" / "gather_state.json"
        target.write_bytes(b"preserve")
    else:
        (program / "journal").mkdir()
        target = program / "journal" / "nested"
        target.mkdir()
    original = Path.stat

    def stat_path(self: Path, *, follow_symlinks: bool = True):
        value = original(self, follow_symlinks=follow_symlinks)
        if self == target:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if link_type == "symlink" else value.st_mode,
                st_file_attributes=0 if link_type == "symlink" else stat.FILE_ATTRIBUTE_REPARSE_POINT,
                st_size=value.st_size,
            )
        return value

    monkeypatch.setattr(Path, "stat", stat_path)
    with pytest.raises(ValueError, match="links or reparse"):
        program_inventory.inventory_program("fixture", root, root_only=False)


def test_canonical_runtime_wrong_kind_is_an_explicit_finding(tmp_path: Path) -> None:
    program = _program(tmp_path)
    (program / "runtime" / "gather_state.json").mkdir(parents=True)
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)
    assert report["summary"]["runtime_kind_mismatch_count"] == 1
    assert report["summary"]["clean"] is False


@pytest.mark.parametrize("location", ["program", "ancestor", "runtime"])
def test_wrong_kind_directory_paths_fail(tmp_path: Path, location: str) -> None:
    if location == "program":
        (tmp_path / "fixture").write_bytes(b"preserve")
        root = tmp_path
    elif location == "ancestor":
        root = tmp_path / "not-a-directory"
        root.write_bytes(b"preserve")
    else:
        program = _program(tmp_path)
        (program / "runtime").write_bytes(b"preserve")
        root = tmp_path
    with pytest.raises(ValueError):
        program_inventory.inventory_program("fixture", root, root_only=True)


def test_human_report_shows_canonical_only_store_and_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    program = _program(tmp_path)
    (program / "runtime").mkdir()
    (program / "runtime" / "gather_state.json").write_bytes(b"preserve")
    assert program_inventory.main(["--program", "fixture", "--programs-root", str(tmp_path), "--root-only"]) == 0
    output = capsys.readouterr()
    assert "gather_state.json: legacy=absent, canonical=file" in output.out
    assert "metadata only" in output.out
    assert "not an atomic snapshot or cleanup authorization" in output.out
    assert output.err == ""


def test_human_report_exposes_dirty_and_recursive_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    program = _program(tmp_path)
    (program / "runtime").mkdir()
    for path in (program / "gather_state.json", program / "runtime" / "gather_state.json"):
        path.write_bytes(b"preserve")
    (program / "channel_registry.sqlite3").write_bytes(b"preserve")
    (program / "program.yaml.bak").write_bytes(b"preserve")
    (program / "unknown").write_bytes(b"preserve")
    (program / "journal" / "nested").mkdir(parents=True)
    (program / "journal" / "nested" / "history").write_bytes(b"12345")
    assert program_inventory.main(["--program", "fixture", "--programs-root", str(tmp_path)]) == 1
    output = capsys.readouterr()
    for label in ("SPLIT-BRAIN", "checkpointed", "CLUTTER", "UNRECOGNIZED", "subdirectory sizes"):
        assert label in output.out
    report = program_inventory.inventory_program("fixture", tmp_path, root_only=False)
    journal = next(entry for entry in report["subdir_sizes"] if entry["name"] == "journal")
    assert journal == {"name": "journal", "file_count": 1, "size_bytes": 5}


def test_missing_ancestor_and_human_missing_program_remain_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    missing_root = tmp_path / "missing" / "programs"
    assert program_inventory.main(["--program", "fixture", "--programs-root", str(missing_root)]) == 1
    assert "exists=False" in capsys.readouterr().out


@pytest.mark.parametrize("root_only", [True, False])
def test_special_entries_cannot_be_counted_as_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_only: bool,
) -> None:
    program = _program(tmp_path)
    if root_only:
        target = program / "special"
    else:
        (program / "journal").mkdir()
        target = program / "journal" / "special"
    target.write_bytes(b"preserve")
    original = Path.stat

    def stat_path(self: Path, *, follow_symlinks: bool = True):
        value = original(self, follow_symlinks=follow_symlinks)
        if self == target:
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_file_attributes=0, st_size=value.st_size)
        return value

    monkeypatch.setattr(Path, "stat", stat_path)
    if root_only:
        report = program_inventory.inventory_program("fixture", tmp_path, root_only=True)
        assert report["summary"]["clean"] is False
        assert next(entry for entry in report["root_entries"] if entry["name"] == "special")["kind"] == "other"
    else:
        with pytest.raises(ValueError, match="unsupported"):
            program_inventory.inventory_program("fixture", tmp_path, root_only=False)


def test_recursive_directory_changed_to_a_file_is_not_a_zero_size(tmp_path: Path) -> None:
    path = tmp_path / "not-a-directory"
    path.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="remain directories"):
        program_inventory._dir_size(path)
