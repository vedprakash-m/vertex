"""Unit tests for scripts/program_inventory.py (specs/declutter.md Phase 0.5 task 1).

Locks the root-entry classification contract: recognized tiers, runtime-artifact
location reporting (legacy root vs canonical runtime/ + split-brain), clutter
detection, and the clean/dirty exit gate.
"""
from __future__ import annotations

from pathlib import Path

from scripts.program_inventory import inventory_program


def _seed_program(root: Path, program_id: str = "t") -> Path:
    program_dir = root / program_id
    program_dir.mkdir(parents=True)
    # Recognized T-1 authored config.
    (program_dir / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    # Recognized T-2 mutable state.
    (program_dir / "decisions.yaml").write_text("{}", encoding="utf-8")
    # Recognized dir.
    (program_dir / "journal").mkdir()
    # Runtime artifact at legacy root.
    (program_dir / "channel_registry.sqlite3").write_bytes(b"\x00" * 16)
    # Runtime artifact that has ALSO been migrated to runtime/ (split-brain).
    (program_dir / "gather_state.json").write_text("{}", encoding="utf-8")
    runtime = program_dir / "runtime"
    runtime.mkdir()
    (runtime / "gather_state.json").write_text("{}", encoding="utf-8")
    # Clutter remnants.
    (program_dir / "decisions.yaml.bak").write_text("stale", encoding="utf-8")
    (program_dir / "risk_register.yaml.cp1252bak").write_text("stale", encoding="utf-8")
    # Unrecognized file.
    (program_dir / "mystery_unknown.yaml").write_text("?", encoding="utf-8")
    # Recognized marker.
    (program_dir / ".edition_layout.json").write_text("{}", encoding="utf-8")
    return program_dir


def _by_name(report: dict) -> dict[str, dict]:
    return {e["name"]: e for e in report["root_entries"]}


def test_classifies_recognized_t1_t2_and_dirs(tmp_path: Path) -> None:
    _seed_program(tmp_path)
    report = inventory_program("t", tmp_path, root_only=True)
    entries = _by_name(report)
    assert entries["program.yaml"]["classification"] == "recognized"
    assert entries["program.yaml"]["tier"] == "T-1"
    assert entries["decisions.yaml"]["classification"] == "recognized"
    assert entries["decisions.yaml"]["tier"] == "T-2"
    assert entries["journal"]["classification"] == "recognized"
    assert entries["journal"]["kind"] == "dir"


def test_classifies_runtime_artifacts_with_location_signals(tmp_path: Path) -> None:
    _seed_program(tmp_path)
    report = inventory_program("t", tmp_path, root_only=True)
    entries = _by_name(report)
    chan = entries["channel_registry.sqlite3"]
    assert chan["classification"] == "runtime-artifact"
    assert chan["tier"] == "T-3b"
    assert chan["artifact"] == "channel_registry"
    assert chan["checkpointed"] is True
    assert chan["at_root"] is True
    assert chan["at_runtime"] is False
    assert chan["split_brain"] is False

    gather = entries["gather_state.json"]
    assert gather["classification"] == "runtime-artifact"
    # Present at both legacy root and canonical runtime/ -> split-brain signal.
    assert gather["at_root"] is True
    assert gather["at_runtime"] is True
    assert gather["split_brain"] is True


def test_clutter_and_unrecognized_make_report_dirty(tmp_path: Path) -> None:
    _seed_program(tmp_path)
    report = inventory_program("t", tmp_path, root_only=True)
    s = report["summary"]
    clutter_names = {c["name"] for c in s["clutter"]}
    assert "decisions.yaml.bak" in clutter_names
    assert "risk_register.yaml.cp1252bak" in clutter_names
    assert s["clutter_count"] == 2
    assert s["unrecognized_count"] == 1
    assert s["unrecognized"][0]["name"] == "mystery_unknown.yaml"
    assert s["clean"] is False


def test_clean_program_when_no_clutter_or_unrecognized(tmp_path: Path) -> None:
    program_dir = tmp_path / "clean"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text("schema_version: '3.0'\n", encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text("{}", encoding="utf-8")
    report = inventory_program("clean", tmp_path, root_only=True)
    assert report["summary"]["clean"] is True
    assert report["summary"]["clutter_count"] == 0
    assert report["summary"]["unrecognized_count"] == 0


def test_root_only_omits_subdir_sizes(tmp_path: Path) -> None:
    _seed_program(tmp_path)
    report = inventory_program("t", tmp_path, root_only=True)
    assert report["subdir_sizes"] == []


def test_recursive_mode_reports_subdir_sizes(tmp_path: Path) -> None:
    _seed_program(tmp_path)
    report = inventory_program("t", tmp_path, root_only=False)
    names = {d["name"] for d in report["subdir_sizes"]}
    assert "journal" in names
    assert "runtime" in names
    # runtime/ holds gather_state.json (one file).
    runtime_entry = next(d for d in report["subdir_sizes"] if d["name"] == "runtime")
    assert runtime_entry["file_count"] >= 1


def test_missing_program_dir_reports_exists_false(tmp_path: Path) -> None:
    report = inventory_program("absent", tmp_path, root_only=True)
    assert report["program_dir_exists"] is False
    assert report["root_entries"] == []
    assert report["summary"] == {}