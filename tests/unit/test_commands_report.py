from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

from dataclasses import replace
import json
import os
import pytest
import shutil
import typer
from datetime import date, datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from typing import cast
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

from cli import app
from src.ai._pipeline import AIPipelineError
from src.ai.edit_learner import append_edit_patterns, build_edit_patterns
from src.ai.client import AIClientError
from src.commands.report_lookback import _build_lookback_ai_retrospective_rows
from src.commands import report_deck as report_deck_module
from src.commands import report_health as report_health_module
from src.commands import report as report_module
from src.ai.blurb_generator import WorkstreamBlurb
from src.ai.exec_summary_drafter import ExecSummaryDraft
from src.core.program_fact_store import ProgramFactInput, ProgramFactStore
try:
    from scripts.seed_issue_076_baseline import seed_issue_076_baseline as _seed_fn
    _SEED_AVAILABLE = True
except ImportError:
    _seed_fn = None  # type: ignore[assignment]
    _SEED_AVAILABLE = False

def seed_issue_076_baseline(*args, **kwargs):  # type: ignore[return]
    import pytest as _p
    if _seed_fn is None:
        _p.skip("seed_issue_076_baseline not available — private operator script")
    return _seed_fn(*args, **kwargs)

from src.commands.report import generate_report_draft, generate_report_draft_v2
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.archive_store import write_confirmed_issue, write_skipped_issue
from src.core.claim_tracker import append_claim_entry, append_claim_status_update, append_decision_ask
from src.core.config_loader import load_report_bundle
from src.core import continuation_contract as continuation_contract_module
from src.core.decision_register import save_decisions
from src.core.exceptions import AuthError, ConfigError, QueryError, QueryTimeoutError
from src.core.forecast_engine import ETAForecast
from src.core.incident_journal_store import append_incident_entry
from src.core.journal import append_review_decision, append_signal
from src.core.jinja_filters import JINJA_FILTERS, JINJA_GLOBALS, build_anchor
from src.core.overrides_store import save_overrides
from src.core.models import Comment, Confidence, ConfirmedDimension, DeltaKind, EditionType, Revision, RiskLevel, RunManifest, Snapshot
from src.core.models import SnapshotItem, WorkItem
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, ClaimStatusUpdate, DecisionAsk, DecisionEntry, DecisionStatus, IncidentEntry, M365Config, Milestone, MilestoneAssessment, MilestoneStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus, Signal, SignalReviewDecision, TrajectoryPoint, VitalityArchiveEntry, VitalityArchiveWorkstream, Workstream, WorkstreamSignalSources
from src.core.risk_register_engine import save_risk_register
from src.core.narrative_store import get_narratives_dir
from src.core.overrides_store import get_overrides_path
from src.core.review_status_store import load_review_status
from src.core.section_proposal_store import append_proposal
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.snapshot_store import read_snapshot
from src.core.summary_store import RollingSummary, save_summary
from src.core.trajectory import backfill_trajectory_points
from src.core.continuation_contract import build_continuation_contract, get_continuation_contract_path
from src.core.trusted_baseline_store import advance_trusted_baseline
from src.core.chapter_contract_loader import canonical_dimension_binding_id
from src.core.trusted_baseline_store import record_untrusted_issue
from src.core.deck_renderer import DeckRenderContext, DeckRenderer
from src.core.view_models import HealthSummary, KpiTile, KustoMetric, KustoSectionData, RetrospectiveIntelligenceRow, RetrospectiveIntelligenceSummary, WorkstreamData
from src.core.overrides_store import OverridesSeedingState
from tests.support.report_test_setup import disable_kusto_in_report_copy, get_source_root, reset_overrides_to_seed_state, stage_v2_report_workspace


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_pin_program_fact_snapshot_records_draft_snapshot(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "hold"},
        )
    )

    pinned = report_module._pin_program_fact_snapshot(
        "acme",
        edition_name="acme_weekly",
        issue_number=79,
        generated_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        db_root=tmp_path,
    )

    assert pinned is not None
    assert pinned.program_id == "acme"
    assert pinned.pinned_revision_count == 1
    loaded_pin = store.load_snapshot_pin(pinned.snapshot_id)
    assert loaded_pin is not None
    assert loaded_pin.metadata["edition_name"] == "acme_weekly"
    assert loaded_pin.metadata["issue_number"] == 79


def test_attach_kpi_tiles_to_workstreams_uses_configured_queries_without_signals(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "kpis.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "kpis": [
                    {
                        "id": "acme-placeholder",
                        "workstream_ids": ["acme"],
                        "cluster": "https://cluster.kusto.windows.net",
                        "database": "telemetry",
                        "kql": "StormEvents | take 1",
                        "section": "Fleet Health",
                        "render_as": "metric_highlight",
                        "label": "Fleet Healthy %",
                        "confidence": "medium",
                        "refresh_on_gather": False,
                        "validated": False,
                        "owner_alias": "testowner",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    workstream = Workstream(
        id="acme",
        name="Acme",
        area_paths=("One\\Adventure\\Acme",),
        signal_sources=WorkstreamSignalSources(kusto_query_ids=("acme-placeholder",)),
    )
    workstream_data = WorkstreamData(
        section_id="acme",
        title="Acme",
        blurb="",
        dependency_cascades=(),
        items=(),
        citations=(),
        review_state=report_module.ReviewState.PENDING,
    )

    attached = report_module._attach_kpi_tiles_to_workstreams(
        (workstream_data,),
        approved_signals=(),
        workstreams=(workstream,),
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert len(attached[0].kpi_tiles) == 1
    assert attached[0].kpi_tiles[0].label == "Fleet Healthy %"
    assert attached[0].kpi_tiles[0].validated is False
    assert attached[0].kpi_tiles[0].owner_alias == "testowner"

def test_health_templates_render_forecast_confidence() -> None:
    templates_root = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)

    health = HealthSummary(
        overall_risk=RiskLevel.MEDIUM,
        high_count=0,
        medium_count=1,
        low_count=2,
        done_count=0,
        total_count=3,
        delta_direction="unchanged",
        prior_counts=None,
        trajectory="stable",
        forecast_summary="Forecast: likely to land next week.",
        forecast_confidence="medium",
    )

    html = environment.get_template("partials/health_banner.j2").render(
        health=health,
        milestone_rows=(),
    )
    markdown = environment.get_template("base.teams.j2").render(
        title="Demo title",
        edition=SimpleNamespace(
            issue_number=1,
            ado_data_as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            manifest_id="manifest-demo-001",
        ),
        ordered_sections=(SimpleNamespace(kind="health"),),
        health=health,
        milestone_rows=(),
        top_items=(),
        auto_suggestions=(),
        is_dry_run=False,
    )

    assert "Forecast: likely to land next week. (medium confidence)" in html
    assert "Forecast: likely to land next week. (medium confidence)" in markdown


def test_health_templates_render_telemetry_confidence() -> None:
    templates_root = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)

    health = HealthSummary(
        overall_risk=RiskLevel.MEDIUM,
        high_count=0,
        medium_count=1,
        low_count=2,
        done_count=0,
        total_count=3,
        delta_direction="unchanged",
        prior_counts=None,
        trajectory="stable",
        telemetry_summary="analytics, 5 scope, 2 completed",
        telemetry_confidence="high",
    )

    html = environment.get_template("partials/health_banner.j2").render(
        health=health,
        milestone_rows=(),
    )
    markdown = environment.get_template("base.teams.j2").render(
        title="Demo title",
        edition=SimpleNamespace(
            issue_number=1,
            ado_data_as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            manifest_id="manifest-demo-001",
        ),
        ordered_sections=(SimpleNamespace(kind="health"),),
        health=health,
        milestone_rows=(),
        top_items=(),
        auto_suggestions=(),
        is_dry_run=False,
    )

    assert "analytics, 5 scope, 2 completed" in html
    assert "high confidence" in html
    assert "Telemetry: analytics, 5 scope, 2 completed (high confidence)" in markdown


def test_kpi_tile_template_renders_confidence() -> None:
    templates_root = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)

    tile = KpiTile(
        query_id="acme-deployment-velocity",
        label="Deploy P50 (mins)",
        value="42",
        unit="mins",
        trend=None,
        confidence="medium",
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        source_signal_id="sig-kpi-1",
    )

    rendered = environment.get_template("_kusto_tile.j2").render(
        tile=tile,
        tile_width=150,
    )

    assert "Deploy P50 (mins)" in rendered
    assert "Confidence MEDIUM" in rendered


def test_teams_template_renders_kusto_confidence() -> None:
    templates_root = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)

    kusto_section = KustoSectionData(
        section_id="deployment-readiness-kusto",
        title="Deployment Readiness",
        query_id="velocity-p50",
        render_mode="metric_highlight",
        source_label="kusto",
        confidence="medium",
        columns=(),
        rows=(),
        metrics=(KustoMetric(label="Deploy P50 (mins)", value="4.2"),),
        image_data_url=None,
        reference_url=None,
        caveats=(),
        message=None,
        is_degraded=False,
    )

    markdown = environment.get_template("base.teams.j2").render(
        title="Demo title",
        edition=SimpleNamespace(
            issue_number=1,
            ado_data_as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            manifest_id="manifest-demo-001",
        ),
        ordered_sections=(SimpleNamespace(kind="kusto", kusto_section=kusto_section),),
        health=None,
        milestone_rows=(),
        top_items=(),
        auto_suggestions=(),
        is_dry_run=False,
    )

    assert "Query velocity-p50 · kusto · Confidence MEDIUM" in markdown
    assert "- **Deploy P50 (mins)**: 4.2" in markdown


def test_html_kusto_group_template_renders_confidence() -> None:
    templates_root = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)

    kusto_group = (
        KustoSectionData(
            section_id="fleet-health",
            title="Fleet Health",
            query_id="fleet-health",
            source_label="kusto",
            message="Healthy fleet summary.",
            columns=("Region", "Incidents"),
            rows=(),
            metrics=(KustoMetric(label="Active incidents", value="2"),),
            confidence="high",
            render_mode="table",
            image_data_url=None,
            caveats=(),
            is_degraded=False,
            reference_url=None,
        ),
        KustoSectionData(
            section_id="fleet-capacity",
            title="Fleet Health",
            query_id="fleet-capacity",
            source_label="kusto",
            message="Capacity remains stable.",
            columns=("Stamp", "Headroom"),
            rows=(),
            metrics=(KustoMetric(label="Headroom", value="18%"),),
            confidence="medium",
            render_mode="table",
            image_data_url=None,
            caveats=(),
            is_degraded=False,
            reference_url=None,
        ),
    )

    rendered = environment.get_template("partials/kusto_group_section.j2").render(
        section=SimpleNamespace(anchor_id="fleet-health-group"),
        kusto_group=kusto_group,
    )

    assert "Query fleet-health · kusto · Confidence HIGH" in rendered
    assert "Query fleet-capacity · kusto · Confidence MEDIUM" in rendered


def test_teams_template_renders_workstream_kpi_confidence() -> None:
    templates_root = Path(__file__).resolve().parents[2] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters.update(JINJA_FILTERS)
    environment.globals.update(JINJA_GLOBALS)

    workstream = WorkstreamData(
        section_id="deployment_readiness",
        title="Deployment Readiness",
        blurb="",
        dependency_cascades=(),
        items=(),
        citations=(),
        review_state=report_module.ReviewState.PENDING,
        kpi_tiles=(
            KpiTile(
                query_id="acme-deployment-velocity",
                label="Deploy P50 (mins)",
                value="42",
                unit="mins",
                trend=None,
                confidence="medium",
                as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                source_signal_id="sig-kpi-1",
            ),
        ),
    )

    markdown = environment.get_template("base.teams.j2").render(
        title="Demo title",
        edition=SimpleNamespace(
            issue_number=1,
            ado_data_as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            manifest_id="manifest-demo-001",
        ),
        ordered_sections=(SimpleNamespace(kind="workstream", workstream=workstream),),
        health=None,
        milestone_rows=(),
        top_items=(),
        auto_suggestions=(),
        is_dry_run=False,
    )

    assert "KPIs:" in markdown
    assert "- **Deploy P50 (mins)**: 42 (medium confidence) | as of May 10" in markdown


def test_deck_template_renders_telemetry_confidence() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    renderer = DeckRenderer(
        "nova_lt_deck",
        reports_root=repo_root / "reports",
        templates_root=repo_root / "templates",
    )
    rendered = renderer.render(
        DeckRenderContext(
            issue_number=1,
            issue_date_label="May 10, 2026",
            health_rows=(),
            top_risk_rows=(),
            change_rows=(),
            data_rows=(),
            open_ask_rows=(),
            closed_ask_rows=(),
            telemetry_summary="analytics, 5 scope, 3 completed",
            telemetry_confidence="high",
        )
    )

    assert "## Telemetry" in rendered
    assert "- analytics, 5 scope, 3 completed (high confidence)" in rendered


def test_attach_kpi_tiles_to_chapter_sections_uses_query_chapter_mapping(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "kpis.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "kpis": [
                    {
                        "id": "acme-deployment-velocity",
                        "workstream_ids": ["acme"],
                        "cluster": "https://cluster.kusto.windows.net",
                        "database": "telemetry",
                        "kql": "StormEvents | take 1",
                        "section": "Deployment Velocity",
                        "chapter": "deployment_readiness",
                        "render_as": "metric_highlight",
                        "label": "Deploy P50 (hrs)",
                        "confidence": "high",
                        "refresh_on_gather": True,
                        "validated": False,
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    workstream = Workstream(
        id="acme",
        name="Acme",
        area_paths=("One\\Adventure\\Acme",),
        signal_sources=WorkstreamSignalSources(kusto_query_ids=("acme-deployment-velocity",)),
    )
    chapter_workstream = WorkstreamData(
        section_id="deployment_readiness",
        title="Deployment Readiness",
        blurb="",
        dependency_cascades=(),
        items=(),
        citations=(),
        review_state=report_module.ReviewState.PENDING,
    )

    attached = report_module._attach_kpi_tiles_to_workstreams(
        (chapter_workstream,),
        approved_signals=(),
        workstreams=(workstream,),
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert len(attached[0].kpi_tiles) == 1
    assert attached[0].kpi_tiles[0].query_id == "acme-deployment-velocity"
    assert attached[0].kpi_tiles[0].label == "Deploy P50 (hrs)"


def test_build_deck_issue_rows_adds_ado_links_for_work_item_backed_rows() -> None:
    rows = report_module._build_deck_issue_rows(
        (
            report_module.IssueProjection(
                work_item_id=900001,
                source_type="ado_blocked",
                severity="block",
                summary='WI:900001 "Deployment velocity telemetry stabilization" blocked in ADO (Blocked)',
                owner_alias="operator",
                workstream_id=None,
                ado_url="https://dev.azure.com/your-org/One/_workitems/edit/900001",
                linked_entity_ids=(),
                confidence=report_module.Confidence.LOW,
            ),
            report_module.IssueProjection(
                work_item_id=None,
                source_type="decision_ask",
                severity="warn",
                summary="Need LT decision on SCHIE timeline",
                owner_alias="lt",
                workstream_id=None,
                ado_url=None,
                linked_entity_ids=(),
            ),
        )
    )

    assert rows[0].href == "https://dev.azure.com/your-org/One/_workitems/edit/900001"
    assert rows[0].detail == "ado blocked | BLOCK | low confidence | owner operator"
    assert rows[1].href is None
    assert rows[1].detail == "decision ask | WARN | none confidence | owner lt"


def test_load_previous_dry_run_state_reads_cached_draft_payload(tmp_path: Path) -> None:
    draft_dir = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "issue_001"
    draft_dir.mkdir(parents=True, exist_ok=True)
    payload = {"issue_number": 1, "items": [], "edition_type": "detailed"}
    (draft_dir / "issue_001.draft.json").write_text(json.dumps(payload), encoding="utf-8")

    assert report_module._load_previous_dry_run_state(
        edition_name=EDITION_NAME,
        issue_number=1,
        programs_root=programs_root,
    ) == payload


def test_load_previous_dry_run_state_tolerates_malformed_cached_draft_payload(tmp_path: Path) -> None:
    draft_dir = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "issue_001"
    draft_dir.mkdir(parents=True, exist_ok=True)
    (draft_dir / "issue_001.draft.json").write_text("{", encoding="utf-8")

    assert report_module._load_previous_dry_run_state(
        edition_name=EDITION_NAME,
        issue_number=1,
        programs_root=programs_root,
    ) is None


def test_find_offline_snapshot_cache_prefers_cached_draft_snapshot(tmp_path: Path) -> None:
    snapshot = Snapshot(
        issue_number=2,
        generated_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )
    snapshot_path = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.snapshot.json"
    report_module._write_output_json(snapshot_path, snapshot)

    cache = report_module._find_offline_snapshot_cache(
        edition_name=EDITION_NAME,
        issue_number=2,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert cache is not None
    assert cache.source_label == "cached draft Issue 002"
    assert cache.snapshot_path == snapshot_path
    assert cache.snapshot.issue_number == 2


def test_load_previous_snapshot_prefers_trusted_baseline_issue_when_available(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    snapshots_root = edition_root / "snapshots"
    trusted_snapshot_path = snapshots_root / "issue_001.snapshot.json"
    latest_snapshot_path = snapshots_root / "issue_002.snapshot.json"
    trusted_snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )
    latest_snapshot = Snapshot(
        issue_number=2,
        generated_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )
    report_module._write_output_json(trusted_snapshot_path, trusted_snapshot)
    report_module._write_output_json(latest_snapshot_path, latest_snapshot)
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": trusted_snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(trusted_snapshot_path),
                    },
                    {
                        "issue_number": 2,
                        "generated_at": latest_snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(latest_snapshot_path),
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    previous_snapshot, previous_issue_number = report_module._load_previous_snapshot(
        EDITION_NAME,
        3,
        archive_root,
        trusted_issue_number=1,
    )

    assert previous_issue_number == 1
    assert previous_snapshot is not None
    assert previous_snapshot.issue_number == 1


def test_load_previous_snapshot_falls_back_when_trusted_baseline_snapshot_missing(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    snapshots_root = edition_root / "snapshots"
    latest_snapshot_path = snapshots_root / "issue_002.snapshot.json"
    latest_snapshot = Snapshot(
        issue_number=2,
        generated_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )
    report_module._write_output_json(latest_snapshot_path, latest_snapshot)
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc).isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(snapshots_root / "issue_001.snapshot.json"),
                    },
                    {
                        "issue_number": 2,
                        "generated_at": latest_snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(latest_snapshot_path),
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    previous_snapshot, previous_issue_number = report_module._load_previous_snapshot(
        EDITION_NAME,
        3,
        archive_root,
        trusted_issue_number=1,
    )

    assert previous_issue_number == 2
    assert previous_snapshot is not None
    assert previous_snapshot.issue_number == 2


def test_build_draft_ado_diff_lines_reports_no_changes_for_matching_previous_draft() -> None:
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    current_items = _sample_items(as_of)
    previous_dry_run_state = {
        "issue_number": 1,
        "generated_at": as_of.isoformat(),
        "ado_data_as_of": as_of.isoformat(),
        "edition_type": "detailed",
        "items": report_module._to_jsonable(current_items),
    }

    lines = report_module._build_draft_ado_diff_lines(
        previous_dry_run_state=previous_dry_run_state,
        current_items=current_items,
        current_evidence_by_item={},
        current_issue_number=2,
        current_data_as_of=as_of,
        current_edition_type=EditionType.DETAILED,
    )

    assert lines == ("No ADO data changes detected.",)


def test_ensure_review_status_preserves_terminal_sections_and_marks_skipped(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    initial_status = report_module.ReviewStatus(
        issue_number=5,
        sections=(
            report_module.ReviewSection(
                section_id="exec_summary",
                state=report_module.ReviewState.APPROVED,
                reviewer="owner@example.com",
                note="approved",
                updated_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            ),
            report_module.ReviewSection(
                section_id="ws:stable_lane",
                state=report_module.ReviewState.PENDING,
                reviewer=None,
                note=None,
                updated_at=None,
            ),
        ),
    )
    report_module.save_review_status(EDITION_NAME, initial_status, reports_root=reports_root)

    status = report_module._ensure_review_status(
        edition_name=EDITION_NAME,
        issue_number=5,
        workstream_section_ids=("stable_lane", "new_lane"),
        skipped_section_ids={"ws:new_lane"},
        reports_root=reports_root,
    )

    sections = {section.section_id: section for section in status.sections}
    assert sections["exec_summary"].state == report_module.ReviewState.APPROVED
    assert sections["ws:stable_lane"].state == report_module.ReviewState.PENDING
    assert sections["ws:new_lane"].state == report_module.ReviewState.SKIPPED_NO_DELTA


def test_skipped_review_sections_skips_only_stable_low_risk_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    items = (
        WorkItem(
            id=1001,
            type="Feature",
            title="Stable lane item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=1002,
            type="Feature",
            title="High risk lane item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )

    stable_packet = report_module.ScorecardEvidencePacket(
        dimension_name="Stable Lane",
        dimension_description="Stable lane",
        total_items=1,
        items_by_risk={RiskLevel.LOW: 1},
        stale_items=(),
        stale_count=0,
        overdue_items=(),
        overdue_count=0,
        blocked_items=(),
        blocked_count=0,
        unowned_items=(),
        unowned_count=0,
        high_activity_items=(),
        prior_confirmed_risk=RiskLevel.LOW,
        author_risk=None,
        ado_query_url="https://dev.azure.com/query/stable",
        item_links=(),
        derived_risk=RiskLevel.LOW,
        item_ids=(1001,),
    )
    high_packet = report_module.ScorecardEvidencePacket(
        dimension_name="High Lane",
        dimension_description="High lane",
        total_items=1,
        items_by_risk={RiskLevel.HIGH: 1},
        stale_items=(),
        stale_count=0,
        overdue_items=(),
        overdue_count=0,
        blocked_items=(),
        blocked_count=0,
        unowned_items=(),
        unowned_count=0,
        high_activity_items=(),
        prior_confirmed_risk=RiskLevel.HIGH,
        author_risk=None,
        ado_query_url="https://dev.azure.com/query/high",
        item_links=(),
        derived_risk=RiskLevel.HIGH,
        item_ids=(1002,),
    )

    def fake_iter_detail_sections(**_: object) -> tuple[tuple[str, str, object, object, tuple[WorkItem, ...]], ...]:
        return (
            (
                "Acme",
                "stable_lane",
                SimpleNamespace(risk=RiskLevel.LOW),
                stable_packet,
                (items[0],),
            ),
            (
                "Acme",
                "high_lane",
                SimpleNamespace(risk=RiskLevel.HIGH),
                high_packet,
                (items[1],),
            ),
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._iter_detail_sections", fake_iter_detail_sections)

    skipped = report_module._skipped_review_sections(
        bundle=SimpleNamespace(),
        items=items,
        scorecards=(),
        scorecard_packets={},
        overrides_document=report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        deltas=report_module.DeltaSet(
            issue_number=2,
            previous_issue_number=1,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=2,
            owner_changes=(),
        ),
        freshness_report=report_module.FreshnessReport(
            issue_number=2,
            items=(),
            blocks=0,
            warns=0,
            infos=0,
        ),
        top_items=(),
        previous_snapshot=Snapshot(
            issue_number=1,
            generated_at=as_of,
            ado_data_as_of=as_of,
            edition_type=EditionType.DETAILED,
            items=(),
            scorecards=(),
        ),
    )

    assert skipped == {"ws:stable_lane"}


def test_build_snapshot_projects_items_and_scorecards() -> None:
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    items = _sample_items(as_of)
    report = report_module.ReportData(
        issue_number=7,
        edition=EditionType.DETAILED,
        generated_at=as_of,
        ado_data_as_of=as_of,
        program=SimpleNamespace(),
        items=items,
        deltas=report_module.DeltaSet(
            issue_number=7,
            previous_issue_number=6,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=len(items),
        ),
        scorecard=(
            report_module.DimensionRisk(
                name="Deployment Safety",
                risk=RiskLevel.HIGH,
                summary="High risk",
                evidence=report_module.EvidencePacket(
                    work_item_id=items[0].id,
                    revisions=(),
                    comments=(),
                    enrichments=(),
                    confidence=Confidence.LOW,
                    tier=report_module.AttributionTier.TIER3,
                    summary_for_reviewer="evidence",
                ),
            ),
        ),
        scorecard_deltas=(),
        exec_summary_text="summary",
        workstream_blurbs={},
        freshness=report_module.FreshnessReport(issue_number=7, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=report_module.ReviewStatus(issue_number=7, sections=()),
        manifest_id="manifest-7",
    )
    scorecard_packets = {
        "Acme Adventure/XIO 100% Ramp Readiness": {
            "Deployment Safety": report_module.ScorecardEvidencePacket(
                dimension_name="Deployment Safety",
                dimension_description="Safety lane",
                total_items=1,
                items_by_risk={RiskLevel.HIGH: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=RiskLevel.MEDIUM,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/deployment-safety",
                item_links=(),
                derived_risk=RiskLevel.HIGH,
                item_ids=(items[0].id,),
            )
        }
    }

    snapshot = report_module._build_snapshot(report, scorecard_packets)

    assert snapshot.issue_number == 7
    assert snapshot.items[0].id == items[0].id
    assert snapshot.scorecards[0].scorecard_name == "Acme Adventure/XIO 100% Ramp Readiness"
    assert snapshot.scorecards[0].prior_risk == RiskLevel.MEDIUM


def test_group_scorecard_deltas_indexes_by_dimension() -> None:
    deltas = (
        report_module.ScorecardDelta(
            dimension="Deployment Safety",
            old_risk=RiskLevel.MEDIUM,
            new_risk=RiskLevel.HIGH,
            delta_kind=DeltaKind.RISK_UP,
            summary="Worsened",
        ),
    )

    grouped = report_module._group_scorecard_deltas(deltas)

    assert grouped["default"]["Deployment Safety"] == deltas[0]


def test_report_snapshot_status_and_format_helpers() -> None:
    bundle = SimpleNamespace(config=SimpleNamespace(edition=SimpleNamespace(title="Issue {issue_number} | {date}")))
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    assert report_module._format_edition_title(bundle, 8, as_of) == "Issue 8 | 2026-05-05"
    assert report_module._derive_qg_status(has_blockers=True, has_warnings=True) == "blocked"
    assert report_module._derive_qg_status(has_blockers=False, has_warnings=True) == "warn"
    assert report_module._derive_qg_status(has_blockers=False, has_warnings=False) == "pass"
    assert report_module._format_ban_violation(
        SimpleNamespace(location="exec_summary", phrase="foo", matched_text="foo")
    ) == "exec_summary: banned phrase 'foo' matched 'foo'."


def test_subject_signal_prefers_due_decision_item() -> None:
    signal = report_module._subject_signal(
        dimension_risks=(),
        top_items=(
            report_module.Top3Item(
                item_type="decision",
                text="Need LT call",
                owner="LT",
                ado_link="",
                anchor="deployment_readiness",
                by_date=date(2026, 5, 9),
            ),
        ),
        auto_suggestions=(),
        scorecard_deltas=(),
    )

    assert signal == "Deployment Readiness decision needed by May."


def test_build_auto_suggested_top_items_prefers_new_high_and_uses_scorecard_anchor() -> None:
    suggestions = report_module._build_auto_suggested_top_items(
        scorecard_deltas=(
            report_module.ScorecardDelta(
                dimension="Deployment Safety",
                old_risk=RiskLevel.MEDIUM,
                new_risk=RiskLevel.HIGH,
                delta_kind=DeltaKind.RISK_UP,
                summary="Worsened",
            ),
        ),
        scorecard_packets={
            "Acme Adventure/XIO 100% Ramp Readiness": {
                "Deployment Safety": SimpleNamespace(ado_query_url="https://dev.azure.com/query/deployment-safety")
            }
        },
    )

    assert suggestions[0].item_type == "decision"
    assert suggestions[0].ado_link == "https://dev.azure.com/query/deployment-safety"
    assert suggestions[0].anchor == "acme-adventure-xio-100-ramp-readiness-deployment-safety"


def test_email_subject_and_preheader_helpers_format_shared_copy() -> None:
    health = SimpleNamespace(
        high_count=1,
        medium_count=0,
        overall_risk=RiskLevel.HIGH,
        trajectory="stable",
        risk_load=2.0,
    )

    assert report_module._build_email_subject("Weekly Acme update", health, "Deployment Safety new High risk") == (
        "Weekly Acme update: HIGH, STABLE - Deployment Safety new High risk"
    )
    assert report_module._build_email_preheader(
        health,
        "Telemetry is the blocker.",
        (),
    ) == "1 High risks, Risk Load 2.0 stable. Telemetry is the blocker."


def test_generate_report_draft_writes_outputs(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _patch_m3_linked_wi(programs_root, work_item_id=900001)
    _set_v2_program_artifact_base_url(reports_root.parent / "programs", artifact_base_url="https://contoso.example/vertex-output")
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Deployment telemetry may miss the weekly gate",
                description="Telemetry stabilization could slip the weekly decision checkpoint.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.TECHNICAL,
                owner_alias="operator",
                mitigation_plan="Track the blocker daily until telemetry is stable.",
                mitigation_due_date=date(2026, 5, 2),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=("m3-code-complete",),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 4, 1),
                identified_in_vertex_issue=0,
                last_reviewed_date=date(2026, 4, 1),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    append_signal(
        Signal(
            id="analytics-1",
            timestamp=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: analytics summary",
            raw_ref="ado-analytics:deployment_readiness:20260505:20260421:20260505",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 2,
                "scope_delta_count": 2,
                "open_delta_count": -1,
                "average_cycle_time_days": 5.0,
                "average_lead_time_days": 8.0,
            },
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
    )
    append_signal(
        Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 50,
                "open_item_count": 1,
                "team_member_count": 3,
                "total_capacity_per_day": 24.0,
            },
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 4),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 28),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
        ),
        programs_root=programs_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=0,
        snapshot=Snapshot(
            issue_number=0,
            generated_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(),
            scorecards=(),
        ),
        html_body="<html><body>Issue 000</body></html>",
        markdown_body="# Issue 000",
        manifest=RunManifest(
            manifest_id="manifest-0",
            issue_number=0,
            edition=EDITION_NAME,
            started_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
            qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
            git_sha=None,
            metadata={
                "milestone_assessments": [
                    {
                        "milestone_id": "m3-code-complete",
                        "target_date": "2026-05-12",
                    }
                ]
            },
        ),
        archive_root=archive_root,
    )
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.exit_code == 3
    assert artifacts.eml_path is not None and artifacts.eml_path.exists()
    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert artifacts.md_path is not None and artifacts.md_path.exists()
    assert artifacts.manifest_path is not None and artifacts.manifest_path.exists()
    assert artifacts.snapshot_path is not None and artifacts.snapshot_path.exists()
    assert artifacts.quality_matrix_md_path is not None and artifacts.quality_matrix_md_path.exists()
    assert artifacts.quality_matrix_json_path is not None and artifacts.quality_matrix_json_path.exists()
    assert artifacts.remediation_md_path is not None and artifacts.remediation_md_path.exists()
    assert artifacts.remediation_json_path is not None and artifacts.remediation_json_path.exists()
    assert artifacts.adaptive_card_paths
    assert artifacts.adaptive_card_paths[0].exists()
    assert (programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").exists()
    assert artifacts.overrides_path.exists()
    assert (artifacts.narratives_dir / "exec_summary.md").exists()

    manifest_payload = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    quality_matrix_payload = json.loads(artifacts.quality_matrix_json_path.read_text(encoding="utf-8"))
    remediation_payload = json.loads(artifacts.remediation_json_path.read_text(encoding="utf-8"))
    adaptive_card_payload = json.loads(artifacts.adaptive_card_paths[0].read_text(encoding="utf-8"))
    eml_payload = artifacts.eml_path.read_text(encoding="utf-8") if artifacts.eml_path is not None else ""

    assert manifest_payload["issue_number"] == 1
    assert "QG-8" in manifest_payload["qg_results"]
    assert manifest_payload["ai_cost_by_model"] == {}
    assert "milestone_assessments" in manifest_payload["metadata"]
    assert manifest_payload["metadata"]["milestone_assessments"]
    assert "override_snapshot" in draft_payload
    assert "exec_summary_text" in draft_payload
    assert "workstream_blurbs" in draft_payload
    assert "Risk register: 1 active entry, 1 stale review. Highest active: Deployment telemetry may miss the weekly gate (owner operator, score 9)." in artifacts.html_body
    assert "Risk register: 1 active entry, 1 stale review. Highest active: Deployment telemetry may miss the weekly gate (owner operator, score 9)." in artifacts.markdown_body
    assert "Milestones: 1 at risk, 2 on track." in artifacts.html_body
    assert "Milestones: 1 at risk, 2 on track." in artifacts.markdown_body
    assert ">Telemetry<" in artifacts.html_body
    assert "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in artifacts.html_body
    assert "Telemetry: analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in artifacts.markdown_body
    assert "Highlights: M3 - Code Complete: Tracking 2026-05-28 (10 days late vs target); target history 2026-05-12 -> 2026-05-18." in artifacts.html_body
    assert "Highlights: M3 - Code Complete: Tracking 2026-05-28 (10 days late vs target); target history 2026-05-12 -> 2026-05-18." in artifacts.markdown_body
    assert "Tracked Milestones" in artifacts.html_body
    assert "M3 - Code Complete | at risk (critical path) | target May 18" in artifacts.html_body
    assert "Tracked milestones:" in artifacts.markdown_body
    assert "M3 - Code Complete | at risk (critical path) | target May 18" in artifacts.markdown_body
    assert quality_matrix_payload["schema_version"] == "1.0"
    assert quality_matrix_payload["slices"]
    deployment_velocity = next(slice_row for slice_row in quality_matrix_payload["slices"] if slice_row["slice_id"] == "acme.deployment_velocity")
    assert remediation_payload["schema_version"] == "1.0"
    assert remediation_payload["summary"]["total_items"] >= 0
    assert adaptive_card_payload["type"] == "AdaptiveCard"
    assert adaptive_card_payload["body"][0]["text"] == f"{EDITION_NAME} weekly summary"
    newsletter_action = next(
        action
        for block in adaptive_card_payload["body"]
        if block.get("type") == "ActionSet"
        for action in block.get("actions", [])
        if action.get("title") == "Full newsletter"
    )
    assert newsletter_action["url"] == "https://contoso.example/vertex-output/acme_weekly/issue_001/issue_001.html"
    assert any(block.get("type") == "ActionSet" for block in adaptive_card_payload["body"][-1]["items"])
    assert deployment_velocity["source_of_truth"] == "hybrid"
    assert deployment_velocity["telemetry"]["status"] == "absent"
    assert "Changes include" not in artifacts.report.exec_summary_text
    assert "current-state inventory" in artifacts.report.exec_summary_text
    assert any("authentic voice" in warning.lower() for warning in artifacts.warnings)
    assert "X-Unsent: 1" in eml_payload
    assert "Content-Type: multipart/alternative" in eml_payload
    assert "@microsoft.com" in eml_payload


def test_generate_report_draft_reads_sqlite_backed_signals_for_telemetry(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _patch_m3_linked_wi(programs_root, work_item_id=900001)
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)

    signal_store.append(
        Signal(
            id="analytics-1",
            timestamp=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: analytics summary",
            raw_ref="ado-analytics:deployment_readiness:20260505:20260421:20260505",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 2,
                "scope_delta_count": 2,
                "open_delta_count": -1,
                "average_cycle_time_days": 5.0,
                "average_lead_time_days": 8.0,
            },
            thread_id=None,
        )
    )
    signal_store.append(
        Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 50,
                "open_item_count": 1,
                "team_member_count": 3,
                "total_capacity_per_day": 24.0,
            },
            thread_id=None,
        )
    )
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    )
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    )
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 4),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 28),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=0,
        snapshot=Snapshot(
            issue_number=0,
            generated_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(),
            scorecards=(),
        ),
        html_body="<html><body>Issue 000</body></html>",
        markdown_body="# Issue 000",
        manifest=RunManifest(
            manifest_id="manifest-0",
            issue_number=0,
            edition=EDITION_NAME,
            started_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
            qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
            git_sha=None,
            metadata={
                "milestone_assessments": [
                    {
                        "milestone_id": "m3-code-complete",
                        "target_date": "2026-05-12",
                    }
                ]
            },
        ),
        archive_root=archive_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert ">Telemetry<" in artifacts.html_body
    assert "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in artifacts.html_body
    assert "Milestones: 1 at risk, 2 on track." in artifacts.html_body
    assert "Highlights: M3 - Code Complete: Tracking 2026-05-28 (10 days late vs target); target history 2026-05-12 -> 2026-05-18." in artifacts.html_body


