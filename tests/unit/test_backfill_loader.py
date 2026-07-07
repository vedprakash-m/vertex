from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.backfill_loader import load_backfill_config_for_edition, load_backfill_plan_for_edition


def test_load_backfill_plan_and_config_support_expected_shapes() -> None:
    with TemporaryDirectory() as temp_dir:
        repo_root = Path(temp_dir)
        editions_root = repo_root / "editions"
        program_root = repo_root / "programs" / "demo"
        editions_root.mkdir(parents=True)
        program_root.mkdir(parents=True)
        (editions_root / "demo_weekly.yaml").write_text(
            """
id: demo_weekly
program_id: demo
name: Demo Weekly
type: detailed
altitude: helicopter
cadence: weekly
""".strip(),
            encoding="utf-8",
        )
        (program_root / "backfill.yaml").write_text(
            """
sources:
  - kind: prior_emails
    glob: "backfill/emails/*.html"
  - kind: reviews
    glob: "backfill/reviews/*.eml"
extract:
  workstream_blurbs: true
  scorecard_dimensions: false
output: "output/demo/backfill"
""".strip(),
            encoding="utf-8",
        )
        (program_root / "backfill_config.yaml").write_text(
            """
newsletters:
  search_strategy: "m365"
  directions:
    - source: email
      filter: "to:acme_newsletter@example.com"
      date_range: "last 12 months"
      description: "Past issues"
feedback:
  directions:
    - question: "Find review feedback threads"
meetings:
  - source: meeting_transcript
    filter: "title contains 'Acme Weekly'"
people_intelligence:
  - question: "Find communication style patterns"
""".strip(),
            encoding="utf-8",
        )

        plan = load_backfill_plan_for_edition("demo_weekly", repo_root=repo_root)
        config = load_backfill_config_for_edition("demo_weekly", repo_root=repo_root)

    assert plan is not None
    assert plan.sources[0].kind == "prior_emails"
    assert plan.extract.workstream_blurbs is True
    assert plan.extract.scorecard_dimensions is False
    assert plan.output == "output/demo/backfill"
    assert config is not None
    assert config.newsletters.search_strategy == "m365"
    assert config.newsletters.directions[0].filter == "to:acme_newsletter@example.com"
    assert config.feedback.directions[0].question == "Find review feedback threads"
    assert config.meetings.directions[0].source == "meeting_transcript"
    assert config.people_intelligence.directions[0].question == "Find communication style patterns"