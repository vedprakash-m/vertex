from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import pytest

from src.commands.setup_preview import (
    PreviewDimension,
    PreviewItem,
    PreviewScorecard,
    PreviewWorkstream,
    render_preview_html,
)


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


_UNTRUSTED = (
    "Synthetic <svg onload=alert(1)> & \"quotes\" ' "
    "\u4e2d\u6587 \u0645\u0631\u062d\u0628\u0627 \U0001f680"
)


@pytest.mark.parametrize(
    "field",
    (
        "program_name",
        "edition_slug",
        "workstream_name",
        "item_title",
        "risk_label",
        "owner",
        "eta",
        "scorecard_name",
        "dimension_name",
        "dimension_score",
        "dimension_detail",
    ),
)
@pytest.mark.parametrize(
    "value",
    (
        _UNTRUSTED,
        "Synthetic </title><script>alert(1)</script> &amp; text",
    ),
)
def test_preview_preserves_untrusted_fields_as_text(field: str, value: str) -> None:
    program_name = value if field == "program_name" else "Synthetic program"
    edition_slug = value if field == "edition_slug" else "synthetic_weekly"
    item = PreviewItem("Synthetic item", "green", "On Track", "Owner", "2026-06-01")
    item_fields = {
        "item_title": "title",
        "risk_label": "risk_label",
        "owner": "owner",
        "eta": "eta",
    }
    if field in item_fields:
        item = replace(item, **{item_fields[field]: value})
    workstream = PreviewWorkstream(
        value if field == "workstream_name" else "Synthetic workstream",
        (item,),
    )
    dimension = PreviewDimension("Synthetic dimension", "On Track", "Synthetic detail")
    dimension_fields = {
        "dimension_name": "name",
        "dimension_score": "score",
        "dimension_detail": "detail",
    }
    if field in dimension_fields:
        dimension = replace(dimension, **{dimension_fields[field]: value})
    scorecard = PreviewScorecard(
        value if field == "scorecard_name" else "Synthetic scorecard",
        (dimension,),
    )

    document = _Document()
    document.feed(render_preview_html(program_name, edition_slug, [workstream], [scorecard]))

    assert "svg" not in document.tags
    assert "script" not in document.tags
    assert value in "".join(document.text)


def test_preview_preserves_empty_demo_structure() -> None:
    html = render_preview_html("Synthetic", "synthetic", [], [], demo=True)
    document = _Document()
    document.feed(html)

    assert 'data-demo="true"' in html
    assert "Preview \u2014 sample data only" in "".join(document.text)
    assert "0 on track, 0 at risk, 0 blocked" in "".join(document.text)
    assert document.tags.count("table") == 0
    assert all(line == line.rstrip() for line in html.splitlines())


def test_setup_preview_golden_fixture_uses_synthetic_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.golden.test_setup_preview import _synthetic_preview_data

    workstreams, scorecards = _synthetic_preview_data(monkeypatch)
    owners = {item.owner for workstream in workstreams for item in workstream.items}

    assert owners <= {
        "Synthetic owner A",
        "Synthetic owner B",
        "Synthetic owner C",
        "Synthetic owner D",
        "Synthetic owner E",
    }
    assert sum(len(workstream.items) for workstream in workstreams) == 12
    assert sum(len(scorecard.dimensions) for scorecard in scorecards) == 3


def test_setup_writes_literal_program_and_workstream_names_without_opening_browser(tmp_path: Path) -> None:
    from src.commands.onboard import IdentityStage
    from src.commands.setup import _run_preview
    from src.core.setup_state import SetupDraft

    draft = SetupDraft(
        identity=IdentityStage(
            program_name=_UNTRUSTED,
            program_id="synthetic",
            objective="Synthetic objective",
            mission="Synthetic mission",
            newsletter_title="Synthetic weekly",
            cadence="weekly",
            author_display_name="Synthetic author",
            author_email="author@example.com",
            send_day=None,
            send_time_local=None,
            timezone=None,
        )
    )

    with patch("webbrowser.open") as open_browser:
        _run_preview(draft, [(_UNTRUSTED, "Synthetic description")], output_dir=tmp_path, no_open=True)

    document = _Document()
    document.feed((tmp_path / "setup_preview.html").read_text(encoding="utf-8"))
    assert "svg" not in document.tags
    assert "".join(document.text).count(_UNTRUSTED) >= 3
    open_browser.assert_not_called()
