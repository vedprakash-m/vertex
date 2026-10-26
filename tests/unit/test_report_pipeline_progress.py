from types import SimpleNamespace

import pytest

from src.commands import report
from src.core.models import EditionType
from src.core.pipeline import StageContext


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("resolution", "issue=003, type=detailed"),
        ("fetch", "items=0, ado_calls=2, synthetic"),
        ("compute", "items=0"),
        ("milestone", "warnings=0"),
        ("risk", "stale=0, warnings=0"),
        ("action", "overdue=0"),
        ("narrative", "top_items=0"),
        ("ai", "offline"),
        ("render", ""),
        ("validation", ""),
        ("output", ""),
        ("lookback", ""),
        ("unknown", ""),
    ],
)
def test_progress_detail_preserves_stage_output(stage: str, expected: str) -> None:
    ctx = StageContext(
        resolved_issue_number=3,
        resolved_edition_type=EditionType.DETAILED,
        ado_calls=2,
        offline_source_label="synthetic",
        offline=True,
    )
    assert report._format_report_pipeline_progress_detail(stage, ctx) == expected


def test_progress_detail_uses_rebound_facade_length(monkeypatch) -> None:
    monkeypatch.setattr(report, "_safe_length", lambda value: 17)
    assert report._format_report_pipeline_progress_detail("compute", StageContext()) == "items=0, scorecards=17"


def test_progress_callback_uses_rebound_facade_formatter(monkeypatch, capsys) -> None:
    monkeypatch.setattr(report, "_format_report_pipeline_progress_detail", lambda stage, ctx: "synthetic detail")
    records = []
    logger = SimpleNamespace(info=lambda message, *, extra: records.append((message, extra)))
    callback = report._build_report_pipeline_progress_callback(show_progress=True, trace_logger=logger)
    assert callback is not None
    callback(SimpleNamespace(name=lambda: "compute"), 2, 3, StageContext(), StageContext(), 0.25)
    assert capsys.readouterr().out == "[2/3] compute    0.25s | synthetic detail\n"
    assert records[0][1]["detail"] == "synthetic detail"


@pytest.mark.parametrize("value, expected", [(None, None), (object(), None), ((), 0), (("one",), 1)])
def test_progress_safe_length(value: object, expected: int | None) -> None:
    assert report._safe_length(value) == expected