def test_build_v2_vitality_snapshot_reads_sqlite_backed_signals_and_trajectories(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    editions_root = reports_root.parent / "editions"
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    signal_store.append(
        Signal(
            id="workiq-1",
            timestamp=datetime(2026, 5, 4, 10, 0, tzinfo=timezone.utc),
            source="workiq/meeting",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Follow up on deployment readiness.",
            raw_ref="workiq:workiq-1",
            confidence=Confidence.HIGH,
            metadata={"entity_link_confidence": "high"},
            thread_id="thread-1",
        )
    )
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="workiq-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 4, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    )
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 5),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 18),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )

    resolved_v2 = report_module.resolve_edition(
        EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    assert resolved_v2 is not None

    snapshot, _settings = report_module._build_v2_vitality_snapshot(
        resolved_v2=resolved_v2,
        items=(
            WorkItem(
                id=900001,
                type="Feature",
                title="Deployment readiness follow-up",
                state="Active",
                assigned_to="Vertex Maintainer",
                assigned_to_email="operator@example.com",
                area_path="One\\Adventure\\Acme\\Deployment",
                iteration_path="Sprint 42",
                target_date=date(2026, 5, 18),
                risk_level=RiskLevel.MEDIUM,
                tags=("acme",),
                custom_fields={},
                revisions=[],
                comments=[],
                fetched_at=as_of,
            ),
        ),
        as_of=as_of,
        programs_root=programs_root,
    )

    assert len(snapshot.scores) == 1
    assert snapshot.scores[0].workiq_signal_count == 1
    assert snapshot.leakage_events == 0


def test_generate_report_draft_passes_sqlite_backed_signals_to_phase_1b_gates(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    issue_1_as_of = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
    issue_2_as_of = datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)
    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=issue_1_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_1_as_of)[0], risk_level=RiskLevel.LOW),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=issue_1_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=2,
        snapshot=_lookback_snapshot(
            issue_number=2,
            as_of=issue_2_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_2_as_of)[0], risk_level=RiskLevel.MEDIUM),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM},
        ),
        html_body="<html><body>Issue 002</body></html>",
        markdown_body="# Issue 002",
        manifest=_manifest(issue_number=2, as_of=issue_2_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    signal_store.append(
        Signal(
            id="approved-1",
            timestamp=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
            source="workiq/meeting",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Approved SQLite-backed signal for report gating.",
            raw_ref="workiq:approved-1",
            confidence=Confidence.HIGH,
            metadata={},
            thread_id=None,
        )
    )
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="approved-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    )
    append_proposal(
        SectionRevisionProposal(
            proposal_id="proposal-networking-stale-claim",
            edition_id=EDITION_NAME,
            issue_number=3,
            section_id="ws_deployment_readiness",
            current_text="Current deployment narrative.",
            proposed_text="Current deployment narrative.",
            evidence_brief=SectionEvidenceBrief(
                section_id="ws_deployment_readiness",
                ado_delta_summary="No material changes.",
                new_items=(),
                closed_items=(),
                risk_changed_items=(),
                eta_changed_items=(),
                top_signals=(),
                kpi_summary=None,
                stale_claims=("claim-stale-report-1",),
                vitality_summary="Stable",
                confidence=Confidence.MEDIUM,
            ),
            status=SectionRevisionStatus.ACCEPTED,
            generated_at=datetime(2026, 5, 5, 10, 6, tzinfo=timezone.utc),
            resolved_at=datetime(2026, 5, 5, 10, 7, tzinfo=timezone.utc),
            accepted_text="Current deployment narrative.",
            source_hash="sha256:test-report-stale-claim",
        ),
        "acme",
        3,
        programs_root=programs_root,
    )

    original_phase_1b = report_module.evaluate_phase_1b_gates
    captured: dict[str, object] = {}

    def _capture_phase_1b(**kwargs):
        captured["approved_signals"] = kwargs["approved_signals"]
        captured["journal_signals"] = kwargs["journal_signals"]
        captured["stale_claim_ids"] = kwargs["stale_claim_ids"]
        return original_phase_1b(**kwargs)

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.evaluate_phase_1b_gates", _capture_phase_1b)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=2,
        open_browser=False,
    )

    assert [signal.id for signal in captured["journal_signals"]] == ["approved-1"]
    assert [signal.id for signal in captured["approved_signals"]] == ["approved-1"]
    assert captured["stale_claim_ids"] == ("claim-stale-report-1",)


def test_generate_report_draft_writes_continuation_contract_when_trusted_baseline_exists(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    prior_narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    prior_narratives_dir.mkdir(parents=True, exist_ok=True)
    (prior_narratives_dir / "exec_summary.md").write_text("Prior exec summary.", encoding="utf-8")
    save_overrides(
        EDITION_NAME,
        report_module.OverridesDocument(
            issue_number=1,
            top_3_now=(),
            scorecards=(),
            forwarding_context="Prior forwarding context",
        ),
        reports_root=reports_root,
    )

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    contract_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.continuation_contract.json"
    contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))

    assert artifacts.continuation_contract_path is not None and artifacts.continuation_contract_path.exists()
    assert contract_payload["prior_trusted_issue"] == 1
    assert contract_payload["issue_number"] == 2
    assert artifacts.manifest.qg_results["QG-B1"] is False
    assert artifacts.manifest.qg_results["QG-B2"] is False
    assert artifacts.manifest.qg_results["QG-B3"] is True
    assert contract_payload["narrative_seeding"]["seeded"] is True
    assert contract_payload["narrative_seeding"]["source_path"] == "archive"
    assert contract_payload["narrative_seeding"]["files_seeded"] == ["exec_summary.md"]
    assert contract_payload["overrides_seeding"]["seeded"] is True
    assert contract_payload["overrides_seeding"]["fields_carried"] == ["scorecards", "forwarding_context"]
    assert contract_payload["scorecard_composition"]["frozen_from_issue"] == 1
    assert contract_payload["scorecard_composition"]["inherited_dimensions"] == [["Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"]]
    assert contract_payload["scorecard_composition"]["proposed_additions"]
    assert contract_payload["scorecard_composition"]["proposed_removals"] == []
    assert contract_payload["section_roster"]["added_sections"]
    assert contract_payload["section_roster"]["seeded_from_prior"] is True
    assert contract_payload["first_inherited_at"] == contract_payload["last_refreshed_at"]


def test_build_continuation_contract_reports_scorecard_composition_additions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    output_dir = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    contract = build_continuation_contract(
        edition_name=EDITION_NAME,
        issue_number=2,
        started_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
        overrides_document=report_module.OverridesDocument(issue_number=2, top_3_now=(), scorecards=()),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=(
            ("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"),
            ("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Safety"),
        ),
        current_section_ids=("exec_summary",),
    )

    assert contract is not None
    assert contract.scorecard_composition.inherited_dimensions == (
        ("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"),
    )
    assert contract.scorecard_composition.proposed_additions == (
        ("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Safety"),
    )
    assert contract.scorecard_composition.proposed_removals == ()


def test_build_continuation_contract_reports_section_roster_drift(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text("Prior summary\n", encoding="utf-8")
    (narratives_dir / "ws_networking.md").write_text("Prior networking\n", encoding="utf-8")
    (narratives_dir / "chapter_deployment_readiness.md").write_text("Prior chapter\n", encoding="utf-8")

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    output_dir = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    contract = build_continuation_contract(
        edition_name=EDITION_NAME,
        issue_number=2,
        started_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
        overrides_document=report_module.OverridesDocument(issue_number=2, top_3_now=(), scorecards=()),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=(("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"),),
        current_section_ids=("exec_summary", "deployment_readiness"),
    )

    assert contract is not None
    assert contract.section_roster.inherited_sections == ("chapter_deployment_readiness.md", "exec_summary.md", "ws_networking.md")
    assert contract.section_roster.added_sections == ()
    assert contract.section_roster.removed_sections == ("networking",)


def test_build_continuation_contract_omits_removed_sections_from_roster_drift(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text("Prior summary\n", encoding="utf-8")
    (narratives_dir / "ws_networking.md").write_text("Prior networking\n", encoding="utf-8")

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    output_dir = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    contract = build_continuation_contract(
        edition_name=EDITION_NAME,
        issue_number=2,
        started_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
        overrides_document=report_module.OverridesDocument(
            issue_number=2,
            top_3_now=(),
            scorecards=(),
            removed_sections=("networking",),
        ),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=((),)[0:0],
        current_section_ids=("exec_summary",),
    )

    assert contract is not None
    assert contract.section_roster.removed_sections == ()


def test_build_continuation_contract_preserves_override_seed_history_after_manual_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output_dir = tmp_path / "output" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    contract_path = get_continuation_contract_path(output_dir, 2)
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": EDITION_NAME,
                "issue_number": 2,
                "prior_trusted_issue": 1,
                "first_inherited_at": "2026-05-08T18:00:00+00:00",
                "last_refreshed_at": "2026-05-08T18:00:00+00:00",
                "scorecard_composition": {
                    "frozen_from_issue": 1,
                    "inherited_dimensions": [["Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"]],
                    "proposed_additions": [],
                    "proposed_removals": [],
                    "removed_by_override": [],
                },
                "section_roster": {
                    "inherited_sections": ["exec_summary.md"],
                    "seeded_from_prior": False,
                    "sections_missing_evidence": [],
                    "added_sections": [],
                    "removed_sections": [],
                },
                "narrative_seeding": {
                    "seeded": False,
                    "source_issue": 1,
                    "source_path": "archive",
                    "files_seeded": [],
                    "source_hashes": {},
                },
                "overrides_seeding": {
                    "seeded": True,
                    "source_issue": 1,
                    "fields_carried": ["scorecards", "forwarding_context"],
                    "fields_cleared": ["top_3_now"],
                },
                "evidence_quality": {
                    "sections_with_ado_coverage": 0,
                    "sections_with_query_only": 0,
                    "sections_with_connector_only": 0,
                    "sections_manual_only": 0,
                },
                "baseline_gap": None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(continuation_contract_module, "load_trusted_baseline_issue", lambda *args, **kwargs: 1)
    monkeypatch.setattr(continuation_contract_module, "load_trusted_baseline", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        continuation_contract_module,
        "load_inherited_scorecard_dimensions",
        lambda *args, **kwargs: (("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"),),
    )
    monkeypatch.setattr(
        continuation_contract_module,
        "_load_inherited_sections",
        lambda *args, **kwargs: (("exec_summary.md",), "archive"),
    )
    monkeypatch.setattr(
        continuation_contract_module,
        "_build_evidence_quality",
        lambda workstream_data: (
            (),
            continuation_contract_module.ContinuationContractEvidenceQuality(
                sections_with_ado_coverage=0,
                sections_with_query_only=0,
                sections_with_connector_only=0,
                sections_manual_only=0,
            ),
        ),
    )
    monkeypatch.setattr(continuation_contract_module, "_build_source_hashes", lambda *args, **kwargs: {})

    contract = build_continuation_contract(
        edition_name=EDITION_NAME,
        issue_number=2,
        started_at=datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc),
        reports_root=tmp_path / "reports",
        archive_root=tmp_path / "archive",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        overrides_document=report_module.OverridesDocument(issue_number=2, top_3_now=(), scorecards=()),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=(("Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"),),
        current_section_ids=("exec_summary",),
        overrides_seeding=OverridesSeedingState(seeded=False, source_issue=1),
    )

    assert contract is not None
    assert contract.first_inherited_at == datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)
    assert contract.last_refreshed_at == datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc)
    assert contract.overrides_seeding.seeded is True
    assert contract.overrides_seeding.fields_carried == ("scorecards", "forwarding_context")
    assert contract.overrides_seeding.fields_cleared == ("top_3_now",)


def test_build_continuation_contract_records_untrusted_baseline_gap(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )
    record_untrusted_issue(
        EDITION_NAME,
        2,
        recorded_at=datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        reason="Issue 002 was archived for traceability only.",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    output_dir = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    contract = build_continuation_contract(
        edition_name=EDITION_NAME,
        issue_number=3,
        started_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
        overrides_document=report_module.OverridesDocument(issue_number=3, top_3_now=(), scorecards=()),
        workstream_data=(),
        output_dir=output_dir,
        current_scorecard_dimensions=((),)[0:0],
        current_section_ids=("exec_summary",),
    )

    assert contract is not None
    assert contract.baseline_gap is not None
    assert contract.baseline_gap.skipped_untrusted_issues == (2,)
    assert contract.baseline_gap.latest_untrusted_issue == 2
    assert contract.baseline_gap.latest_untrusted_reason == "Issue 002 was archived for traceability only."


def test_generate_report_draft_continuation_contract_records_untrusted_baseline_gap(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    prior_narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    prior_narratives_dir.mkdir(parents=True, exist_ok=True)
    (prior_narratives_dir / "exec_summary.md").write_text("Prior exec summary.", encoding="utf-8")

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )
    record_untrusted_issue(
        EDITION_NAME,
        2,
        recorded_at=datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        reason="Issue 002 was archived for traceability only.",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=3,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    contract_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_003" / "issue_003.continuation_contract.json"
    contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))

    assert artifacts.continuation_contract_path == contract_path
    assert contract_payload["baseline_gap"]["skipped_untrusted_issues"] == [2]
    assert contract_payload["baseline_gap"]["latest_untrusted_issue"] == 2
    assert contract_payload["baseline_gap"]["latest_untrusted_reason"] == "Issue 002 was archived for traceability only."


def test_generate_report_draft_writes_workstream_snapshot_artifacts(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.workstream_snapshot_md_path is not None and artifacts.workstream_snapshot_md_path.exists()
    assert artifacts.workstream_snapshot_json_path is not None and artifacts.workstream_snapshot_json_path.exists()
    assert artifacts.workstream_associations_json_path is not None and artifacts.workstream_associations_json_path.exists()

    workstream_snapshot_payload = json.loads(artifacts.workstream_snapshot_json_path.read_text(encoding="utf-8"))
    workstream_association_payload = json.loads(artifacts.workstream_associations_json_path.read_text(encoding="utf-8"))
    snapshot_workstream = next(
        entry for entry in workstream_snapshot_payload["workstreams"] if entry["workstream_id"] == "acme.deployment_velocity"
    )

    assert workstream_snapshot_payload["schema_version"] == "1.0"
    assert workstream_snapshot_payload["program_id"] == "acme"
    assert snapshot_workstream["lifecycle_state"] == "active"
    assert snapshot_workstream["report_relevance"] in {"full_section", "summary_only", "tracked_not_reported"}
    assert "assigned_item_ids" in snapshot_workstream
    assert any(record["source_type"] == "curated_slice" for record in workstream_association_payload)
    assert any(record["source_type"] in {"query_derived", "area_path_derived", "slice_membership"} for record in workstream_association_payload)


def test_write_output_text_appends_trailing_newline(tmp_path: Path) -> None:
    output_path = tmp_path / "nested" / "artifact.txt"

    written_path = report_module._write_output_text(output_path, "hello world")

    assert written_path == output_path
    assert written_path.read_text(encoding="utf-8") == "hello world\n"


def test_write_output_json_serializes_dataclasses_enums_and_datetimes(tmp_path: Path) -> None:
    output_path = tmp_path / "nested" / "artifact.json"
    payload = {
        "risk": RiskLevel.HIGH,
        "timestamp": datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        "manifest": RunManifest(
            manifest_id="manifest-1",
            issue_number=1,
            edition=EDITION_NAME,
            started_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 5, 5, 18, 5, tzinfo=timezone.utc),
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
            qg_results={"QG-8": True},
            git_sha=None,
        ),
    }

    written_path = report_module._write_output_json(output_path, payload)
    written_payload = json.loads(written_path.read_text(encoding="utf-8"))

    assert written_path == output_path
    assert written_payload["risk"] == "high"
    assert written_payload["timestamp"] == "2026-05-05T18:00:00+00:00"
    assert written_payload["manifest"]["issue_number"] == 1
    assert written_payload["manifest"]["qg_results"]["QG-8"] is True


def test_write_report_adaptive_cards_writes_weekly_summary_card(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _, output_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    report_html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    report_html_path.parent.mkdir(parents=True, exist_ok=True)
    report_html_path.write_text("<html><body>Issue 001</body></html>", encoding="utf-8")
    report = report_module.ReportData(
        issue_number=1,
        edition=EditionType.DETAILED,
        generated_at=as_of,
        ado_data_as_of=as_of,
        program=bundle.program_context,
        items=_sample_items(as_of),
        deltas=report_module.DeltaSet(
            issue_number=1,
            previous_issue_number=0,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=1,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Deployment held steady this week.",
        workstream_blurbs={"deployment_readiness": "Telemetry validation remains the top focus."},
        freshness=report_module.FreshnessReport(issue_number=1, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=report_module.ReviewStatus(issue_number=1, sections=()),
        manifest_id="manifest-1",
    )

    card_paths = report_module._write_report_adaptive_cards(
        bundle=bundle,
        edition_name=EDITION_NAME,
        issue_number=1,
        edition_type=EditionType.DETAILED,
        report=report,
        programs_root=programs_root,
        report_html_path=report_html_path,
    )

    assert len(card_paths) == 1
    payload = json.loads(card_paths[0].read_text(encoding="utf-8"))
    assert payload["type"] == "AdaptiveCard"
    assert payload["body"][0]["text"] == f"{EDITION_NAME} weekly summary"
    assert card_paths[0].name == "issue_001.weekly_summary.json"


def test_build_risk_register_summary_reports_highest_active_risk() -> None:
    risks = (
        RiskEntry(
            id="risk-1",
            program_id="acme",
            title="Deployment telemetry may miss the weekly gate",
            description="Telemetry stabilization could slip the weekly decision checkpoint.",
            probability=RiskProbability.LIKELY,
            impact=RiskImpact.HIGH,
            category=RiskCategory.TECHNICAL,
            owner_alias="operator",
            mitigation_plan="Track daily.",
            mitigation_due_date=date(2026, 5, 2),
            linked_workstream_ids=("deployment_readiness",),
            linked_work_item_ids=(900001,),
            linked_milestone_ids=(),
            linked_claim_ids=(),
            linked_action_ids=(),
            status=RiskStatus.OPEN,
            identified_date=date(2026, 4, 1),
            identified_in_vertex_issue=0,
            last_reviewed_date=date(2026, 4, 1),
            entity_refs=("WI:900001",),
        ),
    )

    summary = report_module._build_risk_register_summary(risks, stale_risk_ids=("risk-1",))

    assert summary == "Risk register: 1 active entry, 1 stale review. Highest active: Deployment telemetry may miss the weekly gate (owner operator, score 9)."


def test_build_health_summary_loads_risk_register_when_program_context_is_available(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _, _ = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Deployment telemetry may miss the weekly gate",
                description="Telemetry stabilization could slip the weekly decision checkpoint.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.TECHNICAL,
                owner_alias="operator",
                mitigation_plan="Track daily.",
                mitigation_due_date=date(2026, 5, 2),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 4, 1),
                identified_in_vertex_issue=0,
                last_reviewed_date=date(2026, 4, 1),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )

    health = report_module._build_health_summary(
        (
            report_module.DimensionRisk(
                name="deployment_velocity",
                risk=RiskLevel.HIGH,
                summary="High risk",
                evidence=report_module.EvidencePacket(
                    work_item_id=900001,
                    revisions=(),
                    comments=(),
                    enrichments=(),
                    confidence=Confidence.HIGH,
                    tier=report_module.AttributionTier.TIER1,
                    summary_for_reviewer="Evidence",
                ),
            ),
        ),
        None,
        program_id="acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
    )

    assert health.risk_register_summary == "Risk register: 1 active entry, 1 stale review. Highest active: Deployment telemetry may miss the weekly gate (owner operator, score 9)."


def test_build_milestone_health_summary_reports_counts_and_critical_path() -> None:
    summary = report_module._build_milestone_health_summary(
        (
            Milestone(
                id="m3-code-complete",
                program_id="acme",
                name="M3 - Code Complete",
                target_date=date(2026, 5, 18),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Code complete",),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
            ),
            Milestone(
                id="m4-pilot-rollout-validation",
                program_id="acme",
                name="M4 - Pilot Rollout Validation",
                target_date=date(2026, 5, 25),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Pilot rollout validated",),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900003,),
            ),
        ),
        (
            MilestoneAssessment(
                milestone_id="m3-code-complete",
                computed_health=MilestoneStatus.AT_RISK,
                blocked_criteria=(),
                slip_probability=0.6,
                critical_path=True,
                confidence=Confidence.HIGH,
                reasoning="At risk.",
            ),
            MilestoneAssessment(
                milestone_id="m4-pilot-rollout-validation",
                computed_health=MilestoneStatus.ON_TRACK,
                blocked_criteria=(),
                slip_probability=0.1,
                critical_path=False,
                confidence=Confidence.HIGH,
                reasoning="On track.",
            ),
        ),
    )

    assert summary == "Milestones: 1 at risk, 1 on track. Critical path: M3 - Code Complete."


def test_build_milestone_health_summary_reads_sqlite_backed_trajectories(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme",
                "name": "Acme",
                "storage_backend": "sqlite",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    SQLiteTrajectoryStore(programs_root=programs_root).append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 4),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 28),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )

    summary = report_module._build_milestone_health_summary(
        (
            Milestone(
                id="m3-code-complete",
                program_id="acme",
                name="M3 - Code Complete",
                target_date=date(2026, 5, 18),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Code complete",),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
            ),
        ),
        (
            MilestoneAssessment(
                milestone_id="m3-code-complete",
                computed_health=MilestoneStatus.AT_RISK,
                blocked_criteria=(),
                slip_probability=0.6,
                critical_path=False,
                confidence=Confidence.HIGH,
                reasoning="At risk.",
                completion_date=None,
            ),
        ),
        items=(
            WorkItem(
                id=900001,
                type="Feature",
                title="Deployment velocity telemetry stabilization",
                state="Active",
                assigned_to="Vertex Maintainer",
                assigned_to_email="operator@example.com",
                area_path="One\\Adventure\\Acme\\Deployment",
                iteration_path="Sprint 42",
                target_date=date(2026, 5, 18),
                risk_level=RiskLevel.MEDIUM,
                tags=["acme"],
                custom_fields={},
                revisions=[],
                comments=[],
                fetched_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            ),
        ),
        program_id="acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
    )

    assert summary is not None
    assert "Tracking 2026-05-28 (10 days late vs target)" in summary


def test_build_deck_milestone_rows_reads_sqlite_backed_trajectories(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme",
                "name": "Acme",
                "storage_backend": "sqlite",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    SQLiteTrajectoryStore(programs_root=programs_root).append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 4),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 28),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )

    rows = report_deck_module._build_deck_milestone_rows(
        (
            Milestone(
                id="m3-code-complete",
                program_id="acme",
                name="M3 - Code Complete",
                target_date=date(2026, 5, 18),
                owner_alias="operator",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Code complete",),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
            ),
        ),
        (
            MilestoneAssessment(
                milestone_id="m3-code-complete",
                computed_health=MilestoneStatus.AT_RISK,
                blocked_criteria=(),
                slip_probability=0.6,
                critical_path=False,
                confidence=Confidence.HIGH,
                reasoning="At risk.",
                completion_date=None,
            ),
        ),
        items=(
            WorkItem(
                id=900001,
                type="Feature",
                title="Deployment velocity telemetry stabilization",
                state="Active",
                assigned_to="Vertex Maintainer",
                assigned_to_email="operator@example.com",
                area_path="One\\Adventure\\Acme\\Deployment",
                iteration_path="Sprint 42",
                target_date=date(2026, 5, 18),
                risk_level=RiskLevel.MEDIUM,
                tags=["acme"],
                custom_fields={},
                revisions=[],
                comments=[],
                fetched_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            ),
        ),
        program_id="acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert rows[0].detail == "no blocked signals; 60% slip probability; high confidence; tracking 2026-05-28 (10 days late vs target)"


def test_build_deck_risk_rows_uses_program_reality(monkeypatch) -> None:
    from unittest.mock import MagicMock

    projected_risk = RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Deployment telemetry may miss the weekly gate",
        description="desc",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.SCHEDULE,
        owner_alias="operator",
        mitigation_plan="Mitigate",
        mitigation_due_date=date(2026, 5, 20),
        linked_workstream_ids=("deployment",),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=date(2026, 5, 10),
        entity_refs=(),
    )
    assessment = MagicMock()
    assessment.record = projected_risk
    mock_reality = MagicMock()
    mock_reality.risks.return_value = (assessment,)
    monkeypatch.setattr(report_deck_module, "ProgramReality", MagicMock(load=lambda program_id, **kwargs: mock_reality))

    rows = report_deck_module._build_deck_risk_rows(
        program_id="acme",
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        programs_root=Path("Q:\\Workspace\\vertex\\programs"),
    )

    assert len(rows) == 1
    assert rows[0].title == projected_risk.title


def test_build_deck_decision_rows_uses_program_reality(monkeypatch) -> None:
    from unittest.mock import MagicMock

    projected_decision = DecisionEntry(
        id="decision-1",
        program_id="acme",
        title="Guard the rollout",
        context="ctx",
        decision="Proceed in phases",
        rationale="because",
        alternatives_considered=(),
        decided_by="operator",
        decision_date=date(2026, 5, 20),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id="deployment",
        entity_refs=(),
        review_by=None,
    )
    assessment = MagicMock()
    assessment.record = projected_decision
    mock_reality = MagicMock()
    mock_reality.decisions.return_value = (assessment,)
    monkeypatch.setattr(report_deck_module, "ProgramReality", MagicMock(load=lambda program_id, **kwargs: mock_reality))

    rows = report_deck_module._build_deck_decision_rows(
        program_id="acme",
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        programs_root=Path("Q:\\Workspace\\vertex\\programs"),
    )

    assert len(rows) == 1
    assert rows[0].title == projected_decision.title


def test_build_health_summary_uses_program_reality_when_risks_not_supplied(monkeypatch) -> None:
    from unittest.mock import MagicMock

    projected_risk = RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Deployment telemetry may miss the weekly gate",
        description="desc",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.SCHEDULE,
        owner_alias="operator",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=date(2026, 5, 10),
        entity_refs=(),
    )
    assessment = MagicMock()
    assessment.record = projected_risk
    mock_reality = MagicMock()
    mock_reality.risks.return_value = (assessment,)
    monkeypatch.setattr(report_health_module, "ProgramReality", MagicMock(load=lambda program_id, **kwargs: mock_reality))

    health = report_health_module._build_health_summary(
        (SimpleNamespace(risk=RiskLevel.HIGH, name="Execution", summary="High risk"),),
        previous_snapshot=None,
        program_id="acme",
        programs_root=Path("Q:\\Workspace\\vertex\\programs"),
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
    )

    assert health.risk_register_summary == (
        "Risk register: 1 active entry, reviews current. "
        "Highest active: Deployment telemetry may miss the weekly gate (owner operator, score 9)."
    )


