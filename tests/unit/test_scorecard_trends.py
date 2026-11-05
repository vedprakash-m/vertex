from __future__ import annotations

import json
from itertools import product
from pathlib import Path

import pytest

from src.core.ado_narrative_hint_engine import HintKind, generate_delta_hints
from src.core.models import DeltaSet, RiskLevel
from src.core.models_v2 import Program
from src.core.scorecard_trends import load_scorecard_trends


def test_load_scorecard_trends_reports_consecutive_high_and_improvement(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / "demo_weekly"
    edition_root.mkdir(parents=True)
    (edition_root / "scorecards.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "issue_number": 1,
                        "scorecard_name": "Demo Scorecard",
                        "dimension": "Deployment Velocity",
                        "risk": "high",
                    },
                    {
                        "issue_number": 2,
                        "scorecard_name": "Demo Scorecard",
                        "dimension": "Deployment Velocity",
                        "risk": "high",
                    },
                    {
                        "issue_number": 3,
                        "scorecard_name": "Demo Scorecard",
                        "dimension": "Deployment Velocity",
                        "risk": "high",
                    },
                    {
                        "issue_number": 3,
                        "scorecard_name": "Demo Scorecard",
                        "dimension": "Deployment Safety",
                        "risk": "high",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    trends = load_scorecard_trends(
        "demo_weekly",
        {
            ("Demo Scorecard", "Deployment Velocity"): RiskLevel.HIGH,
            ("Demo Scorecard", "Deployment Safety"): RiskLevel.MEDIUM,
        },
        archive_root=archive_root,
    )

    velocity = trends[("Demo Scorecard", "Deployment Velocity")]
    safety = trends[("Demo Scorecard", "Deployment Safety")]

    assert velocity.consecutive_high_count == 4
    assert velocity.direction == "stable"
    assert velocity.annotation == "High for 4 consecutive issues."
    assert safety.direction == "improving"
    assert safety.annotation == "Improved from High to Medium."


@pytest.mark.parametrize(("prior", "current"), list(product(
    [None, RiskLevel.UNKNOWN, RiskLevel.BLOCKED, RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW, RiskLevel.DONE],
    [RiskLevel.UNKNOWN, RiskLevel.BLOCKED, RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW, RiskLevel.DONE],
)))
def test_missing_assessment_has_no_comparative_trend_or_narrative_hint(
    tmp_path: Path, prior: RiskLevel | None, current: RiskLevel,
) -> None:
    edition_root = tmp_path / "synthetic_weekly"
    edition_root.mkdir()
    entries = [] if prior is None else [{
        "issue_number": 1, "scorecard_name": "Synthetic", "dimension": "Coverage", "risk": prior.value,
    }]
    (edition_root / "scorecards.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    trends = load_scorecard_trends(
        "synthetic_weekly", {("Synthetic", "Coverage"): current}, archive_root=tmp_path,
    )
    trend = trends[("Synthetic", "Coverage")]
    ranks = {RiskLevel.DONE: 1, RiskLevel.LOW: 2, RiskLevel.MEDIUM: 3, RiskLevel.HIGH: 4, RiskLevel.BLOCKED: 5}
    missing = prior is None or RiskLevel.UNKNOWN in {prior, current}
    direction = (
        "unknown" if missing else
        "improving" if ranks[current] < ranks[prior] else
        "worsening" if ranks[current] > ranks[prior] else "stable"
    )
    assert trend.direction == direction
    assert trend.prior_risk == prior
    assert trend.current_risk == current
    assert trend.history == ((current,) if prior is None else (prior, current))
    if missing:
        assert trend.annotation is None
    hints = generate_delta_hints(
        delta_set=DeltaSet(
            issue_number=2, previous_issue_number=1, new_items=(), closed_items=(),
            risk_changes=(), eta_changes=(), owner_changes=(), unchanged_count=0,
        ),
        items={}, issue_number=2,
        program=Program(schema_version="2.0", id="synthetic", name="Synthetic"),
        trends=trends,
    )
    expected_kinds = (
        [HintKind.TREND_IMPROVING] if direction == "improving" else
        [HintKind.TREND_WORSENING] if direction == "worsening" else []
    )
    assert [hint.hint_kind for hint in hints] == expected_kinds