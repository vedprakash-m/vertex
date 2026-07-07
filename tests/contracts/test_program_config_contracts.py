from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.chapter_contract_loader import canonical_dimension_binding_id


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"


@pytest.mark.skipif(not PROGRAMS_ROOT.exists(), reason="Requires programs/ config data")
def test_slice_contract_ids_match_scorecard_dimension_bindings() -> None:
    failures: list[str] = []

    for program_dir in sorted(path for path in PROGRAMS_ROOT.iterdir() if path.is_dir()):
        scorecards_path = program_dir / "scorecards.yaml"
        slice_contracts_path = program_dir / "slice_contracts.yaml"
        program_path = program_dir / "program.yaml"
        if not scorecards_path.exists() or not slice_contracts_path.exists():
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

    assert failures == [], "Slice contract ids must match canonical scorecard dimension ids:\n" + "\n".join(failures)