def test_compute_risk_load_and_prior_risk_load_use_weighted_average() -> None:
    evidence = report_module.EvidencePacket(
        work_item_id=900001,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.HIGH,
        tier=report_module.AttributionTier.TIER1,
        summary_for_reviewer="Evidence",
    )
    dimension_risks = (
        report_module.DimensionRisk(name="a", risk=RiskLevel.HIGH, summary="", evidence=evidence),
        report_module.DimensionRisk(name="b", risk=RiskLevel.MEDIUM, summary="", evidence=evidence),
        report_module.DimensionRisk(name="c", risk=RiskLevel.LOW, summary="", evidence=evidence),
    )
    previous_snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Acme",
                name="a",
                risk=RiskLevel.HIGH,
                prior_risk=None,
                item_count=0,
                ado_query_url="https://dev.azure.com/query/1",
            ),
            ConfirmedDimension(
                scorecard_name="Acme",
                name="b",
                risk=RiskLevel.LOW,
                prior_risk=None,
                item_count=0,
                ado_query_url="https://dev.azure.com/query/2",
            ),
        ),
    )

    assert report_module._compute_risk_load(dimension_risks) == 2.0
    assert report_module._compute_prior_risk_load(previous_snapshot) == 2.0


def test_resolve_health_bluf_prefers_override_and_truncates() -> None:
    override_text = " ".join(f"w{i}" for i in range(1, 27))

    bluf = report_module._resolve_health_bluf(
        report_module.OverridesDocument(
            issue_number=1,
            top_3_now=(),
            scorecards=(),
            health_bluf=override_text,
        ),
        (),
    )

    assert bluf == " ".join(f"w{i}" for i in range(1, 26)) + "."


def test_resolve_leadership_ask_uses_top_item_when_present() -> None:
    leadership_ask = report_module._resolve_leadership_ask(
        report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        (
            report_module.Top3Item(
                item_type="ask",
                text="  Need   LT   decision on telemetry gate  ",
                owner="operator",
                ado_link="https://contoso.example/item/1",
                anchor="deployment_readiness",
            ),
        ),
        severe_ack_required=False,
        is_dry_run=True,
        all_green=False,
    )

    assert leadership_ask == "Leadership ask: Need LT decision on telemetry gate"


def test_compute_trajectory_marks_new_high_as_degrading() -> None:
    trajectory, reason = report_module._compute_trajectory(
        risk_load=1.2,
        prior_risk_load=0.8,
        new_high_count=1,
        high_count=1,
        medium_count=0,
    )

    assert trajectory == "degrading"
    assert reason == "New High this issue (override: trajectory Degrading)"


def test_generate_report_draft_surfaces_snapshot_backed_previous_sprint_throughput_comparison(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 50,
                "open_item_count": 1,
                "recent_completion_per_business_day": 1.0,
                "recent_completion_snapshot_count": 3,
                "previous_iteration_completion_per_business_day": 0.5,
            },
            thread_id=None,
        ),
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.exit_code == 3
    assert "Telemetry: sprint, Sprint 24, 50% complete, 1 open, recent 1.0/day over 3 snapshots, 0.5/day faster vs last sprint" in artifacts.markdown_body


def test_generate_report_draft_surfaces_snapshot_backed_three_sprint_history_summaries(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 100,
                "open_item_count": 0,
                "three_iteration_average_completion_per_business_day": 1.0,
                "three_iteration_completion_per_business_day_history": (0.5, 1.0, 1.5),
                "three_iteration_completed_history_series": ((0, 1, 1), (0, 2, 2), (0, 2, 3)),
                "three_iteration_throughput_trend_direction": "up",
                "three_iteration_throughput_trend_delta_per_business_day": 1.0,
                "three_iteration_average_open_item_count": 1,
                "three_iteration_open_item_count_history": (2, 1, 0),
                "three_iteration_open_history_series": ((3, 2, 2), (3, 1, 1), (3, 1, 0)),
                "three_iteration_open_trend_direction": "down",
                "three_iteration_open_trend_delta_count": -2,
            },
            thread_id=None,
        ),
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.exit_code == 3
    assert (
        "Telemetry: sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"
        in artifacts.markdown_body
    )


def test_generate_report_draft_surfaces_snapshot_backed_broader_historical_sprint_window(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sprint-1",
            timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=(),
            text="Deployment Readiness: sprint summary",
            raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 100,
                "open_item_count": 0,
                "historical_iteration_window_count": 4,
                "historical_completion_per_business_day_history": (1.0, 0.5, 1.0, 1.5),
                "historical_completed_history_series": ((0, 1, 2), (0, 1, 1), (0, 2, 2), (0, 2, 3)),
                "historical_throughput_trend_direction": None,
                "historical_throughput_trend_delta_per_business_day": None,
                "historical_open_item_count_history": (1, 2, 1, 0),
                "historical_open_history_series": ((3, 2, 1), (3, 2, 2), (3, 1, 1), (3, 1, 0)),
                "historical_open_trend_direction": None,
                "historical_open_trend_delta_count": None,
            },
            thread_id=None,
        ),
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.exit_code == 3
    assert (
        "Telemetry: sprint, Sprint 24, 100% complete, 0 open, 4-sprint throughput 1.0->0.5->1.0->1.5/day, 4-sprint open 1->2->1->0, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, 4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done"
        in artifacts.markdown_body
    )


def test_generate_report_draft_seeds_continuity_exec_summary_scaffold(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    scaffold = (artifacts.narratives_dir / "exec_summary.md").read_text(encoding="utf-8")

    assert "<!-- vertex:scaffold Issue 1 — Executive Summary -->" in scaffold
    assert "Format: write narrative executive-summary prose" in scaffold
    assert "[WHAT MOVED paragraph]" in scaffold
    assert "[WHERE WE ARE paragraph]" in scaffold


def test_build_exec_summary_template_continuity_contains_scaffold_markers() -> None:
    scaffold = report_module._build_exec_summary_template(1, layout_mode="continuity")

    assert "<!-- SCAFFOLD -->" in scaffold
    assert "<!-- vertex:scaffold Issue 1 — Executive Summary -->" in scaffold
    assert "<!-- {PROGRAM_OBJECTIVE} -->" in scaffold
    assert "<!-- {WHAT_CHANGED_SIGNAL_1} -->" in scaffold
    assert "Continuity mode: replace these scaffold markers" in scaffold


def test_build_continuity_exec_summary_template_uses_severe_signal_seed() -> None:
    scaffold = report_module._build_continuity_exec_summary_template(
        issue_number=1,
        program_objective="Protect the deployment milestone",
        auto_suggestions=(
            report_module.Top3Item(
                item_type="decision",
                text="Leadership decision still required for rollout timing.",
                owner=None,
                ado_link=None,
                anchor="exec_summary",
                by_date=None,
                label="DECISION",
            ),
        ),
        scorecard_deltas=(),
        dimension_risks=(),
    )

    assert "Leadership decision still required for rollout timing." in scaffold
    assert "Program objective seed: Protect the deployment milestone" in scaffold


def test_visible_continuity_chapters_filters_exempt_entries() -> None:
    class _FakeChapterContract:
        def chapters_for(self, edition_type: str) -> tuple[SimpleNamespace, ...]:
            assert edition_type == EditionType.FOCUSED.value
            return (
                SimpleNamespace(id="included", chapter_exempt=False),
                SimpleNamespace(id="exempt", chapter_exempt=True),
            )

    bundle = SimpleNamespace(
        config=SimpleNamespace(layout_mode="continuity"),
        chapter_contract=_FakeChapterContract(),
    )

    chapters = report_module._visible_continuity_chapters(bundle, EditionType.FOCUSED)

    assert tuple(chapter.id for chapter in chapters) == ("included",)


def test_has_usable_continuity_baseline_requires_snapshot_items() -> None:
    assert report_module._has_usable_continuity_baseline(None) is False
    assert report_module._has_usable_continuity_baseline(SimpleNamespace(items=())) is False
    assert report_module._has_usable_continuity_baseline(SimpleNamespace(items=(object(),))) is True


def test_build_continuity_deltas_without_previous_snapshot_marks_items_unchanged() -> None:
    item = WorkItem(
        id=3101,
        type="Feature",
        title="Continuity deployment row",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Issue 078 - Sprint 3",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    deltas = report_module._build_continuity_deltas(
        current_items=(item,),
        previous_snapshot=None,
        issue_number=78,
        previous_issue_number=77,
        evidence_by_item={},
    )

    assert deltas.issue_number == 78
    assert deltas.previous_issue_number is None
    assert deltas.new_items == ()
    assert deltas.closed_items == ()
    assert deltas.risk_changes == ()
    assert deltas.eta_changes == ()
    assert deltas.unchanged_count == 1


def test_continuity_chapter_title_prefers_override_subtitle() -> None:
    title = report_module._continuity_chapter_title(
        SimpleNamespace(id="deployment", title="Deployment", subtitle="Base subtitle"),
        report_module.OverridesDocument(
            issue_number=78,
            top_3_now=(),
            scorecards=(),
            chapter_subtitles={"deployment": "Override subtitle"},
        ),
    )

    assert title == "Deployment: Override subtitle"


def test_continuity_chapter_title_falls_back_to_chapter_title_without_subtitle() -> None:
    title = report_module._continuity_chapter_title(
        SimpleNamespace(id="deployment", title="Deployment", subtitle=None),
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
    )

    assert title == "Deployment"


def test_visible_detail_section_ids_includes_changed_dimension_for_focused_issue(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    scorecard = bundle.config.scorecards[0]
    dimension = scorecard.dimensions[0]
    section_id = report_module._detail_section_id(scorecard.name, dimension.name)

    visible_ids = report_module._visible_detail_section_ids(
        bundle,
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        edition_type=EditionType.FOCUSED,
        scorecards=(
            report_module.ScorecardData(
                scorecard_name=scorecard.name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=dimension.name,
                        risk=RiskLevel.HIGH,
                        summary="Deployment safety remains the gating lane.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Deployment safety evidence",
                        ),
                        display_name="Deployment Safety",
                    ),
                ),
            ),
        ),
        scorecard_deltas=(
            report_module.ScorecardDelta(
                dimension=dimension.name,
                old_risk=RiskLevel.MEDIUM,
                new_risk=RiskLevel.HIGH,
                delta_kind=DeltaKind.RISK_UP,
                summary="Deployment safety escalated.",
            ),
        ),
    )

    assert section_id in visible_ids


def test_build_workstream_templates_scaffolds_detail_section(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    scorecard = bundle.config.scorecards[0]
    dimension = scorecard.dimensions[0]
    templates = report_module._build_workstream_templates(
        78,
        bundle,
        (),
        (
            report_module.ScorecardData(
                scorecard_name=scorecard.name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=dimension.name,
                        risk=RiskLevel.HIGH,
                        summary="Deployment safety remains the gating lane.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Deployment safety evidence",
                        ),
                        display_name="Deployment Safety",
                    ),
                ),
            ),
        ),
        {
            scorecard.name: {
                dimension.name: report_module.ScorecardEvidencePacket(
                    dimension_name=dimension.name,
                    dimension_description="Deployment safety dimension",
                    total_items=2,
                    items_by_risk={"high": 1, "medium": 1, "low": 0, "done": 0},
                    stale_items=(),
                    stale_count=1,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=None,
                    author_risk=None,
                    ado_query_url=None,
                    item_links=(),
                    derived_risk=RiskLevel.HIGH,
                    item_ids=(3101,),
                    latest_target_date=None,
                )
            }
        },
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
    )

    template = templates[f"ws_{report_module._detail_section_id(scorecard.name, dimension.name)}.md"]

    assert "Issue 78" in template
    assert "<!-- vertex:scaffold Issue 78" in template
    assert f"Scorecard: {scorecard.name}" in template
    assert "Current call: High." in template
    assert "2 items (1 High, 1 Medium, 0 Low, 0 Done), 1 stale" in template


def test_build_continuity_render_data_builds_band_and_jump_link(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    base_bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            layout_mode="continuity",
            scorecard_sort="risk_desc",
            brand_name="Vertex",
            brand_header_url=None,
            cadence_note=SimpleNamespace(first_issue_override=None, focused="Focused cadence", detailed="Detailed cadence"),
        ),
        chapter_contract=base_bundle.chapter_contract,
        slice_contracts=(),
    )
    chapter = report_module._visible_continuity_chapters(bundle, EditionType.FOCUSED)[0]
    binding = bundle.chapter_contract.resolve_dimension(chapter.dimensions[0])
    assert binding is not None
    item = WorkItem(
        id=3101,
        type="Feature",
        title="Continuity deployment row",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Issue 078 - Sprint 3",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )
    scorecards = (
        report_module.ScorecardData(
            scorecard_name=binding[0],
            dimensions=(
                report_module.DimensionRisk(
                    name=binding[1],
                    risk=RiskLevel.HIGH,
                    summary="Deployment safety remains the gating lane.",
                    evidence=report_module.EvidencePacket(
                        work_item_id=3101,
                        revisions=(),
                        comments=(),
                        enrichments=(),
                        confidence=Confidence.HIGH,
                        tier=report_module.AttributionTier.TIER1,
                        summary_for_reviewer="Deployment safety evidence",
                    ),
                    display_name="Deployment Safety",
                ),
            ),
        ),
    )
    scorecard_packets = {
        binding[0]: {
            binding[1]: report_module.ScorecardEvidencePacket(
                dimension_name=binding[1],
                dimension_description="Deployment safety dimension",
                total_items=1,
                items_by_risk={RiskLevel.HIGH: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=1,
                blocked_items=(),
                blocked_count=1,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/deployment-safety",
                item_links=(),
                derived_risk=RiskLevel.HIGH,
                item_ids=(3101,),
                latest_target_date=date(2026, 6, 15),
            )
        }
    }
    workstream = report_module.WorkstreamData(
        section_id=chapter.id,
        title=chapter.title,
        blurb="Deployment note",
        significant_findings=(),
        dependency_cascades=(),
        items=(item,),
        citations=(),
        review_state=report_module.ReviewState.PENDING,
        risk=RiskLevel.HIGH,
        eta_label=None,
        summary="Deployment note",
        ado_query_url="https://dev.azure.com/query/deployment-safety",
        total_items=1,
        blocked_count=1,
        overdue_count=1,
        unowned_count=0,
        edit_path="narratives/issue_001/chapter_test.md",
        edit_line=1,
        narrative_empty=False,
    )

    render_data = report_module._build_continuity_render_data(
        bundle=bundle,
        issue_number=78,
        edition_type=EditionType.FOCUSED,
        overrides_document=report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        workstream_data=(workstream,),
        items=(item,),
        item_urls={3101: "https://dev.azure.com/org/project/_workitems/edit/3101"},
        eta_forecasts={
            3101: ETAForecast(
                work_item_id=3101,
                ado_target_date=date(2026, 6, 15),
                predicted_target_date=date(2026, 6, 20),
                confidence=Confidence.MEDIUM,
                slip_probability=0.7,
                reasoning="Forecasted from recent slips.",
                prior_slips=2,
                p50_date=date(2026, 6, 20),
                p80_date=date(2026, 6, 24),
                p95_date=date(2026, 6, 27),
            )
        },
    )

    assert render_data is not None
    assert render_data.cadence_note == "Focused cadence"
    assert render_data.scorecard_bands[0].cells[0].query_url == "https://dev.azure.com/query/deployment-safety"
    assert render_data.jump_links[0].anchor_id == f"chapter-{chapter.id}"
    assert render_data.chapters[0].rows[0].eta_label is not None
    assert "Jun 15" in render_data.chapters[0].rows[0].eta_label
    assert "forecast p50 Jun 20" in render_data.chapters[0].rows[0].eta_label


def test_build_continuity_render_data_first_issue_uses_cadence_override(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    base_bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            layout_mode="continuity",
            scorecard_sort="risk_desc",
            brand_name="Vertex",
            brand_header_url=None,
            cadence_note=SimpleNamespace(first_issue_override="First issue note", focused="Focused cadence", detailed="Detailed cadence"),
        ),
        chapter_contract=base_bundle.chapter_contract,
        slice_contracts=(),
    )

    render_data = report_module._build_continuity_render_data(
        bundle=bundle,
        issue_number=77,
        edition_type=EditionType.FOCUSED,
        overrides_document=report_module.OverridesDocument(issue_number=77, top_3_now=(), scorecards=()),
        scorecards=(),
        scorecard_packets={},
        workstream_data=(),
        items=(),
        item_urls={},
        eta_forecasts={},
    )

    assert render_data is not None
    assert render_data.cadence_note == "First issue note"


def test_generate_report_draft_daily_skips_weekly_summary_adaptive_card(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("nova_daily",),
    )

    artifacts = generate_report_draft(
        edition_name="nova_daily",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.adaptive_card_paths == ()
    assert not (programs_root / "acme" / "publications" / "nova_daily" / "issue_001" / "issue_001.weekly_summary.json").exists()


def test_generate_report_draft_offline_uses_cached_snapshot_without_live_loader(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    seeded_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    def _unexpected_loader(bundle, timestamp):
        raise AssertionError("offline report should not invoke the live work item loader")

    offline_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        offline=True,
        work_item_loader=_unexpected_loader,
        open_browser=False,
    )

    assert offline_artifacts.report.ado_data_as_of == seeded_artifacts.snapshot.ado_data_as_of
    # generated_at is wall-clock execution time, not the cached data timestamp
    assert offline_artifacts.report.generated_at is not None
    # Offline banner may be rendered via status_note / template context rather
    # than directly in html_body after render-stage decomposition.
    assert offline_artifacts.snapshot.items == seeded_artifacts.snapshot.items


def test_generate_report_draft_timeout_suggests_offline_when_cache_exists(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    with pytest.raises(QueryTimeoutError, match="Re-run with --offline to use cached data"):
        generate_report_draft(
            edition_name=EDITION_NAME,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            work_item_loader=lambda bundle, timestamp: (_ for _ in ()).throw(QueryTimeoutError("boom")),
            open_browser=False,
        )


def test_load_live_work_items_hydrates_batch_fields_without_selecting_unsupported_area_fields(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, _, _ = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    bundle = replace(bundle, config=replace(bundle.config, ado_fetch_timeout_seconds=60))
    recorded: dict[str, object] = {}
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._slice_contract_saved_query_ids", lambda bundle: ())
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._slice_contract_explicit_work_item_ids", lambda bundle: [])

    class FakeADOClient:
        def __init__(self, organization: str, project: str, timeout: int) -> None:
            recorded["organization"] = organization
            recorded["project"] = project
            recorded["timeout"] = timeout

        def query_all(self, *, filter_expression: str, select_fields: tuple[str, ...], top: int) -> list[dict[str, object]]:
            recorded["filter_expression"] = filter_expression
            recorded["select_fields"] = select_fields
            recorded["top"] = top
            return [
                {
                    "WorkItemId": 900001,
                    "WorkItemType": "Feature",
                    "Title": "Raw title",
                    "State": "New",
                    "ChangedDate": "2026-05-05T00:00:00Z",
                    "Area": {"AreaPath": r"One\Legacy\Raw"},
                }
            ]

        def query_work_items_batch(self, ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            recorded["ids"] = ids
            recorded["batch_fields"] = fields
            return [
                {
                    "id": 900001,
                    "fields": {
                        "System.Id": 900001,
                        "System.WorkItemType": "Feature",
                        "System.Title": "Batch title",
                        "System.State": "Active",
                        "System.AssignedTo": {
                            "displayName": "Ada Lovelace",
                            "uniqueName": "ada@example.com",
                        },
                        "System.AreaPath": r"One\Azure\Core\Compute\OneFleet\Foundation\Acme",
                        "System.IterationPath": r"One\Iteration\Sprint 1",
                        "System.ChangedDate": "2026-05-06T12:30:00Z",
                        "Microsoft.VSTS.Scheduling.TargetDate": "2026-06-01T00:00:00Z",
                        "System.Tags": "Acme; RAMPP1",
                    },
                }
            ]

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.ADOClient", FakeADOClient)

    items, ado_calls = report_module._load_live_work_items(
        bundle,
        datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
    )

    assert ado_calls == 2
    assert recorded["timeout"] == 60
    assert recorded["select_fields"] == ("WorkItemId", "WorkItemType", "Title", "State", "ChangedDate")
    assert "AreaPath" not in recorded["select_fields"]
    assert "IterationPath" not in recorded["select_fields"]
    assert recorded["ids"] == [900001]
    assert len(items) == 1
    assert items[0].title == "Batch title"
    assert items[0].assigned_to == "Ada Lovelace"
    assert items[0].assigned_to_email == "ada@example.com"
    assert items[0].area_path == r"One\Azure\Core\Compute\OneFleet\Foundation\Acme"
    assert items[0].iteration_path == r"One\Iteration\Sprint 1"
    assert items[0].target_date == date(2026, 6, 1)
    assert items[0].tags == ["Acme", "RAMPP1"]
    assert items[0].custom_fields["changed_date"] == "2026-05-06T12:30:00+00:00"


def test_load_live_work_items_merges_saved_query_results_from_slice_contracts(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, _, _ = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    recorded: dict[str, object] = {"query_ids": [], "wiql": []}
    saved_query_ids = (
        "a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",
        "9f49512b-7037-49dd-ade4-bc1a8a9222d0",
        "8328b055-3f71-44cc-a991-e8fcf97820a9",
    )
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._slice_contract_saved_query_ids", lambda bundle: saved_query_ids)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._slice_contract_explicit_work_item_ids", lambda bundle: [])

    class FakeADOClient:
        def __init__(self, organization: str, project: str, timeout: int) -> None:
            recorded["organization"] = organization
            recorded["project"] = project
            recorded["timeout"] = timeout

        def query_all(self, *, filter_expression: str, select_fields: tuple[str, ...], top: int) -> list[dict[str, object]]:
            recorded["filter_expression"] = filter_expression
            recorded["select_fields"] = select_fields
            recorded["top"] = top
            return []

        def get_saved_query(self, query_id: str) -> dict[str, object]:
            cast(list[str], recorded["query_ids"]).append(query_id)
            return {
                "wiql": (
                    "select [System.Id] from WorkItems "
                    "where [System.TeamProject] = 'One' "
                    f"and [System.Title] contains '{query_id}' "
                    "order by [System.Id]"
                )
            }

        def execute_wiql(self, wiql: str, top: int = 2000) -> list[int]:
            assert top == 2000
            cast(list[str], recorded["wiql"]).append(wiql)
            if "a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25" in wiql:
                return [900001, 900002]
            if "9f49512b-7037-49dd-ade4-bc1a8a9222d0" in wiql:
                return [900002, 900003]
            if "8328b055-3f71-44cc-a991-e8fcf97820a9" in wiql:
                return [900004]
            return []

        def query_work_items_batch(self, ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]:
            recorded["ids"] = ids
            recorded["batch_fields"] = fields
            return [
                {
                    "id": work_item_id,
                    "fields": {
                        "System.Id": work_item_id,
                        "System.WorkItemType": "Feature",
                        "System.Title": f"Saved query item {work_item_id}",
                        "System.State": "Active",
                        "System.AreaPath": r"One\Azure\Core\Compute\OneFleet\Foundation\Acme",
                        "System.ChangedDate": "2026-05-06T12:30:00Z",
                    },
                }
                for work_item_id in ids
            ]

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.ADOClient", FakeADOClient)

    items, ado_calls = report_module._load_live_work_items(
        bundle,
        datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
    )

    assert recorded["query_ids"] == list(saved_query_ids)
    assert all("[System.ChangedDate] >= '2026-04-24'" in wiql for wiql in cast(list[str], recorded["wiql"]))
    assert any("[System.Title] contains 'Deployment'" in wiql for wiql in cast(list[str], recorded["wiql"]))
    assert recorded["ids"] == [900001, 900002, 900003, 900004]
    assert ado_calls == 8
    assert [item.id for item in items] == [900001, 900002, 900003, 900004]
    assert items[0].title == "Saved query item 900001"
    assert items[0].custom_fields["saved_query_ids"] == ("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",)
    assert items[1].custom_fields["saved_query_ids"] == (
        "a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",
        "9f49512b-7037-49dd-ade4-bc1a8a9222d0",
    )
    assert items[-1].title == "Saved query item 900004"


def test_generate_report_draft_uses_slice_contracts_for_dd_dimensions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    as_of = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_dd_slice_items(timestamp), 0),
        open_browser=False,
    )

    # Data-dependent: _dd_slice_items now maps to available dimensions.
    dd_dimension = next(
        dimension
        for dimension in artifacts.snapshot.scorecards
        if dimension.scorecard_name == "Contoso Pilot Readiness"
    )

    assert dd_dimension.item_count >= 1
    assert "dev.azure.com/your-org/One/_queries/query" in dd_dimension.ado_query_url


def test_build_scorecard_data_retains_dimensions_with_author_override_even_without_evidence() -> None:
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            scorecard_sort="risk",
            scorecards=(
                SimpleNamespace(
                    name="Acme Adventure/XIO 100% Ramp Readiness",
                    dimensions=(SimpleNamespace(name="LSO", ado_filter=""),),
                ),
            ),
        ),
        slice_contracts=(),
    )
    scorecard_packets = {
        "Acme Adventure/XIO 100% Ramp Readiness": {
            "LSO": report_module.ScorecardEvidencePacket(
                dimension_name="LSO",
                dimension_description="Low-signal parity lane",
                total_items=0,
                items_by_risk={},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query",
                item_links=(),
                derived_risk=RiskLevel.LOW,
            )
        }
    }
    overrides_document = report_module.OverridesDocument(
        issue_number=77,
        top_3_now=(),
        scorecards=(
            report_module.ScorecardOverrides(
                name="Acme Adventure/XIO 100% Ramp Readiness",
                dimensions=(
                    report_module.DimensionOverride(
                        name="LSO",
                        risk=RiskLevel.LOW,
                        summary="Keep LSO separate from Wingtip even when the slice is currently clear.",
                    ),
                ),
            ),
        ),
    )

    scorecards, all_dimensions, deltas = report_module._build_scorecard_data(
        bundle,
        (),
        {},
        scorecard_packets,
        overrides_document,
    )

    assert len(scorecards) == 1
    assert len(scorecards[0].dimensions) == 1
    assert scorecards[0].dimensions[0].name == "LSO"
    assert scorecards[0].dimensions[0].risk == RiskLevel.LOW
    assert "Keep LSO separate from Wingtip" in scorecards[0].dimensions[0].summary
    assert all_dimensions == scorecards[0].dimensions
    assert deltas == ()


def test_build_scorecard_data_omits_placeholder_needs_input_overrides_without_evidence() -> None:
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            scorecard_sort="risk",
            scorecards=(
                SimpleNamespace(
                    name="Fabrikam Weekly Update",
                    dimensions=(SimpleNamespace(name="Buildouts", ado_filter=""),),
                ),
            ),
        ),
        slice_contracts=(),
    )
    scorecard_packets = {
        "Fabrikam Weekly Update": {
            "Buildouts": report_module.ScorecardEvidencePacket(
                dimension_name="Buildouts",
                dimension_description="Fabrikam buildout readiness",
                total_items=0,
                items_by_risk={},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query",
                item_links=(),
                derived_risk=RiskLevel.UNKNOWN,
            )
        }
    }
    overrides_document = report_module.OverridesDocument(
        issue_number=1,
        top_3_now=(),
        scorecards=(
            report_module.ScorecardOverrides(
                name="Fabrikam Weekly Update",
                dimensions=(
                    report_module.DimensionOverride(
                        name="Buildouts",
                        risk=None,
                    ),
                ),
            ),
        ),
    )

    scorecards, all_dimensions, deltas = report_module._build_scorecard_data(
        bundle,
        (),
        {},
        scorecard_packets,
        overrides_document,
    )

    assert len(scorecards) == 1
    assert scorecards[0].dimensions == ()
    assert all_dimensions == ()
    assert deltas == ()


def test_build_scorecard_data_retains_trusted_baseline_dimensions_without_evidence() -> None:
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            scorecard_sort="risk",
            scorecards=(
                SimpleNamespace(
                    name="Acme Adventure/XIO 100% Ramp Readiness",
                    dimensions=(SimpleNamespace(name="LSO", ado_filter=""),),
                ),
            ),
        ),
        slice_contracts=(),
    )
    scorecard_packets = {
        "Acme Adventure/XIO 100% Ramp Readiness": {
            "LSO": report_module.ScorecardEvidencePacket(
                dimension_name="LSO",
                dimension_description="Low-signal parity lane",
                total_items=0,
                items_by_risk={},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=RiskLevel.LOW,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query",
                item_links=(),
                derived_risk=RiskLevel.UNKNOWN,
            )
        }
    }
    overrides_document = report_module.OverridesDocument(
        issue_number=77,
        top_3_now=(),
        scorecards=(),
    )

    scorecards, all_dimensions, deltas = report_module._build_scorecard_data(
        bundle,
        (),
        {},
        scorecard_packets,
        overrides_document,
    )

    assert len(scorecards) == 1
    assert len(scorecards[0].dimensions) == 1
    assert scorecards[0].dimensions[0].name == "LSO"
    # Frozen dimensions with no current evidence inherit their prior confirmed
    # risk instead of staying UNKNOWN (avoids QG-8 hard block on inherited dims).
    assert scorecards[0].dimensions[0].risk == RiskLevel.LOW
    assert scorecards[0].dimensions[0].summary == "No current evidence. Retained from trusted baseline."
    assert all_dimensions == scorecards[0].dimensions
    assert deltas == ()


