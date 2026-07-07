"""GAP-40: Schema evolution / state migration engine."""
from __future__ import annotations

import json
from pathlib import Path

from src.core.schema_evolution import (
    PROGRAM_YAML_EVOLUTION,
    SchemaEvolutionResult,
    SchemaVersionStep,
    run_evolution,
)


def test_run_evolution_applies_2_to_4_full_walk() -> None:
    """A 2.0 program.yaml walks both 2→3 and 3→4 steps to reach 4.0."""
    document = {
        "schema_version": "2.0",
        "name": "acme",
        "workstreams": [{"id": "ws-1"}],
    }
    result = run_evolution(
        document, artifact="acme/program.yaml", evolution=PROGRAM_YAML_EVOLUTION
    )
    assert document["schema_version"] == "4.0"
    assert document["retention_days"] == 365
    assert document["fact_store_sor"] == "legacy"
    assert document["workstreams"][0]["privacy_classification"] == "internal"
    assert result.starting_version == "2.0"
    assert result.ending_version == "4.0"
    assert len(result.steps_applied) == 2


def test_run_evolution_single_2_to_3() -> None:
    """A 3.0 program.yaml is left at 3.0 with the 2→3 step already applied.

    Running the engine on a 3.0 document that already has 2→3 fields
    (retention_days, privacy_classification) only applies the 3→4 step
    because the 2→3 step's from_version='2.0' doesn't match the current
    version '3.0'.
    """
    document = {
        "schema_version": "3.0",
        "name": "acme",
        "workstreams": [{"id": "ws-1", "privacy_classification": "internal"}],
        "retention_days": 365,
    }
    result = run_evolution(
        document, artifact="acme/program.yaml", evolution=PROGRAM_YAML_EVOLUTION
    )
    # Second step (3→4) DOES apply
    assert document["schema_version"] == "4.0"
    assert document["fact_store_sor"] == "legacy"
    assert document["decisions_corroboration_required"] is False
    assert result.starting_version == "3.0"
    assert result.ending_version == "4.0"
    assert len(result.steps_applied) == 1
    assert result.steps_applied[0].from_version == "3.0"


def test_run_evolution_2_to_4_in_two_steps() -> None:
    """A 2.0 program.yaml walks both steps to reach 4.0."""
    document = {"schema_version": "2.0", "name": "acme"}
    result = run_evolution(
        document, artifact="acme/program.yaml", evolution=PROGRAM_YAML_EVOLUTION
    )
    assert document["schema_version"] == "4.0"
    assert document["fact_store_sor"] == "legacy"
    assert document["decisions_corroboration_required"] is False
    assert len(result.steps_applied) == 2


def test_run_evolution_4_to_4_is_no_op() -> None:
    """A 4.0 program.yaml is left untouched (no further steps)."""
    document = {"schema_version": "4.0", "name": "acme"}
    result = run_evolution(
        document, artifact="acme/program.yaml", evolution=PROGRAM_YAML_EVOLUTION
    )
    assert document.get("schema_version") == "4.0"
    assert result.steps_applied == ()


def test_run_evolution_dry_run_does_not_mutate() -> None:
    """apply=False leaves the original document unchanged."""
    document = {"schema_version": "2.0", "name": "acme"}
    original = json.loads(json.dumps(document))
    result = run_evolution(
        document,
        artifact="acme/program.yaml",
        evolution=PROGRAM_YAML_EVOLUTION,
        apply=False,
    )
    assert document == original
    assert result.applied is False
    # Both steps still "applied" in the audit (just not to the caller)
    assert len(result.steps_applied) == 2


def test_run_evolution_writes_migration_log(tmp_path: Path) -> None:
    """A migration_log.jsonl line is appended for each applied step."""
    document = {"schema_version": "2.0", "name": "acme"}
    log_path = tmp_path / "acme" / "migration_log.jsonl"
    run_evolution(
        document,
        artifact="acme/program.yaml",
        evolution=PROGRAM_YAML_EVOLUTION,
        migration_log_path=log_path,
        operator="maintainer",
    )
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["from_version"] == "2.0"
    assert first["to_version"] == "3.0"
    assert first["operator"] == "maintainer"
    assert first["artifact"] == "acme/program.yaml"
    assert first["pre_hash"]  # non-empty SHA-256 hex
    assert second["from_version"] == "3.0"
    assert second["to_version"] == "4.0"


def test_run_evolution_pre_hash_changes_between_steps(tmp_path: Path) -> None:
    """pre_hash on step 2 differs from step 1 (because the document changed)."""
    document = {"schema_version": "2.0", "name": "acme"}
    log_path = tmp_path / "migration_log.jsonl"
    run_evolution(
        document,
        artifact="acme/program.yaml",
        evolution=PROGRAM_YAML_EVOLUTION,
        migration_log_path=log_path,
    )
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["pre_hash"] != lines[1]["pre_hash"]


def test_run_evolution_default_schema_version_is_0_0() -> None:
    """A document with no schema_version is treated as 0.0 — never a 2.0 step."""
    document: dict = {"name": "acme"}
    result = run_evolution(
        document, artifact="acme/program.yaml", evolution=PROGRAM_YAML_EVOLUTION
    )
    # Neither step applies (2.0 != 0.0)
    assert result.steps_applied == ()


def test_run_evolution_handles_2_to_3_with_no_workstreams() -> None:
    """Empty/missing workstreams list does not crash the evolution."""
    document = {"schema_version": "2.0", "name": "acme"}
    result = run_evolution(
        document, artifact="acme/program.yaml", evolution=PROGRAM_YAML_EVOLUTION
    )
    # Walks all the way to 4.0 (no workstreams list → no per-entry work needed)
    assert document["schema_version"] == "4.0"
    assert document["retention_days"] == 365
    assert document["fact_store_sor"] == "legacy"
    assert "workstreams" not in document or document["workstreams"] == []


def test_custom_evolution_single_step() -> None:
    """Custom one-step evolution (3.0 → 3.5) works."""
    def _bump_minor(doc):
        doc = dict(doc)
        doc["schema_version"] = "3.5"
        doc["new_field"] = "set"
        return doc

    evolution = (
        SchemaVersionStep(
            from_version="3.0",
            to_version="3.5",
            description="add new_field, bump minor",
            transform=_bump_minor,
        ),
    )
    document = {"schema_version": "3.0"}
    result = run_evolution(
        document, artifact="x/program.yaml", evolution=evolution
    )
    assert document["schema_version"] == "3.5"
    assert document["new_field"] == "set"
    assert len(result.steps_applied) == 1


def test_run_evolution_skips_unrelated_steps() -> None:
    """A step whose from_version doesn't match the current version is skipped."""
    def _noop(doc):
        return doc

    evolution = (
        SchemaVersionStep(
            from_version="9.0",
            to_version="10.0",
            description="never reachable from 2.0",
            transform=_noop,
        ),
        SchemaVersionStep(
            from_version="2.0",
            to_version="3.0",
            description="bump to 3.0",
            transform=lambda d: {**d, "schema_version": "3.0"},
        ),
    )
    document = {"schema_version": "2.0"}
    result = run_evolution(
        document, artifact="x/program.yaml", evolution=evolution
    )
    assert document["schema_version"] == "3.0"
    assert len(result.steps_applied) == 1
    assert result.steps_applied[0].from_version == "2.0"
