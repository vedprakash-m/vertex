from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.archive_store import find_archive_index_inconsistencies, get_all_green_streak, load_previous_confirmed_snapshot, read_archive_index, read_scorecard_history, read_vitality_history
from src.core.archive_store import write_confirmed_issue, write_skipped_issue
from src.core.archive_store import update_archive_issue_metadata
from src.core.archive_store import verify_archive_integrity, archive_integrity_waived
from src.core.models import ConfirmedDimension, EditionType, RunManifest, RiskLevel, Snapshot, SnapshotItem
from src.core.models_v2 import VitalityArchiveEntry, VitalityArchiveWorkstream


EDITION_NAME = "acme_weekly"


def _build_snapshot(issue_number: int = 78) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=101,
                type="Feature",
                title="Deployment readiness",
                state="Active",
                assigned_to="Vertex Maintainer",
                area_path="One\\Adventure\\Acme",
                target_date=date(2026, 6, 30),
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
    )


def _build_manifest(issue_number: int = 78) -> RunManifest:
    return RunManifest(
        manifest_id="manifest-78",
        issue_number=issue_number,
        edition=EDITION_NAME,
        started_at=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=3,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha=None,
    )


def test_write_confirmed_issue_archives_outputs_and_updates_index(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    overrides_source = tmp_path / "overrides.yaml"
    review_source = tmp_path / "review_status.yaml"
    narratives_source = tmp_path / "narratives"
    continuation_contract_source = tmp_path / "issue_078.continuation_contract.json"
    overrides_source.write_text("issue_number: 78\n", encoding="utf-8")
    review_source.write_text("issue_number: 78\nsections: []\n", encoding="utf-8")
    narratives_source.mkdir(parents=True, exist_ok=True)
    (narratives_source / "exec_summary.md").write_text("Final summary.\n", encoding="utf-8")
    continuation_contract_source.write_text('{"schema_version":"1.0","issue_number":78}\n', encoding="utf-8")
    manifest = replace(
        _build_manifest(),
        notes=("Reconstructed baseline imported from local corpus.",),
        metadata={"reconstructed_baseline": True},
    )

    paths = write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=78,
        snapshot=_build_snapshot(),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=manifest,
        eml_bytes=b"From: Vertex Maintainer <maintainer@example.com>\r\nSubject: Issue 78\r\n\r\nRendered",
        overrides_source=overrides_source,
        review_status_source=review_source,
        narratives_source_dir=narratives_source,
        continuation_contract_source=continuation_contract_source,
        vitality_record=VitalityArchiveEntry(
            issue_number=78,
            confirmed_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
            aggregate_score=73,
            items_total=52,
            items_fresh=38,
            avg_richness=68,
            leakage_events=5,
            per_workstream={"deployment_readiness": VitalityArchiveWorkstream(score=80, items=15, fresh=12)},
            per_owner={"operator": VitalityArchiveWorkstream(score=88, items=4, fresh=3)},
        ),
        archive_metadata={"reconstructed_baseline": True},
        archive_root=archive_root,
    )

    index = read_archive_index(EDITION_NAME, archive_root)
    scorecard_history = read_scorecard_history(EDITION_NAME, archive_root)
    vitality_history = read_vitality_history(EDITION_NAME, archive_root)
    manifest_payload = json.loads(paths.manifest_path.read_text(encoding="utf-8"))

    assert paths.snapshot_path.exists()
    assert paths.eml_path is not None and paths.eml_path.exists()
    assert paths.html_path.exists()
    assert paths.md_path.exists()
    assert paths.overrides_path is not None and paths.overrides_path.exists()
    assert paths.review_path is not None and paths.review_path.exists()
    assert paths.narratives_path is not None and (paths.narratives_path / "exec_summary.md").exists()
    assert paths.continuation_contract_path is not None and paths.continuation_contract_path.exists()
    assert paths.vitality_path is not None and paths.vitality_path.exists()
    assert index.issues[0].issue_number == 78
    assert index.issues[0].eml_path is not None
    assert index.issues[0].metadata["reconstructed_baseline"] is True
    assert manifest_payload["schema_version"] == "1.0"
    assert manifest_payload["notes"] == ["Reconstructed baseline imported from local corpus."]
    assert manifest_payload["metadata"]["reconstructed_baseline"] is True
    assert scorecard_history[0]["scorecard_name"] == "Acme Readiness"
    assert scorecard_history[0]["risk"] == "medium"
    assert vitality_history[0].aggregate_score == 73
    assert vitality_history[0].per_workstream["deployment_readiness"].fresh == 12
    assert vitality_history[0].per_owner["operator"].score == 88


