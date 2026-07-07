from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.core.calibration_engine import (
    build_calibration_report,
    build_dri_calibration_priors,
    build_calibration_priors,
    compute_calibration_for_edition,
    read_calibration_prior,
    write_calibration_prior,
)
from src.core.models_v2 import ClaimEntry, WorkstreamCalibration


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
_PROGRAM_ID = "acme"
_EDITION = "acme_weekly"


def _claim(claim_id: str, workstream_id: str | None, status: str = "open") -> ClaimEntry:
    return ClaimEntry(
        id=claim_id,
        program_id=_PROGRAM_ID,
        edition_id=_EDITION,
        issue_number=78,
        workstream_id=workstream_id,
        text=f"Claim {claim_id}",
        entity_refs=(),
        claim_date=date(2026, 4, 1),
        owner_alias=None,
        due_date=None,
        status=status,
    )


def test_build_calibration_priors_computes_accuracy() -> None:
    claims = (
        _claim("c1", "deployment"),
        _claim("c2", "deployment"),
        _claim("c3", "deployment"),
        _claim("c4", "deployment"),
        _claim("c5", "deployment"),
    )
    assessments_by_id = {
        "c1": "met",
        "c2": "met",
        "c3": "met",
        "c4": "contradicted",
        "c5": "stale",
    }
    result = build_calibration_priors(claims, assessments_by_id)
    assert len(result) == 1
    ws = result[0]
    assert ws.workstream_id == "deployment"
    assert ws.met == 3
    assert ws.contradicted == 1
    assert ws.stale == 1
    assert ws.sample_size == 5
    assert ws.claim_accuracy == pytest.approx(3 / 5)


def test_build_calibration_priors_null_accuracy_below_threshold() -> None:
    claims = (_claim("c1", "deployment"),)
    assessments_by_id = {"c1": "met"}
    result = build_calibration_priors(claims, assessments_by_id)
    assert len(result) == 1
    assert result[0].sample_size == 1
    assert result[0].claim_accuracy is None  # < 5 samples


def test_build_calibration_priors_skips_open_and_no_workstream() -> None:
    claims = (
        _claim("c1", None),       # no workstream → excluded
        _claim("c2", "ws"),        # open status → not counted
    )
    assessments_by_id = {}  # both remain "open"
    result = build_calibration_priors(claims, assessments_by_id)
    assert len(result) == 0


def test_build_calibration_priors_multiple_workstreams() -> None:
    claims = (
        _claim("a1", "alpha"),
        _claim("a2", "alpha"),
        _claim("a3", "alpha"),
        _claim("a4", "alpha"),
        _claim("a5", "alpha"),
        _claim("b1", "beta"),
        _claim("b2", "beta"),
        _claim("b3", "beta"),
        _claim("b4", "beta"),
        _claim("b5", "beta"),
    )
    assessments_by_id = {
        "a1": "met", "a2": "met", "a3": "met", "a4": "met", "a5": "met",
        "b1": "stale", "b2": "stale", "b3": "stale", "b4": "stale", "b5": "stale",
    }
    result = build_calibration_priors(claims, assessments_by_id)
    assert len(result) == 2
    alpha = next(r for r in result if r.workstream_id == "alpha")
    beta = next(r for r in result if r.workstream_id == "beta")
    assert alpha.claim_accuracy == pytest.approx(1.0)
    assert beta.claim_accuracy == pytest.approx(0.0)


def test_build_dri_calibration_priors_groups_by_owner_alias() -> None:
    claims = (
        ClaimEntry(
            id="c1",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="deployment",
            text="Claim c1",
            entity_refs=(),
            claim_date=date(2026, 4, 1),
            owner_alias="alex",
            due_date=None,
        ),
        ClaimEntry(
            id="c2",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="deployment",
            text="Claim c2",
            entity_refs=(),
            claim_date=date(2026, 4, 2),
            owner_alias="alex",
            due_date=None,
        ),
        ClaimEntry(
            id="c3",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="repair",
            text="Claim c3",
            entity_refs=(),
            claim_date=date(2026, 4, 3),
            owner_alias="jamie",
            due_date=None,
        ),
    )
    assessments_by_id = {
        "c1": "met",
        "c2": "contradicted",
        "c3": "stale",
    }

    result = build_dri_calibration_priors(claims, assessments_by_id)

    assert [row.subject_id for row in result] == ["alex", "jamie"]
    assert result[0].met == 1
    assert result[0].contradicted == 1
    assert result[1].stale == 1


