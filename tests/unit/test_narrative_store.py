from __future__ import annotations

import json
from pathlib import Path

from src.core.narrative_store import REMOVED_SECTION_MARKER, archive_narratives, build_workstream_narrative_history, load_narratives, merge_narratives
from src.core.narrative_store import reset_narratives_for_next_issue
from src.core.narrative_store import strip_scaffold_comments


EDITION_NAME = "acme_weekly"


def test_merge_narratives_preserves_existing_creates_new_and_marks_removed(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    active_dir = reports_root / EDITION_NAME / "narratives" / "issue_078"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "exec_summary.md").write_text("Author-edited summary.\n", encoding="utf-8")
    (active_dir / "ws_old.md").write_text("Retired section content.\n", encoding="utf-8")

    merged_dir = merge_narratives(
        edition=EDITION_NAME,
        issue_number=78,
        templates={
            "exec_summary.md": "Template summary.",
            "ws_deployment_velocity.md": "Deployment velocity placeholder.",
        },
        reports_root=reports_root,
    )
    narratives = load_narratives(EDITION_NAME, 78, reports_root)

    assert merged_dir == active_dir
    assert narratives["exec_summary.md"] == "Author-edited summary.\n"
    assert narratives["ws_deployment_velocity.md"] == "Deployment velocity placeholder.\n"
    assert narratives["ws_old.md"].startswith(f"{REMOVED_SECTION_MARKER}\n")


def test_merge_narratives_does_not_duplicate_removed_marker_for_seeded_removed_section(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    active_dir = reports_root / EDITION_NAME / "narratives" / "issue_078"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "chapter_old.md").write_text(
        "\n".join(
            (
                "<!-- SEEDED from Issue 077 — published EML baseline, review and update with current evidence -->",
                "",
                REMOVED_SECTION_MARKER,
                "",
                "Retired section content.",
                "",
            )
        ),
        encoding="utf-8",
    )

    merge_narratives(
        edition=EDITION_NAME,
        issue_number=78,
        templates={"exec_summary.md": "Template summary."},
        reports_root=reports_root,
    )

    content = (active_dir / "chapter_old.md").read_text(encoding="utf-8")

    assert content.count("REMOVED") == 1


def test_archive_and_reset_narratives(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    merge_narratives(
        edition=EDITION_NAME,
        issue_number=78,
        templates={"exec_summary.md": "Summary body."},
        reports_root=reports_root,
    )

    archived_dir = archive_narratives(
        edition=EDITION_NAME,
        issue_number=78,
        archive_root=archive_root,
        reports_root=reports_root,
    )
    next_dir = reset_narratives_for_next_issue(
        edition=EDITION_NAME,
        next_issue_number=79,
        templates={"exec_summary.md": "Carry-forward summary."},
        reports_root=reports_root,
    )

    assert archived_dir == archive_root / EDITION_NAME / "narratives" / "issue_078"
    assert (archived_dir / "exec_summary.md").read_text(encoding="utf-8") == "Summary body.\n"
    assert (next_dir / "exec_summary.md").read_text(encoding="utf-8") == "Carry-forward summary.\n"


def test_build_workstream_narrative_history_reads_current_and_last_two_confirmed(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    (edition_root / "narratives" / "issue_001").mkdir(parents=True, exist_ok=True)
    (edition_root / "narratives" / "issue_002").mkdir(parents=True, exist_ok=True)
    (edition_root / "narratives" / "issue_001" / "ws_acme.md").write_text("No material change.\n", encoding="utf-8")
    (edition_root / "narratives" / "issue_002" / "ws_acme.md").write_text("No material change.\n", encoding="utf-8")
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {"issue_number": 1, "generated_at": "2026-04-15T12:00:00+00:00", "kind": "confirmed"},
                    {"issue_number": 2, "generated_at": "2026-04-22T12:00:00+00:00", "kind": "confirmed"},
                ],
            }
        ),
        encoding="utf-8",
    )

    history = build_workstream_narrative_history(
        edition=EDITION_NAME,
        issue_number=3,
        workstream_names=("Acme",),
        current_workstream_blurbs={"acme": "No material change."},
        archive_root=archive_root,
    )

    assert history == {"Acme": ("No material change.", "No material change.", "No material change.")}


