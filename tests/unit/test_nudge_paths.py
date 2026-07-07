"""Unit tests for NudgePaths and get_nudge_paths() in edition_resolver."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.core.edition_resolver import NudgePaths, get_nudge_paths


# ---------------------------------------------------------------------------
# get_nudge_paths structure
# ---------------------------------------------------------------------------


def test_get_nudge_paths_returns_nudge_paths_instance(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert isinstance(np, NudgePaths)


def test_nudge_paths_nudge_root_under_program_dir(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.nudge_root == tmp_path / "nova" / "nudge"


def test_nudge_paths_state_under_nudge_root(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.state_path == np.nudge_root / "nudge_state.json"


def test_nudge_paths_audit_under_nudge_root(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.audit_path == np.nudge_root / "nudge_audit.jsonl"


def test_nudge_paths_audit_lock_derives_from_audit(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.audit_lock_path == np.nudge_root / "nudge_audit.jsonl.lock"


def test_nudge_paths_title_cache_under_nudge_root(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.title_cache_path == np.nudge_root / "title_cache.json"


def test_nudge_paths_run_lock_under_nudge_root(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.run_lock_path == np.nudge_root / ".run.lock"


def test_nudge_paths_drafts_dir_under_nudge_root(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.drafts_dir == np.nudge_root / "drafts"


def test_nudge_paths_published_eml_dir_under_nudge_root(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.published_eml_dir == np.nudge_root / "published_eml"


def test_nudge_paths_published_eml_index_under_published_dir(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    assert np.published_eml_index_path == np.published_eml_dir / "index.json"


def test_get_nudge_paths_different_programs_have_different_roots(tmp_path: Path) -> None:
    np_nova = get_nudge_paths("nova", programs_root=tmp_path)
    np_armada = get_nudge_paths("armada", programs_root=tmp_path)
    assert np_nova.nudge_root != np_armada.nudge_root
    assert np_nova.state_path != np_armada.state_path


def test_nudge_paths_is_frozen(tmp_path: Path) -> None:
    np = get_nudge_paths("nova", programs_root=tmp_path)
    with pytest.raises((AttributeError, TypeError)):
        np.nudge_root = tmp_path  # type: ignore[misc]


# ---------------------------------------------------------------------------
# NudgePaths constants
# ---------------------------------------------------------------------------


def test_nudge_draft_retain_is_positive_integer() -> None:
    from src.core.nudge_models import NUDGE_DRAFT_RETAIN
    assert isinstance(NUDGE_DRAFT_RETAIN, int)
    assert NUDGE_DRAFT_RETAIN > 0


def test_nudge_published_index_schema_version_is_string() -> None:
    from src.core.nudge_models import NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION
    assert isinstance(NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION, str)
    assert NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION


def test_nudge_audit_event_includes_marked_sent() -> None:
    import typing
    from src.core.nudge_models import NudgeAuditEvent
    hints = typing.get_type_hints(NudgeAuditEvent)
    event_type_hint = hints["event_type"]
    args = typing.get_args(event_type_hint)
    assert "nudge_marked_sent" in args


# ---------------------------------------------------------------------------
# Migration script tests
# ---------------------------------------------------------------------------


def test_migrate_nudge_layout_noop_when_no_legacy(tmp_path: Path) -> None:
    """Script returns no-op when no legacy paths exist."""
    from scripts.migrate_nudge_layout import migrate_program
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    (programs_root / "nova").mkdir()

    result = migrate_program("nova", programs_root=programs_root, dry_run=False, verbose=False)
    assert result["status"] == "no-op"


def test_migrate_nudge_layout_dry_run_makes_no_changes(tmp_path: Path) -> None:
    """Dry run reports what would happen but creates no new files."""
    from scripts.migrate_nudge_layout import migrate_program
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    program_dir = programs_root / "nova"
    program_dir.mkdir()

    legacy_state = program_dir / "nudge_state.json"
    legacy_state.write_text('{"schema_version": "1.1"}', encoding="utf-8")

    result = migrate_program("nova", programs_root=programs_root, dry_run=True, verbose=False)
    assert result["status"] == "dry-run"
    assert not (program_dir / "nudge" / "nudge_state.json").exists()
    assert legacy_state.exists()  # Not deleted in dry-run


def test_migrate_nudge_layout_moves_state_file(tmp_path: Path) -> None:
    """State file is moved from legacy to canonical location."""
    from scripts.migrate_nudge_layout import migrate_program
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    program_dir = programs_root / "nova"
    program_dir.mkdir()

    legacy_state = program_dir / "nudge_state.json"
    legacy_state.write_text('{"schema_version": "1.1", "item:42": "2026-06-01T00:00:00+00:00"}', encoding="utf-8")

    result = migrate_program("nova", programs_root=programs_root, dry_run=False, verbose=False)
    assert result["status"] == "migrated"
    new_state = program_dir / "nudge" / "nudge_state.json"
    assert new_state.exists()
    assert not legacy_state.exists()
    data = json.loads(new_state.read_text(encoding="utf-8"))
    assert data.get("schema_version") == "1.1"


def test_migrate_nudge_layout_moves_eml_files(tmp_path: Path) -> None:
    """EML files from full/ and preview/ move to drafts/ with normalized names."""
    from scripts.migrate_nudge_layout import migrate_program
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    program_dir = programs_root / "nova"

    # Create legacy EML structure
    full_dir = program_dir / "output" / "nova_nudge" / "full"
    preview_dir = program_dir / "output" / "nova_nudge" / "preview"
    full_dir.mkdir(parents=True)
    preview_dir.mkdir(parents=True)
    full_eml = full_dir / "nudge_20260613T235418Z_abc12345.full.eml"
    preview_eml = preview_dir / "nudge_20260614T000000Z_def67890.preview.eml"
    full_eml.write_bytes(b"MIME-Version: 1.0\r\n")
    preview_eml.write_bytes(b"MIME-Version: 1.0\r\n")

    result = migrate_program("nova", programs_root=programs_root, dry_run=False, verbose=False)
    assert result["status"] == "migrated"
    assert result["eml_moved"] == 2

    drafts_dir = program_dir / "nudge" / "drafts"
    # Check normalized names (no .full / .preview suffix)
    assert (drafts_dir / "nudge_20260613T235418Z_abc12345.eml").exists()
    assert (drafts_dir / "nudge_20260614T000000Z_def67890.eml").exists()
    assert not full_eml.exists()
    assert not preview_eml.exists()


def test_migrate_nudge_layout_idempotent(tmp_path: Path) -> None:
    """Running migration twice does not error or duplicate files."""
    from scripts.migrate_nudge_layout import migrate_program
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    program_dir = programs_root / "nova"
    program_dir.mkdir()

    legacy_state = program_dir / "nudge_state.json"
    legacy_state.write_text('{"schema_version": "1.1"}', encoding="utf-8")

    result1 = migrate_program("nova", programs_root=programs_root, dry_run=False, verbose=False)
    assert result1["status"] == "migrated"

    result2 = migrate_program("nova", programs_root=programs_root, dry_run=False, verbose=False)
    assert result2["status"] == "no-op"
    assert not result2.get("errors")


def test_migrate_nudge_layout_scaffolds_published_eml_index(tmp_path: Path) -> None:
    """Migration creates published_eml/index.json if not present."""
    from scripts.migrate_nudge_layout import migrate_program
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    program_dir = programs_root / "nova"
    program_dir.mkdir()

    legacy_state = program_dir / "nudge_state.json"
    legacy_state.write_text('{"schema_version": "1.1"}', encoding="utf-8")

    migrate_program("nova", programs_root=programs_root, dry_run=False, verbose=False)

    index_path = program_dir / "nudge" / "published_eml" / "index.json"
    assert index_path.exists()
    data = json.loads(index_path.read_text(encoding="utf-8"))
    assert data == []
