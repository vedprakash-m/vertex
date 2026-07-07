from __future__ import annotations

import json
from pathlib import Path

from src.core.models import RiskLevel
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