"""ADF-W2.6: unit tests for src/core/entity_binding_correction_store.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.entity_binding_correction_store import load_entity_binding_corrections
from src.core.exceptions import ConfigError


def test_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    assert load_entity_binding_corrections("xpf", programs_root=tmp_path / "programs") == {}


def test_rejects_wrong_schema_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "entity_binding_corrections.yaml").write_text('schema_version: "2.0"\ncorrections: []\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="schema version"):
        load_entity_binding_corrections("xpf", programs_root=programs_root)


def test_rejects_missing_raw_ref(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "entity_binding_corrections.yaml").write_text(
        """
schema_version: "1.0"
corrections:
  - accepted_entity_id: t1
    corrected_by: alice@example.com
    corrected_at: "2026-07-01"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="raw_ref"):
        load_entity_binding_corrections("xpf", programs_root=programs_root)


def test_rejects_missing_corrected_by(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "entity_binding_corrections.yaml").write_text(
        """
schema_version: "1.0"
corrections:
  - raw_ref: "Jordan River"
    accepted_entity_id: t1
    corrected_at: "2026-07-01"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="corrected_by"):
        load_entity_binding_corrections("xpf", programs_root=programs_root)


def test_rejects_invalid_corrected_at_date(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "entity_binding_corrections.yaml").write_text(
        """
schema_version: "1.0"
corrections:
  - raw_ref: "Jordan River"
    accepted_entity_id: t1
    corrected_by: alice@example.com
    corrected_at: "not-a-date"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="corrected_at"):
        load_entity_binding_corrections("xpf", programs_root=programs_root)


def test_later_duplicate_raw_ref_overwrites_earlier_one(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "entity_binding_corrections.yaml").write_text(
        """
schema_version: "1.0"
corrections:
  - raw_ref: "Jordan River"
    accepted_entity_id: t1
    corrected_by: alice@example.com
    corrected_at: "2026-07-01"
  - raw_ref: "Jordan River"
    accepted_entity_id: t2
    corrected_by: bob@example.com
    corrected_at: "2026-07-02"
""".strip(),
        encoding="utf-8",
    )
    corrections = load_entity_binding_corrections("xpf", programs_root=programs_root)
    assert corrections["Jordan River"].accepted_entity_id == "t2"
    assert corrections["Jordan River"].corrected_by == "bob@example.com"
