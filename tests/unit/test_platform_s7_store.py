from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.platform_s7_store import load_platform_s7_state, save_platform_s7_state


def test_save_and_load_platform_s7_state_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 7, 14, 0, tzinfo=timezone.utc)

    save_platform_s7_state(
        position="deferred",
        recorded_at=recorded_at,
        recorded_by="operator",
        justification="Tracked under approved deferral plan.",
        programs_root=programs_root,
    )

    loaded = load_platform_s7_state(programs_root=programs_root)

    assert loaded is not None
    assert loaded.position == "deferred"
    assert loaded.recorded_at == recorded_at
    assert loaded.recorded_by == "operator"
    assert loaded.justification == "Tracked under approved deferral plan."


def test_save_platform_s7_state_rejects_naive_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    with pytest.raises(ValueError, match="recorded_at must include timezone information"):
        save_platform_s7_state(
            position="complete",
            recorded_at=datetime(2026, 6, 7, 14, 0),
            recorded_by="operator",
            programs_root=programs_root,
        )


def test_load_platform_s7_state_rejects_non_string_position(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": 1,
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform S7 position must be a string"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_non_string_recorded_by(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": "complete",
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": 123,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform S7 recorded_by must be a string"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_non_string_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": "complete",
                "recorded_at": 123,
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform S7 recorded_at must be a string"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_yaml_timestamp_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        "schema_version: '1.0'\nposition: complete\nrecorded_at: 2026-06-07 14:00:00+00:00\nrecorded_by: operator\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform S7 recorded_at must be a string"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_naive_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": "complete",
                "recorded_at": "2026-06-07T14:00:00",
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform S7 recorded_at must include timezone information"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_noncanonical_schema_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.1",
                "position": "complete",
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unsupported platform S7 schema_version '1.1'"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_schema_version_with_whitespace(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": " 1.0 ",
                "position": "complete",
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": "operator",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unsupported platform S7 schema_version ' 1.0 '"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_noncanonical_position_case(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": "Deferred",
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": "operator",
                "justification": "Tracked under approved deferral plan.",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Platform S7 position .* must be 'complete' or 'deferred'"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_position_with_whitespace(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": " deferred ",
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": "operator",
                "justification": "Tracked under approved deferral plan.",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Platform S7 position .* must be 'complete' or 'deferred'"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_recorded_by_with_whitespace(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": "complete",
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": " operator ",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform S7 recorded_by must not contain surrounding whitespace"):
        load_platform_s7_state(programs_root=programs_root)


def test_load_platform_s7_state_rejects_justification_with_whitespace(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    state_path = programs_root / "platform_state.yaml"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "position": "deferred",
                "recorded_at": "2026-06-07T14:00:00+00:00",
                "recorded_by": "operator",
                "justification": " deferred until sign-off ",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform S7 justification must not contain surrounding whitespace"):
        load_platform_s7_state(programs_root=programs_root)