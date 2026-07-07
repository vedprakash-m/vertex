from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.archive_store import write_confirmed_issue
from src.core.incident_journal_store import append_incident_entry
from src.core.models import ConfirmedDimension, EditionType, RunManifest, RiskLevel, Snapshot, SnapshotItem
from src.core.models import Confidence
from src.core.models_v2 import IncidentEntry
from src.core.semantic_index import (
    find_archive_similarity_match,
    get_semantic_index_path,
    get_semantic_index_state_path,
    load_semantic_index_state,
    mark_semantic_index_dirty,
    search_archive_semantic_index,
    search_history_semantic_index,
    update_archive_semantic_index_for_issue,
)


EDITION_NAME = "acme_weekly"


def test_search_archive_semantic_index_builds_local_index_and_returns_ranked_matches(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=3,
        markdown_body="# Issue 003\nUD chunking latency regression remains the gating risk for deployment readiness.\n",
    )
    _write_confirmed_issue(
        archive_root,
        issue_number=4,
        markdown_body="# Issue 004\nRepair workstream improved its follow-through and reduced stale asks.\n",
    )

    matches = search_archive_semantic_index(EDITION_NAME, "UD chunking risk", archive_root=archive_root)

    assert matches
    assert matches[0].issue_number == 3
    assert matches[0].source_type == "narrative"
    assert matches[0].risk_level == RiskLevel.MEDIUM
    assert "UD chunking latency regression" in matches[0].excerpt
    assert get_semantic_index_path(EDITION_NAME, archive_root=archive_root).exists()
    state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root)
    assert state is not None
    assert state.latest_confirmed_issue == 4
    assert state.semantic_index_dirty is False
    assert get_semantic_index_state_path(EDITION_NAME, archive_root=archive_root).exists()


def test_semantic_index_dirty_state_clears_after_incremental_issue_refresh(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=8,
        markdown_body="# Issue 008\nInitial semantic memory excerpt.\n",
    )

    search_archive_semantic_index(EDITION_NAME, "initial semantic", archive_root=archive_root)

    mark_semantic_index_dirty(
        EDITION_NAME,
        "confirm index refresh failed",
        archive_root=archive_root,
    )
    dirty_state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root)

    assert dirty_state is not None
    assert dirty_state.semantic_index_dirty is True
    assert dirty_state.dirty_reason == "confirm index refresh failed"

    _write_confirmed_issue(
        archive_root,
        issue_number=9,
        markdown_body="# Issue 009\nFollow-up excerpt after confirm success.\n",
    )

    update_archive_semantic_index_for_issue(
        EDITION_NAME,
        9,
        archive_root=archive_root,
    )
    refreshed_state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root)

    assert refreshed_state is not None
    assert refreshed_state.semantic_index_dirty is False
    assert refreshed_state.latest_confirmed_issue == 9

    matches = search_archive_semantic_index(EDITION_NAME, "follow-up excerpt", archive_root=archive_root)
    assert matches
    assert matches[0].issue_number == 9


def test_find_archive_similarity_match_returns_high_overlap_prior_excerpt(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=11,
        markdown_body="# Issue 011\nUD chunking latency regression remains the gating risk for deployment readiness.\n",
    )

    similarity_match = find_archive_similarity_match(
        EDITION_NAME,
        "UD chunking latency regression remains the gating risk for deployment readiness.",
        archive_root=archive_root,
        min_similarity=0.9,
    )

    assert similarity_match is not None
    assert similarity_match.issue_number == 11
    assert similarity_match.similarity >= 0.9


def test_search_history_semantic_index_includes_incident_journal_entries_and_refreshes_stale_index(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_edition_scaffold(tmp_path, program_id="acme")
    _write_confirmed_issue(
        archive_root,
        issue_number=13,
        markdown_body="# Issue 013\nDeployment rhythm stabilized after prior mitigation work completed.\n",
    )

    search_archive_semantic_index(EDITION_NAME, "deployment rhythm", archive_root=archive_root)

    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="456789",
            signal_id="sig-456789",
            observed_at=datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 6, 10, 5, tzinfo=timezone.utc),
            belief_change_summary="Bridge outage exposed hidden capacity coupling and forced a rollback mitigation.",
            workstream_id="runtime",
            owning_team="Acme Runtime",
            severity=2,
            linked_work_item_ids=(4112,),
            ado_entity_refs=("wi:4112",),
            confidence=Confidence.HIGH,
        ),
        programs_root=tmp_path / "programs",
    )

    history_matches = search_history_semantic_index(
        EDITION_NAME,
        "bridge outage rollback mitigation",
        archive_root=archive_root,
    )

    assert history_matches
    assert history_matches[0].source_type == "incident"
    assert history_matches[0].issue_number is None
    assert history_matches[0].source_ref == "IcM 456789"
    assert history_matches[0].risk_level == RiskLevel.HIGH
    assert "hidden capacity coupling" in history_matches[0].excerpt

    archive_only_matches = search_archive_semantic_index(
        EDITION_NAME,
        "bridge outage rollback mitigation",
        archive_root=archive_root,
    )
    assert archive_only_matches
    assert all(match.source_type == "narrative" for match in archive_only_matches)
    assert all(match.source_ref != "IcM 456789" for match in archive_only_matches)


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


def _write_edition_scaffold(root: Path, *, program_id: str) -> None:
    editions_dir = root / "editions"
    programs_dir = root / "programs" / program_id
    editions_dir.mkdir(parents=True, exist_ok=True)
    programs_dir.mkdir(parents=True, exist_ok=True)
    (editions_dir / f"{EDITION_NAME}.yaml").write_text(
        "\n".join(
            (
                f"id: {EDITION_NAME}",
                f"program_id: {program_id}",
            )
        )
        + "\n",
        encoding="utf-8",
    )