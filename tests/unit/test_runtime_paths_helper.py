"""Tests for the sanctioned runtime-path test-fixture helper
(tests/support/runtime_paths.py, specs/declutter.md §12 Phase 0.5)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.runtime_paths import (
    RUNTIME_ARTIFACT_NAMES,
    canonical_runtime_artifact_path,
    legacy_runtime_artifact_path,
    seed_canonical_runtime_artifact,
    seed_legacy_runtime_artifact,
    seed_split_brain_runtime_artifact,
)


def test_runtime_artifact_names_match_registry() -> None:
    assert RUNTIME_ARTIFACT_NAMES == frozenset(
        {
            "gather_state",
            "run_telemetry",
            "dedup_drop_log",
            "vertex_analytics",
            "readiness_snapshot",
            "m365_registry",
            "channel_registry",
        }
    )


def test_legacy_and_canonical_paths_are_distinct(tmp_path: Path) -> None:
    legacy = legacy_runtime_artifact_path(tmp_path, "nova", "channel_registry")
    canonical = canonical_runtime_artifact_path(tmp_path, "nova", "channel_registry")
    assert legacy == tmp_path / "nova" / "channel_registry.sqlite3"
    assert canonical == tmp_path / "nova" / "runtime" / "channel_registry.sqlite3"
    assert legacy != canonical


def test_unknown_artifact_name_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        legacy_runtime_artifact_path(tmp_path, "nova", "not_a_real_artifact")


def test_seed_legacy_places_file_at_root(tmp_path: Path) -> None:
    path = seed_legacy_runtime_artifact(tmp_path, "nova", "gather_state", content="{}")
    assert path == tmp_path / "nova" / "gather_state.json"
    assert path.read_text(encoding="utf-8") == "{}"


def test_seed_canonical_places_file_in_runtime_dir(tmp_path: Path) -> None:
    path = seed_canonical_runtime_artifact(tmp_path, "nova", "run_telemetry", content=b"x")
    assert path == tmp_path / "nova" / "runtime" / "run_telemetry.jsonl"
    assert path.read_bytes() == b"x"


def test_seed_split_brain_places_at_both_locations(tmp_path: Path) -> None:
    legacy, canonical = seed_split_brain_runtime_artifact(tmp_path, "nova", "m365_registry")
    assert legacy == tmp_path / "nova" / "m365_registry.yaml"
    assert canonical == tmp_path / "nova" / "runtime" / "m365_registry.yaml"
    assert legacy.exists() and canonical.exists()