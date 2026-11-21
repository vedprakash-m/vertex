"""Decision W: opt-in 1920x1080 >=16px projector presentation profile contract."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.core.ban_list_validator import find_ban_list_violations
from src.core.config_loader import load_bundle
from src.core.html_renderer import (
    HTMLRenderer,
    RenderContext,
    build_render_payload,
    render_projector_variant,
)
from src.core.models import (
    DeltaSet,
    EditionType,
    FreshnessReport,
    ReportData,
    ReviewState,
    ReviewStatus,
)
from src.core.view_models import EditionMeta, HealthSummary
from src.core.models import RiskLevel
from tests.support.report_test_setup import stage_v2_report_workspace
from tests.support.synthetic_report_source import EDITION_NAME, build_synthetic_report_source


NOW = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)


def test_projector_option_validation_precedes_workspace_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_preflight():
        pytest.fail("incompatible projector options must fail before workspace resolution")

    monkeypatch.setattr("src.commands.report._preflight_report_workspace", unexpected_preflight)

    result = CliRunner().invoke(
        app,
        [
            "report",
            "--edition",
            "missing",
            "--dry-run",
            "--edition-type",
            "deck",
            "--projector-preview",
        ],
    )

    assert result.exit_code == 2
    assert "--projector-preview requires a newsletter --dry-run" in result.output


def _sample_render_context(tmp_path: Path) -> tuple[Path, Path, RenderContext]:
    source = build_synthetic_report_source(tmp_path / "src")
    ws = tmp_path / "ws"
    reports_root = stage_v2_report_workspace(source, ws)
    programs_root = ws / "programs"
    bundle = load_bundle(EDITION_NAME, reports_root=reports_root, programs_root=programs_root)
    report = ReportData(
        issue_number=78,
        edition=EditionType.DETAILED,
        generated_at=NOW,
        ado_data_as_of=NOW,
        program=bundle.program_context,
        items=(),
        deltas=DeltaSet(
            issue_number=78,
            previous_issue_number=77,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=0,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Material claim: integration gate completed on schedule.",
        workstream_blurbs={},
        freshness=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(issue_number=78, sections=()),
        manifest_id="m-78",
    )
    ctx = RenderContext(
        title="Acme Weekly | Issue 078",
        subtitle="Program briefing",
        preheader="Stable",
        report=report,
        edition_meta=EditionMeta(
            edition=EDITION_NAME,
            issue_number=78,
            generated_at=NOW,
            ado_data_as_of=NOW,
            manifest_id="m-78",
            qg_status="PASS",
        ),
        health=HealthSummary(
            overall_risk=RiskLevel.LOW,
            high_count=0,
            medium_count=0,
            low_count=1,
            done_count=0,
            total_count=1,
            delta_direction="unchanged",
            prior_counts=None,
        ),
        is_dry_run=True,
    )
    return reports_root, programs_root, ctx


def test_default_html_preserves_14px_and_680_widths_while_projector_uses_16px_1080p(tmp_path: Path) -> None:
    reports_root, programs_root, ctx = _sample_render_context(tmp_path)
    bundle = load_bundle(EDITION_NAME, reports_root=reports_root, programs_root=programs_root)
    renderer = HTMLRenderer(EDITION_NAME, reports_root=reports_root)

    assert ctx.projector_profile is False
    assert build_render_payload(ctx)["projector_profile"] is False

    default_html = renderer.render(ctx)
    assert "font-size:14px" in default_html
    assert 'width="680"' in default_html
    assert 'width="640"' in default_html
    assert "projector_1080p" not in default_html

    projector_html = render_projector_variant(renderer, ctx)
    assert "Decision W" in projector_html
    assert "td, th" in projector_html
    assert "font-size: 16px !important" in projector_html
    assert "font-size:16px" in projector_html
    assert "overflow-x: hidden" in projector_html
    assert "max-width:1200px" in projector_html
    assert "color:#111827" in projector_html
    assert "Segoe UI" in projector_html
    assert find_ban_list_violations({"html": projector_html}, bundle.editorial_rules) == ()
