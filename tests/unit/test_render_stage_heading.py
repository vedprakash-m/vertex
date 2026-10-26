from __future__ import annotations

from src.core.models import RiskLevel
from src.core.stages.render_stage import _split_heading_risk, _split_leading_heading


def test_split_leading_heading_extracts_title() -> None:
    assert _split_leading_heading("# FW Management on EXN\nBody text") == ("FW Management on EXN", "Body text")


def test_split_leading_heading_without_heading_returns_body() -> None:
    title, body = _split_leading_heading("Body text\n# not a title")

    assert title is None
    assert body.startswith("Body text")


def test_split_heading_risk_extracts_trailing_marker() -> None:
    assert _split_heading_risk("Diagnostics Surface | Pilot+ [Medium]") == ("Diagnostics Surface | Pilot+", RiskLevel.MEDIUM)


def test_split_heading_risk_without_marker_keeps_title() -> None:
    assert _split_heading_risk("FW Management on EXN") == ("FW Management on EXN", None)
    assert _split_heading_risk(None) == (None, None)
