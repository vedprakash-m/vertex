"""Tests for scripts/vertex_onboard_scaffold.py — extended onboard scaffold generator."""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path

from scripts.vertex_onboard_scaffold import (
    ScaffoldResult,
    _load_program_id,
    _load_workstream_ids,
    _stage6_content,
    _stage7_content,
    _stage8_content,
    _stage9_content,
    main,
    run_scaffold,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _seed_program(tmp_path: Path, program_id: str = "testprog") -> Path:
    """Seed a minimal program directory with stages 1-5 artifacts."""
    prog = tmp_path / "programs" / program_id
    prog.mkdir(parents=True)
    (prog / "program.yaml").write_text(
        yaml.dump({"schema_version": "3.0", "id": program_id, "name": "Test Program", "mission": "testing"}),
        encoding="utf-8",
    )
    (prog / "workstreams.yaml").write_text(
        yaml.dump({"workstreams": [{"id": "ws1", "name": "Workstream 1"}, {"id": "ws2", "name": "Workstream 2"}]}),
        encoding="utf-8",
    )
    return prog


# ─────────────────────────────────────────────────────────────────────────────
# Helper function tests
# ─────────────────────────────────────────────────────────────────────────────

def test_load_program_id_from_yaml(tmp_path: Path) -> None:
    prog = tmp_path / "myprog"
    prog.mkdir()
    (prog / "program.yaml").write_text(yaml.dump({"id": "myprog", "name": "X"}), encoding="utf-8")
    assert _load_program_id(prog) == "myprog"


def test_load_program_id_fallback_to_dir_name(tmp_path: Path) -> None:
    prog = tmp_path / "fallbackprog"
    prog.mkdir()
    assert _load_program_id(prog) == "fallbackprog"


def test_load_workstream_ids(tmp_path: Path) -> None:
    prog = tmp_path / "prog"
    prog.mkdir()
    (prog / "workstreams.yaml").write_text(
        yaml.dump({"workstreams": [{"id": "ws-a"}, {"id": "ws-b"}]}),
        encoding="utf-8",
    )
    assert _load_workstream_ids(prog) == ["ws-a", "ws-b"]


def test_load_workstream_ids_missing_file(tmp_path: Path) -> None:
    prog = tmp_path / "prog"
    prog.mkdir()
    assert _load_workstream_ids(prog) == []


# ─────────────────────────────────────────────────────────────────────────────
# Content generator tests
# ─────────────────────────────────────────────────────────────────────────────

def test_stage6_content_valid_yaml() -> None:
    content = _stage6_content("acme", ["ws1", "ws2"])
    data = yaml.safe_load(content)
    assert "pages" in data
    assert len(data["pages"]) >= 1
    page = data["pages"][0]
    assert "id" in page
    assert "url" in page
    assert "acme" in page.get("program_ids", [])


def test_stage7_content_valid_yaml() -> None:
    content = _stage7_content("acme", ["ws1", "ws2"])
    data = yaml.safe_load(content)
    assert "entities" in data
    entities = data["entities"]
    entity_ids = [e.get("id") for e in entities if isinstance(e, dict)]
    assert "ws1" in entity_ids
    assert "ws2" in entity_ids


def test_stage7_content_no_workstreams() -> None:
    content = _stage7_content("acme", [])
    data = yaml.safe_load(content)
    assert "entities" in data


def test_stage8_content_valid_yaml() -> None:
    content = _stage8_content("acme", ["ws1"])
    data = yaml.safe_load(content)
    assert "sources" in data
    assert "m365_calendar" in data["sources"]
    assert "kusto" in data["sources"]
    assert data["sources"]["kusto"]["enabled"] is False


def test_stage9_content_has_required_sections() -> None:
    content = _stage9_content("acme")
    assert "Backfill Sessions" in content
    assert "Gap Event Register" in content
    assert "OSD Decisions" in content
    assert "acme" in content


# ─────────────────────────────────────────────────────────────────────────────
# run_scaffold tests
# ─────────────────────────────────────────────────────────────────────────────

def test_run_scaffold_missing_program_raises(tmp_path: Path) -> None:
    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        with pytest.raises(FileNotFoundError, match="Program directory not found"):
            run_scaffold("nonexistent_program")
    finally:
        mod._programs_root = orig


def test_run_scaffold_dry_run_creates_nothing(tmp_path: Path) -> None:
    prog = _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        results = run_scaffold("testprog", dry_run=True)
    finally:
        mod._programs_root = orig

    # No files should have been created
    assert not any(r.created for r in results)
    assert not (prog / "knowledge" / "engms_pages.yaml").exists()
    assert not (prog / "knowledge" / "entities.yaml").exists()


def test_run_scaffold_creates_stage6_file(tmp_path: Path) -> None:
    prog = _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        results = run_scaffold("testprog", stages=[6])
    finally:
        mod._programs_root = orig

    stage6_result = next(r for r in results if r.stage == 6)
    assert stage6_result.created
    target = prog / "knowledge" / "engms_pages.yaml"
    assert target.exists()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert "pages" in data


def test_run_scaffold_creates_stage7_entities(tmp_path: Path) -> None:
    prog = _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        results = run_scaffold("testprog", stages=[7])
    finally:
        mod._programs_root = orig

    stage7_result = next(r for r in results if r.stage == 7)
    assert stage7_result.created
    target = prog / "knowledge" / "entities.yaml"
    assert target.exists()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert "entities" in data
    # Should have entities for ws1 and ws2
    entity_ids = [e.get("id") for e in data["entities"] if isinstance(e, dict)]
    assert "ws1" in entity_ids


def test_run_scaffold_creates_stage8_discovery_config(tmp_path: Path) -> None:
    prog = _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        results = run_scaffold("testprog", stages=[8])
    finally:
        mod._programs_root = orig

    stage8_result = next(r for r in results if r.stage == 8)
    assert stage8_result.created
    target = prog / "knowledge" / "discovery_config.yaml"
    assert target.exists()


def test_run_scaffold_creates_stage9_run_log(tmp_path: Path) -> None:
    prog = _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        results = run_scaffold("testprog", stages=[9])
    finally:
        mod._programs_root = orig

    stage9_result = next(r for r in results if r.stage == 9)
    assert stage9_result.created
    target = prog / "onboard_run_log.md"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "Backfill Sessions" in content


def test_run_scaffold_skips_stage10_and_11() -> None:
    """Stages 10 and 11 have no scaffold file — should be skipped with guidance."""
    # We can test this without a real program dir since the skip happens before file ops
    from scripts.vertex_onboard_scaffold import _scaffold_stage, ScaffoldResult
    import pathlib

    dummy = pathlib.Path("/nonexistent")
    r10 = _scaffold_stage(10, dummy, "test", [], dry_run=True)
    assert r10.skipped
    assert "vertex doctor" in r10.reason

    r11 = _scaffold_stage(11, dummy, "test", [], dry_run=True)
    assert r11.skipped
    assert "gap" in r11.reason.lower() or "ledger" in r11.reason.lower()


def test_run_scaffold_skips_existing_file(tmp_path: Path) -> None:
    prog = _seed_program(tmp_path)
    knowledge = prog / "knowledge"
    knowledge.mkdir(exist_ok=True)
    existing = knowledge / "engms_pages.yaml"
    existing.write_text("# existing\nschema_version: '1.0'\npages: []\n", encoding="utf-8")

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        results = run_scaffold("testprog", stages=[6])
    finally:
        mod._programs_root = orig

    stage6_result = next(r for r in results if r.stage == 6)
    assert stage6_result.skipped
    assert not stage6_result.created
    # File should be unchanged
    assert "# existing" in existing.read_text(encoding="utf-8")


def test_run_scaffold_all_stages_returns_6_results(tmp_path: Path) -> None:
    _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        results = run_scaffold("testprog")
    finally:
        mod._programs_root = orig

    assert len(results) == 6  # stages 6-11
    stage_nums = [r.stage for r in results]
    assert stage_nums == [6, 7, 8, 9, 10, 11]


# ─────────────────────────────────────────────────────────────────────────────
# CLI tests
# ─────────────────────────────────────────────────────────────────────────────

def test_main_dry_run_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        rc = main(["--program", "testprog", "--dry-run"])
    finally:
        mod._programs_root = orig

    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()


def test_main_missing_program_exits_one(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--program", "nonexistent_prog_xyz"])
    assert rc == 1


def test_main_stage_filter(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        rc = main(["--program", "testprog", "--stage", "6", "--dry-run"])
    finally:
        mod._programs_root = orig

    assert rc == 0
    out = capsys.readouterr().out
    assert "Stage  6" in out
    assert "Stage  7" not in out


def test_main_creates_files(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _seed_program(tmp_path)

    import scripts.vertex_onboard_scaffold as mod
    orig = mod._programs_root
    mod._programs_root = lambda: tmp_path / "programs"
    try:
        rc = main(["--program", "testprog"])
    finally:
        mod._programs_root = orig

    assert rc == 0
    prog = tmp_path / "programs" / "testprog"
    assert (prog / "knowledge" / "engms_pages.yaml").exists()
    assert (prog / "knowledge" / "entities.yaml").exists()
    assert (prog / "knowledge" / "discovery_config.yaml").exists()
    assert (prog / "onboard_run_log.md").exists()


def test_script_exists() -> None:
    script = REPO_ROOT / "scripts" / "vertex_onboard_scaffold.py"
    assert script.exists()


REPO_ROOT = Path(__file__).resolve().parents[2]
