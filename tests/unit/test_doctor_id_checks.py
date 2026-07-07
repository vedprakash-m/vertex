from __future__ import annotations

from pathlib import Path

import pytest

from src.commands.doctor_checks.id_checks import (
    load_dependency_workstream_ids,
    load_program_edition_ids,
    load_scorecard_dimension_bindings,
)
from src.core.exceptions import ConfigError


def test_load_program_edition_ids_ignores_invalid_documents(tmp_path: Path) -> None:
    editions_root = tmp_path / "editions"
    editions_root.mkdir()
    (editions_root / "demo_weekly.yaml").write_text("id: demo_weekly\nprogram_id: demo\n", encoding="utf-8")
    (editions_root / "demo_monthly.yaml").write_text("id: demo_monthly\nprogram_id: demo\n", encoding="utf-8")
    (editions_root / "other.yaml").write_text("id: other_weekly\nprogram_id: other\n", encoding="utf-8")
    (editions_root / "missing_id.yaml").write_text("program_id: demo\n", encoding="utf-8")
    (editions_root / "invalid.yaml").write_text("[\n", encoding="utf-8")

    assert load_program_edition_ids("demo", editions_root=editions_root) == ("demo_monthly", "demo_weekly")


def test_load_dependency_workstream_ids_reads_current_workstreams(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    (program_root / "workstreams.yaml").write_text(
        "workstreams:\n  - id: ws-beta\n    name: Beta\n  - id: ws-alpha\n    name: Alpha\n",
        encoding="utf-8",
    )

    assert load_dependency_workstream_ids("demo", programs_root=programs_root) == ("ws-alpha", "ws-beta")


def test_load_scorecard_dimension_bindings_requires_workstream_ids(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    (program_root / "scorecards.yaml").write_text(
        "scorecards:\n"
        "  - name: Health\n"
        "    dimensions:\n"
        "      - name: Delivery\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="is missing workstream_id"):
        load_scorecard_dimension_bindings("demo", programs_root=programs_root)