def test_strip_scaffold_comments_preserves_state_separator_with_authored_exec_summary() -> None:
    text = "\n".join(
        [
            "<!-- vertex:scaffold Issue 78 — Executive Summary -->",
            "What changed paragraph.",
            "",
            "<!-- state -->",
            "",
            "Where we are paragraph.",
            "",
        ]
    )

    assert strip_scaffold_comments(text) == "What changed paragraph.\n\n<!-- state -->\n\nWhere we are paragraph.\n"


def test_strip_scaffold_comments_returns_empty_for_scaffold_only_exec_summary() -> None:
    text = "\n".join(
        [
            "<!-- vertex:scaffold Issue 78 — Executive Summary -->",
            "[WHAT MOVED paragraph]",
            "",
            "<!-- state -->",
            "",
            "[WHERE WE ARE paragraph]",
            "",
        ]
    )

    assert strip_scaffold_comments(text) == ""


def test_strip_scaffold_comments_does_not_strip_legacy_khabari_scaffold_markers() -> None:
    text = "\n".join(
        [
            "<!-- khabari:scaffold Issue 78 — Executive Summary -->",
            "What changed paragraph.",
            "",
        ]
    )

    # khabari scaffold markers are no longer recognised after rebrand to vertex
    assert strip_scaffold_comments(text) == text


def test_inject_narrative_placeholders_metric_and_decision(mocker) -> None:
    mock_resolve = mocker.patch("src.core.edition_resolver.resolve_edition_paths")
    from src.core.edition_resolver import ResolvedEditionPaths
    mock_resolve.return_value = ResolvedEditionPaths(
        edition_id="acme_weekly",
        edition_path=Path("dummy_edition.yaml"),
        program_id="acme",
        program_dir=Path("dummy_program_dir"),
        knowledge_dir=Path("dummy_knowledge"),
        archive_dir=Path("dummy_archive"),
        publications_dir=Path("dummy_output"),
    )

    mock_overrides = mocker.patch("src.core.overrides_store.load_overrides")
    from src.core.overrides_store import OverridesDocument, DecisionRecord
    from datetime import date
    mock_overrides.return_value = OverridesDocument(
        issue_number=78,
        top_3_now=(),
        scorecards=(),
        decisions=(
            DecisionRecord(
                id="dec-1",
                workstream="velocity",
                type="gate",
                statement="Production gate approved for UD deployment.",
                source_type="manual",
                source_ref="manual",
                owner="alias",
                status="active",
                effective_date=date(2026, 5, 1),
            ),
        )
    )

    mock_store_cls = mocker.patch("src.core.reality_store.RealityStore")
    mock_store = mock_store_cls.return_value
    
    from src.core.metric_models import MetricObservation, MetricQualityState
    from datetime import datetime, timezone
    mock_store.list_metric_observations.return_value = (
        MetricObservation(
            observation_id="obs-1",
            program_id="acme",
            metric_id="acme.deployment_p50_mins",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 2, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
            value_num=47.3,
            value_text=None,
            sample_count=100,
            quality_state=MetricQualityState.OK,
        ),
    )

    mock_metrics = mocker.patch("src.core.metric_registry.load_metric_definition_map")
    from src.core.metric_models import MetricDefinition, MetricAggregation
    mock_metrics.return_value = {
        "acme.deployment_p50_mins": MetricDefinition(
            id="acme.deployment_p50_mins",
            title="Deployment P50 Velocity",
            unit="mins",
            aggregation=MetricAggregation.LAST,
        )
    }

    from src.core.narrative_store import inject_narrative_placeholders
    raw_narratives = {
        "ws_velocity.md": "P50 velocity was <!-- vertex:metric: acme.deployment_p50_mins -->. Decision: <!-- vertex:decision: dec-1 -->."
    }

    injected = inject_narrative_placeholders("acme_weekly", 78, raw_narratives)

    assert injected["ws_velocity.md"] == "P50 velocity was 47.3 min. Decision: Production gate approved for UD deployment.."


