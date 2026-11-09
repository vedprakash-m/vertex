from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from src.core.chapter_contract_loader import canonical_dimension_binding_id
from src.core.workstream_registry import load_workstream_registry
from tests.support.slice_contract_fixtures import build_test_slice_contract


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"


@pytest.mark.parametrize(
    ("scorecard_name", "namespace", "expected"),
    (
        ("EXD EXN Readiness", None, "exd_exn_readiness.performance"),
        ("EXD on EXN Readiness", None, "exd_on_exn_readiness.performance"),
        ("EXD on EXN Readiness", "exd", "exd.performance"),
        ("Delivery Readiness", "other", "delivery_readiness.performance"),
    ),
)
def test_dimension_binding_uses_scorecard_slug_or_matching_explicit_namespace(
    scorecard_name: str, namespace: str | None, expected: str,
) -> None:
    assert canonical_dimension_binding_id(
        scorecard_name, "Performance", chapter_namespace=namespace,
    ) == expected


def test_workstream_sub_program_requires_authored_binding(tmp_path: Path) -> None:
    contract = build_test_slice_contract(
        contract_id="exd.performance", scorecard_name="Delivery Readiness",
        title="Performance", section="performance", workstream="delivery",
    )
    entries = load_workstream_registry(
        program_id="sample", slice_contracts=(contract,), programs_root=tmp_path,
    )
    assert entries[0].id == "exd.performance"
    assert entries[0].sub_program_id is None
    registry_path = tmp_path / "sample" / "workstream_registry.yaml"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        "schema_version: '1.0'\nworkstreams:\n"
        "  - id: exd.performance\n    sub_program_id: authored-delivery\n",
        encoding="utf-8",
    )
    entries = load_workstream_registry(
        program_id="sample", slice_contracts=(contract,), programs_root=tmp_path,
    )
    assert entries[0].sub_program_id == "authored-delivery"


@pytest.mark.skipif(not PROGRAMS_ROOT.exists(), reason="Requires programs/ config data")
def test_slice_contract_ids_match_scorecard_dimension_bindings() -> None:
    failures: list[str] = []

    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--", "programs/"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    tracked_paths = {Path(path) for path in tracked}
    tracked_files = {REPO_ROOT / path for path in tracked_paths}
    tracked_programs = {
        REPO_ROOT / path.parent
        for path in tracked_paths
        if path.name == "program.yaml" and path.parts[0] == "programs"
    }

    for program_dir in sorted(tracked_programs):
        scorecards_path = program_dir / "scorecards.yaml"
        slice_contracts_path = program_dir / "slice_contracts.yaml"
        program_path = program_dir / "program.yaml"
        if scorecards_path not in tracked_files or slice_contracts_path not in tracked_files:
            continue

        scorecards_doc = yaml.safe_load(scorecards_path.read_text(encoding="utf-8")) or {}
        slice_contracts_doc = yaml.safe_load(slice_contracts_path.read_text(encoding="utf-8")) or {}
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8")) if program_path.exists() else {}

        raw_scorecards = scorecards_doc.get("scorecards") or []
        raw_slices = slice_contracts_doc.get("slices") or []
        if not isinstance(raw_scorecards, list) or not isinstance(raw_slices, list):
            failures.append(f"{program_dir.name}: scorecards.yaml or slice_contracts.yaml has an unexpected shape")
            continue

        chapter_namespace = str(program_doc.get("chapter_namespace") or program_dir.name).strip()
        expected_ids = {
            canonical_dimension_binding_id(
                str(scorecard.get("name") or ""),
                str(dimension.get("name") or ""),
                chapter_namespace=chapter_namespace,
            )
            for scorecard in raw_scorecards
            if isinstance(scorecard, dict)
            for dimension in (scorecard.get("dimensions") or [])
            if isinstance(dimension, dict)
        }
        actual_ids = {
            str(entry.get("id") or "").strip()
            for entry in raw_slices
            if isinstance(entry, dict)
        }

        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        if missing or extra:
            parts: list[str] = []
            if missing:
                parts.append(f"missing {missing}")
            if extra:
                parts.append(f"extra {extra}")
            failures.append(f"{program_dir.name}: " + "; ".join(parts))

    if not tracked_programs or not any(
        (program_dir / "scorecards.yaml") in tracked_files
        and (program_dir / "slice_contracts.yaml") in tracked_files
        for program_dir in tracked_programs
    ):
        pytest.skip("No tracked program scorecard/slice-contract pair is available")

    assert failures == [], "Slice contract ids must match canonical scorecard dimension ids:\n" + "\n".join(failures)