def test_build_calibration_report_summarizes_terminal_claims_and_trend() -> None:
    claims = (
        ClaimEntry(
            id="old-1",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="deployment",
            text="Claim old-1",
            entity_refs=(),
            claim_date=date(2026, 2, 1),
            owner_alias="alex",
            due_date=None,
            status="contradicted",
        ),
        ClaimEntry(
            id="old-2",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="deployment",
            text="Claim old-2",
            entity_refs=(),
            claim_date=date(2026, 2, 8),
            owner_alias="alex",
            due_date=None,
            status="stale",
        ),
        ClaimEntry(
            id="old-3",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="repair",
            text="Claim old-3",
            entity_refs=(),
            claim_date=date(2026, 2, 15),
            owner_alias="jamie",
            due_date=None,
            status="met",
        ),
        ClaimEntry(
            id="new-1",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="deployment",
            text="Claim new-1",
            entity_refs=(),
            claim_date=date(2026, 4, 20),
            owner_alias="alex",
            due_date=None,
            status="met",
        ),
        ClaimEntry(
            id="new-2",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="repair",
            text="Claim new-2",
            entity_refs=(),
            claim_date=date(2026, 4, 27),
            owner_alias="jamie",
            due_date=None,
            status="met",
        ),
        ClaimEntry(
            id="new-3",
            program_id=_PROGRAM_ID,
            edition_id=_EDITION,
            issue_number=78,
            workstream_id="repair",
            text="Claim new-3",
            entity_refs=(),
            claim_date=date(2026, 5, 3),
            owner_alias="jamie",
            due_date=None,
            status="met",
        ),
    )

    report = build_calibration_report(
        _PROGRAM_ID,
        claims=claims,
        items=(),
        as_of=_NOW,
    )

    assert report.total_terminal_claims == 6
    assert report.met == 4
    assert report.contradicted == 1
    assert report.stale == 1
    assert report.week_span >= 1
    assert report.trajectory_delta_points == 67
    assert [row.subject_id for row in report.dri_rows] == ["alex", "jamie"]


def test_write_and_read_calibration_prior_round_trips(tmp_path: Path) -> None:
    calibrations = (
        WorkstreamCalibration(workstream_id="deployment", met=4, contradicted=1, stale=1),
        WorkstreamCalibration(workstream_id="safety", met=10, contradicted=0, stale=2),
    )
    # Use a temp archive structure
    archive_root = tmp_path / "archive"

    # We need to stub get_archive_root — simplest: write directly to known path
    from unittest.mock import patch
    edition_archive = archive_root / _EDITION
    edition_archive.mkdir(parents=True, exist_ok=True)

    with patch("src.core.calibration_engine.get_archive_root", return_value=edition_archive):
        path = write_calibration_prior(_EDITION, 78, calibrations, archive_root=archive_root)
        assert path.exists()

        loaded = read_calibration_prior(_EDITION, 78, archive_root=archive_root)

    assert loaded is not None
    assert len(loaded) == 2
    assert loaded[0].workstream_id == "deployment"
    assert loaded[0].met == 4
    assert loaded[1].workstream_id == "safety"
    assert loaded[1].sample_size == 12
    assert loaded[1].claim_accuracy == pytest.approx(10 / 12)


def test_read_calibration_prior_returns_none_when_missing(tmp_path: Path) -> None:
    from unittest.mock import patch
    edition_archive = tmp_path / "archive" / _EDITION
    edition_archive.mkdir(parents=True, exist_ok=True)
    with patch("src.core.calibration_engine.get_archive_root", return_value=edition_archive):
        result = read_calibration_prior(_EDITION, 99, archive_root=tmp_path / "archive")
    assert result is None


def test_workstream_calibration_sample_size_and_accuracy_properties() -> None:
    ws = WorkstreamCalibration(workstream_id="ws", met=3, contradicted=2, stale=1)
    assert ws.sample_size == 6
    assert ws.claim_accuracy == pytest.approx(0.5)

    ws_small = WorkstreamCalibration(workstream_id="ws", met=2, contradicted=1, stale=0)
    assert ws_small.sample_size == 3
    assert ws_small.claim_accuracy is None
