"""FR-19: offline 30-50 program synthetic fleet envelope benchmark (RELO-W0.7a perf lane)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
import tracemalloc

import pytest

from src.commands.fleet import (
    build_fleet_report,
    render_fleet_csv,
    render_fleet_html,
    render_fleet_markdown,
    render_fleet_report,
)
from src.core.activation_fleet import FleetProgramSample, FleetSoakBudget, evaluate_fleet_soak
from tests.unit.test_commands_fleet import _write_program

pytestmark = pytest.mark.perf

FLEET_PROGRAM_COUNT = 40
FROZEN_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _seed_synthetic_fleet(programs_root: Path, count: int) -> None:
    for idx in range(count):
        pid = f"fleet_prog_{idx:02d}"
        edition = f"{pid}_weekly"
        risk = "low" if idx % 3 else "medium"
        _write_program(
            programs_root / pid,
            name=f"Fleet Program {idx:02d}",
            current_phase="Execution",
            archive_payloads={
                edition: {
                    "latest_issue": 5,
                    "generated_at": FROZEN_NOW.isoformat(),
                    "scorecards": [
                        {"issue_number": 4, "scorecard_name": "Health", "dimension": "Delivery", "risk": "low"},
                        {"issue_number": 5, "scorecard_name": "Health", "dimension": "Delivery", "risk": risk},
                    ],
                    "top_items": (),
                }
            },
            dependencies_text=None,
        )


def test_fr19_forty_program_synthetic_fleet_envelope(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_synthetic_fleet(programs_root, FLEET_PROGRAM_COUNT)

    tracemalloc.start()
    started = perf_counter()
    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)
    text_out = render_fleet_report(report)
    md_out = render_fleet_markdown(report)
    csv_out = render_fleet_csv(report)
    html_out = render_fleet_html(report)
    elapsed_s = perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(report.programs) == FLEET_PROGRAM_COUNT
    assert report.stale_program_count == 0
    assert f"{FLEET_PROGRAM_COUNT} programs" in text_out
    assert f"Fleet Program {FLEET_PROGRAM_COUNT - 1:02d}" in md_out
    assert len(csv_out.splitlines()) >= FLEET_PROGRAM_COUNT + 1
    assert "Fleet Program 00" in html_out
    assert elapsed_s < 30.0, f"40-program fleet envelope exceeded 30s ceiling: {elapsed_s:.2f}s"

    peak_mb_per_program = (peak_bytes / (1024 * 1024)) / FLEET_PROGRAM_COUNT
    samples = tuple(
        FleetProgramSample(
            program_id=program.program_id,
            rendered_from_program_reality=True,
            quiet_lane=(idx % 4 == 0),
            isolation_violations=0,
            growth_mb_this_cycle=round(peak_mb_per_program, 4),
            cost_usd_this_cycle=0.0,
            observed_concurrency=4,
        )
        for idx, program in enumerate(report.programs)
    )
    verdict = evaluate_fleet_soak(
        samples,
        budget=FleetSoakBudget(min_programs=30, growth_mb_per_program=25.0, cost_usd_per_program=5.0, fleet_concurrency_cap=12),
    )
    assert verdict.passed is True, verdict.reasons
    assert verdict.program_count == FLEET_PROGRAM_COUNT
