from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.narrative_store import REMOVED_SECTION_MARKER, archive_narratives, build_workstream_narrative_history, load_narratives, merge_narratives
from src.core.narrative_store import reset_narratives_for_next_issue
from src.core.narrative_store import strip_scaffold_comments


EDITION_NAME = "acme_weekly"


def _selected_artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "custom-programs"
    editions = root / "fixture" / "editions"
    editions.mkdir(parents=True)
    (editions / "fixture_weekly.yaml").write_text("program_id: fixture\n", encoding="utf-8")
    return root


def test_explicit_narrative_root_write_load_reset_and_delete(tmp_path: Path) -> None:
    from src.core.narrative_store import write_narrative_section, delete_narratives_dir

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "elsewhere" / "reports"
    decoy = reports / "fixture_weekly" / "narratives" / "issue_007"
    decoy.mkdir(parents=True)
    (decoy / "exec_summary.md").write_text("decoy", encoding="utf-8")
    path = write_narrative_section("fixture_weekly", 7, "exec_summary", "selected", reports, programs_root=root)
    assert path == root / "fixture" / "narratives" / "issue_007" / "exec_summary.md"
    assert load_narratives("fixture_weekly", 7, reports, programs_root=root)["exec_summary.md"] == "selected\n"
    reset_narratives_for_next_issue("fixture_weekly", 8, {"exec_summary.md": "next"}, reports, programs_root=root)
    delete_narratives_dir("fixture_weekly", 8, reports, programs_root=root)
    assert not (root / "fixture" / "narratives" / "issue_008").exists()
    assert (decoy / "exec_summary.md").read_text(encoding="utf-8") == "decoy"


@pytest.mark.parametrize("operation", ["merge", "write", "delete", "archive", "load"])
def test_explicit_missing_narrative_context_never_falls_back(tmp_path: Path, operation: str) -> None:
    from src.core import narrative_store as store
    from src.core.exceptions import ConfigError

    root = tmp_path / "custom-programs"
    root.mkdir()
    reports = tmp_path / "reports"
    before = list(tmp_path.rglob("*"))
    calls = {
        "merge": lambda: store.merge_narratives("missing", 7, {}, reports, programs_root=root),
        "write": lambda: store.write_narrative_section("missing", 7, "exec_summary", "text", reports, programs_root=root),
        "delete": lambda: store.delete_narratives_dir("missing", 7, reports, programs_root=root),
        "archive": lambda: store.archive_narratives("missing", 7, tmp_path / "archive", reports, programs_root=root),
        "load": lambda: store.load_narratives("missing", 7, reports, programs_root=root),
    }
    with pytest.raises(ConfigError):
        calls[operation]()
    assert list(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("kind", ["missing", "relative", "file", "flat", "ambiguous", "malformed", "denied"])
@pytest.mark.parametrize("surface", ["narrative", "override", "review", "sync", "history", "pending"])
def test_explicit_artifact_context_rejects_invalid_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, surface: str,
) -> None:
    from src.core import narrative_store, overrides_store, review_status_store
    from src.core.exceptions import ConfigError

    root = _selected_artifact_root(tmp_path)
    reports = tmp_path / "unrelated" / "reports"
    decoy = reports / "fixture_weekly"
    decoy.mkdir(parents=True)
    (decoy / "overrides.yaml").write_text("decoy", encoding="utf-8")
    edition = root / "fixture" / "editions" / "fixture_weekly.yaml"
    if kind == "missing":
        root = tmp_path / "absent"
    elif kind == "relative":
        root = Path("relative-programs")
    elif kind == "file":
        root = edition
    elif kind == "flat":
        edition.unlink()
        flat = root.parent / "editions"
        flat.mkdir()
        (flat / edition.name).write_text("program_id: fixture\n", encoding="utf-8")
    elif kind == "ambiguous":
        other = root / "other" / "editions"
        other.mkdir(parents=True)
        (other / edition.name).write_text("program_id: other\n", encoding="utf-8")
    elif kind == "malformed":
        edition.write_text("program_id: ../escape\n", encoding="utf-8")
    else:
        original = Path.glob

        def denied(path: Path, pattern: str, **kwargs):
            if path == root:
                raise PermissionError("synthetic inaccessible root")
            return original(path, pattern, **kwargs)

        monkeypatch.setattr(Path, "glob", denied)
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    with pytest.raises(ConfigError):
        if surface == "narrative":
            narrative_store.merge_narratives("fixture_weekly", 7, {}, reports, programs_root=root)
        elif surface == "override":
            overrides_store.reset_overrides_for_next_issue("fixture_weekly", 7, (), reports, programs_root=root)
        elif surface == "review":
            review_status_store.reset_review_status("fixture_weekly", 7, (), reports, programs_root=root)
        elif surface == "sync":
            narrative_store.sync_published_baseline_to_target(
                "fixture_weekly", target_issue_number=7, source_issue_number=6,
                reports_root=reports, programs_root=root,
            )
        elif surface == "history":
            narrative_store.build_workstream_narrative_history(
                "fixture_weekly", 7, (), {}, reports_root=reports, programs_root=root,
            )
        else:
            overrides_store.apply_pending_overrides("fixture_weekly", tmp_path / "absent", reports, programs_root=root)
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert (decoy / "overrides.yaml").read_text(encoding="utf-8") == "decoy"


