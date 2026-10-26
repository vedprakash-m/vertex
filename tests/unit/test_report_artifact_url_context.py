from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.models import EditionType


@pytest.mark.parametrize("surface", ["report", "confirm", "review"])
@pytest.mark.parametrize("edition_name", ["synthetic_weekly", "synthetic_monthly"])
def test_card_urls_preserve_edition_under_program_artifact_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str, edition_name: str,
) -> None:
    module = importlib.import_module({
        "report": "src.commands.report_pipeline.assemble_stage",
        "confirm": "src.commands.confirm_stages.weekly_summary_card",
        "review": "src.commands.review_full",
    }[surface])
    programs_root = tmp_path / "programs"
    program_output_root = programs_root / "synthetic" / "output"
    edition_output_root = program_output_root / edition_name
    html_path = edition_output_root / "issue_001" / "issue_001.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text("<p>Synthetic issue.</p>", encoding="utf-8")
    bundle = Mock()
    bundle.program.id = "synthetic"
    bundle.config.m365.artifact_base_url = "https://contoso.example/publications/"
    report = Mock(items=())

    def output_dir(edition: str, *, programs_root: Path) -> Path:
        assert edition == edition_name
        assert programs_root == tmp_path / "programs"
        return edition_output_root

    monkeypatch.setattr(module, "get_program_output_dir", output_dir)
    if surface == "report":
        writer = Mock(return_value=())
        monkeypatch.setattr(module, "_write_report_adaptive_cards_impl", writer)
        module._write_report_adaptive_cards(
            bundle=bundle, edition_name=edition_name, issue_number=1,
            edition_type=EditionType.DETAILED, report=report, programs_root=programs_root,
            report_html_path=html_path,
        )
        url = writer.call_args.kwargs["report_html_url"]
    else:
        renderer = Mock()
        renderer.render_weekly_summary.return_value = {"synthetic": True}
        renderer.render_section_review.return_value = {"synthetic": True}
        monkeypatch.setattr(module, "AdaptiveCardRenderer", lambda: renderer)
        if surface == "confirm":
            module.write_confirm_weekly_summary_card(
                bundle=bundle, edition_name=edition_name, issue_number=1, report=report,
                programs_root=programs_root, archive_paths=Mock(),
            )
            url = renderer.render_weekly_summary.call_args.kwargs["report_html_url"]
        else:
            module._write_section_review_adaptive_cards(
                bundle=bundle, edition_name=edition_name, issue_number=1,
                sections=(Mock(section_id="delivery"),), programs_root=programs_root,
                reviewer_html_path=html_path,
            )
            url = renderer.render_section_review.call_args.kwargs["review_html_url"]
    assert url == f"https://contoso.example/publications/{edition_name}/issue_001/issue_001.html"
