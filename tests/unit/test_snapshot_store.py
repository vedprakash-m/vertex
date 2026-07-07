from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import StateError
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.snapshot_store import find_orphaned_staging, read_snapshot, write_confirmed


EDITION_NAME = "acme_weekly"


def _build_snapshot(issue_number: int = 7) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 5, 8, 30, tzinfo=timezone.utc),
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
                tags=["acme", "readiness"],
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


def test_write_confirmed_stages_then_promotes_snapshot(tmp_path: Path) -> None:
    final_path = write_confirmed(
        edition=EDITION_NAME,
        issue_number=7,
        snapshot=_build_snapshot(),
        archive_root=tmp_path,
    )

    staging_root = tmp_path / EDITION_NAME / "staging"
    payload = json.loads(final_path.read_text(encoding="utf-8"))
    restored = read_snapshot(final_path)

    assert final_path == tmp_path / EDITION_NAME / "snapshots" / "issue_007.snapshot.json"
    assert not staging_root.exists()
    assert payload["issue_number"] == 7
    assert payload["edition_type"] == "detailed"
    assert payload["items"][0]["risk_level"] == "medium"
    assert restored.scorecards[0].scorecard_name == "Acme Readiness"
    assert restored.items[0].target_date == date(2026, 6, 30)


def test_write_confirmed_refuses_orphaned_staging_directory(tmp_path: Path) -> None:
    staging_file = tmp_path / EDITION_NAME / "staging" / "snapshots" / "issue_006.snapshot.json"
    staging_file.parent.mkdir(parents=True, exist_ok=True)
    staging_file.write_text("{}", encoding="utf-8")

    assert find_orphaned_staging(EDITION_NAME, tmp_path) is not None

    with pytest.raises(StateError, match="Incomplete confirm detected"):
        write_confirmed(
            edition=EDITION_NAME,
            issue_number=7,
            snapshot=_build_snapshot(),
            archive_root=tmp_path,
        )


def test_write_confirmed_rejects_naive_generated_at(tmp_path: Path) -> None:
    baseline = _build_snapshot()
    snapshot = Snapshot(
        issue_number=baseline.issue_number,
        generated_at=datetime(2026, 5, 5, 9, 0),
        ado_data_as_of=baseline.ado_data_as_of,
        edition_type=baseline.edition_type,
        items=baseline.items,
        scorecards=baseline.scorecards,
        schema_version=baseline.schema_version,
    )

    with pytest.raises(ValueError, match="snapshot datetimes must include timezone information"):
        write_confirmed(
            edition=EDITION_NAME,
            issue_number=7,
            snapshot=snapshot,
            archive_root=tmp_path,
        )


def test_read_snapshot_rejects_naive_generated_at(tmp_path: Path) -> None:
    snapshot_path = tmp_path / EDITION_NAME / "snapshots" / "issue_007.snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "issue_number": 7,
                "generated_at": "2026-05-05T09:00:00",
                "ado_data_as_of": "2026-05-05T08:30:00+00:00",
                "edition_type": "detailed",
                "items": [],
                "scorecards": [],
                "schema_version": "1.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="generated_at must include timezone information"):
        read_snapshot(snapshot_path)