def test_explicit_none_keeps_flat_artifact_paths(tmp_path: Path) -> None:
    from src.core.overrides_store import get_overrides_path
    from src.core.review_status_store import get_review_status_path
    from src.core.narrative_store import get_narratives_dir

    reports = tmp_path / "reports"
    assert get_narratives_dir("flat", 7, reports, programs_root=None) == reports / "flat" / "narratives" / "issue_007"
    assert get_overrides_path("flat", reports, programs_root=None) == reports / "flat" / "overrides.yaml"
    assert get_review_status_path("flat", reports, programs_root=None) == reports / "flat" / "review_status.yaml"


def test_explicit_artifact_root_preserves_template_and_legacy_output(tmp_path: Path) -> None:
    from src.core.narrative_store import get_narratives_dir
    from src.core.review_status_store import get_review_status_path

    root = tmp_path / "named-home"
    template = root / "_templates" / "fixture"
    editions = template / "editions"
    editions.mkdir(parents=True)
    (editions / "fixture_weekly.yaml").write_text("program_id: template:fixture\n", encoding="utf-8")
    legacy = template / "output" / "fixture_weekly"
    legacy.mkdir(parents=True)
    reports = tmp_path / "reports"
    assert get_narratives_dir("fixture_weekly", 7, reports, programs_root=root) == template / "narratives" / "issue_007"
    assert get_review_status_path("fixture_weekly", reports, programs_root=root) == legacy / "review_status.yaml"


def test_explicit_artifact_root_rejects_escaping_program_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from src.core import edition_resolver
    from src.core import overrides_store as store
    from src.core.exceptions import ConfigError

    root = _selected_artifact_root(tmp_path)
    resolved = edition_resolver.resolve_edition_paths("fixture_weekly", programs_root=root)
    assert resolved is not None
    monkeypatch.setattr(edition_resolver, "resolve_edition_paths", lambda *_args, **_kwargs: replace(resolved, program_dir=tmp_path / "outside"))
    with pytest.raises(ConfigError, match="escapes"):
        store.get_overrides_path("fixture_weekly", tmp_path / "reports", programs_root=root)


def test_explicit_archived_history_and_placeholder_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from src.core import narrative_store as store

    root = _selected_artifact_root(tmp_path)
    reports, archive = tmp_path / "reports", tmp_path / "archive"
    for issue in (1, 2):
        store.write_narrative_section("fixture_weekly", issue, "delivery", f"prior {issue}", reports, programs_root=root)
        store.archive_narratives("fixture_weekly", issue, archive, reports, programs_root=root)
    monkeypatch.setattr(
        store, "read_archive_index",
        lambda *_args, **_kwargs: SimpleNamespace(
            issues=tuple(SimpleNamespace(kind="confirmed", issue_number=i) for i in (1, 2)),
        ),
    )
    assert store.build_workstream_narrative_history(
        "fixture_weekly", 3, ("Delivery",), {"delivery": "current"}, archive,
        reports_root=reports, programs_root=root,
    ) == {"Delivery": ("current", "prior 2", "prior 1")}


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