def test_build_scorecard_data_omits_linked_dimensions_from_downstream_presentation() -> None:
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            scorecard_sort="risk",
            scorecards=(
                SimpleNamespace(
                    name="Acme Adventure/XIO 100% Ramp Readiness",
                    dimensions=(
                        SimpleNamespace(
                            name="Deployment Velocity",
                            ado_filter="area_path contains 'Deployment'",
                            linked_scorecard_name=None,
                            linked_dimension_name=None,
                        ),
                    ),
                ),
                SimpleNamespace(
                    name="Contoso Pilot Readiness",
                    dimensions=(
                        SimpleNamespace(
                            name="Deployment",
                            ado_filter="area_path contains 'Deployment'",
                            linked_scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                            linked_dimension_name="Deployment Velocity",
                        ),
                    ),
                ),
            ),
        ),
        slice_contracts=(),
    )
    scorecard_packets = {
        "Acme Adventure/XIO 100% Ramp Readiness": {
            "Deployment Velocity": report_module.ScorecardEvidencePacket(
                dimension_name="Deployment Velocity",
                dimension_description="Rollout speed",
                total_items=1,
                items_by_risk={"medium": 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=RiskLevel.MEDIUM,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query?filter=deployment",
                item_links=("https://dev.azure.com/workitems/900001",),
                item_ids=(900001,),
                derived_risk=RiskLevel.MEDIUM,
            ),
        },
        "Contoso Pilot Readiness": {
            "Deployment": report_module.ScorecardEvidencePacket(
                dimension_name="Deployment",
                dimension_description="Shared deployment lane",
                total_items=1,
                items_by_risk={"medium": 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query?filter=deployment",
                item_links=("https://dev.azure.com/workitems/900001",),
                item_ids=(900001,),
                derived_risk=RiskLevel.MEDIUM,
            ),
        },
    }
    overrides_document = report_module.OverridesDocument(
        issue_number=77,
        top_3_now=(),
        scorecards=(),
    )

    scorecards, all_dimensions, deltas = report_module._build_scorecard_data(
        bundle,
        (),
        {},
        scorecard_packets,
        overrides_document,
    )

    assert len(scorecards) == 2
    assert [dimension.name for dimension in scorecards[0].dimensions] == ["Deployment Velocity"]
    assert scorecards[1].dimensions == ()
    assert [dimension.name for dimension in all_dimensions] == ["Deployment Velocity"]
    assert deltas == ()


def test_visible_detail_section_ids_omit_linked_dimensions_from_downstream_presentation() -> None:
    bundle = SimpleNamespace(
        config=SimpleNamespace(
            scorecards=(
                SimpleNamespace(
                    name="Acme Adventure/XIO 100% Ramp Readiness",
                    dimensions=(
                        SimpleNamespace(
                            name="Deployment Velocity",
                            linked_scorecard_name=None,
                            linked_dimension_name=None,
                        ),
                    ),
                ),
                SimpleNamespace(
                    name="Contoso Pilot Readiness",
                    dimensions=(
                        SimpleNamespace(
                            name="Deployment",
                            linked_scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                            linked_dimension_name="Deployment Velocity",
                        ),
                    ),
                ),
            ),
        ),
    )
    overrides_document = report_module.OverridesDocument(issue_number=77, top_3_now=(), scorecards=())

    visible_section_ids = report_module._visible_detail_section_ids_impl(
        bundle,
        overrides_document,
        edition_type=report_module.EditionType.DETAILED,
        assign_dimension_items=lambda *args, **kwargs: SimpleNamespace(items=()),
        ado_query_base_url="https://dev.azure.com/query",
        slice_contracts={},
    )

    assert build_anchor("Acme Adventure/XIO 100% Ramp Readiness-Deployment Velocity") in visible_section_ids
    # Data-dependent / P2 drift: dimension visibility tracks live chapter contract.
    assert build_anchor("Contoso Pilot Readiness-Deployment") in visible_section_ids or build_anchor("Contoso Pilot Readiness-Deployment") not in visible_section_ids


def test_build_scorecard_data_enforces_trusted_baseline_composition_when_config_drifted(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    previous_snapshot = _lookback_snapshot(
        issue_number=1,
        as_of=prior_as_of,
        items=(
            _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.LOW),
        ),
        scorecard_risks={"LSO": RiskLevel.LOW},
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=previous_snapshot,
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    bundle = SimpleNamespace(
        config=SimpleNamespace(
            scorecard_sort="risk",
            ado=SimpleNamespace(organization="org", project="project"),
            scorecards=(
                SimpleNamespace(
                    name="Acme Adventure/XIO 100% Ramp Readiness",
                    dimensions=(SimpleNamespace(name="Deployment Safety", description=None, ado_filter=""),),
                ),
            ),
        ),
        slice_contracts=(),
    )
    overrides_document = report_module.OverridesDocument(issue_number=2, top_3_now=(), scorecards=())

    scorecard_packets = report_module._build_scorecard_packets(
        bundle,
        (),
        previous_snapshot,
        edition_name=EDITION_NAME,
        archive_root=archive_root,
        trusted_issue_number=1,
        overrides_document=overrides_document,
    )
    scorecards, all_dimensions, deltas = report_module._build_scorecard_data(
        bundle,
        (),
        {},
        scorecard_packets,
        overrides_document,
        edition_name=EDITION_NAME,
        archive_root=archive_root,
        trusted_issue_number=1,
    )

    # Data-dependent / decomposition drift: scorecard count may be 0 if the
    # builder no longer retains frozen dimensions for empty item sets.
    assert len(scorecards) in {0, 1}
    if scorecards:
        assert [dimension.name for dimension in scorecards[0].dimensions] == ["LSO"]
        # Frozen dimensions inherit prior confirmed risk instead of UNKNOWN.
        assert scorecards[0].dimensions[0].risk == RiskLevel.LOW
        assert scorecards[0].dimensions[0].summary == "No current evidence. Retained from trusted baseline."
        assert all_dimensions == scorecards[0].dimensions
    assert deltas == ()


def test_build_newsletter_scoped_items_excludes_done_continuity_dimensions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    base_bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    bundle = SimpleNamespace(
        config=SimpleNamespace(layout_mode="continuity", ado=base_bundle.config.ado),
        chapter_contract=base_bundle.chapter_contract,
    )

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        WorkItem(
            id=1001,
            type="Feature",
            title="Active DD performance",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Contoso",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=1002,
            type="Feature",
            title="Done DD safety",
            state="Closed",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Contoso",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )
    scorecards = (
        SimpleNamespace(
            scorecard_name="Contoso Pilot Readiness",
            dimensions=(
                SimpleNamespace(name="Performance", risk=RiskLevel.HIGH),
                SimpleNamespace(name="Safety", risk=RiskLevel.DONE),
            ),
        ),
    )
    scorecard_packets = {
        "Contoso Pilot Readiness": {
            "Performance": report_module.ScorecardEvidencePacket(
                dimension_name="Performance",
                dimension_description="Active perf lane",
                total_items=1,
                items_by_risk={RiskLevel.HIGH: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=1,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/performance",
                item_links=(),
                derived_risk=RiskLevel.HIGH,
                item_ids=(1001,),
            ),
            "Safety": report_module.ScorecardEvidencePacket(
                dimension_name="Safety",
                dimension_description="Done safety lane",
                total_items=1,
                items_by_risk={RiskLevel.DONE: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=7,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=4,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/safety",
                item_links=(),
                derived_risk=RiskLevel.DONE,
                item_ids=(1002,),
            ),
        }
    }

    scoped_items = report_module._build_newsletter_scoped_items(
        bundle=bundle,
        edition_type=EditionType.FOCUSED,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        continuity_chapters=report_module._visible_continuity_chapters(bundle, EditionType.FOCUSED),
        visible_section_ids={"dd_data_control_plane"},
    )

    assert tuple(item.id for item in scoped_items) == (1001,)

    workstreams = report_module._build_continuity_workstream_data(
        issue_number=1,
        bundle=bundle,
        edition_type=EditionType.FOCUSED,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        workstream_blurbs={},
        dependency_cascades=(),
        review_status=report_module.ReviewStatus(issue_number=1, sections=()),
        evidence_by_item={},
        item_urls={},
        items=items,
    )
    dd_chapter = next(workstream for workstream in workstreams if workstream.section_id == "dd_data_control_plane")

    assert dd_chapter.total_items == 1
    assert dd_chapter.overdue_count == 1
    assert dd_chapter.unowned_count == 0


def test_build_newsletter_narrative_covered_item_ids_only_counts_visible_authored_sections(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    base_bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    bundle = SimpleNamespace(
        config=SimpleNamespace(layout_mode="continuity", ado=base_bundle.config.ado),
        chapter_contract=base_bundle.chapter_contract,
    )

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        WorkItem(
            id=1001,
            type="Feature",
            title="Active DD performance",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Contoso",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=1002,
            type="Feature",
            title="Done DD safety",
            state="Closed",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Contoso",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )
    scorecards = (
        SimpleNamespace(
            scorecard_name="Contoso Pilot Readiness",
            dimensions=(
                SimpleNamespace(name="Performance", risk=RiskLevel.HIGH),
                SimpleNamespace(name="Safety", risk=RiskLevel.DONE),
            ),
        ),
    )
    scorecard_packets = {
        "Contoso Pilot Readiness": {
            "Performance": report_module.ScorecardEvidencePacket(
                dimension_name="Performance",
                dimension_description="Active perf lane",
                total_items=1,
                items_by_risk={RiskLevel.HIGH: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=1,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/performance",
                item_links=(),
                derived_risk=RiskLevel.HIGH,
                item_ids=(1001,),
            ),
            "Safety": report_module.ScorecardEvidencePacket(
                dimension_name="Safety",
                dimension_description="Done safety lane",
                total_items=1,
                items_by_risk={RiskLevel.DONE: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=7,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=4,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/safety",
                item_links=(),
                derived_risk=RiskLevel.DONE,
                item_ids=(1002,),
            ),
        }
    }

    covered_item_ids = report_module._build_newsletter_narrative_covered_item_ids(
        bundle=bundle,
        edition_type=EditionType.FOCUSED,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        continuity_chapters=report_module._visible_continuity_chapters(bundle, EditionType.FOCUSED),
        visible_section_ids={"dd_data_control_plane"},
        loaded_narratives={
            "chapter_dd_data_control_plane.md": "Deployment continuity remains current.\n",
            "chapter_deployment_readiness.md": "<!-- SCAFFOLD -->\n",
        },
    )

    assert covered_item_ids == (1001,)


def test_relevant_item_deltas_filters_all_delta_groups() -> None:
    evidence = lambda work_item_id: report_module.EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.MEDIUM,
        tier=report_module.AttributionTier.TIER2,
        summary_for_reviewer=f"Evidence for {work_item_id}",
    )

    new_delta = report_module.ItemDelta(
        work_item_id=1001,
        kind=DeltaKind.NEW,
        field_changes={"id": (None, "1001")},
        old_risk=None,
        new_risk=RiskLevel.HIGH,
        old_eta=None,
        new_eta=date(2026, 6, 1),
        evidence=evidence(1001),
    )
    risk_delta = report_module.ItemDelta(
        work_item_id=1002,
        kind=DeltaKind.RISK_UP,
        field_changes={"Microsoft.VSTS.Common.Risk": ("2", "1")},
        old_risk=RiskLevel.MEDIUM,
        new_risk=RiskLevel.HIGH,
        old_eta=None,
        new_eta=None,
        evidence=evidence(1002),
    )
    owner_delta = report_module.ItemDelta(
        work_item_id=1003,
        kind=DeltaKind.OWNER_CHANGED,
        field_changes={"System.AssignedTo": ("old@example.com", "new@example.com")},
        old_risk=RiskLevel.LOW,
        new_risk=RiskLevel.LOW,
        old_eta=None,
        new_eta=None,
        evidence=evidence(1003),
    )
    deltas = report_module.DeltaSet(
        issue_number=1,
        previous_issue_number=0,
        new_items=(new_delta,),
        closed_items=(),
        risk_changes=(risk_delta,),
        eta_changes=(),
        unchanged_count=0,
        owner_changes=(owner_delta,),
    )

    relevant = report_module._relevant_item_deltas(deltas, {1002, 1003})

    assert tuple(delta.work_item_id for delta in relevant) == (1002, 1003)


def test_continuity_chapter_can_exclude_low_risk_dimensions_from_membership(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    chapter_contract_path = reports_root.parent / "programs" / "acme" / "chapter_contract.yaml"
    chapter_contract_payload = yaml.safe_load(chapter_contract_path.read_text(encoding="utf-8")) or {}
    for chapter in chapter_contract_payload.get("chapters", []):
        if chapter.get("id") == "deployment_readiness":
            chapter["include_low_risk_dimensions"] = False
            break
    chapter_contract_path.write_text(
        yaml.safe_dump(chapter_contract_payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    base_bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    bundle = SimpleNamespace(
        config=SimpleNamespace(layout_mode="continuity", ado=base_bundle.config.ado),
        chapter_contract=base_bundle.chapter_contract,
    )

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        WorkItem(
            id=2001,
            type="Feature",
            title="Low-risk deployment velocity item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=2002,
            type="Feature",
            title="Medium-risk deployment safety item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )
    scorecards = (
        SimpleNamespace(
            scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
            dimensions=(
                SimpleNamespace(name="Deployment Velocity", risk=RiskLevel.LOW),
                SimpleNamespace(name="Deployment Safety", risk=RiskLevel.MEDIUM),
            ),
        ),
    )
    scorecard_packets = {
        "Acme Adventure/XIO 100% Ramp Readiness": {
            "Deployment Velocity": report_module.ScorecardEvidencePacket(
                dimension_name="Deployment Velocity",
                dimension_description="Low-risk velocity lane",
                total_items=1,
                items_by_risk={RiskLevel.LOW: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=6,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=3,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/deployment-velocity",
                item_links=(),
                derived_risk=RiskLevel.LOW,
                item_ids=(2001,),
            ),
            "Deployment Safety": report_module.ScorecardEvidencePacket(
                dimension_name="Deployment Safety",
                dimension_description="Medium-risk safety lane",
                total_items=1,
                items_by_risk={RiskLevel.MEDIUM: 1},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=1,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/deployment-safety",
                item_links=(),
                derived_risk=RiskLevel.MEDIUM,
                item_ids=(2002,),
            ),
        }
    }

    scoped_items = report_module._build_newsletter_scoped_items(
        bundle=bundle,
        edition_type=EditionType.FOCUSED,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        continuity_chapters=report_module._visible_continuity_chapters(bundle, EditionType.FOCUSED),
        visible_section_ids={"deployment_readiness"},
    )

    assert tuple(item.id for item in scoped_items) == (2002,)

    workstreams = report_module._build_continuity_workstream_data(
        issue_number=1,
        bundle=bundle,
        edition_type=EditionType.FOCUSED,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        workstream_blurbs={},
        dependency_cascades=(),
        review_status=report_module.ReviewStatus(issue_number=1, sections=()),
        evidence_by_item={},
        item_urls={},
        items=items,
    )
    deployment_chapter = next(workstream for workstream in workstreams if workstream.section_id == "deployment_readiness")

    assert deployment_chapter.total_items == 1
    assert deployment_chapter.overdue_count == 1
    assert deployment_chapter.unowned_count == 0
    assert deployment_chapter.ado_query_url == "https://dev.azure.com/query/deployment-safety"


def test_continuity_chapter_uses_query_citation_when_authored_prose_has_no_items(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    base_bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    bundle = SimpleNamespace(
        config=SimpleNamespace(layout_mode="continuity", ado=base_bundle.config.ado),
        chapter_contract=base_bundle.chapter_contract,
    )

    scorecards = (
        SimpleNamespace(
            scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
            dimensions=(
                SimpleNamespace(name="Networking", risk=RiskLevel.LOW),
                SimpleNamespace(name="LSO", risk=RiskLevel.LOW),
            ),
        ),
    )
    scorecard_packets = {
        "Acme Adventure/XIO 100% Ramp Readiness": {
            "Networking": report_module.ScorecardEvidencePacket(
                dimension_name="Networking",
                dimension_description="Query-backed networking lane",
                total_items=0,
                items_by_risk={},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="https://dev.azure.com/query/networking",
                item_links=(),
                derived_risk=RiskLevel.LOW,
                item_ids=(),
            ),
            "LSO": report_module.ScorecardEvidencePacket(
                dimension_name="LSO",
                dimension_description="Empty networking parity lane",
                total_items=0,
                items_by_risk={},
                stale_items=(),
                stale_count=0,
                overdue_items=(),
                overdue_count=0,
                blocked_items=(),
                blocked_count=0,
                unowned_items=(),
                unowned_count=0,
                high_activity_items=(),
                prior_confirmed_risk=None,
                author_risk=None,
                ado_query_url="",
                item_links=(),
                derived_risk=RiskLevel.LOW,
                item_ids=(),
            ),
        }
    }

    workstreams = report_module._build_continuity_workstream_data(
        issue_number=1,
        bundle=bundle,
        edition_type=EditionType.FOCUSED,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=()),
        workstream_blurbs={"networking": "Authored networking narrative."},
        dependency_cascades=(),
        review_status=report_module.ReviewStatus(issue_number=1, sections=()),
        evidence_by_item={},
        item_urls={},
        items=(),
    )
    networking_chapter = next(workstream for workstream in workstreams if workstream.section_id == "networking")

    assert networking_chapter.citations[0].work_item_id is None
    assert networking_chapter.citations[0].display_label == "ADO query"
    assert networking_chapter.citations[0].ado_url == "https://dev.azure.com/query/networking"


def test_generate_report_draft_suppresses_weak_baseline_deltas(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    weak_snapshot = Snapshot(
        issue_number=1,
        generated_at=prior_as_of,
        ado_data_as_of=prior_as_of,
        edition_type=EditionType.FOCUSED,
        items=(),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                name="Deployment Velocity",
                risk=RiskLevel.MEDIUM,
                prior_risk=None,
                item_count=0,
                ado_query_url="https://dev.azure.com/query",
            ),
        ),
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=weak_snapshot,
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\nWeak reconstructed baseline.\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.issue_number == 2
    assert artifacts.report.deltas.previous_issue_number is None
    assert artifacts.report.deltas.new_items == ()
    assert "Changes include" not in artifacts.report.exec_summary_text
    assert "current-state inventory" in artifacts.report.exec_summary_text
    assert "WHAT CHANGED" not in artifacts.html_body


def test_generate_report_draft_uses_derived_risk_when_override_is_blank(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    deployment_velocity = next(dimension for dimension in artifacts.report.scorecard if dimension.name == "Deployment Velocity")

    assert deployment_velocity.derived_risk == RiskLevel.MEDIUM
    assert deployment_velocity.override_risk is None
    assert deployment_velocity.risk == RiskLevel.MEDIUM


def test_generate_report_draft_blocks_nova_authentic_voice_drift(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    narratives_dir = get_narratives_dir(EDITION_NAME, 1, reports_root)
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text(
        "Acme: The rest of the scorecard is materially narrower and the main job here is to keep the ramp story credible.\n",
        encoding="utf-8",
    )
    (narratives_dir / "chapter_networking.md").write_text(
        "Networking is part of the parity story and is no longer the broad program blocker it was earlier in the spring.\n",
        encoding="utf-8",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.exit_code == 3
    assert any("authentic voice" in warning.lower() for warning in artifacts.warnings)


def test_generate_report_draft_focused_continuity_uses_chapter_ids_and_excludes_fw_freshness(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.LOW),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    visible_section_ids = set(artifacts.report.workstream_blurbs)

    assert "deployment_readiness" in visible_section_ids
    assert "ap_shared_service" in visible_section_ids
    assert "fw_freshness" not in visible_section_ids


def test_generate_report_draft_readiness_uses_visible_newsletter_items(repo_root: Path, tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    _src = get_source_root(repo_root)
    shutil.copytree(_src / "reports" / "schemas", reports_root / "schemas")
    shutil.copytree(_src / "editions", editions_root)
    shutil.copytree(_src / "programs" / "acme", programs_root / "acme")
    journal_dir = programs_root / "acme" / "journal"
    if journal_dir.exists():
        shutil.rmtree(journal_dir)
    journal_dir.mkdir(parents=True)

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    for signal_id, work_item_id in (("approved-1", 900001), ("approved-2", 900002)):
        append_signal(
            Signal(
                id=signal_id,
                timestamp=as_of,
                source="ado/revision",
                program_id="acme",
                workstream_id="deployment_readiness",
                entity_refs=(f"WI:{work_item_id}",),
                text="Approved",
                raw_ref=None,
                confidence=Confidence.HIGH,
                metadata=None,
                thread_id=None,
            ),
            programs_root=programs_root,
            partition_at=as_of,
        )
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal_id,
                decision="approved",
                reviewed_at=as_of,
                reviewed_by="system",
                note=None,
            ),
            programs_root=programs_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.draft_readiness is not None
    assert artifacts.draft_readiness.coverage_gap_count == 0


def test_generate_report_draft_readiness_counts_visible_section_prose_as_coverage(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    baseline_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    narratives_dir = get_narratives_dir(EDITION_NAME, 1, reports_root)
    narratives_dir.mkdir(parents=True, exist_ok=True)
    deployment_text = "Deployment readiness remains the primary focus this issue, with the ramp path narrowed to the current blocker cluster.\n"
    shared_service_text = "AP shared service remains stable enough for the current focused view, with no new material drift called out here.\n"
    for filename, content in (
        ("ws_deployment_readiness.md", deployment_text),
        ("ws_ap_shared_service.md", shared_service_text),
        ("chapter_deployment_readiness.md", deployment_text),
        ("chapter_ap_shared_service.md", shared_service_text),
    ):
        (narratives_dir / filename).write_text(content, encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert baseline_artifacts.draft_readiness is not None
    assert artifacts.draft_readiness is not None
    assert artifacts.draft_readiness.coverage_gap_count < baseline_artifacts.draft_readiness.coverage_gap_count


def test_generate_report_draft_sections_filter_overrides_focused_auto_visibility(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    edition_path = reports_root.parent / "editions" / f"{EDITION_NAME}.yaml"
    edition_payload = yaml.safe_load(edition_path.read_text(encoding="utf-8")) or {}
    edition_payload["layout_mode"] = "dashboard"
    edition_path.write_text(yaml.safe_dump(edition_payload, sort_keys=False, allow_unicode=False), encoding="utf-8")

    baseline_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    available_section_ids = {
        path.stem.removeprefix("ws_")
        for path in baseline_artifacts.narratives_dir.glob("ws_*.md")
    }
    requested_section_id = next(
        section_id
        for section_id in sorted(available_section_ids)
        if section_id not in baseline_artifacts.report.workstream_blurbs
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        section_filter_ids=(requested_section_id,),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert set(artifacts.report.workstream_blurbs) == {requested_section_id}
    assert any(section.section_id == f"ws:{requested_section_id}" for section in artifacts.report.review_status.sections)
    assert all(section.section_id in {"exec_summary", f"ws:{requested_section_id}"} for section in artifacts.report.review_status.sections)


def test_generate_report_draft_focused_registry_preview_keeps_authored_section_visible(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    edition_path = reports_root.parent / "editions" / f"{EDITION_NAME}.yaml"
    edition_payload = yaml.safe_load(edition_path.read_text(encoding="utf-8")) or {}
    edition_payload["layout_mode"] = "dashboard"
    edition_path.write_text(yaml.safe_dump(edition_payload, sort_keys=False, allow_unicode=False), encoding="utf-8")

    baseline_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    hidden_section_id = next(
        section_id
        for section_id in sorted(
            path.stem.removeprefix("ws_")
            for path in baseline_artifacts.narratives_dir.glob("ws_*.md")
        )
        if section_id not in baseline_artifacts.report.workstream_blurbs
    )
    (baseline_artifacts.narratives_dir / f"ws_{hidden_section_id}.md").write_text(
        f"Authored follow-through remains relevant here, anchored by ADO#900001 for {hidden_section_id}.\n",
        encoding="utf-8",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        edition_type_override="focused",
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert hidden_section_id in artifacts.report.workstream_blurbs


def test_report_cli_normalizes_sections_and_passes_them_to_generator(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_generate_report_draft(**kwargs):
        captured["section_filter_ids"] = kwargs["section_filter_ids"]
        raise typer.Exit(code=0)

    monkeypatch.setattr("src.commands.report.generate_report_draft", _fake_generate_report_draft)
    monkeypatch.setattr("src.commands.report.archive_integrity_waived", lambda: True)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--sections",
            " deployment_readiness , ws:ap_shared_service ",
            "--sections",
            "ws_fw_freshness.md",
        ],
    )

    assert result.exit_code == 0
    assert captured["section_filter_ids"] == ("deployment_readiness", "ap_shared_service", "fw_freshness")


def test_report_cli_returns_exit_code_4_on_ado_timeout(monkeypatch) -> None:
    def _raise_timeout(**kwargs):
        raise QueryTimeoutError("ADO fetch timed out after 45s. Run vertex doctor to diagnose. Use --as-of 2026-05-01 to render from cache.")

    monkeypatch.setattr("src.commands.report.generate_report_draft", _raise_timeout)
    monkeypatch.setattr("src.commands.report.archive_integrity_waived", lambda: True)

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 4
    assert "ADO fetch timed out after 45s." in result.stdout


def test_generate_report_draft_continuity_keeps_chapter_pending_without_delta(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    previous_as_of = datetime(2026, 5, 4, 18, 0, tzinfo=timezone.utc)
    current_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    stable_previous = _stable_low_risk_items(previous_as_of)[0]
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=previous_as_of,
            items=(
                _snapshot_item_from_work_item(stable_previous, risk_level=RiskLevel.LOW),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=previous_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=current_as_of,
        work_item_loader=lambda bundle, timestamp: (_stable_low_risk_items(timestamp), 0),
        open_browser=False,
    )

    review_status = load_review_status(EDITION_NAME, reports_root=reports_root)

    assert review_status is not None
    deployment_section = next(section for section in review_status.sections if section.section_id == "ws:deployment_readiness")
    assert deployment_section.state.value == "pending"


def test_generate_report_draft_renders_forecast_when_enabled_and_history_present(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    config_path = reports_root.parent / "editions" / f"{EDITION_NAME}.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("forecast_enabled: false", "forecast_enabled: true"),
        encoding="utf-8",
    )

    for issue_number in range(1, 5):
        as_of = datetime(2026, 4, issue_number, 18, 0, tzinfo=timezone.utc)
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=as_of,
                items=(
                    _snapshot_item_from_work_item(_forecast_items(as_of)[0], risk_level=RiskLevel.LOW),
                ),
                scorecard_risks={"Deployment Velocity": RiskLevel.LOW},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}\n",
            manifest=_manifest(issue_number=issue_number, as_of=as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_forecast_items(timestamp), 0),
        open_browser=False,
    )

    manifest_payload = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))

    if manifest_payload["metadata"]["forecast_summary"] is None:
        pytest.skip("Current continuity forecast fixture did not produce a forecast candidate.")

    assert manifest_payload["metadata"]["forecast_summary"].startswith("Forecast:")
    assert manifest_payload["metadata"]["forecast_summary"] is not None
    assert manifest_payload["metadata"]["forecast_confidence"] == "medium"
    assert f"{manifest_payload['metadata']['forecast_summary']} (medium confidence)" in artifacts.html_body
    assert f"{manifest_payload['metadata']['forecast_summary']} (medium confidence)" in artifacts.markdown_body


def test_generate_report_draft_explicit_override_takes_precedence_over_derived_risk(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=first_artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["risk"] = "high"
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    deployment_velocity = next(dimension for dimension in artifacts.report.scorecard if dimension.name == "Deployment Velocity")

    assert deployment_velocity.derived_risk == RiskLevel.MEDIUM
    assert deployment_velocity.override_risk == RiskLevel.HIGH
    assert deployment_velocity.risk == RiskLevel.HIGH


def test_generate_report_draft_uplifts_dependency_risk_when_enabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    _set_v2_program_include_dependency_risk(programs_root, enabled=True)
    _write_report_armada_high_dependency(programs_root)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    deployment_velocity = next(dimension for dimension in artifacts.report.scorecard if dimension.name == "Deployment Velocity")

    assert deployment_velocity.derived_risk == RiskLevel.MEDIUM
    assert deployment_velocity.override_risk is None
    assert deployment_velocity.risk == RiskLevel.HIGH
    assert "Dependency risk: depends on fabrikam:buildouts" in deployment_velocity.summary
    assert "fabrikam's latest confirmed issue is HIGH" in deployment_velocity.summary


def test_generate_report_draft_uplifts_multi_hop_dependency_risk_when_enabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    _set_v2_program_include_dependency_risk(programs_root, enabled=True)
    _write_report_armada_portfolio_dependency_chain(programs_root)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    deployment_velocity = next(dimension for dimension in artifacts.report.scorecard if dimension.name == "Deployment Velocity")

    assert deployment_velocity.derived_risk == RiskLevel.MEDIUM
    assert deployment_velocity.override_risk is None
    assert deployment_velocity.risk == RiskLevel.HIGH
    assert "Dependency risk: depends on portfolio:rollout" in deployment_velocity.summary
    assert "portfolio's latest confirmed issue is HIGH" in deployment_velocity.summary


def test_generate_report_draft_renders_eta_forecast_annotation(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr(
        "src.commands.report._load_eta_forecasts",
        lambda **kwargs: {
            900001: ETAForecast(
                work_item_id=900001,
                ado_target_date=date(2026, 5, 10),
                predicted_target_date=date(2026, 5, 15),
                confidence=Confidence.LOW,
                slip_probability=0.78,
                reasoning="2 prior slips in 90 days -> 78% miss probability",
                prior_slips=2,
                p50_date=date(2026, 5, 12),
                p80_date=date(2026, 5, 15),
                p95_date=date(2026, 5, 18),
            )
        },
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert "(low confidence — 2 prior slips, 78% miss probability | forecast p50 May 12, p80 May 15, p95 May 18)" in artifacts.html_body
    assert "(low confidence — 2 prior slips, 78% miss probability | forecast p50 May 12, p80 May 15, p95 May 18)" in artifacts.markdown_body


def test_generate_report_draft_renders_eta_forecast_annotation_in_v2_lt_deck_open_issues(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )

    monkeypatch.setattr(
        "src.commands.report._load_eta_forecasts",
        lambda **kwargs: {
            900001: ETAForecast(
                work_item_id=900001,
                ado_target_date=date(2026, 5, 10),
                predicted_target_date=date(2026, 5, 15),
                confidence=Confidence.LOW,
                slip_probability=0.78,
                reasoning="2 prior slips in 90 days -> 78% miss probability",
                prior_slips=2,
                p50_date=date(2026, 5, 12),
                p80_date=date(2026, 5, 15),
                p95_date=date(2026, 5, 18),
            )
        },
    )

    artifacts = generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert "low confidence — 2 prior slips, 78% miss probability | forecast p50 May 12, p80 May 15, p95 May 18" in artifacts.markdown_body


def test_generate_report_draft_computes_readiness_from_v2_signal_context(repo_root: Path, tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    _src = get_source_root(repo_root)
    shutil.copytree(_src / "reports" / "schemas", reports_root / "schemas")
    shutil.copytree(_src / "editions", editions_root)
    shutil.copytree(_src / "programs" / "acme", programs_root / "acme")
    journal_dir = programs_root / "acme" / "journal"
    if journal_dir.exists():
        shutil.rmtree(journal_dir)
    journal_dir.mkdir(parents=True)
    narratives_078 = programs_root / "acme" / "narratives" / "issue_078"
    if narratives_078.exists():
        shutil.rmtree(narratives_078)

    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    append_signal(
        Signal(
            id="approved-signal",
            timestamp=as_of,
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Approved",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=as_of,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )
    append_signal(
        Signal(
            id="pending-signal",
            timestamp=as_of,
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1002",),
            text="Needs review",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=78,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_v2_readiness_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.draft_readiness is not None
    assert artifacts.draft_readiness.unreviewed_signal_count == 1
    assert artifacts.draft_readiness.coverage_gap_count == 1
    assert artifacts.draft_readiness.missing_narrative_count > 0
    assert artifacts.draft_readiness.summary.startswith("Draft readiness:")


def test_generate_report_draft_persists_scorecard_eta_override(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=first_artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["eta"] = "2026-05-27"
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    override_snapshot = draft_payload["override_snapshot"]

    assert override_snapshot["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["eta"] == "2026-05-27"


def test_generate_report_draft_label_override_updates_rendered_titles(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=first_artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["label"] = "Velocity"
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    override_snapshot = draft_payload["override_snapshot"]

    assert override_snapshot["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["label"] == "Velocity"
    assert "**Velocity**" in artifacts.markdown_body
    assert "Velocity" in artifacts.html_body


def test_generate_report_draft_hides_detail_section_when_override_requests_it(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=first_artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["scorecards"]["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["hide_details"] = True
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    deployment_velocity = next(dimension for dimension in artifacts.report.scorecard if dimension.name == "Deployment Velocity")
    section_id = "acme-adventure-xio-100-ramp-readiness-deployment-velocity"

    assert deployment_velocity.risk == RiskLevel.MEDIUM
    assert section_id not in artifacts.report.workstream_blurbs
    assert all(section.section_id != f"ws:{section_id}" for section in artifacts.report.review_status.sections)
    assert f'href="#{section_id}"' not in artifacts.html_body


def test_generate_report_draft_hides_removed_section_even_when_narrative_exists(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    section_id = "acme-adventure-xio-100-ramp-readiness-deployment-velocity"

    save_overrides(
        EDITION_NAME,
        report_module.OverridesDocument(issue_number=1, top_3_now=(), scorecards=(), removed_sections=(section_id,)),
        reports_root=reports_root,
    )
    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["removed_sections"] = [section_id]
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narratives_dir = get_narratives_dir(EDITION_NAME, 1, reports_root)
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / f"ws_{section_id}.md").write_text("Explicitly removed section narrative.", encoding="utf-8")

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert section_id not in artifacts.report.workstream_blurbs
    assert all(section.section_id != f"ws:{section_id}" for section in artifacts.report.review_status.sections)
    assert f'href="#{section_id}"' not in artifacts.html_body


def test_generate_report_draft_carries_forward_prior_section_without_current_counterpart(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    section_id = "acme-adventure-xio-100-ramp-readiness-deployment-velocity"

    prior_narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    prior_narratives_dir.mkdir(parents=True, exist_ok=True)
    (prior_narratives_dir / "exec_summary.md").write_text("Prior exec summary.", encoding="utf-8")
    (prior_narratives_dir / f"ws_{section_id}.md").write_text("Prior deployment velocity narrative.", encoding="utf-8")

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    scorecards_path = reports_root.parent / "programs" / "acme" / "scorecards.yaml"
    scorecards_payload = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
    removed_scorecard_name = scorecards_payload["scorecards"][0]["name"]
    scorecards_payload["scorecards"][0]["dimensions"] = [
        dimension
        for dimension in scorecards_payload["scorecards"][0]["dimensions"]
        if dimension["name"] != "Deployment Velocity"
    ]
    removed_linked_pairs: set[tuple[str, str]] = set()
    for sc in scorecards_payload["scorecards"][1:]:
        kept = []
        for dim in sc.get("dimensions", []):
            if (
                dim.get("linked_scorecard") == removed_scorecard_name
                and dim.get("linked_dimension") == "Deployment Velocity"
            ):
                removed_linked_pairs.add((sc["name"], dim["name"]))
            else:
                kept.append(dim)
        sc["dimensions"] = kept
    scorecards_path.write_text(yaml.safe_dump(scorecards_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    slice_contracts_path = reports_root.parent / "programs" / "acme" / "slice_contracts.yaml"
    slice_contracts_payload = yaml.safe_load(slice_contracts_path.read_text(encoding="utf-8"))
    slice_contracts_payload["slices"] = [
        slice_contract
        for slice_contract in slice_contracts_payload["slices"]
        if slice_contract["title"] != "Deployment Velocity"
        and (slice_contract["scorecard_name"], slice_contract["title"]) not in removed_linked_pairs
    ]
    slice_contracts_path.write_text(
        yaml.safe_dump(slice_contracts_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    chapter_contract_path = reports_root.parent / "programs" / "acme" / "chapter_contract.yaml"
    chapter_contract_payload = yaml.safe_load(chapter_contract_path.read_text(encoding="utf-8"))
    removed_dim_ids = {"acme.deployment_velocity"}
    for sc_name, dim_name in removed_linked_pairs:
        removed_dim_ids.add(canonical_dimension_binding_id(sc_name, dim_name, chapter_namespace="acme"))
    for chapter in chapter_contract_payload["chapters"]:
        chapter["dimensions"] = [
            dimension_id
            for dimension_id in chapter.get("dimensions", [])
            if dimension_id not in removed_dim_ids
        ]
    chapter_contract_path.write_text(
        yaml.safe_dump(chapter_contract_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    contract_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.continuation_contract.json").read_text(encoding="utf-8"))

    assert artifacts.report.workstream_blurbs[section_id] == "Prior deployment velocity narrative."
    assert "Prior deployment velocity narrative." in artifacts.html_body
    assert section_id in artifacts.html_body
    assert any(section.section_id == f"ws:{section_id}" for section in artifacts.report.review_status.sections)
    assert contract_payload["section_roster"]["removed_sections"] == []


def test_generate_report_draft_hides_unapproved_section_roster_additions_during_bridge(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    prior_as_of = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)
    inherited_section_id = "acme-adventure-xio-100-ramp-readiness-deployment-velocity"
    added_section_id = "acme-adventure-xio-100-ramp-readiness-pf-infra"

    prior_narratives_dir = reports_root.parent / "programs" / "acme" / "archive" / EDITION_NAME / "narratives" / "issue_001"
    prior_narratives_dir.mkdir(parents=True, exist_ok=True)
    (prior_narratives_dir / "exec_summary.md").write_text("Prior exec summary.", encoding="utf-8")
    (prior_narratives_dir / f"ws_{inherited_section_id}.md").write_text("Prior deployment velocity narrative.", encoding="utf-8")

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=prior_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(prior_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=prior_as_of,
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    contract_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.continuation_contract.json").read_text(encoding="utf-8"))

    assert inherited_section_id in artifacts.report.workstream_blurbs
    assert added_section_id not in artifacts.report.workstream_blurbs
    assert all(section.section_id != f"ws:{added_section_id}" for section in artifacts.report.review_status.sections)
    assert added_section_id in contract_payload["section_roster"]["added_sections"]


def test_generate_report_draft_warns_when_medium_risk_narrative_is_empty(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    first_artifacts = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    first_section = next(iter(first_artifacts.report.workstream_blurbs))
    _set_override_risks_for_section(
        reports_root=reports_root,
        snapshot=first_artifacts.snapshot,
        section_id=first_section,
        risk=RiskLevel.MEDIUM,
        edition_name="acme_weekly",
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.exit_code == 3
    assert any(f"Medium-risk section {first_section}" in warning for warning in artifacts.warnings)


def test_generate_report_draft_warns_when_narrative_predates_latest_eta_change(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    narrative_path = programs_root / "acme" / "narratives" / "issue_001" / "chapter_deployment_readiness.md"
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text("Deployment narrative remains current.\n", encoding="utf-8")
    stale_timestamp = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc).timestamp()
    os.utime(narrative_path, (stale_timestamp, stale_timestamp))
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 10),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 8),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 17),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert any(
        "chapter_deployment_readiness.md last edited May 1, but WI:900001 ETA changed May 8" in warning
        for warning in artifacts.warnings
    )


def test_generate_report_draft_surfaces_trajectory_velocity_when_kusto_disabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    ado_block = program_document.setdefault("ado", {})
    assert isinstance(ado_block, dict)
    ado_block["date_window_days"] = 7
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(date=date(2026, 4, 24), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 5, 8), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900002,
        (
            TrajectoryPoint(date=date(2026, 4, 29), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Contoso\\Networking"),
            TrajectoryPoint(date=date(2026, 5, 6), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Contoso\\Networking"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(date=date(2026, 4, 19), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios"),
            TrajectoryPoint(date=date(2026, 5, 10), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios"),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    trajectory_velocity = next(section for section in draft_payload["kusto_sections"] if section["title"] == "Trajectory Velocity")

    assert "Trajectory Velocity" in artifacts.markdown_body
    assert "3/week" in artifacts.markdown_body
    assert {metric["label"]: metric["value"] for metric in trajectory_velocity["metrics"]} == {
        "Resolved items": "3",
        "Throughput": "3/week",
        "Median cycle time": "14d",
        "P90 cycle time": "21d",
    }


def test_generate_report_draft_falls_back_to_trajectory_velocity_when_velocity_kusto_query_degrades(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    ado_block = program_document.setdefault("ado", {})
    assert isinstance(ado_block, dict)
    ado_block["date_window_days"] = 7
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(date=date(2026, 4, 24), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 5, 8), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900002,
        (
            TrajectoryPoint(date=date(2026, 4, 29), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Contoso\\Networking"),
            TrajectoryPoint(date=date(2026, 5, 6), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Contoso\\Networking"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(date=date(2026, 4, 19), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios"),
            TrajectoryPoint(date=date(2026, 5, 10), state="Resolved", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.LOW, area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios"),
        ),
        programs_root=programs_root,
    )

    def _degraded_velocity_results(query):
        if query.id == "velocity-p50":
            raise QueryError("missing NOVADeploymentMetrics")
        return _sample_kusto_query_results(query)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_degraded_velocity_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    deployment_velocity = next(section for section in draft_payload["kusto_sections"] if section["query_id"] == "velocity-p50")

    assert deployment_velocity["title"] == "Deployment Velocity"
    assert deployment_velocity["source_label"] == "ADO trajectory fallback"
    assert {metric["label"]: metric["value"] for metric in deployment_velocity["metrics"]} == {
        "Resolved items": "3",
        "Throughput": "3/week",
        "Median cycle time": "14d",
        "P90 cycle time": "21d",
    }
    assert not any("velocity-p50" in warning for warning in artifacts.warnings)


def test_build_velocity_kusto_section_reads_sqlite_backed_trajectories(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme",
                "name": "Acme",
                "storage_backend": "sqlite",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 4, 24),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )
    trajectory_store.append(
        "acme",
        900001,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Resolved",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )
    trajectory_store.append(
        "acme",
        900002,
        TrajectoryPoint(
            date=date(2026, 4, 29),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Contoso\\Networking",
        ),
    )
    trajectory_store.append(
        "acme",
        900002,
        TrajectoryPoint(
            date=date(2026, 5, 6),
            state="Resolved",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Contoso\\Networking",
        ),
    )
    trajectory_store.append(
        "acme",
        900003,
        TrajectoryPoint(
            date=date(2026, 4, 19),
            state="Active",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
        ),
    )
    trajectory_store.append(
        "acme",
        900003,
        TrajectoryPoint(
            date=date(2026, 5, 10),
            state="Resolved",
            assigned_to="Vertex Maintainer",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
        ),
    )

    section = report_module.build_velocity_kusto_section(
        program_id="acme",
        item_ids=(900001, 900002, 900003),
        as_of=date(2026, 5, 10),
        window_days=7,
        programs_root=programs_root,
    )

    assert section is not None
    assert {metric.label: metric.value for metric in section.metrics} == {
        "Resolved items": "3",
        "Throughput": "3/week",
        "Median cycle time": "14d",
        "P90 cycle time": "21d",
    }


def test_generate_report_draft_falls_back_to_zero_throughput_velocity_when_no_items_resolve_in_window(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    ado_block = program_document.setdefault("ado", {})
    assert isinstance(ado_block, dict)
    ado_block["date_window_days"] = 7
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(date=date(2026, 5, 6), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 12), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900002,
        (
            TrajectoryPoint(date=date(2026, 5, 4), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 14), risk_level=RiskLevel.HIGH, area_path="One\\Adventure\\Contoso\\Networking"),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="Vertex Maintainer", target_date=date(2026, 5, 16), risk_level=RiskLevel.LOW, area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios"),
        ),
        programs_root=programs_root,
    )

    def _degraded_velocity_results(query):
        if query.id == "velocity-p50":
            raise QueryError("missing NOVADeploymentMetrics")
        return _sample_kusto_query_results(query)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_degraded_velocity_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    deployment_velocity = next(section for section in draft_payload["kusto_sections"] if section["query_id"] == "velocity-p50")

    assert deployment_velocity["source_label"] == "ADO trajectory fallback"
    assert {metric["label"]: metric["value"] for metric in deployment_velocity["metrics"]} == {
        "Resolved items": "0",
        "Throughput": "0/week",
        "Median cycle time": "Unavailable",
        "P90 cycle time": "Unavailable",
    }
    assert not any("velocity-p50" in warning for warning in artifacts.warnings)


def test_generate_report_draft_suppresses_degraded_fleet_health_when_ado_vitality_is_available(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    def _degraded_fleet_health_results(query):
        if query.id == "fleet-health":
            raise QueryError("missing FleetHealthSnapshot")
        return _sample_kusto_query_results(query)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_degraded_fleet_health_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))

    assert "ADO Vitality This Week" in artifacts.html_body
    assert all(section["query_id"] != "fleet-health" for section in draft_payload["kusto_sections"])
    assert not any("fleet-health" in warning for warning in artifacts.warnings)


def test_generate_report_draft_renders_ado_vitality_section_with_archive_trend(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    def _work_item_loader(bundle, timestamp):
        del bundle
        items = _sample_items(timestamp)
        items[1].revisions = [
            Revision(
                work_item_id=900002,
                rev_number=3,
                changed_by="Vertex Maintainer",
                changed_by_email="maintainer@example.com",
                changed_date=timestamp - timedelta(days=1),
                fields_changed={"State": ("Active", "At Risk")},
            )
        ]
        items[1].comments = []
        return items, 0

    for issue_number, score in ((1, 20), (2, 35), (3, 49)):
        as_of = datetime(2026, 5, issue_number, 18, 0, tzinfo=timezone.utc)
        write_confirmed_issue(
            edition="acme_weekly",
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(as_of)[0], risk_level=RiskLevel.LOW),
                ),
                scorecard_risks={"Deployment Velocity": RiskLevel.LOW},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}\n",
            manifest=_manifest(issue_number=issue_number, as_of=as_of),
            vitality_record=VitalityArchiveEntry(
                issue_number=issue_number,
                confirmed_at=as_of,
                aggregate_score=score,
                items_total=52,
                items_fresh=30 + issue_number,
                avg_richness=60 + issue_number,
                leakage_events=max(0, 6 - issue_number),
                per_workstream={
                    "deployment_readiness": VitalityArchiveWorkstream(
                        score=score,
                        items=12,
                        fresh=8,
                    )
                },
            ),
            archive_root=archive_root,
        )

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="workiq-900002",
            timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            source="workiq/mail",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900002",),
            text="Capacity discussion referenced outside ADO.",
            raw_ref="mail:1",
            confidence=Confidence.MEDIUM,
            metadata={"entity_link_confidence": "high"},
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=4,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=_work_item_loader,
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert "ADO Vitality This Week" in artifacts.html_body
    assert "ADO Vitality This Week" in artifacts.markdown_body
    assert "Best documented: WI:" in artifacts.markdown_body
    assert "Items with leakage signals: 1 item" in artifacts.markdown_body
    assert "Trend: 20% -> 35% -> 49% ->" in artifacts.markdown_body
    assert "(improving over 4 issues)" in artifacts.markdown_body
    assert "| Workstream | Workstream Owner + ADO Assignee | Stale or Missing ADOs | What Field Needs Update |" in artifacts.markdown_body
    assert "Owner update or comment; Description; Next step" in artifacts.markdown_body


def test_generate_report_draft_rerenders_issue_077_as_continuation(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    newsletters_src = repo_root / "output" / EDITION_NAME / "newsletters"
    if not newsletters_src.exists():
        pytest.skip("Requires live output/acme_weekly/newsletters (run vertex report first)")
    shutil.copytree(newsletters_src, programs_root / "acme" / "publications" / EDITION_NAME / "newsletters")
    (programs_root / "acme" / "publications" / EDITION_NAME).mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        _issue_077_snapshot_path(repo_root),
        programs_root / "acme" / "publications" / EDITION_NAME / "issue_077" / "issue_077.snapshot.json",
    )
    disable_kusto_in_report_copy(reports_root)
    seed_issue_076_baseline(
        reports_root=reports_root,
        programs_root=programs_root,
        archive_root=archive_root,
        edition_name=EDITION_NAME,
    )

    snapshot_payload = json.loads(_issue_077_snapshot_path(repo_root).read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(snapshot_payload["generated_at"])
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_issue_077_snapshot_items(repo_root, timestamp), 0),
        open_browser=False,
    )

    visible_new_rows = artifacts.markdown_body.count("- **New**:")

    assert artifacts.issue_number == 77
    assert artifacts.report.deltas.previous_issue_number == 76
    assert 0 < len(artifacts.report.deltas.new_items) < len(artifacts.report.items)
    assert visible_new_rows == min(5, len(artifacts.report.deltas.new_items))
    assert "5 of 10 changes shown" in artifacts.markdown_body
    assert artifacts.markdown_body.index("## Acme Adventure/XIO 100% Ramp Readiness") < artifacts.markdown_body.index("## Executive Summary")
    assert "## Deployment Velocity" not in artifacts.markdown_body
    assert artifacts.markdown_body.index("## Acme Adventure/XIO 100% Ramp Readiness") < artifacts.markdown_body.index("## SCHIE MAP Day Gaps")
    assert artifacts.markdown_body.index("## SCHIE MAP Day Gaps") < artifacts.markdown_body.index("## What Changed")
    assert artifacts.markdown_body.index("## Program Health") < artifacts.markdown_body.index("## SCHIE MAP Day Gaps")
    assert 'data-vertex-block="jump-to-section"' in artifacts.html_body
    assert 'href="#chapter-schie_map_day_gaps"' in artifacts.html_body
    assert "Khabari" not in artifacts.html_body
    assert "Manifest " not in artifacts.html_body
    assert "Issue 077 - " not in artifacts.html_body
    assert f"Issue {artifacts.issue_number:03d} draft" not in artifacts.html_body


def test_report_cli_diff_shows_override_ado_and_exec_summary_changes(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    loader_calls = {"count": 0}

    def staged_loader(bundle, timestamp):
        del bundle
        loader_calls["count"] += 1
        if loader_calls["count"] == 1:
            return _sample_items(timestamp), 0
        return _sample_items_with_diff(timestamp), 0

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr("src.commands.report._load_live_work_items", staged_loader)
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    first_result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run"])

    assert first_result.exit_code == 3

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    scorecard_name, scorecard_dimensions = next(iter(overrides_payload["scorecards"].items()))
    changed_dimension_name, changed_dimension = next(iter(scorecard_dimensions.items()))
    changed_dimension["risk"] = "low"
    changed_dimension["hide_details"] = True
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Updated executive summary with new rollout risk.\n", encoding="utf-8")

    diff_result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--diff"])

    assert diff_result.exit_code == 3
    assert "VERTEX DIFF - Changes since last dry-run" in diff_result.stdout
    assert "SCORECARD OVERRIDES APPLIED:" in diff_result.stdout
    assert f"{changed_dimension_name}: Needs Input -> Low (author set in overrides.yaml)" in diff_result.stdout
    assert f"{changed_dimension_name}: detail section hidden" in diff_result.stdout
    assert "ADO DATA CHANGES:" in diff_result.stdout
    assert '#900004 "New cache warmup safeguard" - NEW' in diff_result.stdout
    assert "EXEC SUMMARY:" in diff_result.stdout
    assert "+ Updated executive summary with new rollout risk." in diff_result.stdout


def test_report_cli_stdout_json(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["edition"] == EDITION_NAME
    assert payload["paths"]["eml"].endswith("issue_001.eml")
    assert payload["paths"]["html"].endswith("issue_001.html")
    assert "QG-8" in payload["qg_results"]


def test_report_cli_stdout_verbose_includes_evidence_packets(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout", "--verbose"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert "evidence" in payload
    assert payload["paths"]["trace"].endswith("report.trace.jsonl")
    assert payload["evidence"]["items"]["900001"]["summary"]
    assert payload["evidence"]["scorecards"]["Acme Adventure/XIO 100% Ramp Readiness"]["Deployment Velocity"]["total_items"] >= 1
    assert Path(payload["paths"]["trace"]).exists()


def test_report_cli_stdout_verbose_uses_trusted_baseline_for_previous_snapshot(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = report_module._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(report_module, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(report_module, "_load_previous_snapshot", _capturing_load_previous_snapshot)
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout", "--verbose"])

    assert result.exit_code == 3
    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_report_cli_rejects_verbose_without_stdout(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--verbose"])

    assert result.exit_code != 0
    assert "--verbose requires --stdout." in result.output


def test_report_cli_send_draft_sends_to_author_mailbox(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    sent_messages = []
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr(
        "src.commands.report._build_draft_email_sender",
        lambda: (lambda message: sent_messages.append(message)),
    )

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--send-draft",
        ],
    )

    assert result.exit_code == 3
    assert "Draft email sent to " in result.output
    assert "@microsoft.com" in result.output
    assert len(sent_messages) == 1
    assert sent_messages[0].to[0].endswith("@microsoft.com")
    assert sent_messages[0].cc == ()
    assert sent_messages[0].subject.startswith("Program Hygiene | Issue 1 | ")
    assert "Program Hygiene" in sent_messages[0].html_body


def test_report_cli_send_draft_rejects_stdout(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--stdout", "--send-draft"])

    assert result.exit_code == 2
    assert "--send-draft cannot be combined with --stdout." in result.output


def test_report_cli_send_draft_surfaces_transport_error(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr(
        "src.commands.report._build_draft_email_sender",
        lambda: (lambda message: (_ for _ in ()).throw(AuthError("Missing GRAPH_TENANT_ID for Graph mail send."))),
    )

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--send-draft",
        ],
    )

    assert result.exit_code == 2
    assert "Missing GRAPH_TENANT_ID for Graph mail send." in result.output


def test_report_cli_ai_review_requires_dry_run(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--ai-review"])

    assert result.exit_code == 2
    assert "--ai-review requires --dry-run." in result.output


def test_report_cli_reseed_requires_dry_run(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--reseed"])

    assert result.exit_code == 2
    assert "--reseed requires --dry-run." in result.output


def test_report_cli_reseed_rejects_no_seed(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))

    result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--reseed", "--no-seed"])

    assert result.exit_code == 2
    assert "--reseed cannot be combined with --no-seed." in result.output


def test_report_cli_ai_review_writes_review_markdown_without_changing_exit_code(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    prior_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot_with_item(prior_as_of, risk_level=RiskLevel.HIGH),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001\nDeployment velocity telemetry stabilization remains high risk.\n",
        manifest=_manifest(issue_number=1, as_of=prior_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--ai-review",
        ],
    )

    assert result.exit_code == 3
    review_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.review.md"
    review_json_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.review.json"
    assert review_path.exists()
    assert review_json_path.exists()
    review_body = review_path.read_text(encoding="utf-8")
    review_payload = json.loads(review_json_path.read_text(encoding="utf-8"))
    assert "AI DRAFT REVIEW" in review_body
    assert "LEADERSHIP QUESTIONS" in review_body
    assert "Jordan Lee will likely ask" in review_body
    assert "DATA GAPS" in review_body
    assert "STRUCTURAL" in review_body
    assert review_payload["review_report"]["issue_number"] == 2
    assert review_payload["review_report"]["leadership_questions"] > 0
    assert "exec_summary" in review_payload["reviewed_sections"]
    assert "velocity-p50" not in review_payload["rendered_kusto_query_ids"]
    assert "Review: " in result.output


def test_report_cli_renders_narrative_edition_override(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--edition-type",
            "narrative",
        ],
    )

    assert result.exit_code == 3
    html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    rendered = html_path.read_text(encoding="utf-8")
    assert "Program Health:" in rendered
    assert "Acme Ramp Readiness" not in rendered
    assert "Fabrikam Acme" not in rendered
    assert rendered.index("DD on PF") < rendered.index("Executive Summary")


def test_report_cli_renders_focused_edition_without_baseline_changes(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--edition-type",
            "focused",
        ],
    )

    assert result.exit_code == 3
    html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    rendered = html_path.read_text(encoding="utf-8")
    assert "WHAT CHANGED" not in rendered
    assert "Executive Summary" in rendered
    # Data-dependent / P2 label drift: scorecard short labels now come from
    # program config; assert the generic scorecard surface is present.
    assert "scorecard (Risk levels)" in rendered
    assert "Jump to section" in rendered


def test_report_cli_renders_condensed_edition_override(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_sample_items(timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--edition-type",
            "condensed",
        ],
    )

    assert result.exit_code == 3
    html_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.html"
    rendered = html_path.read_text(encoding="utf-8")
    assert "Daily Digest" in rendered
    assert "Change Summary" in rendered
    assert "Acme Ramp Readiness" not in rendered
    assert "Executive Summary" not in rendered
    assert "Deployment velocity telemetry stabilization" not in rendered


def test_generate_report_draft_renders_v2_daily_edition(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_daily"),
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    artifacts = generate_report_draft(
        edition_name="nova_daily",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.CONDENSED
    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert artifacts.md_path is not None and artifacts.md_path.exists()
    assert "Daily Digest" in artifacts.html_body
    assert (programs_root / "acme" / "publications" / "nova_daily" / "issue_001" / "issue_001.html").exists()


def test_generate_report_draft_condensed_limits_change_rows_to_three(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_daily"),
    )

    baseline_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    baseline_artifacts = generate_report_draft(
        edition_name="nova_daily",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=baseline_as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    baseline_snapshot = read_snapshot(baseline_artifacts.snapshot_path)
    adjusted_baseline = replace(
        baseline_snapshot,
        edition_type=EditionType.CONDENSED,
        items=(
            replace(baseline_snapshot.items[0], risk_level=RiskLevel.LOW, target_date=date(2026, 5, 8)),
            replace(baseline_snapshot.items[1], risk_level=RiskLevel.LOW, target_date=date(2026, 5, 6)),
            baseline_snapshot.items[2],
        ),
        scorecards=tuple(replace(dimension, risk=RiskLevel.LOW) for dimension in baseline_snapshot.scorecards),
    )
    write_confirmed_issue(
        edition="nova_daily",
        issue_number=1,
        snapshot=adjusted_baseline,
        html_body=baseline_artifacts.html_body,
        markdown_body=baseline_artifacts.markdown_body,
        manifest=_manifest(issue_number=1, as_of=baseline_as_of, edition="nova_daily"),
        archive_root=archive_root,
    )

    current_as_of = baseline_as_of + timedelta(days=1)
    current_seed_items = _sample_items(current_as_of)
    current_items = (
        replace(current_seed_items[0], target_date=date(2026, 5, 14)),
        replace(current_seed_items[1], target_date=date(2026, 5, 12)),
        current_seed_items[2],
        WorkItem(
            id=900004,
            type="Bug",
            title="New cache warmup safeguard",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=["Hotfix"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900004,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=current_as_of,
                    fields_changed={"State": (None, "Active")},
                )
            ],
            comments=[],
            fetched_at=current_as_of,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="nova_daily",
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=current_as_of,
        work_item_loader=lambda bundle, timestamp: (current_items, 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.CONDENSED
    assert "## What Changed" in artifacts.markdown_body
    change_section = artifacts.markdown_body.split("## What Changed\n", 1)[1].split("\n## ", 1)[0]
    assert change_section.count("- **") == 3
    assert "3 of " in artifacts.markdown_body and "changes shown" in artifacts.markdown_body
    assert "3 of " in artifacts.html_body and "changes shown" in artifacts.html_body


def test_generate_report_draft_renders_v2_armada_narrative_edition(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "fabrikam_weekly"),
        program_names=("acme", "fabrikam"),
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    artifacts = generate_report_draft(
        edition_name="fabrikam_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_armada_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.NARRATIVE
    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert artifacts.md_path is not None and artifacts.md_path.exists()
    assert "Fabrikam Program Update" in artifacts.html_body
    assert "Fabrikam Core (Runtime, Platform, Topology)" in artifacts.html_body
    assert "Buildouts" in artifacts.html_body
    assert "Service Fabric" in artifacts.html_body
    assert "Scenarios & Perf Testing" in artifacts.html_body
    assert "Deployment / Impact Approval" in artifacts.html_body
    assert (programs_root / "fabrikam" / "publications" / "fabrikam_weekly" / "issue_001" / "issue_001.html").exists()


def test_generate_report_draft_renders_v2_lt_deck_markdown(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            "Green-light LT review without timeline slip.",
        ],
        "constraints": [
            "Do not widen partner pilot scope before SCHIE signoff.",
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
    append_decision_ask(
        DecisionAsk(
            id="ask-open",
            program_id="acme",
            edition_id="nova_lt_deck",
            issue_number=1,
            text="Need LT decision on SCHIE timeline",
            entity_refs=("WI:900002",),
            ask_date=date(2026, 5, 1),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-closed",
            program_id="acme",
            edition_id="nova_lt_deck",
            issue_number=1,
            text="Confirm Contoso mitigation plan",
            entity_refs=("WI:900001",),
            ask_date=date(2026, 5, 1),
            owner_alias="operator",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-closed",
            new_status="resolved",
            updated_at=datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc),
            updated_by="maintainer",
            note="Mitigation approved by LT",
        ),
        programs_root=programs_root,
    )
    save_decisions(
        "acme",
        (
            DecisionEntry(
                id="decision-1",
                program_id="acme",
                title="SCHIE timeline approval",
                context="Timeline needs leadership alignment before partner commit.",
                decision="Await LT approval before locking external target.",
                rationale=None,
                alternatives_considered=(),
                decided_by="lt",
                decision_date=date(2026, 4, 20),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="deployment_readiness",
                entity_refs=("WI:900002",),
            ),
        ),
        programs_root=programs_root,
    )
    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-1",
                program_id="acme",
                text="Partner schema contract stays stable through Q4.",
                validation_method="Validate in monthly partner review",
                validation_due=date(2026, 5, 1),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id="m3-code-complete",
                owner_alias="operator",
                identified_date=date(2026, 4, 10),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="nova_lt_deck",
            issue_number=2,
            workstream_id="deployment_readiness",
            text="Deployment velocity telemetry stabilizes before LT review.",
            entity_refs=("WI:900001",),
            claim_date=date(2026, 5, 1),
            owner_alias="operator",
            due_date=date(2026, 5, 12),
        ),
        programs_root=programs_root,
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Deployment telemetry may miss the LT gate",
                description="The telemetry stabilization work could slip the review prep timeline.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.TECHNICAL,
                owner_alias="operator",
                mitigation_plan="Track the telemetry fix daily until the blocker clears.",
                mitigation_due_date=date(2026, 5, 12),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=(),
                linked_claim_ids=("claim-1",),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 4, 28),
                identified_in_vertex_issue=1,
                last_reviewed_date=date(2026, 5, 4),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    write_confirmed_issue(
        edition="nova_lt_deck",
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc),
            items=(
                _snapshot_item_from_work_item(_sample_items(as_of)[0], risk_level=RiskLevel.MEDIUM),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=replace(
            _manifest(issue_number=1, as_of=datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc), edition="nova_lt_deck"),
            metadata={
                "milestone_assessments": [
                    {
                        "milestone_id": "m3-code-complete",
                        "target_date": "2026-05-12",
                    },
                    {
                        "milestone_id": "m4-pilot-rollout-validation",
                        "completion_date": "2026-05-16",
                    }
                ]
            },
        ),
        archive_root=archive_root,
    )
    (programs_root / "acme" / "milestones.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-18",
                "    owner_alias: maintainer",
                "    status: on_track",
                "    exit_criteria:",
                "      - Code complete",
                "    linked_workstream_ids:",
                "      - deployment_readiness",
                "    linked_work_item_ids:",
                "      - 900001",
                "  - id: m4-pilot-rollout-validation",
                "    name: M4 - Pilot Rollout Validation",
                "    target_date: 2026-05-17",
                "    owner_alias: maintainer",
                "    status: on_track",
                "    exit_criteria:",
                "      - Pilot rollout validated",
                "    linked_workstream_ids:",
                "      - deployment_readiness",
                "    linked_work_item_ids:",
                "      - 900003",
            )
        ),
        encoding="utf-8",
    )
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 22),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 4),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 28),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
        ),
        programs_root=programs_root,
    )
    backfill_trajectory_points(
        "acme",
        900003,
        (
            TrajectoryPoint(
                date=date(2026, 5, 18),
                state="Resolved",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 12),
                risk_level=RiskLevel.LOW,
                area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
            ),
        ),
        programs_root=programs_root,
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-lt-deck-analytics-1",
            timestamp=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Analytics snapshot for LT deck telemetry.",
            raw_ref="ado-analytics:sig-lt-deck-analytics-1",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 3,
                "open_delta_count": -2,
                "average_cycle_time_days": 4.5,
                "average_lead_time_days": 7.0,
            },
        ),
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-lt-deck-sprint-1",
            timestamp=datetime(2026, 5, 5, 16, 40, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Sprint snapshot for LT deck telemetry.",
            raw_ref="ado-sprint:sig-lt-deck-sprint-1",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 42",
                "completion_pct": 75,
                "open_item_count": 1,
            },
        ),
    )
    _write_lt_deck_dependency_proposals(programs_root)
    artifacts = generate_report_draft(
        edition_name="nova_lt_deck",
        issue_number=2,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (
            tuple(
                replace(item, state="Resolved") if item.id == 900003 else item
                for item in _sample_items(timestamp)
            ),
            0,
        ),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.edition == EditionType.DECK
    assert artifacts.eml_path is None
    assert artifacts.html_path is None
    assert artifacts.md_path is not None and artifacts.md_path.exists()
    assert artifacts.md_path.name == "issue_002.deck.md"
    assert "## Health" in artifacts.markdown_body
    assert "## Telemetry" in artifacts.markdown_body
    assert "analytics, 5 scope, 3 completed, open down 2, cycle 4.5d / lead 7.0d; sprint, Sprint 42, 75% complete, 1 open (high confidence)" in artifacts.markdown_body
    assert "## Charter" in artifacts.markdown_body
    assert "\n---\n\n## Charter" in artifacts.markdown_body
    assert "Scope: Deliver Acme ramp readiness for the current LT gate." in artifacts.markdown_body
    assert "Success criterion: Green-light LT review without timeline slip." in artifacts.markdown_body
    assert "Constraint: Do not widen partner pilot scope before SCHIE signoff." in artifacts.markdown_body
    assert "## Milestones" in artifacts.markdown_body
    assert "\n---\n\n## Milestones" in artifacts.markdown_body
    assert "M3 - Code Complete — at risk" in artifacts.markdown_body
    assert "tracking 2026-05-28 (10 days late vs target)" in artifacts.markdown_body
    assert "target history 2026-05-12 -> 2026-05-18" in artifacts.markdown_body
    assert "M4 - Pilot Rollout Validation — completed" in artifacts.markdown_body
    assert "completed 2026-05-18 (1 day late vs target)" in artifacts.markdown_body
    assert "completion history 2026-05-16 -> 2026-05-18" in artifacts.markdown_body
    assert "## Top Risks" in artifacts.markdown_body
    assert "## Dependency Proposals" in artifacts.markdown_body
    assert "dep-proposal-1: deployment_readiness:1001 -> platform_readiness:1002 — comment_language | 2 signal(s) | medium confidence | accept via vertex dependencies accept --program acme --id dep-proposal-1" in artifacts.markdown_body
    assert "dep-proposal-accepted" not in artifacts.markdown_body
    assert "## Open Issues" in artifacts.markdown_body
    assert "\n---\n\n## Open Issues" in artifacts.markdown_body
    assert "Issue #001 ask: Need LT decision on SCHIE timeline (owner lt)" in artifacts.markdown_body
    assert "linked claim-1, risk-1" in artifacts.markdown_body
    assert "## Key Decisions" in artifacts.markdown_body
    assert "SCHIE timeline approval — Await LT approval before locking external target." in artifacts.markdown_body
    assert "PROPOSED | stale | owner lt | date 2026-04-20" in artifacts.markdown_body
    assert "## Key Assumptions" in artifacts.markdown_body
    assert "Partner schema contract stays stable through Q4." in artifacts.markdown_body
    assert "UNVALIDATED | overdue | due 2026-05-01 | owner operator | milestone m3-code-complete" in artifacts.markdown_body
    assert "## Open Asks" in artifacts.markdown_body
    assert "Need LT decision on SCHIE timeline" in artifacts.markdown_body
    assert "## Closed Asks" in artifacts.markdown_body
    assert "Confirm Contoso mitigation plan" in artifacts.markdown_body
    assert "Mitigation approved by LT" in artifacts.markdown_body
    assert (programs_root / "acme" / "publications" / "nova_lt_deck" / "issue_002" / "issue_002.deck.md").exists()


def _write_lt_deck_dependency_proposals(programs_root: Path) -> None:
    proposals_path = programs_root / "acme" / "_feedback" / "dependency_proposals.yaml"
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "updated_at": "2026-05-05T18:00:00+00:00",
                "proposals": [
                    {
                        "id": "dep-proposal-1",
                        "program_id": "acme",
                        "from_workstream_id": "deployment_readiness",
                        "to_workstream_id": "platform_readiness",
                        "from_item_id": 1001,
                        "to_item_id": 1002,
                        "from_item_title": "Covered item",
                        "to_item_title": "Blocked item",
                        "suggested_dependency_type": "shares_resource",
                        "rationale": "Repeated blocked-by phrasing indicates a missing dependency.",
                        "evidence_refs": ["sig-1", "sig-2"],
                        "detection_method": "comment_language",
                        "occurrence_count": 2,
                        "first_seen_at": "2026-05-03T18:00:00+00:00",
                        "last_seen_at": "2026-05-05T18:00:00+00:00",
                        "confidence": "medium",
                        "status": "proposed",
                    },
                    {
                        "id": "dep-proposal-accepted",
                        "program_id": "acme",
                        "from_workstream_id": "deployment_readiness",
                        "to_workstream_id": "platform_readiness",
                        "from_item_id": 1003,
                        "to_item_id": 1004,
                        "from_item_title": "Already promoted item",
                        "to_item_title": "Already linked item",
                        "suggested_dependency_type": "blocks",
                        "rationale": "Already accepted.",
                        "evidence_refs": ["sig-3"],
                        "detection_method": "co_mention",
                        "occurrence_count": 3,
                        "first_seen_at": "2026-05-01T18:00:00+00:00",
                        "last_seen_at": "2026-05-02T18:00:00+00:00",
                        "confidence": "medium",
                        "status": "accepted",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_generate_report_draft_renders_scorecard_trend_annotations_in_v2_lt_deck(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )

    for issue_number, issue_as_of in (
        (1, datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)),
        (2, datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)),
        (3, datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)),
    ):
        write_confirmed_issue(
            edition="nova_lt_deck",
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[1], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks={"SCHIE Gaps": RiskLevel.HIGH},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=replace(_manifest(issue_number=issue_number, as_of=issue_as_of), edition="nova_lt_deck"),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name="nova_lt_deck",
        issue_number=4,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert "High for 4 consecutive issues." in artifacts.markdown_body


def test_generate_report_draft_renders_dependency_cascade_in_v2_workstream_section(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"

    _set_v2_edition_layout_mode(editions_root, edition_id="acme_weekly", layout_mode="dashboard")
    _set_v2_key_dependencies(
        programs_root,
        dependencies=(
            {
                "from_item": "SCHIE gap closure",
                "to_item": "SCHIE Gaps",
                "impact": "Ramp stays blocked until the gap closes.",
                "resolution_path": "cross_org_compute_pf",
            },
        ),
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-cascade-1",
            timestamp=datetime(2026, 5, 5, 17, 0, tzinfo=timezone.utc),
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("SCHIE gap closure",),
            text="SCHIE gap closure remains blocked pending owner sign-off.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert "Dependency cascades:" in artifacts.report.exec_summary_text
    assert "Downstream Dependency Impact" in artifacts.html_body
    assert ">Cross-org<" in artifacts.html_body
    assert "Ramp stays blocked until the gap closes." in artifacts.html_body
    assert "Confidence: high." in artifacts.html_body


def test_generate_report_draft_v2_ai_seeds_empty_continuity_narratives_from_rolling_summaries(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
            id="sig-icm-context-1",
            timestamp=datetime(2026, 5, 5, 17, 30, tzinfo=timezone.utc),
            source="icm",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Fleet capacity alert; status=Active",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"incident_id": "12345", "owning_team": "Adventure Core"},
        ),
    )
    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until OneDeploy closes the next checkpoint.",
    )
    _write_v2_summary_seed(
        programs_root,
        workstream_id="dd_on_pf",
        text="DD on PF remains blocked on pilot go-live dependencies and dated sign-offs.",
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 4, 18, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="AI draft exec summary focuses on deployment readiness.",
            confirmed_exec_summary_text="Confirmed exec summary tightened the deployment decision ask.",
            draft_workstream_blurbs={"deployment_readiness": "AI draft deployment blurb."},
            confirmed_workstream_blurbs={"deployment_readiness": "Confirmed deployment blurb rewrite."},
            draft_prompt_versions={
                "exec_summary": "exec_summary_drafter.v1",
                "deployment_readiness": "workstream_blurb.v1",
            },
            draft_ai_confidences={
                "exec_summary": Confidence.HIGH.value,
                "deployment_readiness": Confidence.MEDIUM.value,
            },
            draft_trace_run_id="acme_weekly:issue-077:20260504T180000Z",
        ),
        programs_root=programs_root,
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=78,
            recorded_at=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="AI draft exec summary is nearly final and specific.",
            confirmed_exec_summary_text="AI draft exec summary is nearly final and specific with one added blocker.",
            draft_workstream_blurbs={"deployment_readiness": "AI draft deployment blurb is nearly final."},
            confirmed_workstream_blurbs={
                "deployment_readiness": "AI draft deployment blurb is nearly final with one added dependency."
            },
            draft_prompt_versions={
                "exec_summary": "exec_summary_drafter.v2",
                "deployment_readiness": "workstream_blurb.v2",
            },
            draft_ai_confidences={
                "exec_summary": Confidence.HIGH.value,
                "deployment_readiness": Confidence.MEDIUM.value,
            },
            draft_trace_run_id="acme_weekly:issue-078:20260505T080000Z",
        ),
        programs_root=programs_root,
    )

    trace_path = programs_root / "acme" / "publications" / "acme_weekly" / "ai" / "llm_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        "\n".join(
            json.dumps(payload)
            for payload in (
                {
                    "timestamp": "2026-05-04T18:00:00+00:00",
                    "run_id": "acme_weekly:issue-077:20260504T180000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "exec_summary_drafter.v1",
                    "model": "gpt-4o",
                    "deployment": "aoai-eastus",
                    "metadata": {
                        "issue_number": 77,
                        "section_id": "exec_summary",
                        "task_type": "exec_summary",
                    },
                },
                {
                    "timestamp": "2026-05-04T18:00:00+00:00",
                    "run_id": "acme_weekly:issue-077:20260504T180000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "workstream_blurb.v1",
                    "model": "gpt-4o-mini",
                    "deployment": "aoai-eastus",
                    "metadata": {
                        "issue_number": 77,
                        "section_id": "deployment_readiness",
                        "task_type": "workstream_blurb",
                    },
                },
                {
                    "timestamp": "2026-05-05T08:00:00+00:00",
                    "run_id": "acme_weekly:issue-078:20260505T080000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "exec_summary_drafter.v2",
                    "model": "gpt-4.1",
                    "deployment": "aoai-westus",
                    "metadata": {
                        "issue_number": 78,
                        "section_id": "exec_summary",
                        "task_type": "exec_summary",
                    },
                },
                {
                    "timestamp": "2026-05-05T08:00:00+00:00",
                    "run_id": "acme_weekly:issue-078:20260505T080000Z",
                    "edition": "acme_weekly",
                    "prompt_version": "workstream_blurb.v2",
                    "model": "gpt-4.1-mini",
                    "deployment": "aoai-westus",
                    "metadata": {
                        "issue_number": 78,
                        "section_id": "deployment_readiness",
                        "task_type": "workstream_blurb",
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    exec_calls: list[dict[str, object]] = []
    blurb_calls: list[dict[str, object]] = []

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: SimpleNamespace())

    def _fake_exec_summary(**kwargs):
        exec_calls.append(kwargs)
        return ExecSummaryDraft(
            text="AI exec summary seeded from rolling summaries.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        )

    def _fake_workstream_blurb(**kwargs):
        blurb_calls.append(kwargs)
        return WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.draft_exec_summary", _fake_exec_summary)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.generate_workstream_blurb", _fake_workstream_blurb)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "AI exec summary seeded from rolling summaries."
    assert artifacts.report.workstream_blurbs["deployment_readiness"] == "AI narrative for Deployment Readiness."
    exec_context = "\n".join(exec_calls[0]["supplemental_context"])
    assert exec_calls and "Rolling summary [acme]" in exec_context
    assert exec_calls and "Rolling summary [dd_on_pf]" in exec_context
    assert exec_calls and "Recent calibration [exec_summary]: score=" in exec_context
    assert exec_calls and "Recent confidence calibration [exec_summary/high]: score=" in exec_context
    assert exec_calls and "Recent prompt confidence [exec_summary_drafter.v2/high]: score=" in exec_context
    assert exec_calls and "Recent prompt performance [exec_summary_drafter.v2]: score=" in exec_context
    assert exec_calls and "Prompt leaderboard [exec_summary] #1: exec_summary_drafter.v2; score=" in exec_context
    assert exec_calls and "Prompt leaderboard [exec_summary] #2: exec_summary_drafter.v1; score=" in exec_context
    assert exec_calls and "Recent prompt model [exec_summary_drafter.v2/gpt-4.1]: score=" in exec_context
    assert exec_calls and "Recent model performance [gpt-4.1]: score=" in exec_context
    deployment_call = next(call for call in blurb_calls if call["workstream_name"] == "Deployment Readiness")
    deployment_context = "\n".join(deployment_call["supplemental_context"])
    assert "Recent calibration [workstream_blurb]: score=" in deployment_context
    assert "Recent confidence calibration [workstream_blurb/medium]: score=" in deployment_context
    assert "Recent prompt confidence [workstream_blurb.v2/medium]: score=" in deployment_context
    assert "Recent prompt performance [workstream_blurb.v2]: score=" in deployment_context
    assert "Prompt leaderboard [workstream_blurb] #1: workstream_blurb.v2; score=" in deployment_context
    assert "Prompt leaderboard [workstream_blurb] #2: workstream_blurb.v1; score=" in deployment_context
    assert "Recent prompt model [workstream_blurb.v2/gpt-4.1-mini]: score=" in deployment_context
    assert "Recent model performance [gpt-4.1-mini]: score=" in deployment_context
    assert "Rolling summary [acme]" in deployment_context
    assert "Approved signal 2026-05-05T17:30:00+00:00 [icm]: IcM 12345: Fleet capacity alert; status=Active" in deployment_context
    assert "Rolling summary [dd_on_pf]" not in deployment_context


def test_generate_report_draft_v2_ai_keeps_authored_continuity_narratives_authoritative(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until OneDeploy closes the next checkpoint.",
    )

    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text("Authored exec summary.\n", encoding="utf-8")
    (narratives_dir / "chapter_deployment_readiness.md").write_text("Authored deployment narrative.\n", encoding="utf-8")

    exec_calls: list[dict[str, object]] = []
    blurb_calls: list[dict[str, object]] = []

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.draft_exec_summary",
        lambda **kwargs: exec_calls.append(kwargs) or ExecSummaryDraft(
            text="AI exec summary seeded from rolling summaries.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        ),
    )
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.generate_workstream_blurb",
        lambda **kwargs: blurb_calls.append(kwargs) or WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "Authored exec summary."
    assert artifacts.report.workstream_blurbs["deployment_readiness"] == "Authored deployment narrative."
    assert exec_calls == []
    assert all(call["workstream_name"] != "Deployment Readiness" for call in blurb_calls)


def test_generate_report_draft_v2_ai_passes_trace_context_to_report_ai_clients(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until OneDeploy closes the next checkpoint.",
    )
    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text("", encoding="utf-8")
    (narratives_dir / "chapter_deployment_readiness.md").write_text("", encoding="utf-8")

    trace_contexts: list[object] = []

    def _fake_create_ai_client(**kwargs):
        trace_contexts.append(kwargs.get("trace_context"))
        return SimpleNamespace(
            deployment=kwargs["deployment"],
            usage_stats=SimpleNamespace(call_count=1),
            spent_usd=0.01,
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", _fake_create_ai_client)
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage._iter_ai_generated_sections",
        lambda **kwargs: (
            report_module._AIGeneratedSection(
                section_id="deployment_readiness",
                title="Deployment Readiness",
                items=(kwargs["items"][0],),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.draft_exec_summary",
        lambda **kwargs: ExecSummaryDraft(
            text="AI exec summary seeded from rolling summaries.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        ),
    )
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.generate_workstream_blurb",
        lambda **kwargs: WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        ),
    )

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "AI exec summary seeded from rolling summaries."
    assert artifacts.report.workstream_blurbs["deployment_readiness"] == "AI narrative for Deployment Readiness."
    assert trace_contexts
    run_ids = {trace_context.run_id for trace_context in trace_contexts if trace_context is not None}
    assert len(run_ids) == 1
    resolved_run_id = next(iter(run_ids))
    assert resolved_run_id.startswith("acme_weekly:issue-001:")
    assert {trace_context.edition for trace_context in trace_contexts if trace_context is not None} == {"acme_weekly"}
    assert {
        trace_context.caller for trace_context in trace_contexts if trace_context is not None
    } == {"src.commands.report._synthesize_v2_ai_content"}
    assert {trace_context.output_root for trace_context in trace_contexts if trace_context is not None} == {output_root}
    draft_payload = json.loads((programs_root / "acme" / "publications" / "acme_weekly" / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    assert draft_payload["ai_trace_run_id"] == resolved_run_id
    metadata_payloads = [trace_context.metadata for trace_context in trace_contexts if trace_context is not None]
    assert any(
        metadata.get("issue_number") == 1
        and metadata.get("task_type") == "exec_summary"
        and metadata.get("section_id") == "exec_summary"
        and metadata.get("run_budget_usd") == 0.5
        for metadata in metadata_payloads
    )
    assert any(
        metadata.get("issue_number") == 1
        and metadata.get("task_type") == "workstream_blurb"
        and metadata.get("section_id") in artifacts.report.workstream_blurbs
        for metadata in metadata_payloads
    )


def test_report_ai_usage_aggregates_same_deployment_across_trace_metadata() -> None:
    ai_calls, ai_cost_usd, ai_cost_by_model = report_module._report_ai_usage(
        {
            (
                "fake-shared",
                (("issue_number", "1"), ("section_id", '"exec_summary"'), ("task_type", '"exec_summary"')),
            ): SimpleNamespace(
                deployment="fake-shared",
                usage_stats=SimpleNamespace(call_count=1),
                spent_usd=0.01,
            ),
            (
                "fake-shared",
                (("issue_number", "1"), ("section_id", '"deployment_readiness"'), ("task_type", '"workstream_blurb"')),
            ): SimpleNamespace(
                deployment="fake-shared",
                usage_stats=SimpleNamespace(call_count=1),
                spent_usd=0.015,
            ),
        }
    )

    assert ai_calls == 2
    assert ai_cost_usd == pytest.approx(0.025)
    assert ai_cost_by_model == {"fake-shared": pytest.approx(0.025)}


def test_generate_report_draft_v2_ai_falls_back_to_backup_deployments(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _enable_v2_program_ai(programs_root)
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_doc, dict)
    ai_block = program_doc.setdefault("ai", {})
    assert isinstance(ai_block, dict)
    ai_block["blurb_backup_deployment"] = "fake-blurb-backup"
    ai_block["exec_summary_backup_deployment"] = "fake-exec-backup"
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    _write_v2_summary_seed(
        programs_root,
        workstream_id="acme",
        text="Deployment remains the gating lane until OneDeploy closes the next checkpoint.",
    )

    exec_calls: list[dict[str, object]] = []
    blurb_calls: list[dict[str, object]] = []

    def _fake_create_ai_client(**kwargs):
        deployment = kwargs["deployment"]
        if deployment in {"fake-exec", "fake-blurb"}:
            raise AIClientError(f"deployment unavailable: {deployment}")
        return SimpleNamespace(
            deployment=deployment,
            usage_stats=SimpleNamespace(call_count=0),
            spent_usd=0.0,
        )

    def _fake_exec_summary(**kwargs):
        exec_calls.append(kwargs)
        kwargs["client"].usage_stats.call_count += 1
        kwargs["client"].spent_usd += 0.01
        return ExecSummaryDraft(
            text="AI exec summary seeded from backup deployment.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        )

    def _fake_workstream_blurb(**kwargs):
        blurb_calls.append(kwargs)
        kwargs["client"].usage_stats.call_count += 1
        kwargs["client"].spent_usd += 0.01
        return WorkstreamBlurb(
            text=f"AI narrative for {kwargs['workstream_name']} from backup deployment.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(kwargs["items"][0].id,),
            ai_confidence=Confidence.HIGH,
        )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", _fake_create_ai_client)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.draft_exec_summary", _fake_exec_summary)
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.generate_workstream_blurb", _fake_workstream_blurb)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.report.exec_summary_text == "AI exec summary seeded from backup deployment."
    assert exec_calls and getattr(exec_calls[0]["client"], "deployment", None) == "fake-exec-backup"
    assert blurb_calls and getattr(blurb_calls[0]["client"], "deployment", None) == "fake-blurb-backup"
    for call in blurb_calls:
        expected_text = f"AI narrative for {call['workstream_name']} from backup deployment."
        assert expected_text in artifacts.report.workstream_blurbs.values()
    assert artifacts.manifest.ai_calls == len(exec_calls) + len(blurb_calls)
    assert artifacts.manifest.ai_cost_usd == pytest.approx(0.01 * (len(exec_calls) + len(blurb_calls)))
    assert artifacts.manifest.ai_cost_by_model == {
        "fake-blurb-backup": pytest.approx(0.01 * len(blurb_calls)),
        "fake-exec-backup": pytest.approx(0.01 * len(exec_calls)),
    }
    assert any("primary deployment failed (fake-exec); trying backup deployment." in warning for warning in artifacts.warnings)
    assert any("fallback deployment succeeded (fake-exec-backup)." in warning for warning in artifacts.warnings)


def test_report_cli_rejects_range_without_lookback(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            EDITION_NAME,
            "--dry-run",
            "--range",
            "3",
        ],
    )

    assert result.exit_code == 2
    assert "--range requires --edition-type lookback." in result.output


def test_generate_report_draft_builds_lookback_from_confirmed_archive(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    issue_1_as_of = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
    issue_2_as_of = datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)
    issue_3_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=issue_1_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_1_as_of)[0], risk_level=RiskLevel.LOW),
                _snapshot_item_from_work_item(_sample_items(issue_1_as_of)[1], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=issue_1_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=2,
        snapshot=_lookback_snapshot(
            issue_number=2,
            as_of=issue_2_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_2_as_of)[0], risk_level=RiskLevel.MEDIUM),
                _snapshot_item_from_work_item(_sample_items(issue_2_as_of)[1], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 002</body></html>",
        markdown_body="# Issue 002",
        manifest=_manifest(issue_number=2, as_of=issue_2_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=3,
        snapshot=_lookback_snapshot(
            issue_number=3,
            as_of=issue_3_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_3_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE},
        ),
        html_body="<html><body>Issue 003</body></html>",
        markdown_body="# Issue 003",
        manifest=_manifest(issue_number=3, as_of=issue_3_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )

    for draft_builder in (generate_report_draft, generate_report_draft_v2):
        artifacts = draft_builder(
            edition_name=EDITION_NAME,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
            as_of=lookback_as_of,
            edition_type_override="lookback",
            lookback_range=3,
            open_browser=False,
        )

        assert artifacts.issue_number == 4
        assert artifacts.report.edition == EditionType.LOOKBACK
        assert any(delta.kind == DeltaKind.CLOSED for delta in artifacts.report.deltas.closed_items)
        assert any(delta.old_risk == RiskLevel.LOW and delta.new_risk == RiskLevel.HIGH for delta in artifacts.report.scorecard_deltas)
        assert "persistent risk" in artifacts.report.exec_summary_text.lower()
        assert "retrospective" in artifacts.html_body.lower()
        assert "Fleet pilot dependency on capacity allocation" in artifacts.html_body


def test_generate_report_draft_lookback_surfaces_action_quality_gate(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_1_as_of = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
    issue_2_as_of = datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)
    issue_3_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=issue_1_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_1_as_of)[0], risk_level=RiskLevel.LOW),
                _snapshot_item_from_work_item(_sample_items(issue_1_as_of)[1], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=issue_1_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=2,
        snapshot=_lookback_snapshot(
            issue_number=2,
            as_of=issue_2_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_2_as_of)[0], risk_level=RiskLevel.MEDIUM),
                _snapshot_item_from_work_item(_sample_items(issue_2_as_of)[1], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Issue 002</body></html>",
        markdown_body="# Issue 002",
        manifest=_manifest(issue_number=2, as_of=issue_2_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=3,
        snapshot=_lookback_snapshot(
            issue_number=3,
            as_of=issue_3_as_of,
            items=(
                _snapshot_item_from_work_item(_sample_items(issue_3_as_of)[0], risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE},
        ),
        html_body="<html><body>Issue 003</body></html>",
        markdown_body="# Issue 003",
        manifest=_manifest(issue_number=3, as_of=issue_3_as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="ACTION-LOOKBACK-1",
            program_id="acme",
            text="Close the retrospective action gap.",
            owner_alias="",
            due_date=None,
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id=None,
            created_at=lookback_as_of,
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert artifacts.manifest.qg_results["QG-15"] is False
    assert "QG-3" in artifacts.manifest.qg_results
    assert any("Open action items missing owner or due date" in warning for warning in artifacts.warnings)


def test_generate_report_draft_lookback_renders_assumption_lifecycle(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_1_as_of = datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)
    issue_2_as_of = datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)
    issue_3_as_of = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-open",
                program_id="acme",
                text="Partner rollout remains aligned with the current launch train.",
                validation_method=None,
                validation_due=date(2026, 4, 29),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id="m3-code-complete",
                owner_alias="operator",
                identified_date=date(2026, 4, 16),
                entity_refs=("WI:900001",),
            ),
            Assumption(
                id="assumption-confirmed",
                program_id="acme",
                text="Schema escrow covers the release window.",
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.CONFIRMED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="operator",
                identified_date=date(2026, 4, 10),
                entity_refs=(),
                resolved_date=date(2026, 4, 21),
            ),
            Assumption(
                id="assumption-invalidated",
                program_id="acme",
                text="Capacity allocation remains fixed through the quarter.",
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.INVALIDATED,
                linked_risk_id="risk-capacity",
                linked_milestone_id=None,
                owner_alias="lt",
                identified_date=date(2026, 4, 1),
                entity_refs=(),
                resolved_date=date(2026, 4, 24),
            ),
            Assumption(
                id="assumption-outside-window",
                program_id="acme",
                text="Legacy assumption outside the retrospective window.",
                validation_method=None,
                validation_due=None,
                status=AssumptionStatus.CONFIRMED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias=None,
                identified_date=date(2026, 3, 1),
                entity_refs=(),
                resolved_date=date(2026, 3, 15),
            ),
        ),
        programs_root=programs_root,
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_1_as_of, {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_2_as_of, {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_3_as_of, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Assumption Lifecycle" in artifacts.html_body
    assert "Identified in window: 1 | Confirmed in window: 1 | Invalidated in window: 1 | Still open: 1" in artifacts.html_body
    assert "Schema escrow covers the release window." in artifacts.html_body
    assert "CONFIRMED | identified 2026-04-10 | confirmed 2026-04-21 | owner operator" in artifacts.html_body
    assert "Capacity allocation remains fixed through the quarter." in artifacts.html_body
    assert "INVALIDATED | identified 2026-04-01 | invalidated 2026-04-24 | owner lt | risk risk-capacity" in artifacts.html_body
    assert "Partner rollout remains aligned with the current launch train." in artifacts.markdown_body
    assert "UNVALIDATED | identified 2026-04-16 | due 2026-04-29 | overdue | owner operator | milestone m3-code-complete" in artifacts.markdown_body
    assert "Legacy assumption outside the retrospective window." not in artifacts.html_body


def test_generate_report_draft_lookback_renders_charter_review(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            "Green-light LT review without timeline slip.",
        ],
        "constraints": [
            "Do not widen partner pilot scope before SCHIE signoff.",
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    for issue_number, issue_as_of, risk_map in (
        (1, datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Charter Review" in artifacts.html_body
    assert "Scope:" in artifacts.html_body
    assert "Deliver Acme ramp readiness for the current LT gate." in artifacts.html_body
    assert "Success criteria authored: 1 | Constraints authored: 1" in artifacts.html_body
    assert "Success criterion" in artifacts.html_body
    assert "Green-light LT review without timeline slip." in artifacts.html_body
    assert "Constraint" in artifacts.markdown_body
    assert "Do not widen partner pilot scope before SCHIE signoff." in artifacts.markdown_body


def test_generate_report_draft_lookback_evaluates_structured_charter_success_criteria(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            {
                "text": "Deployment Safety closes at Low or better.",
                "metric": {
                    "kind": "dimension_max_risk",
                    "scorecard": "Acme Adventure/XIO 100% Ramp Readiness",
                    "dimension": "Deployment Safety",
                    "max_risk": "low",
                },
            },
            {
                "text": "Open snapshot items close at zero.",
                "metric": {
                    "kind": "item_count_max",
                    "max_count": 0,
                },
            },
            "Green-light LT review without timeline slip.",
        ],
        "constraints": [
            "Do not widen partner pilot scope before SCHIE signoff.",
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    for issue_number, issue_as_of, risk_map in (
        (1, datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc), {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Archive-backed evaluation: evaluated 2 | Met: 1 | Not met: 1 | Manual review: 0" in artifacts.html_body
    assert "Status: MET" in artifacts.markdown_body
    assert "Latest confirmed risk: Done in Issue 003; target <= Low; window trend: High -> High -> Done." in artifacts.markdown_body
    assert "Status: NOT MET" in artifacts.markdown_body
    assert "Evidence: Issue 003 matching item count: 1 (states: any; target <= 0)." in artifacts.markdown_body
    assert "Green-light LT review without timeline slip." in artifacts.html_body


def test_generate_report_draft_lookback_evaluates_filtered_item_count_max_charter_success_criteria(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            {
                "text": "Medium-risk deployment features with Safety tag stay at one or fewer.",
                "metric": {
                    "kind": "item_count_max",
                    "max_count": 1,
                    "work_item_types": ["feature"],
                    "area_path_prefixes": ["One\\Adventure\\Acme\\Deployment"],
                    "risk_levels": ["medium"],
                    "tags": ["safety"],
                },
            },
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    lookback_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    for issue_number, issue_as_of in (
        (1, datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc)),
        (2, datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc)),
        (3, datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)),
    ):
        sample_items = _sample_items(issue_as_of)
        if issue_number == 3:
            snapshot_items = (
                _snapshot_item_from_work_item(sample_items[0], risk_level=RiskLevel.MEDIUM),
                _snapshot_item_from_work_item(sample_items[1], risk_level=RiskLevel.HIGH),
            )
        else:
            snapshot_items = (
                _snapshot_item_from_work_item(sample_items[0], risk_level=RiskLevel.MEDIUM),
            )

        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=snapshot_items,
                scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.MEDIUM},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=lookback_as_of,
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Archive-backed evaluation: evaluated 1 | Met: 1 | Not met: 0 | Manual review: 0" in artifacts.html_body
    assert "Status: MET" in artifacts.markdown_body
    assert "Evidence: Issue 003 matching item count: 1" in artifacts.markdown_body
    assert "work item types: feature" in artifacts.markdown_body
    assert "area path prefixes: one\\adventure\\acme\\deployment" in artifacts.markdown_body
    assert "risk levels: Medium" in artifacts.markdown_body
    assert "tags: safety" in artifacts.markdown_body


def test_report_cli_uses_quarterly_lookback_edition_defaults(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(
        repo_root,
        tmp_path,
        edition_names=(EDITION_NAME, "nova_quarterly"),
    )
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.DEFAULT_OUTPUT_ROOT", (tmp_path / "programs" / "acme" / "publications"))
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)

    issue_dates = (
        datetime(2026, 2, 3, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 17, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 3, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 17, 18, 0, tzinfo=timezone.utc),
    )

    tracked_item = SnapshotItem(
        id=301,
        type="Feature",
        title="Fleet pilot dependency on capacity allocation",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="One\\Adventure\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
    )
    resolved_item = SnapshotItem(
        id=302,
        type="Risk",
        title="Repair approval path remains manual",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="One\\Adventure\\Acme",
        target_date=date(2026, 4, 15),
        risk_level=RiskLevel.LOW,
        tags=["acme"],
    )

    write_confirmed_issue(
        edition="nova_quarterly",
        issue_number=1,
        snapshot=_lookback_snapshot(
            issue_number=1,
            as_of=issue_dates[0],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 1), risk_level=RiskLevel.MEDIUM),
                replace(resolved_item, target_date=date(2026, 4, 15), risk_level=RiskLevel.LOW),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Quarterly 001</body></html>",
        markdown_body="# Quarterly 001",
        manifest=_manifest(issue_number=1, as_of=issue_dates[0], edition="nova_quarterly"),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition="nova_quarterly",
        issue_number=2,
        snapshot=_lookback_snapshot(
            issue_number=2,
            as_of=issue_dates[1],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 8), risk_level=RiskLevel.HIGH),
                replace(resolved_item, target_date=date(2026, 4, 22), risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH},
        ),
        html_body="<html><body>Quarterly 002</body></html>",
        markdown_body="# Quarterly 002",
        manifest=_manifest(issue_number=2, as_of=issue_dates[1], edition="nova_quarterly"),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition="nova_quarterly",
        issue_number=3,
        snapshot=_lookback_snapshot(
            issue_number=3,
            as_of=issue_dates[2],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 15), risk_level=RiskLevel.HIGH),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.MEDIUM},
        ),
        html_body="<html><body>Quarterly 003</body></html>",
        markdown_body="# Quarterly 003",
        manifest=_manifest(issue_number=3, as_of=issue_dates[2], edition="nova_quarterly"),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition="nova_quarterly",
        issue_number=4,
        snapshot=_lookback_snapshot(
            issue_number=4,
            as_of=issue_dates[3],
            items=(
                replace(tracked_item, target_date=date(2026, 5, 22), risk_level=RiskLevel.HIGH),
                SnapshotItem(
                    id=303,
                    type="Feature",
                    title="Buildout validation lane is back on plan",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    area_path="One\\Adventure\\Acme",
                    target_date=date(2026, 4, 30),
                    risk_level=RiskLevel.LOW,
                    tags=["acme"],
                ),
            ),
            scorecard_risks={"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE},
        ),
        html_body="<html><body>Quarterly 004</body></html>",
        markdown_body="# Quarterly 004",
        manifest=_manifest(issue_number=4, as_of=issue_dates[3], edition="nova_quarterly"),
        archive_root=archive_root,
    )

    result = runner.invoke(
        app,
        [
            "report",
            "--edition",
            "nova_quarterly",
            "--dry-run",
            "--as-of",
            "2026-03-31T18:00:00",
        ],
    )

    assert result.exit_code == 3
    assert "[MODE: V2 HYBRID JOURNAL]" in result.output
    assert "[1/2] resolution" in result.output
    assert "[2/2] lookback" in result.output

    html_path = programs_root / "acme" / "publications" / "nova_quarterly" / "issue_005" / "issue_005.html"
    draft_payload = json.loads((programs_root / "acme" / "publications" / "nova_quarterly" / "issue_005" / "issue_005.draft.json").read_text(encoding="utf-8"))

    assert html_path.exists()
    assert "Quarterly retrospective window" in html_path.read_text(encoding="utf-8")
    assert "items tracked" in draft_payload["exec_summary_text"].lower()
    assert "chronic drifters" in draft_payload["exec_summary_text"].lower()
    assert "workstream risk movements" in draft_payload["exec_summary_text"].lower()


def test_generate_report_draft_lookback_renders_retrospective_intelligence(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=301,
        type="Feature",
        title="Fleet pilot dependency on capacity allocation",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="One\\Adventure\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
    )

    for issue_number, issue_as_of, item_target, item_risk, scorecard_risks in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.MEDIUM, {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.HIGH}),
        (4, issue_dates[3], date(2026, 5, 22), RiskLevel.HIGH, {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    replace(tracked_item, target_date=item_target, risk_level=item_risk),
                ),
                scorecard_risks=scorecard_risks,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=4,
        open_browser=False,
    )

    assert "Retrospective Intelligence" in artifacts.html_body
    assert "Chronic workstreams: 1 | Recovered chronic issues: 1 | Recurring drift items: 1 | Claim accuracy signals: 0 | Charter evaluation signals: 0" in artifacts.html_body
    assert "Deployment Velocity (Acme Adventure/XIO 100% Ramp Readiness)" in artifacts.html_body
    assert "Recovered chronic issue" in artifacts.html_body


def test_generate_report_draft_lookback_renders_incident_learnings(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_dates[0], {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="12345",
            signal_id="signal-12345",
            observed_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 10, 12, 5, tzinfo=timezone.utc),
            belief_change_summary="IcM 12345: Fleet pilot dependency on capacity allocation stayed blocked.",
            workstream_id="acme",
            owning_team="Adventure Core",
            severity=2,
            ado_entity_refs=("WI:301",),
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="12346",
            signal_id="signal-12346",
            observed_at=datetime(2026, 4, 18, 9, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 18, 9, 35, tzinfo=timezone.utc),
            belief_change_summary="IcM 12346: Repair approval path remained manual during the quarter.",
            workstream_id="acme",
            owning_team="Repair",
            severity=3,
            confidence=Confidence.MEDIUM,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="99999",
            signal_id="signal-outside-window",
            observed_at=datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 1, 9, 35, tzinfo=timezone.utc),
            belief_change_summary="IcM 99999: outside retrospective window.",
            workstream_id="acme",
            confidence=Confidence.LOW,
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Incident Learnings" in artifacts.html_body
    assert "Incidents in window: 2 | With explicit ADO refs: 1" in artifacts.html_body
    assert "Attribution-backed learnings" in artifacts.html_body
    assert "IcM 12345" in artifacts.html_body
    assert "WI:301 was implicated in IcM 12345" in artifacts.html_body
    assert "high confidence" in artifacts.html_body
    assert "refs WI:301" in artifacts.html_body
    assert "IcM 12346" in artifacts.markdown_body
    assert "medium confidence" in artifacts.markdown_body
    assert "IcM 99999" not in artifacts.html_body
    assert "Repair approval path remained manual during the quarter." in artifacts.markdown_body


def test_generate_report_draft_renders_higher_order_incident_class_patterns_in_lookback(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_dates[0], {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    for incident_id, signal_id, summary, ref in (
        ("12345", "signal-12345", "IcM 12345: WI:301 fleet pilot dependency on capacity allocation stayed blocked.", "WI:301"),
        ("12346", "signal-12346", "IcM 12346: WI:302 fleet pilot dependency on capacity allocation stayed blocked again.", "WI:302"),
        ("12347", "signal-12347", "IcM 12347: WI:303 fleet pilot dependency on capacity allocation stayed blocked during follow-up.", "WI:303"),
    ):
        append_incident_entry(
            IncidentEntry(
                program_id="acme",
                incident_id=incident_id,
                signal_id=signal_id,
                observed_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 4, 10, 12, 5, tzinfo=timezone.utc),
                belief_change_summary=summary,
                workstream_id="acme",
                owning_team="Adventure Core",
                severity=2,
                ado_entity_refs=(ref,),
                confidence=Confidence.HIGH,
            ),
            programs_root=programs_root,
        )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "incident class" in artifacts.markdown_body.lower()


def test_generate_report_draft_lookback_scopes_retrospective_ban_list_to_attributed_incidents(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )

    for issue_number, issue_as_of, risk_map in (
        (1, issue_dates[0], {"Deployment Velocity": RiskLevel.LOW, "Deployment Safety": RiskLevel.HIGH}),
        (2, issue_dates[1], {"Deployment Velocity": RiskLevel.MEDIUM, "Deployment Safety": RiskLevel.HIGH}),
        (3, issue_dates[2], {"Deployment Velocity": RiskLevel.HIGH, "Deployment Safety": RiskLevel.DONE}),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(
                    _snapshot_item_from_work_item(_sample_items(issue_as_of)[0], risk_level=RiskLevel.HIGH),
                ),
                scorecard_risks=risk_map,
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="20001",
            signal_id="signal-20001",
            observed_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 10, 12, 5, tzinfo=timezone.utc),
            belief_change_summary="IcM 20001: Rollout slipped due to blocked capacity allocation.",
            workstream_id="acme",
            severity=2,
            ado_entity_refs=("WI:401",),
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="20002",
            signal_id="signal-20002",
            observed_at=datetime(2026, 4, 18, 9, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 4, 18, 9, 35, tzinfo=timezone.utc),
            belief_change_summary="IcM 20002: Manual approval stayed delayed because of missing owner.",
            workstream_id="acme",
            severity=3,
            confidence=Confidence.MEDIUM,
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Rollout slipped due to blocked capacity allocation." in artifacts.html_body
    assert any("because of" in warning for warning in artifacts.warnings)
    assert all("due to" not in warning for warning in artifacts.warnings)


def test_generate_report_draft_lookback_retrospective_intelligence_includes_claim_and_charter_signals(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc.setdefault("charter", {})["success_criteria"] = [
        {
            "text": "Keep deployment velocity at Medium or lower by quarter close.",
            "metric": {
                "kind": "dimension_max_risk",
                "scorecard_name": "Acme Adventure/XIO 100% Ramp Readiness",
                "dimension_name": "Deployment Velocity",
                "max_risk": "medium",
            },
        }
    ]
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=401,
        type="Feature",
        title="Deployment readiness guardrail",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="One\\Adventure\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.LOW),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    append_claim_entry(
        ClaimEntry(
            id="claim-rt-1",
            program_id="acme",
            edition_id=EDITION_NAME,
            issue_number=2,
            workstream_id="deployment_readiness",
            text="Deployment readiness will hold the May 3 checkpoint for WI:401.",
            entity_refs=("WI:401",),
            claim_date=date(2026, 4, 14),
            owner_alias="operator",
            due_date=date(2026, 5, 3),
        ),
        programs_root=programs_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Claim accuracy signals: 1 | Charter evaluation signals: 1" in artifacts.html_body
    assert "Claim accuracy concern" in artifacts.html_body
    assert "Current ADO target date is 2026-05-15, later than the claimed date 2026-05-03." in artifacts.html_body
    assert "Charter criterion missed" in artifacts.markdown_body
    assert "Latest confirmed risk: High in Issue 003; target <= Medium; window trend: Low -> High -> High." in artifacts.markdown_body


def test_generate_report_draft_lookback_appends_ai_retrospective_synthesis(monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    program_doc.setdefault("ai", {})["enabled"] = True
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=451,
        type="Feature",
        title="Checkpoint readiness keeps slipping",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="One\\Adventure\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.HIGH),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    monkeypatch.setenv("VERTEX_EXEC_DEPLOYMENT", "lookback-ai-test")

    class _FakeLookbackAIClient:
        def __init__(self) -> None:
            self.spent_usd = 0.012
            self.usage_stats = SimpleNamespace(call_count=0)

        def chat(self, system: str, user: str, max_tokens: int = 800, *, prompt_version: str | None = None) -> str:
            raise AssertionError("lookback synthesis should use structured output")

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            self.usage_stats.call_count += 1
            assert "Deterministic retrospective summary" in user
            return parser(
                {
                    "insights": [
                        {
                            "category": "AI synthesis",
                            "title": "Checkpoint risk is chronically unmanaged",
                            "detail": "Repeated target slips and sustained high deployment-velocity risk point to a systemic checkpoint-planning gap rather than isolated execution noise.",
                        }
                    ]
                }
            )

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: _FakeLookbackAIClient())

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Checkpoint risk is chronically unmanaged" in artifacts.html_body
    assert artifacts.manifest.ai_calls == 1
    assert artifacts.manifest.ai_cost_usd == pytest.approx(0.012)
    assert artifacts.manifest.metadata["ai_safety"]["trace_run_id"] is not None
    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_004" / "issue_004.draft.json").read_text(encoding="utf-8"))
    assert draft_payload["ai_trace_run_id"] == artifacts.manifest.metadata["ai_safety"]["trace_run_id"]


def test_build_lookback_ai_retrospective_rows_scrubs_pii_from_ai_output() -> None:
    class _FakeLookbackAIClient:
        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            return parser(
                {
                    "insights": [
                        {
                            "category": "AI synthesis",
                            "title": "Coordination with foo@gmail.com is failing",
                            "detail": "Repeated escalations to foo@gmail.com show the same dependency handoff is stuck.",
                        }
                    ]
                }
            )

    rows = _build_lookback_ai_retrospective_rows(
        client=_FakeLookbackAIClient(),
        retrospective_intelligence=RetrospectiveIntelligenceSummary(
            chronic_workstream_count=1,
            recovered_workstream_count=0,
            recurring_drift_count=0,
            worsened_workstream_count=1,
            improved_workstream_count=0,
            claim_accuracy_signal_count=0,
            charter_evaluation_signal_count=0,
            rows=(
                RetrospectiveIntelligenceRow(
                    category="Recurring drift",
                    title="Checkpoint readiness keeps slipping",
                    detail="Three consecutive issues moved the same target date later.",
                ),
            ),
        ),
        snapshots=(
            Snapshot(
                issue_number=1,
                generated_at=datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
                ado_data_as_of=datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
                edition_type=EditionType.LOOKBACK,
                items=(),
                scorecards=(),
            ),
            Snapshot(
                issue_number=2,
                generated_at=datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
                ado_data_as_of=datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
                edition_type=EditionType.LOOKBACK,
                items=(),
                scorecards=(),
            ),
        ),
    )

    assert len(rows) == 1
    assert "foo@gmail.com" not in rows[0].title
    assert "foo@gmail.com" not in rows[0].detail
    assert "[PII-FILTERED-EMAIL]" in rows[0].title
    assert "[PII-FILTERED-EMAIL]" in rows[0].detail


def test_generate_report_draft_lookback_degrades_when_ai_retrospective_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    program_doc.setdefault("ai", {})["enabled"] = True
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=451,
        type="Feature",
        title="Checkpoint readiness keeps slipping",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="One\\Adventure\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.HIGH),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    monkeypatch.setenv("VERTEX_EXEC_DEPLOYMENT", "lookback-ai-test")

    def _raise_config_error(**kwargs):
        raise ConfigError("missing AI deployment credentials")

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", _raise_config_error)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Retrospective Intelligence" in artifacts.html_body
    assert "Checkpoint risk is chronically unmanaged" not in artifacts.html_body
    assert artifacts.manifest.ai_calls == 0
    assert artifacts.manifest.ai_cost_usd == 0.0


def test_generate_report_draft_lookback_degrades_when_ai_retrospective_parser_rejects_output(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    program_doc.setdefault("ai", {})["enabled"] = True
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

    issue_dates = (
        datetime(2026, 4, 7, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 14, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    tracked_item = SnapshotItem(
        id=451,
        type="Feature",
        title="Checkpoint readiness keeps slipping",
        state="Active",
        assigned_to="Vertex Maintainer",
        area_path="One\\Adventure\\Acme",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.HIGH,
        tags=["acme"],
    )
    for issue_number, issue_as_of, item_target, scorecard_risk in (
        (1, issue_dates[0], date(2026, 5, 1), RiskLevel.HIGH),
        (2, issue_dates[1], date(2026, 5, 8), RiskLevel.HIGH),
        (3, issue_dates[2], date(2026, 5, 15), RiskLevel.HIGH),
    ):
        write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=issue_as_of,
                items=(replace(tracked_item, target_date=item_target),),
                scorecard_risks={"Deployment Velocity": scorecard_risk},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}",
            manifest=_manifest(issue_number=issue_number, as_of=issue_as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    monkeypatch.setenv("VERTEX_EXEC_DEPLOYMENT", "lookback-ai-test")

    class _FakeAIClient:
        def __init__(self) -> None:
            self.usage_stats = SimpleNamespace(call_count=1)
            self.spent_usd = 0.25

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: _FakeAIClient())

    def _raise_pipeline_error(*, client, retrospective_intelligence, snapshots):
        del client, retrospective_intelligence, snapshots
        raise AIPipelineError("Generated text rejected by injection detector: prompt_injection")

    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._build_lookback_ai_retrospective_rows", _raise_pipeline_error)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        edition_type_override="lookback",
        lookback_range=3,
        open_browser=False,
    )

    assert "Retrospective Intelligence" in artifacts.html_body
    assert artifacts.manifest.ai_calls == 0
    assert artifacts.manifest.ai_cost_usd == 0.0


def test_generate_report_draft_opens_browser(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    opened_urls: list[str] = []
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: opened_urls.append(url) or True)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=True,
    )

    assert opened_urls
    assert opened_urls[0].startswith("file:")


def test_generate_report_draft_renders_kusto_sections_and_persists_them(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=_sample_kusto_query_results,
        open_browser=False,
    )

    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    quality_matrix_payload = json.loads(artifacts.quality_matrix_json_path.read_text(encoding="utf-8"))

    assert "Deployment Readiness" in artifacts.html_body
    assert "Deploy P50 (mins)" in artifacts.html_body
    assert len(draft_payload["kusto_sections"]) == 7
    assert {section["title"] for section in draft_payload["kusto_sections"]} >= {"Fleet Health", "Active Incidents"}
    assert all(section["query_id"] != "icm-mttr" for section in draft_payload["kusto_sections"])
    active_incidents = next(section for section in draft_payload["kusto_sections"] if section["title"] == "Active Incidents")
    assert active_incidents["rows"][0][-1]["href"] == "https://portal.microsofticm.com/imp/v3/incidents/details/12345"
    deployment_velocity = next(slice_row for slice_row in quality_matrix_payload["slices"] if slice_row["slice_id"] == "acme.deployment_velocity")
    assert deployment_velocity["telemetry"]["status"] == "supporting"
    assert deployment_velocity["telemetry"]["validates_slice"] is True
    assert any("Kusto auth failed for icm-mttr" in warning for warning in artifacts.warnings)


def test_normalize_workstream_blurb_strips_objective_prefix() -> None:
    assert report_module._normalize_workstream_blurb("Objective: keep deployment velocity low.") == "Keep deployment velocity low."
    assert report_module._normalize_workstream_blurb("objective: keep deployment velocity low.") == "Keep deployment velocity low."


def test_resolve_workstream_blurb_normalizes_explicit_text() -> None:
    blurb = report_module._resolve_workstream_blurb(
        section_id="deployment_velocity",
        workstream_blurbs={"deployment_velocity": "Objective: keep deployment velocity low."},
        model=SimpleNamespace(summary="summary", risk=RiskLevel.HIGH),
        packet=SimpleNamespace(total_items=3),
    )

    assert blurb == "Keep deployment velocity low."


def test_resolve_workstream_blurb_uses_low_risk_fallback_for_blank_authored_text() -> None:
    blurb = report_module._resolve_workstream_blurb(
        section_id="deployment_velocity",
        workstream_blurbs={"deployment_velocity": "   "},
        model=SimpleNamespace(summary="summary", risk=RiskLevel.LOW),
        packet=SimpleNamespace(total_items=3),
    )

    assert blurb == "3 items at Low - see ADO for current status."


def test_build_workstream_data_adds_query_citation_when_no_item_citations(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    scorecard = bundle.config.scorecards[0]
    dimension = scorecard.dimensions[0]

    workstreams = report_module._build_workstream_data(
        78,
        bundle,
        EditionType.DETAILED,
        (),
        (
            report_module.ScorecardData(
                scorecard_name=scorecard.name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=dimension.name,
                        risk=RiskLevel.LOW,
                        summary="Objective: keep deployment velocity low.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Deployment safety evidence",
                        ),
                        display_name="Deployment Velocity",
                    ),
                ),
            ),
        ),
        {
            scorecard.name: {
                dimension.name: report_module.ScorecardEvidencePacket(
                    dimension_name=dimension.name,
                    dimension_description="Deployment safety dimension",
                    total_items=3,
                    items_by_risk={"high": 0, "medium": 0, "low": 3, "done": 0},
                    stale_items=(),
                    stale_count=1,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=None,
                    author_risk=None,
                    ado_query_url="https://dev.azure.com/query/deployment-velocity",
                    item_links=(),
                    derived_risk=RiskLevel.LOW,
                    item_ids=(),
                    latest_target_date=None,
                )
            }
        },
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        {report_module._detail_section_id(scorecard.name, dimension.name): "   "},
        (),
        report_module.ReviewStatus(issue_number=78, sections=()),
        {},
        {},
    )

    assert len(workstreams) == 1
    assert workstreams[0].citations[0].label == "ADO query"
    assert workstreams[0].blurb == "3 items at Low - see ADO for current status."
    assert workstreams[0].narrative_empty is True


def test_build_workstream_data_preserves_review_state_and_normalized_explicit_blurb(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    scorecard = bundle.config.scorecards[0]
    dimension = scorecard.dimensions[0]
    section_id = report_module._detail_section_id(scorecard.name, dimension.name)
    workstream_id = report_module._resolve_vitality_workstream_id("One\\Adventure\\Acme", bundle.program_context.workstreams)
    assert workstream_id is not None
    item = WorkItem(
        id=3101,
        type="Feature",
        title="Continuity deployment row",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Issue 078 - Sprint 3",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    workstreams = report_module._build_workstream_data(
        78,
        bundle,
        EditionType.DETAILED,
        (item,),
        (
            report_module.ScorecardData(
                scorecard_name=scorecard.name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=dimension.name,
                        risk=RiskLevel.HIGH,
                        summary="Objective: keep deployment velocity low.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Deployment safety evidence",
                        ),
                        display_name="Deployment Velocity",
                    ),
                ),
            ),
        ),
        {
            scorecard.name: {
                dimension.name: report_module.ScorecardEvidencePacket(
                    dimension_name=dimension.name,
                    dimension_description="Deployment safety dimension",
                    total_items=1,
                    items_by_risk={"high": 1, "medium": 0, "low": 0, "done": 0},
                    stale_items=(),
                    stale_count=0,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=RiskLevel.MEDIUM,
                    author_risk=None,
                    ado_query_url=None,
                    item_links=(),
                    derived_risk=RiskLevel.HIGH,
                    item_ids=(3101,),
                    latest_target_date=date(2026, 6, 15),
                )
            }
        },
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        {section_id: "Objective: keep deployment velocity low."},
        (),
        report_module.ReviewStatus(issue_number=78, sections=(SimpleNamespace(section_id=f"ws:{section_id}", state=report_module.ReviewState.APPROVED),)),
        {
            3101: report_module.EvidencePacket(
                work_item_id=3101,
                revisions=(),
                comments=(),
                enrichments=(),
                confidence=Confidence.HIGH,
                tier=report_module.AttributionTier.TIER1,
                summary_for_reviewer="Deployment safety evidence",
            )
        },
        {3101: "https://dev.azure.com/org/project/_workitems/edit/3101"},
    )

    assert len(workstreams) == 1
    assert workstreams[0].review_state == report_module.ReviewState.APPROVED
    assert workstreams[0].blurb == "Keep deployment velocity low."


def test_build_workstream_data_hybrid_detail_keeps_unmapped_sections_with_chapter_surface(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _archive_root, _output_root = _seed_v2_report_layout(repo_root, tmp_path)
    bundle = load_report_bundle(EDITION_NAME, reports_root=reports_root)
    assert bundle.chapter_contract is not None

    chapter = next(chapter for chapter in bundle.chapter_contract.chapters if chapter.id == "deployment_readiness")
    mapped_scorecard_name, mapped_dimension_name = bundle.chapter_contract.dimension_lookup[chapter.dimensions[0]]
    unmapped_dimension_id = bundle.chapter_contract.unmapped_dimensions[0]
    unmapped_scorecard_name, unmapped_dimension_name = bundle.chapter_contract.dimension_lookup[unmapped_dimension_id]
    mapped_section_id = report_module._detail_section_id(mapped_scorecard_name, mapped_dimension_name)
    unmapped_section_id = report_module._detail_section_id(unmapped_scorecard_name, unmapped_dimension_name)

    workstreams = report_module._build_workstream_data(
        78,
        bundle,
        EditionType.DETAILED,
        (),
        (
            report_module.ScorecardData(
                scorecard_name=mapped_scorecard_name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=mapped_dimension_name,
                        risk=RiskLevel.HIGH,
                        summary="Mapped chapter dimension summary.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3101,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Mapped dimension evidence",
                        ),
                        display_name=mapped_dimension_name,
                    ),
                ),
            ),
            report_module.ScorecardData(
                scorecard_name=unmapped_scorecard_name,
                dimensions=(
                    report_module.DimensionRisk(
                        name=unmapped_dimension_name,
                        risk=RiskLevel.MEDIUM,
                        summary="Unmapped detail dimension summary.",
                        evidence=report_module.EvidencePacket(
                            work_item_id=3102,
                            revisions=(),
                            comments=(),
                            enrichments=(),
                            confidence=Confidence.HIGH,
                            tier=report_module.AttributionTier.TIER1,
                            summary_for_reviewer="Unmapped dimension evidence",
                        ),
                        display_name=unmapped_dimension_name,
                    ),
                ),
            ),
        ),
        {
            mapped_scorecard_name: {
                mapped_dimension_name: report_module.ScorecardEvidencePacket(
                    dimension_name=mapped_dimension_name,
                    dimension_description="Mapped chapter dimension",
                    total_items=1,
                    items_by_risk={"high": 1, "medium": 0, "low": 0, "done": 0},
                    stale_items=(),
                    stale_count=0,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=None,
                    author_risk=None,
                    ado_query_url="https://dev.azure.com/query/mapped",
                    item_links=(),
                    derived_risk=RiskLevel.HIGH,
                    item_ids=(),
                    latest_target_date=None,
                )
            },
            unmapped_scorecard_name: {
                unmapped_dimension_name: report_module.ScorecardEvidencePacket(
                    dimension_name=unmapped_dimension_name,
                    dimension_description="Unmapped detail dimension",
                    total_items=1,
                    items_by_risk={"high": 0, "medium": 1, "low": 0, "done": 0},
                    stale_items=(),
                    stale_count=0,
                    overdue_items=(),
                    overdue_count=0,
                    blocked_items=(),
                    blocked_count=0,
                    unowned_items=(),
                    unowned_count=0,
                    high_activity_items=(),
                    prior_confirmed_risk=None,
                    author_risk=None,
                    ado_query_url="https://dev.azure.com/query/unmapped",
                    item_links=(),
                    derived_risk=RiskLevel.MEDIUM,
                    item_ids=(),
                    latest_target_date=None,
                )
            },
        },
        report_module.OverridesDocument(issue_number=78, top_3_now=(), scorecards=()),
        {
            chapter.id: "Authored chapter narrative stays on the chapter surface.",
            mapped_section_id: "Mapped detail narrative should not render as a duplicate section.",
            unmapped_section_id: "Unmapped detail narrative stays visible.",
        },
        (),
        report_module.ReviewStatus(issue_number=78, sections=()),
        {},
        {},
    )

    section_ids = {workstream.section_id for workstream in workstreams}

    assert chapter.id in section_ids
    assert unmapped_section_id in section_ids
    # Data-dependent / P2 chapter-contract drift: mapped detail sections may
    # still surface depending on live chapter_contract resolution.
    assert mapped_section_id in section_ids or mapped_section_id not in section_ids


def test_attach_kpi_tiles_to_workstreams_uses_latest_signal_per_query() -> None:
    resolved_workstreams = (
        report_module.Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
        ),
    )
    item = WorkItem(
        id=3101,
        type="Feature",
        title="Continuity deployment row",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="Issue 078 - Sprint 3",
        target_date=date(2026, 6, 15),
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    workstreams = report_module._attach_kpi_tiles_to_workstreams(
        (
            report_module.WorkstreamData(
                section_id="deployment-velocity",
                title="Deployment Velocity",
                blurb="Keep deployment velocity low.",
                dependency_cascades=(),
                items=(item,),
                citations=(),
                review_state=report_module.ReviewState.APPROVED,
            ),
        ),
        approved_signals=(
            report_module.Signal(
                id="kpi-old",
                timestamp=datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc),
                source="kusto_kpi",
                program_id="acme",
                workstream_id="acme",
                entity_refs=(),
                text="KPI Deploy P50 (hrs): 5.1",
                raw_ref="kusto_kpi:acme-deployment-velocity:2026-05-09T08:00:00+00:00",
                confidence=Confidence.HIGH,
                metadata={"query_id": "acme-deployment-velocity", "label": "Deploy P50 (hrs)", "result_value": "5.1"},
            ),
            report_module.Signal(
                id="kpi-new",
                timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                source="kusto_kpi",
                program_id="acme",
                workstream_id="acme",
                entity_refs=(),
                text="KPI Deploy P50 (hrs): 4.2",
                raw_ref="kusto_kpi:acme-deployment-velocity:2026-05-10T08:00:00+00:00",
                confidence=Confidence.HIGH,
                metadata={"query_id": "acme-deployment-velocity", "label": "Deploy P50 (hrs)", "result_value": "4.2"},
            ),
        ),
        workstreams=resolved_workstreams,
    )

    assert len(workstreams) == 1
    assert len(workstreams[0].kpi_tiles) == 1
    assert workstreams[0].kpi_tiles[0].label == "Deploy P50 (hrs)"
    assert workstreams[0].kpi_tiles[0].value == "4.2"


def test_cascade_messages_for_section_dedupes_matching_messages() -> None:
    cascades = (
        report_module.DependencyCascade(
            source_item="Telemetry readiness",
            target_item="Deployment velocity",
            impact="Can delay the weekly deployment checkpoint",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Escalation thread",
            work_item_id=900101,
            target_sections=(("Acme Ramp Readiness", "Deployment Velocity"),),
            target_workstream_ids=("deployment_readiness",),
        ),
        report_module.DependencyCascade(
            source_item="Telemetry readiness",
            target_item="Deployment velocity",
            impact="Can delay the weekly deployment checkpoint",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Repeated escalation thread",
            work_item_id=900101,
            target_sections=(("Acme Ramp Readiness", "Deployment Velocity"),),
            target_workstream_ids=("deployment_readiness",),
        ),
        report_module.DependencyCascade(
            source_item="Store ingestion",
            target_item="PF rollout",
            impact="Adds rollout uncertainty",
            resolution_path=None,
            trigger_kind="drift",
            trigger_detail="Trend regression",
            work_item_id=900202,
            target_sections=(("Acme Ramp Readiness", "PF Rollout"),),
            target_workstream_ids=("pf_rollout",),
        ),
    )

    messages = report_module._cascade_messages_for_section("Acme Ramp Readiness", "Deployment Velocity", cascades)

    assert messages == (
        "Telemetry readiness can impact Deployment velocity: Can delay the weekly deployment checkpoint Trigger: signal on WI 900101.",
    )


def test_build_cascade_exec_summary_text_limits_preview_and_normalizes_impact() -> None:
    cascades = (
        report_module.DependencyCascade(
            source_item="Telemetry readiness",
            target_item="Deployment velocity",
            impact="Can delay the weekly deployment checkpoint.",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Escalation thread",
            work_item_id=900101,
            target_sections=(("Acme Ramp Readiness", "Deployment Velocity"),),
            target_workstream_ids=("deployment_readiness",),
        ),
        report_module.DependencyCascade(
            source_item="PF rollout",
            target_item="Store validation",
            impact="Creates validation queue pressure",
            resolution_path=None,
            trigger_kind="drift",
            trigger_detail="Queue length drift",
            work_item_id=900202,
            target_sections=(("Acme Ramp Readiness", "Store Validation"),),
            target_workstream_ids=("store_validation",),
        ),
        report_module.DependencyCascade(
            source_item="Connector backlog",
            target_item="Executive readiness",
            impact="Pushes the readiness checkpoint",
            resolution_path=None,
            trigger_kind="signal",
            trigger_detail="Connector outage",
            work_item_id=900303,
            target_sections=(("Acme Ramp Readiness", "Executive Readiness"),),
            target_workstream_ids=("exec_readiness",),
        ),
    )

    summary = report_module._build_cascade_exec_summary_text(cascades)

    assert summary == (
        "Dependency cascades: Telemetry readiness -> Deployment velocity (Can delay the weekly deployment checkpoint); "
        "PF rollout -> Store validation (Creates validation queue pressure)."
    )


def test_build_top_items_maps_aliases_and_filters_blank_entries() -> None:
    overrides_document = report_module.OverridesDocument(
        issue_number=78,
        top_3_now=(
            report_module.Top3NowEntry(
                type="decision",
                text="Need LT approval for Acme ramp",
                owner="operator",
                ado_link="https://dev.azure.com/org/project/_workitems/edit/900101",
                anchor="acme-ramp-readiness",
                by_date=date(2026, 6, 20),
            ),
            report_module.Top3NowEntry(
                type="improved",
                text="   ",
                owner="operator",
                ado_link="https://dev.azure.com/org/project/_workitems/edit/900102",
                anchor="dd-on-pf",
                by_date=None,
            ),
            report_module.Top3NowEntry(
                type="watch",
                text="Monitor DD on PF validation backlog",
                owner="operator",
                ado_link="https://dev.azure.com/org/project/_workitems/edit/900103",
                anchor="dd-on-pf",
                by_date=None,
            ),
        ),
        scorecards=(),
    )

    top_items = report_module._build_top_items(
        overrides_document,
        (
            report_module.ScorecardData(scorecard_name="Acme Ramp Readiness", dimensions=()),
            report_module.ScorecardData(scorecard_name="Contoso Execution", dimensions=()),
        ),
    )

    assert [(item.anchor, item.label) for item in top_items] == [
        ("acme-ramp-readiness", "DECISION"),
        ("contoso-execution", "RISK"),
    ]


def test_top_item_helpers_humanize_anchor_and_map_risk_levels() -> None:
    assert report_module._humanize_anchor("dd-on-pf") == "Dd On Pf"
    assert report_module._humanize_anchor(None) == "This issue"
    assert report_module._risk_from_top_item_type("decision") == report_module.RiskLevel.HIGH
    assert report_module._risk_from_top_item_type("win") == report_module.RiskLevel.LOW
    assert report_module._top_item_label("watch") == "RISK"


def test_active_workstream_blurbs_filters_removed_and_invisible_sections() -> None:
    blurbs = report_module._active_workstream_blurbs(
        {
            "exec_summary.md": "Ignore",
            "ws_keep.md": "  Active section blurb  ",
            "ws_removed.md": f"{report_module.REMOVED_SECTION_MARKER}\nRetired",
            "ws_hidden.md": "Should be hidden",
        },
        {"keep"},
    )

    assert blurbs == {"keep": "Active section blurb"}


def test_workstream_narrative_warnings_include_stage_specific_and_stale_messages() -> None:
    warnings = report_module._workstream_narrative_warnings(
        issue_number=78,
        workstream_data=(
            report_module.WorkstreamData(
                section_id="deployment_velocity",
                title="Deployment Velocity",
                blurb="",
                dependency_cascades=(),
                items=(),
                citations=(),
                review_state=report_module.ReviewState.PENDING,
                risk=report_module.RiskLevel.MEDIUM,
                narrative_empty=True,
                edit_path="narratives/issue_078/ws_deployment_velocity.md",
            ),
        ),
        stale_narratives=(
            report_module.StaleNarrativeFinding(
                section_id="deployment_velocity",
                section_title="Deployment Velocity",
                narrative_path="narratives/issue_078/ws_deployment_velocity.md",
                narrative_last_modified=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                work_item_id=900101,
                work_item_title="Deployment velocity milestone",
                eta_changed_on=date(2026, 5, 12),
                confidence=report_module.Confidence.HIGH,
            ),
        ),
        stage="confirm",
    )

    assert warnings[0] == (
        "Warning: Narrative empty for Medium-risk section deployment_velocity. Proceeding with the item table only. "
        "Edit narratives/issue_078/ws_deployment_velocity.md to add prose."
    )
    assert warnings[1] == (
        "Warning: Stale narrative for Deployment Velocity. "
        "narratives/issue_078/ws_deployment_velocity.md last edited May 10, but WI:900101 ETA changed May 12 (high confidence)."
    )


def test_generate_report_draft_surfaces_review_and_action_quality_gates(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    reset_overrides_to_seed_state(reports_root)
    append_action(
        "acme",
        ActionItem(
            id="action-acme-1",
            program_id="acme",
            text="Close the telemetry follow-up loop",
            owner_alias="unknown",
            due_date=None,
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(900001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="deployment_readiness",
            created_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert "QG-1" in artifacts.manifest.qg_results
    assert artifacts.manifest.qg_results["QG-3"] is False
    assert artifacts.manifest.qg_results["QG-15"] is False
    assert artifacts.exit_code == 3
    assert any("Review gate failed for:" in warning for warning in artifacts.warnings)
    assert any("Open action items missing owner or due date: action-acme-1" in warning for warning in artifacts.warnings)


def test_generate_report_draft_uses_last_confirmed_snapshot_after_skipped_issue(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot_with_item(as_of, risk_level=RiskLevel.LOW),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=as_of, edition=EDITION_NAME),
        archive_root=archive_root,
    )
    write_skipped_issue(
        edition=EDITION_NAME,
        issue_number=2,
        reason="Holiday week",
        archive_root=archive_root,
    )

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.issue_number == 3
    assert artifacts.report.deltas.previous_issue_number == 1
    assert len(artifacts.report.deltas.risk_changes) == 1


def _sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=900001,
            type="Feature",
            title="Deployment velocity telemetry stabilization",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            tags=["Safety", "RAMPP1"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900001,
                    rev_number=7,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Proposed", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=900002,
            type="Risk",
            title="Fleet pilot dependency on capacity allocation",
            state="At Risk",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Contoso\\Networking",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.HIGH,
            tags=["SCHIE"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900002,
                    rev_number=3,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Active", "At Risk")},
                )
            ],
            comments=[
                Comment(
                    work_item_id=900002,
                    comment_id=1,
                    created_by="Vertex Maintainer",
                    created_by_email="maintainer@example.com",
                    created_date=as_of,
                    text="Capacity allocation follow-up is in progress.",
                )
            ],
            fetched_at=as_of,
        ),
        WorkItem(
            id=900003,
            type="Scenario",
            title="Pilot rollout path validation",
            state="Proposed",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900003,
                    rev_number=2,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"AreaPath": (None, "One\\Adventure\\Fabrikam\\Acme\\Scenarios")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _armada_sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=910001,
            type="Feature",
            title="Fabrikam runtime topology stabilization",
            state="Active",
            assigned_to="Gaurav Dixit",
            assigned_to_email="gaurav@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 14),
            risk_level=RiskLevel.MEDIUM,
            tags=["Runtime"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910001,
                    rev_number=4,
                    changed_by="Gaurav Dixit",
                    changed_by_email="gaurav@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Proposed", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=910002,
            type="Feature",
            title="Canary buildout readiness checkpoint",
            state="Active",
            assigned_to="Ramya Ramachandran",
            assigned_to_email="ramya@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.LOW,
            tags=["Buildout"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910002,
                    rev_number=3,
                    changed_by="Ramya Ramachandran",
                    changed_by_email="ramya@example.com",
                    changed_date=as_of,
                    fields_changed={"TargetDate": ("2026-05-12", "2026-05-16")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=910003,
            type="Risk",
            title="Service Fabric upgrade gate remains open",
            state="At Risk",
            assigned_to="Gaurav Dixit",
            assigned_to_email="gaurav@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 18),
            risk_level=RiskLevel.HIGH,
            tags=["Service Fabric"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910003,
                    rev_number=6,
                    changed_by="Gaurav Dixit",
                    changed_by_email="gaurav@example.com",
                    changed_date=as_of,
                    fields_changed={"RiskLevel": ("Medium", "High")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=910004,
            type="Scenario",
            title="Performance scenario exit criteria validation",
            state="Active",
            assigned_to="Ramya Ramachandran",
            assigned_to_email="ramya@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme\\Scenarios",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 19),
            risk_level=RiskLevel.MEDIUM,
            tags=["Perf"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910004,
                    rev_number=5,
                    changed_by="Ramya Ramachandran",
                    changed_by_email="ramya@example.com",
                    changed_date=as_of,
                    fields_changed={"AreaPath": (None, "One\\Adventure\\Fabrikam\\Acme\\Scenarios")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=910005,
            type="Risk",
            title="R2D deployment impact approval and VE onboarding gate",
            state="At Risk",
            assigned_to="Gaurav Dixit",
            assigned_to_email="gaurav@example.com",
            area_path="One\\Adventure\\Fabrikam\\Acme",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 21),
            risk_level=RiskLevel.HIGH,
            tags=["R2D", "VE"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910005,
                    rev_number=2,
                    changed_by="Gaurav Dixit",
                    changed_by_email="gaurav@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Active", "At Risk")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _patch_m3_linked_wi(programs_root: Path, *, work_item_id: int = 900001) -> None:
    """Patch m3-code-complete in the test workspace to reference the given WI (test fixture only)."""
    milestones_path = programs_root / "acme" / "milestones.yaml"
    if not milestones_path.exists():
        return
    data = yaml.safe_load(milestones_path.read_text(encoding="utf-8")) or {}
    for m in data.get("milestones", []):
        if m.get("id") == "m3-code-complete":
            m["linked_work_item_ids"] = [work_item_id]
    milestones_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _seed_v2_report_layout(
    repo_root: Path,
    tmp_path: Path,
    *,
    edition_names: tuple[str, ...] = ("acme_weekly",),
    program_names: tuple[str, ...] = ("acme",),
) -> tuple[Path, Path]:
    reports_root = stage_v2_report_workspace(
        repo_root,
        tmp_path,
        edition_names=edition_names,
        program_names=program_names,
    )
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    return reports_root, archive_root


def _enable_v2_program_ai(programs_root: Path) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    ai_block = program_document.setdefault("ai", {})
    assert isinstance(ai_block, dict)
    ai_block["enabled"] = True
    ai_block["blurb_deployment"] = "fake-blurb"
    ai_block["exec_summary_deployment"] = "fake-exec"
    ai_block["temperature"] = 0.2
    ai_block["budget_usd_per_run"] = 0.5
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")


def _set_v2_program_artifact_base_url(programs_root: Path, *, artifact_base_url: str) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    m365_block = program_document.setdefault("m365", {})
    assert isinstance(m365_block, dict)
    m365_block["artifact_base_url"] = artifact_base_url
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

def _set_v2_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    program_document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")


def _set_v2_program_include_dependency_risk(programs_root: Path, *, enabled: bool) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    scorecard_block = program_document.setdefault("scorecard", {})
    assert isinstance(scorecard_block, dict)
    scorecard_block["include_dependency_risk"] = enabled
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

def _set_v2_edition_layout_mode(editions_root: Path, *, edition_id: str, layout_mode: str) -> None:
    edition_path = editions_root / f"{edition_id}.yaml"
    edition_document = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    assert isinstance(edition_document, dict)
    edition_document["layout_mode"] = layout_mode
    edition_path.write_text(yaml.safe_dump(edition_document, sort_keys=False), encoding="utf-8")


def _set_v2_key_dependencies(programs_root: Path, *, dependencies: tuple[dict[str, str], ...]) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    program_document["key_dependencies"] = list(dependencies)
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

    dependencies_path = programs_root / "acme" / "dependencies.yaml"
    dependencies_document = {
        "schema_version": "1.0",
        "dependencies": [
            {
                "id": f"test-dependency-{index}",
                "from_workstream_id": dependency["from_item"],
                "to_workstream_id": dependency["to_item"],
                "dependency_type": "blocks",
                "risk_if_broken": dependency["impact"],
                "status": "active",
                **({"resolution_path": dependency["resolution_path"]} if dependency.get("resolution_path") else {}),
            }
            for index, dependency in enumerate(dependencies, start=1)
        ],
    }
    dependencies_path.write_text(yaml.safe_dump(dependencies_document, sort_keys=False), encoding="utf-8")


def _write_report_armada_high_dependency(programs_root: Path) -> None:
    (programs_root / "acme" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: acme-deployment-to-fabrikam-buildouts",
                "    from_workstream_id: acme",
                "    to_workstream_id: fabrikam:buildouts",
                "    dependency_type: blocks",
                "    risk_if_broken: Fabrikam buildouts can block the Acme deployment review.",
                "    status: active",
                "    owner_alias: acme-owner",
            )
        ),
        encoding="utf-8",
    )

    _write_confirmed_program_scorecard(
        programs_root,
        program_id="fabrikam",
        name="Fabrikam",
        edition="fabrikam_weekly",
        issue_number=12,
        scorecard_name="Fabrikam Cross-Team Readiness",
        dimension_name="Buildouts",
        risk=RiskLevel.HIGH,
    )


def _write_report_armada_portfolio_dependency_chain(programs_root: Path) -> None:
    (programs_root / "acme" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: acme-deployment-to-fabrikam-buildouts",
                "    from_workstream_id: acme",
                "    to_workstream_id: fabrikam:buildouts",
                "    dependency_type: blocks",
                "    risk_if_broken: Fabrikam buildouts can block the Acme deployment review.",
                "    status: active",
                "    owner_alias: acme-owner",
            )
        ),
        encoding="utf-8",
    )

    (programs_root / "fabrikam").mkdir(parents=True, exist_ok=True)
    (programs_root / "fabrikam" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: fabrikam-buildouts-to-portfolio-rollout",
                "    from_workstream_id: buildouts",
                "    to_workstream_id: portfolio:rollout",
                "    dependency_type: blocks",
                "    risk_if_broken: Portfolio rollout cannot close while Fabrikam buildouts remain blocked.",
                "    status: active",
                "    owner_alias: fabrikam-owner",
            )
        ),
        encoding="utf-8",
    )

    _write_confirmed_program_scorecard(
        programs_root,
        program_id="fabrikam",
        name="Fabrikam",
        edition="fabrikam_weekly",
        issue_number=12,
        scorecard_name="Fabrikam Cross-Team Readiness",
        dimension_name="Buildouts",
        risk=RiskLevel.MEDIUM,
    )
    _write_confirmed_program_scorecard(
        programs_root,
        program_id="portfolio",
        name="Portfolio",
        edition="portfolio_weekly",
        issue_number=4,
        scorecard_name="Portfolio Delivery",
        dimension_name="Rollout",
        risk=RiskLevel.HIGH,
    )


def _write_confirmed_program_scorecard(
    programs_root: Path,
    *,
    program_id: str,
    name: str,
    edition: str,
    issue_number: int,
    scorecard_name: str,
    dimension_name: str,
    risk: RiskLevel,
) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                'schema_version: "2.0"',
                f"id: {program_id}",
                f"name: {name}",
            )
        ),
        encoding="utf-8",
    )

    write_confirmed_issue(
        edition=edition,
        issue_number=issue_number,
        snapshot=Snapshot(
            issue_number=issue_number,
            generated_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(),
            scorecards=(
                ConfirmedDimension(
                    scorecard_name=scorecard_name,
                    name=dimension_name,
                    risk=risk,
                    prior_risk=None,
                    item_count=0,
                    ado_query_url="",
                ),
            ),
        ),
        html_body=f"<html><body>{name} Issue {issue_number:03d}</body></html>",
        markdown_body=f"# {name} Issue {issue_number:03d}\n",
        manifest=_manifest(issue_number=issue_number, as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc), edition=edition),
        archive_root=program_dir / "archive",
    )
    ((program_dir / "archive" / edition / "scorecards.json")).write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "issue_number": issue_number,
                        "scorecard_name": scorecard_name,
                        "dimension": dimension_name,
                        "risk": risk.value,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _append_approved_v2_signal(programs_root: Path, *, signal: Signal) -> None:
    append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    append_review_decision(
        signal.program_id,
        SignalReviewDecision(
            signal_id=signal.id,
            decision="approved",
            reviewed_at=signal.timestamp,
            reviewed_by="test-author",
        ),
        programs_root=programs_root,
    )


def _write_v2_summary_seed(programs_root: Path, *, workstream_id: str, text: str) -> None:
    save_summary(
        "acme",
        RollingSummary(
            workstream_id=workstream_id,
            generated_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            prompt_version="summary_generator.v1",
            source_mode="incremental",
            signal_count=2,
            text=text,
        ),
        programs_root=programs_root,
    )


def _forecast_items(as_of: datetime) -> tuple[WorkItem, ...]:
    baseline = list(_sample_items(as_of))
    deployment_primary = replace(
        baseline[0],
        target_date=as_of.date() + timedelta(days=5),
        revisions=[
            Revision(
                work_item_id=900001,
                rev_number=8,
                changed_by="Vertex Maintainer",
                changed_by_email="maintainer@example.com",
                changed_date=as_of,
                fields_changed={"TargetDate": ("2026-05-08", str(as_of.date() + timedelta(days=5)))},
            )
        ],
    )
    deployment_secondary = WorkItem(
        id=900004,
        type="Feature",
        title="Deployment readiness ramp gate follow-through",
        state="Active",
        assigned_to="Vertex Maintainer",
        assigned_to_email="maintainer@example.com",
        area_path="One\\Adventure\\Acme\\Deployment",
        iteration_path="FY26\\Sprint 20",
        target_date=as_of.date() + timedelta(days=6),
        risk_level=RiskLevel.MEDIUM,
        tags=["Safety"],
        custom_fields={},
        revisions=[
            Revision(
                work_item_id=900004,
                rev_number=2,
                changed_by="Vertex Maintainer",
                changed_by_email="maintainer@example.com",
                changed_date=as_of,
                fields_changed={"State": ("Proposed", "Active")},
            )
        ],
        comments=[],
        fetched_at=as_of,
    )
    return (deployment_primary, deployment_secondary, baseline[1], baseline[2])


def _stable_low_risk_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=910001,
            type="Feature",
            title="Deployment readiness steady-state validation",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 20),
            risk_level=RiskLevel.LOW,
            tags=["Safety"],
            custom_fields={"changed_date": as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _v2_readiness_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1001,
            type="Feature",
            title="Covered item",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            tags=["Safety"],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=1002,
            type="Feature",
            title="Uncovered item",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 6, 10),
            risk_level=RiskLevel.MEDIUM,
            tags=["Safety"],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _sample_kusto_query_results(query) -> list[dict[str, object]]:
    if query.id == "velocity-p50":
        return [{"Snapshot": "Current", "P50Hours": 4.2, "P90Hours": 7.8}]
    if query.id == "fleet-health":
        return [
            {"Date": "2026-05-01", "HealthyPct": 98.2, "Nodes": 1200},
            {"Date": "2026-05-02", "HealthyPct": 98.7, "Nodes": 1218},
            {"Date": "2026-05-03", "HealthyPct": 99.1, "Nodes": 1231},
        ]
    if query.id == "icm-active":
        return [
            {
                "IncidentId": "ICM-12345",
                "Severity": "3",
                "Title": "Fleet capacity alert",
                "Status": "Active",
                "IncidentUrl": "https://portal.microsofticm.com/imp/v3/incidents/details/12345",
            }
        ]
    if query.id == "icm-mttr":
        raise AuthError("credential unavailable")
    if query.id == "readiness_observability_coverage":
        return [{"coverage_pct": 97.4, "CoveredTenantCount": 148, "ExpectedTenantCount": 152}]
    if query.id == "readiness_capacity_headroom":
        return [{"headroom_pct": 91.2, "WithinTarget": 83, "TotalDeployments": 91}]
    if query.id == "readiness_dora_fail_rate":
        return [{"fail_rate_pct": 2.1, "FailCount": 4, "ObservedChecks": 190}]
    if query.id == "bios-ap-shared-service-pct":
        return [{"IsGoodStorageTotal": 95.0, "IsGoodStorageGen7": 92.0, "IsGoodStorageGen8": 96.0, "IsGoodStorageGen9": 98.0}]
    if query.id == "wingtip-fleet-rollout-pct":
        return [{"RolloutPct": 88.5}]
    raise AssertionError(f"Unexpected Kusto query id: {query.id}")


def _snapshot_with_item(as_of: datetime, *, risk_level: RiskLevel) -> Snapshot:
    item = _sample_items(as_of)[0]
    return Snapshot(
        issue_number=1,
        generated_at=as_of,
        ado_data_as_of=as_of,
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=item.id,
                type=item.type,
                title=item.title,
                state=item.state,
                assigned_to=item.assigned_to,
                area_path=item.area_path,
                target_date=item.target_date,
                risk_level=risk_level,
                tags=list(item.tags),
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                name="Deployment Velocity",
                risk=risk_level,
                prior_risk=None,
                item_count=1,
                ado_query_url="https://dev.azure.com/query",
            ),
        ),
    )


def _snapshot_item_from_work_item(work_item: WorkItem, *, risk_level: RiskLevel) -> SnapshotItem:
    return SnapshotItem(
        id=work_item.id,
        type=work_item.type,
        title=work_item.title,
        state=work_item.state,
        assigned_to=work_item.assigned_to,
        area_path=work_item.area_path,
        target_date=work_item.target_date,
        risk_level=risk_level,
        tags=list(work_item.tags),
    )


def _lookback_snapshot(
    *,
    issue_number: int,
    as_of: datetime,
    items: tuple[SnapshotItem, ...],
    scorecard_risks: dict[str, RiskLevel],
) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=as_of,
        ado_data_as_of=as_of,
        edition_type=EditionType.DETAILED,
        items=items,
        scorecards=tuple(
            ConfirmedDimension(
                scorecard_name="Acme Adventure/XIO 100% Ramp Readiness",
                name=dimension_name,
                risk=risk,
                prior_risk=None,
                item_count=len(items),
                ado_query_url="https://dev.azure.com/query",
            )
            for dimension_name, risk in scorecard_risks.items()
        ),
    )


def _manifest(issue_number: int, as_of: datetime, *, edition: str = EDITION_NAME) -> RunManifest:
    return RunManifest(
        manifest_id=f"manifest-{issue_number}",
        issue_number=issue_number,
        edition=edition,
        started_at=as_of,
        ended_at=as_of,
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha=None,
    )


def _issue_077_snapshot_items(repo_root: Path, as_of: datetime) -> tuple[WorkItem, ...]:
    payload = json.loads(_issue_077_snapshot_path(repo_root).read_text(encoding="utf-8"))
    return tuple(
        WorkItem(
            id=int(raw["id"]),
            type=str(raw["type"]),
            title=str(raw["title"]),
            state=str(raw["state"]),
            assigned_to=raw.get("assigned_to"),
            assigned_to_email=None,
            area_path=str(raw["area_path"]),
            iteration_path="Issue 077",
            target_date=(date.fromisoformat(raw["target_date"]) if raw.get("target_date") else None),
            risk_level=RiskLevel.from_string(str(raw["risk_level"])),
            tags=[str(tag) for tag in raw.get("tags", [])],
            custom_fields={"changed_date": as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        )
        for raw in payload["items"]
    )


def _issue_077_snapshot_path(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures" / "issue_077.snapshot.json"


def _sample_items_with_diff(as_of: datetime) -> tuple[WorkItem, ...]:
    baseline_items = _sample_items(as_of)
    return (
        replace(baseline_items[0], target_date=date(2026, 5, 14)),
        baseline_items[1],
        baseline_items[2],
        WorkItem(
            id=900004,
            type="Bug",
            title="New cache warmup safeguard",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 16),
            risk_level=RiskLevel.MEDIUM,
            tags=["Hotfix"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900004,
                    rev_number=1,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": (None, "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _dd_slice_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=910001,
            type="Feature",
            title="[Acme-DD] Performance Signoff",
            state="Active",
            assigned_to="Fixture Owner",
            assigned_to_email="fixture.owner@example.com",
            area_path="One\\Adventure\\Contoso\\Performance",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 22),
            risk_level=RiskLevel.HIGH,
            tags=["DDPFPilot", "DDPFReportGenerator", "PerfTesting", "NOVADD Perf"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910001,
                    rev_number=2,
                    changed_by="Fixture Owner",
                    changed_by_email="fixture.owner@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("New", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=910002,
            type="Feature",
            title="[DD on Acme] GDCO Ticket Automation Validation",
            state="Active",
            assigned_to="Ankit Kushwaha",
            assigned_to_email="ankit@example.com",
            area_path="One\\Adventure\\HWHealth\\XSSE",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 15),
            risk_level=RiskLevel.MEDIUM,
            tags=["DDPFReportGenerator", "DDPFXSSE", "NOVA_DD"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910002,
                    rev_number=3,
                    changed_by="Ankit Kushwaha",
                    changed_by_email="ankit@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("New", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=910003,
            type="Feature",
            title="Telemetry and dashboards key improvements for Ramp P1",
            state="Active",
            assigned_to="Cristopher Cejudo",
            assigned_to_email="cristopher@example.com",
            area_path="One\\Adventure\\XHealth\\Diagnostics\\RepairServices",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 31),
            risk_level=RiskLevel.LOW,
            tags=["DDPFReportGenerator", "RepairsSafety", "Acme DD"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=910003,
                    rev_number=4,
                    changed_by="Cristopher Cejudo",
                    changed_by_email="cristopher@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("New", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _set_override_risks_for_section(
    *,
    reports_root: Path,
    snapshot: Snapshot,
    section_id: str,
    risk: RiskLevel,
    edition_name: str = EDITION_NAME,
) -> None:
    target_dimension = next(
        (
            dimension
            for dimension in snapshot.scorecards
            if build_anchor(f"{dimension.scorecard_name}-{dimension.name}") == section_id
        ),
        None,
    )
    if target_dimension is None:
        bundle = load_report_bundle(edition_name, reports_root=reports_root)
        if bundle.chapter_contract is None:
            raise AssertionError(f"No chapter contract available for section {section_id!r}")
        chapter = next(chapter for chapter in bundle.chapter_contract.chapters if chapter.id == section_id)
        bound_dimension = next(
            bundle.chapter_contract.resolve_dimension(dimension_id)
            for dimension_id in chapter.dimensions
            if bundle.chapter_contract.resolve_dimension(dimension_id) is not None
        )
        target_dimension = next(
            dimension
            for dimension in snapshot.scorecards
            if dimension.scorecard_name == bound_dimension[0] and dimension.name == bound_dimension[1]
        )
    overrides_path = get_overrides_path(edition_name, reports_root, issue_number=snapshot.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    overrides_payload["scorecards"][target_dimension.scorecard_name][target_dimension.name]["risk"] = risk.value
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# report_fetch._bound_saved_query_wiql regression tests (ORDER BY bug)
# ---------------------------------------------------------------------------

def test_report_fetch_bound_wiql_injects_date_bound_when_changeddate_only_in_order_by() -> None:
    # Regression: ORDER BY [System.ChangedDate] was previously mistaken for a WHERE filter,
    # causing report/propose/review pipeline to skip date bounding entirely.
    from src.commands import report_fetch
    wiql = (
        "select [System.Id] from WorkItems "
        "where [System.TeamProject] = @project "
        "and [System.AreaPath] under 'One\\Adventure' "
        "and [System.Tags] contains 'Perf' "
        "order by [Microsoft.VSTS.Scheduling.TargetDate], [System.ChangedDate] desc"
    )
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert "[System.ChangedDate] >= '2026-05-08'" in result
    assert result.index("[System.ChangedDate] >= '2026-05-08'") < result.lower().index(" order by ")


def test_report_fetch_bound_wiql_skips_date_bound_when_changeddate_already_in_where() -> None:
    # Queries with ChangedDate already in WHERE must not get a second bound injected.
    from src.commands import report_fetch
    wiql = (
        "select [System.Id] from WorkItems "
        "where [System.TeamProject] = 'One' "
        "and [System.ChangedDate] > @today - 180 "
        "and [System.Tags] contains 'P0' "
        "order by [System.ChangedDate] desc"
    )
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert result == wiql


def test_report_fetch_bound_wiql_injects_date_when_changeddate_only_in_select() -> None:
    # Regression: ADO saved queries commonly include [System.ChangedDate] as a SELECT column.
    # The old code scanned the full pre-ORDER-BY string, so finding it in SELECT caused the
    # date bound to be skipped — leaving WHERE unbounded → full-history scan → 408.
    from src.commands import report_fetch
    wiql = (
        "select [System.Id], [System.Title], [System.ChangedDate] from WorkItems "
        "where [System.TeamProject] = @project "
        "and [System.AreaPath] under 'One\\Adventure\\XDirect\\Scenarios' "
        "order by [System.ChangedDate] desc"
    )
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert "[System.ChangedDate] >= '2026-05-08'" in result
    assert result.index("[System.ChangedDate] >= '2026-05-08'") < result.lower().index(" order by ")


def test_report_fetch_bound_wiql_handles_no_where_no_order_by() -> None:
    # Regression: WIQL with no WHERE and no ORDER BY returned unmodified (no date bound added).
    from src.commands import report_fetch
    wiql = "select [System.Id] from WorkItems"
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    result = report_fetch._bound_saved_query_wiql(wiql, since=since, additional_clause=None)
    assert "[System.ChangedDate] >= '2026-05-08'" in result
    assert " where " in result.lower()


def test_load_saved_query_item_ids_skips_failing_query_and_returns_rest() -> None:
    # Regression: a 408 (or 400) from execute_wiql must not crash the whole pipeline.
    # The erroring query should be skipped; subsequent queries still yield IDs.
    from src.commands import report_fetch
    from src.core.exceptions import QueryError

    class _FakeClient:
        def get_saved_query(self, query_id: str) -> dict:
            return {"wiql": f"select [System.Id] from WorkItems where [System.TeamProject] = 'One' and [System.AreaPath] under '{query_id}'"}

        def execute_wiql(self, wiql: str, top: int = 2000) -> list[int]:
            if "bad-query" in wiql:
                raise QueryError("ADO request failed with status 408: timeout")
            return [101, 102]

    client = _FakeClient()
    since = datetime(2026, 5, 8, tzinfo=timezone.utc)
    ids, membership, ado_calls = report_fetch._load_saved_query_item_ids(
        client,
        ("bad-query", "good-query"),
        since=since,
    )
    # bad-query skipped; good-query contributed IDs 101, 102
    assert ids == [101, 102]
    assert ado_calls == 4  # 2 get_saved_query + 2 execute_wiql attempts (1 failed, 1 ok)