def _mock_inject_environment(mocker, *, decisions=()):
    """Shared mock setup for inject_narrative_placeholders tests (resolve + overrides + stores)."""
    mock_resolve = mocker.patch("src.core.edition_resolver.resolve_edition_paths")
    from src.core.edition_resolver import ResolvedEditionPaths

    mock_resolve.return_value = ResolvedEditionPaths(
        edition_id="acme_weekly",
        edition_path=Path("dummy_edition.yaml"),
        program_id="acme",
        program_dir=Path("dummy_program_dir"),
        knowledge_dir=Path("dummy_knowledge"),
        archive_dir=Path("dummy_archive"),
        publications_dir=Path("dummy_output"),
    )

    mock_overrides = mocker.patch("src.core.overrides_store.load_overrides")
    from src.core.overrides_store import OverridesDocument

    mock_overrides.return_value = OverridesDocument(
        issue_number=78,
        top_3_now=(),
        scorecards=(),
        decisions=tuple(decisions),
    )

    mock_store_cls = mocker.patch("src.core.reality_store.RealityStore")
    mock_store_cls.return_value.list_metric_observations.return_value = ()
    mocker.patch("src.core.metric_registry.load_metric_definition_map", return_value={})


def test_inject_narrative_placeholders_hint_accepted_and_modified_and_missing(mocker) -> None:
    _mock_inject_environment(mocker)
    from src.core.section_proposal_store import HintProposal

    mocker.patch(
        "src.core.section_proposal_store.load_hint_proposals",
        return_value=(
            HintProposal(
                hint_id="hint-accept",
                edition="acme_weekly",
                issue_number=78,
                workstream_id="velocity",
                hint_kind="CLOSED",
                suggested_sentence="Blocker 123 was closed this week.",
                status="accepted",
                accepted_text="Blocker 123 was closed this week.",
            ),
            HintProposal(
                hint_id="hint-mod",
                edition="acme_weekly",
                issue_number=78,
                workstream_id="velocity",
                hint_kind="RISK_UP",
                suggested_sentence="Risk increased.",
                status="modified",
                accepted_text="Risk increased materially after the regression.",
            ),
            HintProposal(
                hint_id="hint-reject",
                edition="acme_weekly",
                issue_number=78,
                workstream_id="velocity",
                hint_kind="NEW",
                suggested_sentence="A new item appeared.",
                status="rejected",
                accepted_text=None,
            ),
        ),
    )

    from src.core.narrative_store import inject_narrative_placeholders

    raw_narratives = {
        "ws_velocity.md": (
            "A: <!-- vertex:hint: hint-accept -->\n"
            "M: <!-- vertex:hint: hint-mod -->\n"
            "R: <!-- vertex:hint: hint-reject -->\n"
            "X: <!-- vertex:hint: hint-unknown -->"
        )
    }

    injected = inject_narrative_placeholders("acme_weekly", 78, raw_narratives)

    assert injected["ws_velocity.md"] == (
        "A: Blocker 123 was closed this week.\n"
        "M: Risk increased materially after the regression.\n"
        "R: [hint pending]\n"
        "X: [hint pending]"
    )


def test_inject_narrative_placeholders_ws_lead_from_workstream_narrative(mocker) -> None:
    _mock_inject_environment(mocker)
    mocker.patch("src.core.section_proposal_store.load_hint_proposals", return_value=())

    from src.core.narrative_store import inject_narrative_placeholders

    raw_narratives = {
        "exec_summary.md": "Velocity: <!-- vertex:ws-lead: velocity -->\nUnknown: <!-- vertex:ws-lead: missing -->",
        "ws_velocity.md": "Deployment velocity improved sharply this week. Secondary detail follows.",
    }

    injected = inject_narrative_placeholders("acme_weekly", 78, raw_narratives)

    assert injected["exec_summary.md"] == (
        "Velocity: Deployment velocity improved sharply this week.\n"
        "Unknown: [lead pending]"
    )


