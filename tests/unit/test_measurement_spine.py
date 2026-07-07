"""Tests for FR-SG-27: measurement spine metrics computed at confirm time."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.measurement_spine import IssueMetrics, write_issue_metrics, load_issue_metrics


def test_write_and_load_issue_metrics_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics = IssueMetrics(
        program_id="acme",
        issue_number=42,
        edition_id="acme_weekly",
        computed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        override_count=3,
        claim_coverage=None,
        source_health_pct=0.875,
        provenance_confidence=0.6,
        baseline_parity_score=None,
        manual_rewrite_rate=None,
    )
    write_issue_metrics(metrics, programs_root=programs_root)
    loaded = load_issue_metrics("acme", 42, programs_root=programs_root)
    assert loaded is not None
    assert loaded.program_id == "acme"
    assert loaded.issue_number == 42
    assert loaded.override_count == 3
    assert loaded.source_health_pct == 0.875
    assert loaded.provenance_confidence == 0.6
    assert loaded.claim_coverage is None
    assert loaded.baseline_parity_score is None


def test_load_issue_metrics_returns_none_for_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    result = load_issue_metrics("acme", 99, programs_root=programs_root)
    assert result is None


def test_write_issue_metrics_persists_all_null_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics = IssueMetrics(
        program_id="acme",
        issue_number=1,
        edition_id="acme_weekly",
        computed_at=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        override_count=0,
        claim_coverage=None,
        source_health_pct=None,
        provenance_confidence=None,
        baseline_parity_score=None,
        manual_rewrite_rate=None,
    )
    write_issue_metrics(metrics, programs_root=programs_root)
    loaded = load_issue_metrics("acme", 1, programs_root=programs_root)
    assert loaded is not None
    assert loaded.override_count == 0
    assert loaded.source_health_pct is None
    assert loaded.provenance_confidence is None


def test_load_issue_metrics_rejects_non_string_program_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_path = programs_root / "acme" / "metrics" / "issue_42.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        yaml.safe_dump(
            {
                "program_id": 123,
                "issue_number": 42,
                "edition_id": "acme_weekly",
                "computed_at": "2026-05-20T10:00:00+00:00",
                "override_count": 3,
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="program_id must be a string"):
        load_issue_metrics("acme", 42, programs_root=programs_root)


def test_load_issue_metrics_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_path = programs_root / "acme" / "metrics" / "issue_42.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        yaml.safe_dump(
            {
                "program_id": "acme",
                "issue_number": "42",
                "edition_id": "acme_weekly",
                "computed_at": "2026-05-20T10:00:00+00:00",
                "override_count": 3,
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        load_issue_metrics("acme", 42, programs_root=programs_root)


def test_load_issue_metrics_rejects_non_string_computed_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_path = programs_root / "acme" / "metrics" / "issue_42.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        yaml.safe_dump(
            {
                "program_id": "acme",
                "issue_number": 42,
                "edition_id": "acme_weekly",
                "computed_at": 123,
                "override_count": 3,
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="computed_at must be a string"):
        load_issue_metrics("acme", 42, programs_root=programs_root)


def test_load_issue_metrics_rejects_naive_computed_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_path = programs_root / "acme" / "metrics" / "issue_42.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        yaml.safe_dump(
            {
                "program_id": "acme",
                "issue_number": 42,
                "edition_id": "acme_weekly",
                "computed_at": "2026-05-20T10:00:00",
                "override_count": 3,
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="computed_at must include timezone information"):
        load_issue_metrics("acme", 42, programs_root=programs_root)


def test_load_issue_metrics_rejects_numeric_string_claim_coverage(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_path = programs_root / "acme" / "metrics" / "issue_42.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        yaml.safe_dump(
            {
                "program_id": "acme",
                "issue_number": 42,
                "edition_id": "acme_weekly",
                "computed_at": "2026-05-20T10:00:00+00:00",
                "override_count": 3,
                "claim_coverage": "0.75",
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="claim_coverage must be numeric"):
        load_issue_metrics("acme", 42, programs_root=programs_root)


def test_load_issue_metrics_rejects_non_numeric_manual_rewrite_rate(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    metrics_path = programs_root / "acme" / "metrics" / "issue_42.yaml"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(
        yaml.safe_dump(
            {
                "program_id": "acme",
                "issue_number": 42,
                "edition_id": "acme_weekly",
                "computed_at": "2026-05-20T10:00:00+00:00",
                "override_count": 3,
                "manual_rewrite_rate": {"value": 0.1},
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="manual_rewrite_rate must be numeric"):
        load_issue_metrics("acme", 42, programs_root=programs_root)