def test_write_skipped_issue_updates_index_and_preserves_previous_confirmed_snapshot(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_build_snapshot(issue_number=1),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=1),
        archive_root=archive_root,
    )
    index_path = write_skipped_issue(
        edition=EDITION_NAME,
        issue_number=2,
        reason="Holiday week",
        archive_root=archive_root,
    )

    index = read_archive_index(EDITION_NAME, archive_root)
    previous_snapshot, previous_issue_number = load_previous_confirmed_snapshot(
        EDITION_NAME,
        3,
        archive_root=archive_root,
    )

    assert index_path.exists()
    assert len(index.issues) == 2
    assert index.issues[0].kind == "confirmed"
    assert index.issues[1].kind == "skipped"
    assert index.issues[1].reason == "Holiday week"
    assert index.issues[1].snapshot_path is None
    assert previous_snapshot is not None
    assert previous_issue_number == 1


def test_find_archive_index_inconsistencies_allows_published_eml_sidecar(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"

    paths = write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=77,
        snapshot=_build_snapshot(issue_number=77),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=77),
        eml_bytes=b"From: Vertex Maintainer <maintainer@example.com>\r\nSubject: Issue 77\r\n\r\nRendered",
        archive_root=archive_root,
    )

    assert paths.eml_path is not None
    published_eml_path = paths.eml_path.parent.parent / "published_eml" / "issue_077.published.eml"
    published_eml_path.parent.mkdir(parents=True, exist_ok=True)
    published_eml_path.write_bytes(b"published")

    index_path = archive_root / EDITION_NAME / "index.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["issues"][0].setdefault("metadata", {})["published_eml_path"] = str(published_eml_path)
    index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    assert find_archive_index_inconsistencies(EDITION_NAME, archive_root=archive_root) == ()


# ---------------------------------------------------------------------------
# WS-1 archive integrity gate
# ---------------------------------------------------------------------------


def test_verify_archive_integrity_clean_workspace(tmp_path: Path) -> None:
    """WS-1: when every confirmed issue's artifacts are on disk and the index
    matches, verify_archive_integrity returns ok=True with no inconsistencies.
    """
    archive_root = tmp_path / "archive"
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=77,
        snapshot=_build_snapshot(issue_number=77),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=77),
        eml_bytes=b"From: Vertex Maintainer <maintainer@example.com>\r\nSubject: Issue 77\r\n\r\nRendered",
        archive_root=archive_root,
    )
    result = verify_archive_integrity(EDITION_NAME, archive_root=archive_root)
    assert result.ok, f"Expected ok=True, got inconsistencies: {result.inconsistencies}"
    assert result.edition == EDITION_NAME
    assert result.inconsistencies == ()


