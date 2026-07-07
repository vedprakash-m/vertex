from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.archive_store import write_confirmed_issue
from src.core.models import ConfirmedDimension, EditionType, RunManifest, RiskLevel, Snapshot, SnapshotItem
from src.core.semantic_index import get_semantic_index_path, load_semantic_index_state


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_index_rebuild_command_builds_semantic_index(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=5,
        markdown_body="# Issue 005\nUD chunking regression is still the top deployment risk.\n",
    )

    monkeypatch.setattr("src.commands.index.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["index", "rebuild", "--edition", EDITION_NAME])

    assert result.exit_code == 0
    assert "Rebuilt semantic index" in result.stdout
    assert get_semantic_index_path(EDITION_NAME, archive_root=archive_root).exists()
    state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root)
    assert state is not None
    assert state.latest_confirmed_issue == 5
    assert state.semantic_index_dirty is False


def test_index_optimize_if_needed_skips_when_recently_optimized(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=6,
        markdown_body="# Issue 006\nFleet capacity remains within expected bounds.\n",
    )

    monkeypatch.setattr("src.commands.index.ARCHIVE_ROOT", archive_root)

    rebuild_result = runner.invoke(app, ["index", "rebuild", "--edition", EDITION_NAME])
    assert rebuild_result.exit_code == 0

    optimize_result = runner.invoke(app, ["index", "optimize", "--edition", EDITION_NAME, "--if-needed"])

    assert optimize_result.exit_code == 0
    assert "Skipped semantic index optimize" in optimize_result.stdout


def _write_confirmed_issue(
    archive_root: Path,
    *,
    issue_number: int,
    markdown_body: str,
) -> None:
    as_of = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=issue_number,
        snapshot=Snapshot(
            issue_number=issue_number,
            generated_at=as_of,
            ado_data_as_of=as_of,
            edition_type=EditionType.DETAILED,
            items=(
                SnapshotItem(
                    id=issue_number,
                    type="Feature",
                    title=f"Issue {issue_number}",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    area_path="One\\Adventure\\Acme",
                    target_date=date(2026, 5, 12),
                    risk_level=RiskLevel.MEDIUM,
                    tags=["acme"],
                ),
            ),
            scorecards=(
                ConfirmedDimension(
                    scorecard_name="Acme Readiness",
                    name="Deployment Velocity",
                    risk=RiskLevel.MEDIUM,
                    prior_risk=RiskLevel.LOW,
                    item_count=1,
                    ado_query_url="https://dev.azure.com/your-org/One/_queries/query-id",
                ),
            ),
        ),
        html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
        markdown_body=markdown_body,
        manifest=RunManifest(
            manifest_id=f"manifest-{issue_number}",
            issue_number=issue_number,
            edition=EDITION_NAME,
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
        ),
        archive_root=archive_root,
    )
