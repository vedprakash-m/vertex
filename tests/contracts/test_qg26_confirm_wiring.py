"""WS-2 QG-26: confirm-gate wire-in contract.

Tests that:
1. QG-26 (`evaluate_external_dependency_gate`) is reachable from
   `evaluate_phase_1b_gates` so a blocking critical dep actually
   surfaces in the confirm gate.
2. With `program_id=None`, the gate is vacuous and the report contains
   the expected n/a message.
3. With a real program + blocking dep, the gate fails and the failure
   message includes the blocking dep's dep_id.
4. The gate is forceable (operator can override with --force).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.external_dependency import ExternalDependency, save_external_dependency
from src.core.models import FreshnessReport
from src.core.quality_gates import evaluate_phase_1b_gates


def _empty_freshness(issue_number: int) -> FreshnessReport:
    return FreshnessReport(issue_number=issue_number, items=(), blocks=0, warns=0, infos=0)


def _seed_dep(programs_root: Path, dep_id: str, **overrides) -> None:
    base = dict(
        dep_id=dep_id,
        team="T",
        tracked_items=(),
        approval_type="ado",
        gates=(),
        canonical_owner_program=None,
        last_seen=datetime(2026, 6, 9, tzinfo=timezone.utc),
        state="unknown",
        is_fulfilled=False,
        criticality="normal",
        resolved_at=None,
        source_ref=None,
    )
    base.update(overrides)
    save_external_dependency(
        "acme",
        ExternalDependency(**base),
        programs_root=programs_root,
    )


def test_phase_1b_contains_qg26_result(tmp_path: Path) -> None:
    """The QG-26 result must appear in every phase_1b evaluation, even
    when no program is set (vacuous n/a path)."""
    freshness = _empty_freshness(issue_number=80)
    report = evaluate_phase_1b_gates(
        freshness_report=freshness,
        items=(),
        publishable_item_ids=(),
        covered_item_ids=(),
        as_of=datetime.now(timezone.utc),
        edition_name="acme_weekly",
        issue_number=80,
        program_id=None,
        programs_root=tmp_path,
    )
    gate_ids = {result.gate_id for result in report.results}
    assert "QG-26" in gate_ids, f"QG-26 missing from phase_1b results: {sorted(gate_ids)}"
    qg26 = next(r for r in report.results if r.gate_id == "QG-26")
    assert qg26.passed is True
    assert qg26.forceable is True


def test_phase_1b_qg26_fails_on_blocking_critical_dep(tmp_path: Path) -> None:
    """A critical (high/blocker) dep that is NOT in a terminal state must
    surface in the phase_1b QG-26 result as a forceable failure."""
    programs_root = tmp_path
    _seed_dep(programs_root, "ext-123", criticality="blocker", state="open")
    freshness = _empty_freshness(issue_number=1)
    report = evaluate_phase_1b_gates(
        freshness_report=freshness,
        items=(),
        publishable_item_ids=(),
        covered_item_ids=(),
        as_of=datetime.now(timezone.utc),
        edition_name="acme_weekly",
        issue_number=1,
        program_id="acme",
        programs_root=programs_root,
    )
    qg26 = next(r for r in report.results if r.gate_id == "QG-26")
    assert qg26.passed is False
    assert "ext-123" in qg26.message
    assert "blocker" in qg26.message
    assert qg26.forceable is True


def test_phase_1b_qg26_passes_on_terminal_dep(tmp_path: Path) -> None:
    """A blocker dep in a terminal state (closed/merged/fulfilled) must NOT
    surface as a failure — the gate is about the structural gap, not the
    past record."""
    programs_root = tmp_path
    _seed_dep(
        programs_root,
        "ext-closed",
        criticality="blocker",
        state="closed",
        is_fulfilled=True,
        resolved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    freshness = _empty_freshness(issue_number=1)
    report = evaluate_phase_1b_gates(
        freshness_report=freshness,
        items=(),
        publishable_item_ids=(),
        covered_item_ids=(),
        as_of=datetime.now(timezone.utc),
        edition_name="acme_weekly",
        issue_number=1,
        program_id="acme",
        programs_root=programs_root,
    )
    qg26 = next(r for r in report.results if r.gate_id == "QG-26")
    assert qg26.passed is True
    assert "passed" in qg26.message.lower()


def test_phase_1b_qg26_passes_when_no_deps_file(tmp_path: Path) -> None:
    """The vacuous path: program exists but never tracked external deps.
    The gate must pass (n/a) so programs in early onboarding aren't
    blocked."""
    programs_root = tmp_path
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)
    freshness = _empty_freshness(issue_number=1)
    report = evaluate_phase_1b_gates(
        freshness_report=freshness,
        items=(),
        publishable_item_ids=(),
        covered_item_ids=(),
        as_of=datetime.now(timezone.utc),
        edition_name="acme_weekly",
        issue_number=1,
        program_id="acme",
        programs_root=programs_root,
    )
    qg26 = next(r for r in report.results if r.gate_id == "QG-26")
    assert qg26.passed is True
    assert "n/a" in qg26.message.lower()
