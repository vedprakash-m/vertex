"""
Performance baseline harness — S1A.1.

Runs gather against a 50-item in-memory fixture and records per-phase
wall-clock times to output/perf_baseline.json.

The 'fetch' phase proxies the ADO revision-load cost.  On a real program each
item requires one list_work_item_revisions() round-trip, making fetch O(n).
Gate: if fetch_pct >= 0.30 on a real program, Stage 1B (async gather) is
justified.  The fixture run records the processing baseline (fetch is near-zero
because the loader is a simple in-memory return).

Usage:
    # Fixture run (no ADO access needed):
    python -m pytest tests/perf/test_perf_baseline.py -v

    # Results are written to output/perf_baseline.json after each run.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest

from src.commands import gather as gather_module
from src.commands.gather import GatherProgressEvent, gather_program
from src.core.models import Revision, RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, Program, Workstream

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ITEM_COUNT = 50
REVISIONS_PER_ITEM = 3
OUTPUT_PATH = Path("output/perf_baseline.json")

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_program() -> Program:
    return Program(
        schema_version="2.0",
        id="acme",
        name="Adventure + DD on PF",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )


def _make_workstreams() -> tuple[Workstream, ...]:
    return (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )


def _make_revision(work_item_id: int, rev: int, changed_date: datetime) -> Revision:
    return Revision(
        work_item_id=work_item_id,
        rev_number=rev,
        changed_by="user@example.com",
        changed_by_email="user@example.com",
        changed_date=changed_date,
        fields_changed={
            "System.State": ("Proposed", "Active") if rev == 1 else ("Active", "Resolved"),
        },
    )


def _make_items(n: int, as_of: datetime) -> tuple[WorkItem, ...]:
    items = []
    for i in range(1, n + 1):
        revisions = [
            _make_revision(
                work_item_id=i,
                rev=r,
                changed_date=as_of - timedelta(days=n - r),
            )
            for r in range(1, REVISIONS_PER_ITEM + 1)
        ]
        items.append(
            WorkItem(
                id=i,
                type="Feature",
                title=f"Work item {i}",
                state="Active",
                assigned_to="User",
                assigned_to_email="user@example.com",
                area_path="One\\Adventure\\Acme",
                iteration_path="One\\FY26\\Q4",
                target_date=(as_of + timedelta(days=30)).date(),
                risk_level=RiskLevel.MEDIUM,
                tags=[],
                custom_fields={},
                revisions=revisions,
                comments=[],
                fetched_at=as_of,
            )
        )
    return tuple(items)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_perf_baseline_50_item_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Gather 50 items from an in-memory fixture.  Records per-phase elapsed time
    to output/perf_baseline.json and asserts the run completes within 30s.

    fetch_pct reflects processing cost only (no network latency).
    On a real program with live ADO, fetch_pct >= 0.30 justifies Stage 1B.
    """
    as_of = datetime(2026, 5, 18, 18, 0, tzinfo=timezone.utc)
    program = _make_program()
    workstreams = _make_workstreams()
    items = _make_items(ITEM_COUNT, as_of)

    programs_root = tmp_path / "programs"
    monkeypatch.setattr(gather_module, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        gather_module,
        "_load_program_context",
        lambda program_id, programs_root: (program, workstreams),
    )
    monkeypatch.setattr(
        gather_module,
        "_load_freshness_thresholds",
        lambda program_id, programs_root: (14, 30),
    )

    events: list[GatherProgressEvent] = []
    wall_start = perf_counter()

    gather_program(
        "acme",
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda prog, ws, t: (items, ITEM_COUNT),
        freshness_loader=lambda prog, ws, t: (items, ITEM_COUNT),
        progress_callback=events.append,
    )

    total_elapsed = perf_counter() - wall_start
    phase_times = {e.step_name: round(e.elapsed_seconds, 4) for e in events}
    fetch_elapsed = phase_times.get("fetch", 0.0)
    fetch_pct = fetch_elapsed / total_elapsed if total_elapsed > 0 else 0.0

    record = {
        "recorded_at": as_of.isoformat(),
        "item_count": ITEM_COUNT,
        "revisions_per_item": REVISIONS_PER_ITEM,
        "total_elapsed_s": round(total_elapsed, 4),
        "phases": phase_times,
        "fetch_pct": round(fetch_pct, 4),
        "stage_1b_note": (
            "Stage 1B justified (fetch >= 30%)"
            if fetch_pct >= 0.30
            else "Stage 1B not yet justified (fetch < 30% in fixture; measure on real program)"
        ),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(record, indent=2))

    assert total_elapsed < 30.0, f"Fixture gather too slow: {total_elapsed:.1f}s (gate: <30s)"
    assert len(events) >= 4, f"Expected >=4 phase events, got {len(events)}: {list(phase_times)}"
