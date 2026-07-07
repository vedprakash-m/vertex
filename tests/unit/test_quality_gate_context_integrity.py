"""Guards the D-09 / Phase 3 peel of the context-integrity gate cluster."""
from __future__ import annotations

from pathlib import Path

from src.core.quality_gates import evaluate_context_integrity_gates
from src.core.quality_gates.context_integrity import evaluate_context_integrity_gates as context_integrity_entry
from src.core.quality_gates.context_integrity import is_informal_odata_filter


def test_context_integrity_entry_point_is_reexported() -> None:
    assert evaluate_context_integrity_gates is context_integrity_entry


def test_is_informal_odata_filter_detects_prose_but_not_formal_odata() -> None:
    assert is_informal_odata_filter("team = safety") is True
    assert is_informal_odata_filter("contains(Tags, 'Safety') and State eq 'Active'") is False


def test_context_integrity_passes_when_milestones_file_is_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    report = context_integrity_entry(program_id="acme", programs_root=programs_root)

    assert report.qg_results["QG-CI-01"] is True
    result = next(item for item in report.results if item.gate_id == "QG-CI-01")
    assert "milestones.yaml not found" in result.message


def test_context_integrity_blocks_stub_work_item_ids(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "milestones.yaml").write_text(
        """
schema_version: "1.0"
milestones:
  - id: ms-1
    name: Stubbed milestone
    linked_work_item_ids: [900123]
""".strip(),
        encoding="utf-8",
    )

    report = context_integrity_entry(program_id="acme", programs_root=tmp_path / "programs")

    assert report.qg_results["QG-CI-01"] is False
    result = next(item for item in report.results if item.gate_id == "QG-CI-01")
    assert "900000–999999" in result.message


def test_context_integrity_passes_when_scorecards_file_is_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)

    report = context_integrity_entry(program_id="acme", programs_root=programs_root)

    assert report.qg_results["QG-CI-02"] is True
    result = next(item for item in report.results if item.gate_id == "QG-CI-02")
    assert "scorecards.yaml not found" in result.message


def test_context_integrity_blocks_informal_filters(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "scorecards.yaml").write_text(
        """
schema_version: "1.0"
scorecards:
  - name: Acme
    dimensions:
      - name: Safety
        ado_filter: team = safety
""".strip(),
        encoding="utf-8",
    )

    report = context_integrity_entry(program_id="acme", programs_root=tmp_path / "programs")

    assert report.qg_results["QG-CI-02"] is False
    result = next(item for item in report.results if item.gate_id == "QG-CI-02")
    assert "informal OData filter" in result.message
