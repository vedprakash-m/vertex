from __future__ import annotations

from pathlib import Path

import pytest

from src.core.capability_status import load_program_capability_status
from src.core.exceptions import ConfigError


def test_load_program_capability_status_round_trips_yaml_date(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "acme"
    program_root.mkdir(parents=True)
    (program_root / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (program_root / "capability_status.yaml").write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: ado_activation",
                "    status: in_progress",
                "    summary: ADO activation is in progress.",
                "    last_reviewed_on: 2026-05-17",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    statuses = load_program_capability_status("acme", programs_root=programs_root)

    ado = next(status for status in statuses if status.capability_id == "ado_activation")
    assert ado.last_reviewed_on is not None
    assert ado.last_reviewed_on.isoformat() == "2026-05-17"


def test_load_program_capability_status_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "acme"
    program_root.mkdir(parents=True)
    (program_root / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (program_root / "capability_status.yaml").write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: ado_activation",
                "    status: 123",
                "    summary: ADO activation is in progress.",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="status must be a string"):
        load_program_capability_status("acme", programs_root=programs_root)


def test_load_program_capability_status_rejects_non_string_last_reviewed_on(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "acme"
    program_root.mkdir(parents=True)
    (program_root / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (program_root / "capability_status.yaml").write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: ado_activation",
                "    status: in_progress",
                "    summary: ADO activation is in progress.",
                "    last_reviewed_on: 123",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Invalid last_reviewed_on"):
        load_program_capability_status("acme", programs_root=programs_root)