def test_verify_archive_integrity_detects_missing_snapshot(tmp_path: Path) -> None:
    """WS-1 (PB-1 / PB-2): when the index references a snapshot that does
    not exist on disk (the live Acme state for issue 078), the integrity gate
    MUST report it as an inconsistency. The gate is read-only — it does NOT
    delete the dangling index entry. Remediation is via
    scripts/reconcile_archive_index.py with a human-gated strategy.
    """
    archive_root = tmp_path / "archive"
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=77,
        snapshot=_build_snapshot(issue_number=77),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=77),
        eml_bytes=b"From: Vertex Maintainer <maintainer@example.com>\r\nSubject: Issue 77\r\n\r\nRendered",
        archive_root=archive_root,
    )
    # Plant a dangling issue_078 entry whose snapshot does not exist.
    index_path = archive_root / EDITION_NAME / "index.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["issues"].append({
        "eml_path": None,
        "generated_at": "2026-06-07T06:03:20.177354+00:00",
        "html_path": str(archive_root / EDITION_NAME / "html" / "issue_078.html"),
        "issue_number": 78,
        "kind": "confirmed",
        "manifest_path": str(archive_root / EDITION_NAME / "manifests" / "issue_078.json"),
        "md_path": str(archive_root / EDITION_NAME / "md" / "issue_078.md"),
        "metadata": {"published": True},
        "reason": None,
        "snapshot_path": str(archive_root / EDITION_NAME / "snapshots" / "issue_078.snapshot.json"),
    })
    index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = verify_archive_integrity(EDITION_NAME, archive_root=archive_root)
    assert not result.ok
    # At least one inconsistency names issue 078.
    joined = " | ".join(result.inconsistencies)
    assert "078" in joined, f"Expected 078 in inconsistencies, got: {joined}"


def test_archive_integrity_waived_default_false() -> None:
    """WS-1: the waiver is OFF by default. Callers must explicitly set
    VERTEX_ARCHIVE_INTEGRITY_WAIVER=1 (and the per-call flag, where
    applicable) to bypass the gate. This test pins the default to catch any
    silent flip of the safety policy.
    """
    assert archive_integrity_waived(env={}) is False
    assert archive_integrity_waived(env={"VERTEX_ARCHIVE_INTEGRITY_WAIVER": "1"}) is True
    # Anything other than the exact "1" must be False.
    assert archive_integrity_waived(env={"VERTEX_ARCHIVE_INTEGRITY_WAIVER": "true"}) is False
    assert archive_integrity_waived(env={"VERTEX_ARCHIVE_INTEGRITY_WAIVER": "yes"}) is False


def test_update_archive_issue_metadata_merges_published_eml_path(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=77,
        snapshot=_build_snapshot(issue_number=77),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=77),
        archive_metadata={"existing_flag": True},
        archive_root=archive_root,
    )

    published_eml_path = archive_root / EDITION_NAME / "eml" / "issue_077.published.eml"
    published_eml_path.parent.mkdir(parents=True, exist_ok=True)
    published_eml_path.write_bytes(b"published")

    index_path = update_archive_issue_metadata(
        EDITION_NAME,
        77,
        {"published_eml_path": str(published_eml_path)},
        archive_root=archive_root,
    )
    index = read_archive_index(EDITION_NAME, archive_root=archive_root)

    assert index_path.exists()
    assert index.issues[0].metadata == {
        "existing_flag": True,
        "published_eml_path": str(published_eml_path),
    }


def test_get_all_green_streak_counts_only_consecutive_confirmed_issues(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"

    low_snapshot = replace(
        _build_snapshot(issue_number=1),
        scorecards=(replace(_build_snapshot(issue_number=1).scorecards[0], risk=RiskLevel.LOW),),
    )
    done_snapshot = replace(
        _build_snapshot(issue_number=2),
        scorecards=(replace(_build_snapshot(issue_number=2).scorecards[0], risk=RiskLevel.DONE),),
    )
    medium_snapshot = replace(
        _build_snapshot(issue_number=3),
        scorecards=(replace(_build_snapshot(issue_number=3).scorecards[0], risk=RiskLevel.MEDIUM),),
    )

    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=low_snapshot,
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=1),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=2,
        snapshot=done_snapshot,
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=2),
        archive_root=archive_root,
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=3,
        snapshot=medium_snapshot,
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number=3),
        archive_root=archive_root,
    )

    assert get_all_green_streak(EDITION_NAME, archive_root=archive_root, before_issue_number=3) == 2
    assert get_all_green_streak(EDITION_NAME, archive_root=archive_root, before_issue_number=4) == 0

