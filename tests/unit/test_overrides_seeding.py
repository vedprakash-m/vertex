from __future__ import annotations

import json
from datetime import datetime, timezone

from src.core.overrides_store import archive_overrides
from src.core.overrides_store import DimensionOverride, OverridesDocument, RemovedDimension, ScorecardOverrides, Top3NowEntry
from src.core.overrides_store import load_overrides, save_overrides
from src.core.pipeline import StageContext
from src.core.snapshot_store import get_archive_root
from src.core.stages.resolution_stage import ResolutionStage
from src.core.trusted_baseline_store import advance_trusted_baseline
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace


EDITION_NAME = "acme_weekly"


def test_resolution_stage_seeds_overrides_from_trusted_prior_issue(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    save_overrides(
        EDITION_NAME,
        OverridesDocument(
            issue_number=1,
            top_3_now=(
                Top3NowEntry(
                    type="ask",
                    text="Escalate deployment safety review",
                    owner="Operator",
                    ado_link="https://example.invalid/wi/1",
                    anchor="deployment-safety",
                ),
            ),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Readiness",
                    dimensions=(
                        DimensionOverride(
                            name="Deployment Velocity",
                            risk=None,
                            note="Carry forward",
                        ),
                    ),
                ),
            ),
            edition_intro="Prior intro",
            chapter_subtitles={"deployment": "Prior subtitle"},
            chapter_owner_overrides={"deployment": "Owner"},
            forwarding_context="Prior forwarding",
            health_bluf="Prior bluf",
            leadership_ask="Prior ask",
            show_orientation=True,
            removed_dimensions=(
                RemovedDimension(
                    scorecard_name="Acme Readiness",
                    dimension_name="Legacy Dimension",
                ),
            ),
            removed_sections=("legacy-section",),
        ),
        reports_root=reports_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    ctx = ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            issue_number=2,
            as_of=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )
    )

    seeded = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=2)

    assert ctx.trusted_baseline_issue_number == 1
    assert ctx.overrides_seeding is not None and ctx.overrides_seeding.seeded is True
    assert seeded is not None
    assert seeded.top_3_now == ()
    assert seeded.scorecards[0].dimensions[0].note == "Carry forward"
    assert seeded.edition_intro == "Prior intro\n\n<!-- STALE — review -->"
    assert seeded.forwarding_context == "Prior forwarding\n\n<!-- STALE — review -->"
    assert seeded.chapter_subtitles == {"deployment": "Prior subtitle"}
    assert seeded.chapter_owner_overrides == {"deployment": "Owner"}
    assert seeded.health_bluf is None
    assert seeded.leadership_ask is None
    assert seeded.show_orientation is False
    assert seeded.removed_dimensions[0].dimension_name == "Legacy Dimension"
    assert seeded.removed_sections == ("legacy-section",)


def test_resolution_stage_reseed_replaces_seed_like_overrides(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    save_overrides(
        EDITION_NAME,
        OverridesDocument(
            issue_number=1,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Readiness",
                    dimensions=(
                        DimensionOverride(
                            name="Deployment Velocity",
                            risk=None,
                            note="Carry forward",
                        ),
                    ),
                ),
            ),
        ),
        reports_root=reports_root,
    )
    save_overrides(
        EDITION_NAME,
        OverridesDocument(
            issue_number=2,
            top_3_now=(),
            scorecards=(),
        ),
        reports_root=reports_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            issue_number=2,
            reseed=True,
            dry_run=True,
            as_of=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )
    )

    seeded = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=2)

    assert seeded is not None
    assert seeded.scorecards[0].dimensions[0].note == "Carry forward"


def test_resolution_stage_prefers_program_local_over_archived_overrides_for_seed_source(repo_root, tmp_path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    save_overrides(
        EDITION_NAME,
        OverridesDocument(
            issue_number=1,
            top_3_now=(
                Top3NowEntry(
                    type="risk",
                    text="Use local published state",
                    owner="Operator",
                    ado_link="https://example.invalid/wi/local",
                    anchor="legacy-section",
                ),
            ),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Readiness",
                    dimensions=(
                        DimensionOverride(
                            name="Deployment Velocity",
                            risk=None,
                            note="Local note",
                        ),
                    ),
                ),
            ),
            removed_sections=("legacy-section",),
        ),
        reports_root=reports_root,
    )
    archive_overrides(
        EDITION_NAME,
        1,
        OverridesDocument(
            issue_number=1,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Readiness",
                    dimensions=(
                        DimensionOverride(
                            name="Deployment Velocity",
                            risk=None,
                            note="Archived note",
                        ),
                    ),
                ),
            ),
            removed_sections=(),
        ),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            issue_number=2,
            as_of=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )
    )

    seeded = load_overrides(EDITION_NAME, reports_root=reports_root, issue_number=2)

    assert seeded is not None
    assert seeded.scorecards[0].dimensions[0].note == "Local note"
    assert seeded.removed_sections == ("legacy-section",)


def test_resolution_stage_archive_fallback_for_non_weekly_editions(repo_root, tmp_path) -> None:
    """When the program-level trusted_baseline has a weekly issue number >> edition issue
    number (e.g. baseline=50 but quarterly issue=3), load_trusted_baseline_issue returns
    None.  ResolutionStage must fall back to the edition's archive to derive the prior
    issue for seeding, so issue_003.yaml is created seeded from issue_002 overrides."""
    reports_root = stage_v2_report_workspace(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_quarterly"),
    )
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    # Advance the program-level trusted_baseline to a high weekly issue number.
    # For quarterly issue_003 this means baseline (50) >= before_issue_number (3),
    # which triggers the None-return bug in load_trusted_baseline_issue.
    advance_trusted_baseline(
        "acme_weekly",
        50,
        established_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        established_by="test",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
    )

    # Create quarterly overrides for issue_002 (the seed source).
    save_overrides(
        "nova_quarterly",
        OverridesDocument(
            issue_number=2,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Adventure/XIO 100% Ramp Readiness",
                    dimensions=(
                        DimensionOverride(
                            name="Deployment Velocity",
                            risk=None,
                            note="Q2 carry forward",
                        ),
                    ),
                ),
            ),
        ),
        reports_root=reports_root,
    )

    # Create a fake quarterly archive index with issues 1 and 2 confirmed.
    # get_archive_root resolves through nova_quarterly.yaml → programs/acme/archive/nova_quarterly
    # so we must use it to get the real path rather than archive_root / "nova_quarterly".
    quarterly_archive_dir = get_archive_root("nova_quarterly", archive_root=archive_root)
    quarterly_archive_dir.mkdir(parents=True, exist_ok=True)
    (quarterly_archive_dir / "index.json").write_text(
        json.dumps({
            "edition": "nova_quarterly",
            "issues": [
                {
                    "issue_number": 1,
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "kind": "confirmed",
                },
                {
                    "issue_number": 2,
                    "generated_at": "2026-04-01T00:00:00+00:00",
                    "kind": "confirmed",
                },
            ],
        }),
        encoding="utf-8",
    )

    ctx = ResolutionStage().execute(
        StageContext(
            edition_name="nova_quarterly",
            issue_number=3,
            as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )
    )

    seeded = load_overrides("nova_quarterly", reports_root=reports_root, issue_number=3)

    # Archive fallback: trusted_baseline_issue_number should resolve to 2 (not None).
    assert ctx.trusted_baseline_issue_number == 2
    # Overrides for issue_003 must have been seeded from issue_002.
    assert ctx.overrides_seeding is not None and ctx.overrides_seeding.seeded is True
    assert seeded is not None
    assert seeded.scorecards[0].dimensions[0].note == "Q2 carry forward"

