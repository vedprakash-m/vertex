from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import report_test_setup


def _write_program_tree(root: Path, *, program_id: str = "acme", include_workstreams: bool = True) -> Path:
    program_root = root / program_id
    program_root.mkdir(parents=True, exist_ok=True)
    (program_root / "program.yaml").write_text("id: acme\n", encoding="utf-8")
    if include_workstreams:
        (program_root / "workstreams.yaml").write_text("workstreams: []\n", encoding="utf-8")
    return program_root


def test_validate_cached_program_tree_accepts_required_bundle_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_tree(programs_root)

    report_test_setup._validate_cached_program_tree(programs_root)


def test_validate_cached_program_tree_rejects_missing_workstreams_yaml(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_tree(programs_root, include_workstreams=False)

    with pytest.raises(FileNotFoundError, match="acme/workstreams.yaml"):
        report_test_setup._validate_cached_program_tree(programs_root)


def test_validate_cached_program_tree_ignores_template_container_dir(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "_templates").mkdir(parents=True)
    _write_program_tree(programs_root)

    report_test_setup._validate_cached_program_tree(programs_root)
