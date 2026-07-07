"""WS-2 QG-26 tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.external_dependency import ExternalDependency, save_external_dependency
from src.core.quality_gates.external_dependency import (
    evaluate_external_dependency_gate,
)


def _seed_dep(tmp_path: Path, dep_id: str, **overrides) -> None:
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
        programs_root=tmp_path / "programs",
    )


def test_qg26_passes_when_no_dependencies_file(tmp_path: Path) -> None:
    """WS-2: vacuous path — no `external_dependencies.jsonl` => pass (n/a)."""
    report = evaluate_external_dependency_gate(
        program_id="acme",
        programs_root=tmp_path / "programs",
    )
    assert report.passed
    assert report.qg_results["QG-26"] is True
    assert "n/a" in report.results[0].message


def test_qg26_passes_when_no_blocking_deps(tmp_path: Path) -> None:
    _seed_dep(tmp_path, "d1", criticality="normal", state="open")
    _seed_dep(tmp_path, "d2", criticality="high", state="closed")
    _seed_dep(tmp_path, "d3", criticality="high", is_fulfilled=True, state="open")
    report = evaluate_external_dependency_gate(
        program_id="acme",
        programs_root=tmp_path / "programs",
    )
    assert report.passed


def test_qg26_fails_on_critical_open_dep(tmp_path: Path) -> None:
    """WS-2: a critical dep that is open and not fulfilled MUST block."""
    _seed_dep(tmp_path, "d1", criticality="blocker", state="open")
    report = evaluate_external_dependency_gate(
        program_id="acme",
        programs_root=tmp_path / "programs",
    )
    assert not report.passed
    assert "QG-26" in report.qg_results
    assert report.qg_results["QG-26"] is False
    assert report.results[0].forceable is True


def test_qg26_vacuous_when_program_id_is_none(tmp_path: Path) -> None:
    """WS-2: program_id=None => empty report (n/a at the call site)."""
    report = evaluate_external_dependency_gate(
        program_id=None,
        programs_root=tmp_path / "programs",
    )
    assert report.results == ()
