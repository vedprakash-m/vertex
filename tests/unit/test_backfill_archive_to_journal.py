from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import yaml

import pytest

_backfill_module = pytest.importorskip(
    "scripts.backfill_archive_to_journal",
    reason="scripts/backfill_archive_to_journal.py is a private operator script not included in the public repo",
)
backfill_archive_to_journal = _backfill_module.backfill_archive_to_journal
from src.core.archive_store import write_confirmed_issue
from src.core.claim_tracker import load_claim_entries, load_decision_asks
from src.core.journal import load_latest_review_decisions, read_signals
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, RunManifest, Snapshot, SnapshotItem


EDITION_NAME = "acme_weekly"


def test_backfill_archive_to_journal_seeds_signals_claims_and_is_idempotent(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    _seed_program(programs_root)
    _write_confirmed_issue(
        archive_root=archive_root,
        edition_name=EDITION_NAME,
        issue_number=76,
        generated_at=datetime(2026, 4, 10, 21, 12, tzinfo=timezone.utc),
        markdown_body=(
            "# Demo Issue 76\n\n"
            "## Decisions & Signals\n"
            "- **RISK** [ADO](https://dev.azure.com/your-org/One/_workitems/edit/1001) rollout is blocked until 04/15.\n\n"
            "## Executive Summary\n"
            "WI:1001 rollout follow up by 2026-04-15. Need LT decision on staging freeze.\n"
        ),
        snapshot=Snapshot(
            issue_number=76,
            generated_at=datetime(2026, 4, 10, 21, 12, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 4, 10, 21, 12, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(
                SnapshotItem(
                    id=1001,
                    type="Feature",
                    title="Rollout gate",
                    state="Active",
                    assigned_to="owner@example.com",
                    area_path="One\\Adventure\\Acme\\Deployment",
                    target_date=date(2026, 4, 15),
                    risk_level=RiskLevel.HIGH,
                    tags=[],
                ),
            ),
            scorecards=(
                ConfirmedDimension(
                    scorecard_name="Acme",
                    name="Deployment Velocity",
                    risk=RiskLevel.HIGH,
                    prior_risk=RiskLevel.MEDIUM,
                    item_count=1,
                    ado_query_url="https://example.invalid/query/velocity",
                ),
            ),
        ),
    )
    _write_confirmed_issue(
        archive_root=archive_root,
        edition_name=EDITION_NAME,
        issue_number=77,
        generated_at=datetime(2026, 4, 17, 21, 12, tzinfo=timezone.utc),
        markdown_body=(
            "# Demo Issue 77\n\n"
            "## Decisions & Signals\n"
            "- **RISK** [ADO](https://dev.azure.com/your-org/One/_workitems/edit/2002) perf sign-off is on track for 05/22.\n\n"
            "## Executive Summary\n"
            "WI:2002 perf sign-off expected by 05/22.\n"
        ),
        snapshot=Snapshot(
            issue_number=77,
            generated_at=datetime(2026, 4, 17, 21, 12, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 4, 17, 21, 12, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(
                SnapshotItem(
                    id=2002,
                    type="Feature",
                    title="Perf gate",
                    state="Active",
                    assigned_to="perf@example.com",
                    area_path="One\\Adventure\\Contoso\\Networking",
                    target_date=date(2026, 5, 22),
                    risk_level=RiskLevel.MEDIUM,
                    tags=[],
                ),
            ),
            scorecards=(
                ConfirmedDimension(
                    scorecard_name="DD",
                    name="Perf",
                    risk=RiskLevel.MEDIUM,
                    prior_risk=RiskLevel.LOW,
                    item_count=1,
                    ado_query_url="https://example.invalid/query/perf",
                ),
            ),
        ),
    )

    first = backfill_archive_to_journal(
        program_id="acme",
        edition_name=EDITION_NAME,
        archive_root=archive_root,
        programs_root=programs_root,
        apply=True,
    )

    signals = read_signals("acme", programs_root=programs_root)
    claims = load_claim_entries("acme", programs_root=programs_root)
    asks = load_decision_asks("acme", programs_root=programs_root)
    latest_reviews = load_latest_review_decisions("acme", programs_root=programs_root)

    assert first.issues_scanned == 2
    assert first.candidate_signals == 4
    assert first.written_signals == 4
    assert first.written_claims == 2
    assert first.written_decision_asks == 1
    assert {signal.source for signal in signals} == {"archive/newsletter", "archive/risk"}
    assert len(signals) == 4
    assert len(claims) == 2
    assert len(asks) == 1
    assert {claim.issue_number for claim in claims} == {76, 77}
    assert asks[0].issue_number == 76
    assert all(decision.decision == "approved" for decision in latest_reviews.values())
    assert set(latest_reviews) == {signal.id for signal in signals}

    second = backfill_archive_to_journal(
        program_id="acme",
        edition_name=EDITION_NAME,
        archive_root=archive_root,
        programs_root=programs_root,
        apply=True,
    )

    assert second.written_signals == 0
    assert second.skipped_existing_signals == 4
    assert second.written_claims == 0
    assert second.written_decision_asks == 0
    assert len(read_signals("acme", programs_root=programs_root)) == 4
    assert len(load_claim_entries("acme", programs_root=programs_root)) == 2
    assert len(load_decision_asks("acme", programs_root=programs_root)) == 1


def _seed_program(programs_root: Path) -> None:
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "acme",
                "name": "Acme",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "deployment_readiness",
                        "name": "Deployment Readiness",
                        "area_paths": ["One\\Adventure\\Acme\\Deployment"],
                    },
                    {
                        "id": "dd_perf",
                        "name": "DD Perf",
                        "area_paths": ["One\\Adventure\\Contoso\\Networking"],
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_confirmed_issue(
    *,
    archive_root: Path,
    edition_name: str,
    issue_number: int,
    generated_at: datetime,
    markdown_body: str,
    snapshot: Snapshot,
) -> None:
    write_confirmed_issue(
        edition=edition_name,
        issue_number=issue_number,
        snapshot=snapshot,
        html_body=f"<html><body>Issue {issue_number}</body></html>",
        markdown_body=markdown_body,
        manifest=RunManifest(
            manifest_id=f"manifest-{issue_number}",
            issue_number=issue_number,
            edition=edition_name,
            started_at=generated_at,
            ended_at=generated_at,
            config_hash="config",
            snapshot_hash=f"snapshot-{issue_number}",
            html_hash=f"html-{issue_number}",
            md_hash=f"md-{issue_number}",
            ado_calls=0,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={},
            qg_results={},
            git_sha=None,
        ),
        archive_root=archive_root,
    )