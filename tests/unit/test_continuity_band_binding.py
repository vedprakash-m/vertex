from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.commands.report import generate_report_draft
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_report import _sample_items
from tests.support.synthetic_report_source import synthetic_report_root as repo_root


@pytest.mark.parametrize("band_count", [0, 1, 2])
@pytest.mark.parametrize("tool_attribution", [False, True])
def test_actual_report_continuity_gates_use_rendered_band_ids(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    band_count: int,
    tool_attribution: bool,
) -> None:
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "fact-db"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    edition_path = programs_root / "acme" / "editions" / "acme_weekly.yaml"
    edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    edition["cadence_note"]["detailed"] = "Prepared with Vertex" if tool_attribution else "Weekly update"
    edition_path.write_text(yaml.safe_dump(edition), encoding="utf-8")
    scorecards_path = programs_root / "acme" / "scorecards.yaml"
    scorecards = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
    scorecards["scorecards"] = scorecards["scorecards"][:band_count]
    scorecards_path.write_text(yaml.safe_dump(scorecards), encoding="utf-8")
    slice_path = programs_root / "acme" / "slice_contracts.yaml"
    slices = yaml.safe_load(slice_path.read_text(encoding="utf-8"))
    selected_names = {scorecard["name"] for scorecard in scorecards["scorecards"]}
    slices["slices"] = [contract for contract in slices["slices"] if contract["scorecard_name"] in selected_names]
    slice_path.write_text(yaml.safe_dump(slices), encoding="utf-8")
    chapter_path = programs_root / "acme" / "chapter_contract.yaml"
    chapters = yaml.safe_load(chapter_path.read_text(encoding="utf-8"))
    selected_dimensions = {contract["id"] for contract in slices["slices"]}
    for chapter in chapters["chapters"]:
        chapter["dimensions"] = [dimension for dimension in chapter["dimensions"] if dimension in selected_dimensions]
    chapter_path.write_text(yaml.safe_dump(chapters), encoding="utf-8")

    result = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
        as_of=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert result.manifest.qg_results["CG-05"] is True
    assert result.manifest.qg_results["CG-06"] is True
    assert result.manifest.qg_results["CG-07"] is (not tool_attribution)
