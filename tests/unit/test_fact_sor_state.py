from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.fact_sor_state import load_fact_sor_state, save_fact_sor_state


def test_save_and_load_fact_sor_state_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 7, 12, 30, tzinfo=timezone.utc)

    save_fact_sor_state(
        "acme",
        mode="primary",
        recorded_at=recorded_at,
        recorded_by="operator",
        programs_root=programs_root,
    )

    loaded = load_fact_sor_state("acme", programs_root=programs_root)

    assert loaded is not None
    assert loaded.mode == "primary"
    assert loaded.recorded_at == recorded_at
    assert loaded.recorded_by == "operator"


def test_load_fact_sor_state_rejects_non_string_mode(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "acme" / "fact_store_sor.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "mode": 1,
                "recorded_at": "2026-06-07T12:30:00+00:00",
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fact-store SoR mode must be a string"):
        load_fact_sor_state("acme", programs_root=programs_root)


def test_load_fact_sor_state_rejects_non_string_recorded_by(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "acme" / "fact_store_sor.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "mode": "primary",
                "recorded_at": "2026-06-07T12:30:00+00:00",
                "recorded_by": 123,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fact-store SoR recorded_by must be a string"):
        load_fact_sor_state("acme", programs_root=programs_root)


def test_load_fact_sor_state_rejects_non_string_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "acme" / "fact_store_sor.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "mode": "primary",
                "recorded_at": 123,
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="fact-store SoR recorded_at must be a string"):
        load_fact_sor_state("acme", programs_root=programs_root)