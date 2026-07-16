"""ADF-W2.5: unit tests for src/core/fact_lineage_waiver_store.py."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.fact_lineage_waiver_store import load_fact_lineage_waivers


def test_missing_file_returns_empty_tuple(tmp_path: Path) -> None:
    assert load_fact_lineage_waivers("xpf", programs_root=tmp_path / "programs") == ()


def test_loads_valid_waivers(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "fact_lineage_waivers.yaml").write_text(
        """
schema_version: "1.0"
waivers:
  - natural_key: "risk:legacy-item-1"
    owner: "alice@example.com"
    reason: "Pre-dates fact-store migration; no durable source to backfill."
    granted: "2026-01-01"
    expires: "2026-12-31"
""".strip(),
        encoding="utf-8",
    )

    waivers = load_fact_lineage_waivers("xpf", programs_root=programs_root)
    assert len(waivers) == 1
    waiver = waivers[0]
    assert waiver.natural_key == "risk:legacy-item-1"
    assert waiver.owner == "alice@example.com"
    assert waiver.granted == date(2026, 1, 1)
    assert waiver.expires == date(2026, 12, 31)
    assert waiver.is_active(as_of=date(2026, 6, 1))
    assert not waiver.is_active(as_of=date(2027, 1, 1))
    assert not waiver.is_active(as_of=date(2025, 12, 31))


def test_rejects_wrong_schema_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "fact_lineage_waivers.yaml").write_text(
        'schema_version: "2.0"\nwaivers: []\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="schema version"):
        load_fact_lineage_waivers("xpf", programs_root=programs_root)


def test_rejects_missing_required_field(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "fact_lineage_waivers.yaml").write_text(
        """
schema_version: "1.0"
waivers:
  - natural_key: "risk:legacy-item-1"
    owner: "alice@example.com"
    granted: "2026-01-01"
    expires: "2026-12-31"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="reason"):
        load_fact_lineage_waivers("xpf", programs_root=programs_root)


def test_rejects_invalid_date(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "xpf"
    program_dir.mkdir(parents=True)
    (program_dir / "fact_lineage_waivers.yaml").write_text(
        """
schema_version: "1.0"
waivers:
  - natural_key: "risk:legacy-item-1"
    owner: "alice@example.com"
    reason: "test"
    granted: "not-a-date"
    expires: "2026-12-31"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="granted"):
        load_fact_lineage_waivers("xpf", programs_root=programs_root)
