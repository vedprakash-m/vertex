"""ADF-W2.9: cockpit_builder.py's release-gated read of the shared
program-synthesis contract (Section 8.10.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.cockpit_builder import _build_intelligence_summary, _latest_released_program_synthesis_kwargs
from src.core.program_synthesis import ProgramSynthesis, persist_program_synthesis
from src.core.quality_gates.ai_release_audit import ReleaseTerminal, record_ai_release_decision

_OBSERVED_AT = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _synthesis(ai_run_id: str = "run-1") -> ProgramSynthesis:
    return ProgramSynthesis(
        program_id="xpf",
        ai_run_id=ai_run_id,
        through_line="Program through-line.",
        long_poles=(),
        facts=(),
        inferences=(),
        recommendations=(),
        generated_at=_OBSERVED_AT,
        prompt_version="program_synthesis.v1",
        source_item_count=0,
    )


def test_no_synthesis_yields_all_none_kwargs(tmp_path: Path) -> None:
    kwargs = _latest_released_program_synthesis_kwargs("xpf", programs_root=tmp_path / "programs")
    assert kwargs == {
        "program_synthesis_through_line": None,
        "program_synthesis_generated_at": None,
        "program_synthesis_ai_run_id": None,
    }


def test_released_synthesis_is_surfaced(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    record_ai_release_decision(
        program_id="xpf",
        ai_run_id="run-1",
        terminal=ReleaseTerminal.RELEASED,
        reason="test",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    kwargs = _latest_released_program_synthesis_kwargs("xpf", programs_root=programs_root)
    assert kwargs["program_synthesis_through_line"] == "Program through-line."
    assert kwargs["program_synthesis_ai_run_id"] == "run-1"


def test_unreleased_synthesis_is_never_surfaced(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    kwargs = _latest_released_program_synthesis_kwargs("xpf", programs_root=programs_root)
    assert kwargs["program_synthesis_through_line"] is None


def test_intelligence_summary_carries_released_synthesis_end_to_end(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    record_ai_release_decision(
        program_id="xpf",
        ai_run_id="run-1",
        terminal=ReleaseTerminal.RELEASED,
        reason="test",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    summary, findings = _build_intelligence_summary("xpf", programs_root=programs_root, observed_at=_OBSERVED_AT)
    assert summary.program_synthesis_through_line == "Program through-line."
    assert any(finding.finding_id == "intelligence.synthesis.released" for finding in findings)


def test_intelligence_summary_has_none_synthesis_when_nothing_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    summary, findings = _build_intelligence_summary("xpf", programs_root=programs_root, observed_at=_OBSERVED_AT)
    assert summary.program_synthesis_through_line is None
    assert not any(finding.finding_id == "intelligence.synthesis.released" for finding in findings)
