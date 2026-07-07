"""GAP-34: Override→Fact backfill (Judgment fact type)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.judgment_backfill import (
    OverrideExtraction,
    backfill_judgments_from_overrides,
    backfill_program,
    discover_overrides_files,
    extract_judgments_from_overrides,
)


def _write_overrides(
    path: Path,
    *,
    issue_number: int,
    scorecards: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "issue_number": issue_number,
        "top_3_now": [],
        "scorecards": scorecards or {},
    }
    import yaml
    path.write_text(yaml.safe_dump(body), encoding="utf-8")


def test_extract_judgments_skips_needs_input(tmp_path: Path) -> None:
    """Dimensions with ❓ Needs input are not promoted to judgments."""
    overrides = tmp_path / "programs" / "acme" / "overrides" / "issue_001.yaml"
    _write_overrides(
        overrides,
        issue_number=1,
        scorecards={
            "WS-A": {
                "Deployment Safety": {"risk": "❓ Needs input"},
                "Deployment Velocity": {"risk": "Low"},
            },
        },
    )
    extraction = extract_judgments_from_overrides(overrides, program_id="acme")
    assert extraction.issue_number == 1
    assert len(extraction.judgments) == 1
    judgment = extraction.judgments[0]
    assert judgment.dimension == "Deployment Velocity"
    assert judgment.risk_level == "Low"
    assert judgment.issue_number == 1
    assert judgment.program_id == "acme"


def test_extract_judgments_picks_up_edition_from_archive_path(tmp_path: Path) -> None:
    """An overrides file under archive/<edition>/ picks up the edition id."""
    overrides = tmp_path / "programs" / "acme" / "archive" / "nova_daily" / "overrides" / "issue_005.yaml"
    _write_overrides(
        overrides,
        issue_number=5,
        scorecards={"WS-A": {"Deployment Safety": {"risk": "Medium"}}},
    )
    extraction = extract_judgments_from_overrides(overrides, program_id="acme")
    assert extraction.edition_id == "nova_daily"
    assert extraction.issue_number == 5
    assert extraction.judgments[0].edition_id == "nova_daily"


def test_extract_judgments_handles_empty_scorecards(tmp_path: Path) -> None:
    """Empty scorecards → no judgments, no error."""
    overrides = tmp_path / "programs" / "acme" / "overrides" / "issue_002.yaml"
    _write_overrides(overrides, issue_number=2, scorecards={})
    extraction = extract_judgments_from_overrides(overrides, program_id="acme")
    assert extraction.judgments == ()


def test_extract_judgments_skips_non_dict_dimension_values(tmp_path: Path) -> None:
    """A bare string under a dimension (not a dict) is ignored."""
    overrides = tmp_path / "programs" / "acme" / "overrides" / "issue_003.yaml"
    _write_overrides(
        overrides,
        issue_number=3,
        scorecards={"WS-A": {"Deployment Safety": "low"}},  # not a dict
    )
    extraction = extract_judgments_from_overrides(overrides, program_id="acme")
    assert extraction.judgments == ()


def test_extract_judgments_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """Missing file → empty extraction."""
    overrides = tmp_path / "programs" / "acme" / "overrides" / "issue_999.yaml"
    extraction = extract_judgments_from_overrides(overrides, program_id="acme")
    assert extraction.judgments == ()


def test_backfill_dry_run_does_not_call_writer(tmp_path: Path) -> None:
    """apply=False: nothing is written to the fact store."""
    overrides = tmp_path / "programs" / "acme" / "overrides" / "issue_010.yaml"
    _write_overrides(
        overrides,
        issue_number=10,
        scorecards={"WS-A": {"Deployment Safety": {"risk": "High"}}},
    )
    called = {"count": 0}

    def _fail_if_called(*a, **k):
        called["count"] += 1
        raise AssertionError("append_program_event should not be called in dry-run")

    import src.core.judgment_backfill as jb

    real_append = jb.append_program_event
    jb.append_program_event = _fail_if_called  # type: ignore[assignment]
    try:
        extraction = backfill_judgments_from_overrides(
            overrides, program_id="acme", apply=False
        )
    finally:
        jb.append_program_event = real_append  # type: ignore[assignment]
    assert called["count"] == 0
    assert len(extraction.judgments) == 1


def test_backfill_apply_invokes_append(tmp_path: Path) -> None:
    """apply=True: append_program_event is called once per judgment."""
    overrides = tmp_path / "programs" / "acme" / "overrides" / "issue_011.yaml"
    _write_overrides(
        overrides,
        issue_number=11,
        scorecards={
            "WS-A": {
                "Deployment Safety": {"risk": "Low"},
                "Deployment Velocity": {"risk": "Medium"},
            }
        },
    )
    calls: list[dict] = []

    def _fake_append(program_id, event, *, recorded_at=None, home_root=None, db_root=None):
        calls.append(
            {
                "program_id": program_id,
                "event": event,
                "recorded_at": recorded_at,
            }
        )
        return None  # result shape not asserted

    import src.core.judgment_backfill as jb
    real_append = jb.append_program_event
    jb.append_program_event = _fake_append  # type: ignore[assignment]
    try:
        backfill_judgments_from_overrides(overrides, program_id="acme", apply=True)
    finally:
        jb.append_program_event = real_append  # type: ignore[assignment]
    assert len(calls) == 2
    for call in calls:
        assert call["program_id"] == "acme"
        assert call["event"].fact_type == "judgment.dimension"
        # natural_key encodes program + issue + edition + dimension
        assert "acme" in call["event"].natural_key
        assert "|11|" in call["event"].natural_key


def test_discover_overrides_files_finds_all(tmp_path: Path) -> None:
    """discover finds overrides under program and archive paths."""
    _write_overrides(
        tmp_path / "overrides" / "issue_001.yaml",
        issue_number=1,
        scorecards={"WS-A": {"Dim": {"risk": "Low"}}},
    )
    _write_overrides(
        tmp_path / "archive" / "nova_daily" / "overrides" / "issue_002.yaml",
        issue_number=2,
        scorecards={"WS-A": {"Dim": {"risk": "Medium"}}},
    )
    files = discover_overrides_files(tmp_path)
    assert len(files) == 2


def test_backfill_program_returns_extractions_per_file(tmp_path: Path) -> None:
    """backfill_program returns one extraction per overrides file."""
    _write_overrides(
        tmp_path / "overrides" / "issue_020.yaml",
        issue_number=20,
        scorecards={"WS-A": {"Dim": {"risk": "Low"}}},
    )
    _write_overrides(
        tmp_path / "archive" / "acme_weekly" / "overrides" / "issue_021.yaml",
        issue_number=21,
        scorecards={"WS-B": {"Other": {"risk": "High"}}},
    )
    extractions = backfill_program("acme", program_dir=tmp_path, apply=False)
    assert len(extractions) == 2
    numbers = sorted(e.issue_number for e in extractions)
    assert numbers == [20, 21]


def test_judgment_natural_key_is_stable_across_runs(tmp_path: Path) -> None:
    """Running extract twice yields judgments with identical natural keys."""
    overrides = tmp_path / "overrides" / "issue_030.yaml"
    _write_overrides(
        overrides,
        issue_number=30,
        scorecards={"WS-A": {"Dim": {"risk": "Medium"}}},
    )
    first = extract_judgments_from_overrides(overrides, program_id="acme")
    second = extract_judgments_from_overrides(overrides, program_id="acme")
    # Different id (counter) but same natural_key payload composition
    from src.core.judgment_backfill import _judgment_to_event
    e1 = _judgment_to_event(first.judgments[0], program_id="acme")
    e2 = _judgment_to_event(second.judgments[0], program_id="acme")
    assert e1.natural_key == e2.natural_key
