from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.source_waiver_store import load_source_waivers


def test_load_source_waivers_returns_empty_when_file_is_missing(tmp_path: Path) -> None:
    assert load_source_waivers("demo", programs_root=tmp_path) == ()


def test_load_source_waivers_parses_valid_document(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "source_waivers.yaml").write_text(
        """
schema_version: '1.0'
waivers:
  - contract_id: demo.slice
    role: telemetry
    owner: owner@example.com
    reason: Known telemetry delay.
    granted: 2026-05-01
    expires: 2026-06-30
""".strip(),
        encoding="utf-8",
    )

    waivers = load_source_waivers("demo", programs_root=tmp_path)

    assert len(waivers) == 1
    assert waivers[0].contract_id == "demo.slice"
    assert waivers[0].role == "telemetry"
    assert waivers[0].owner == "owner@example.com"


def test_load_source_waivers_rejects_invalid_document(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "source_waivers.yaml").write_text(
        """
schema_version: '1.0'
waivers:
  - contract_id: demo.slice
    role: telemetry
    owner: owner@example.com
    reason: Missing valid dates.
    granted: 2026-05-40
    expires: 2026-06-30
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_source_waivers("demo", programs_root=tmp_path)
