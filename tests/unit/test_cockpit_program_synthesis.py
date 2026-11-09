"""ADF-W2.9: cockpit_builder.py's release-gated read of the shared
program-synthesis contract (Section 8.10.5)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.cockpit_builder import _build_intelligence_summary, _latest_released_program_synthesis_kwargs, build_cockpit_snapshot
from src.core.exceptions import ConfigError
from src.core.ledger.event_log import get_event_logs_dir
from src.core.program_synthesis import ProgramSynthesis, persist_program_synthesis
from src.core.quality_gates.ai_release_audit import ReleaseTerminal, record_ai_release_decision

_OBSERVED_AT = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _synthesis(ai_run_id: str = "run-1") -> ProgramSynthesis:
    return ProgramSynthesis(
        program_id="samplealpha",
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
    kwargs = _latest_released_program_synthesis_kwargs("samplealpha", programs_root=tmp_path / "programs")
    assert kwargs == {
        "program_synthesis_through_line": None,
        "program_synthesis_generated_at": None,
        "program_synthesis_ai_run_id": None,
    }


def test_released_synthesis_is_surfaced(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    record_ai_release_decision(
        program_id="samplealpha",
        ai_run_id="run-1",
        terminal=ReleaseTerminal.RELEASED,
        reason="test",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    kwargs = _latest_released_program_synthesis_kwargs("samplealpha", programs_root=programs_root)
    assert kwargs["program_synthesis_through_line"] == "Program through-line."
    assert kwargs["program_synthesis_ai_run_id"] == "run-1"


def test_unreleased_synthesis_is_never_surfaced(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    kwargs = _latest_released_program_synthesis_kwargs("samplealpha", programs_root=programs_root)
    assert kwargs["program_synthesis_through_line"] is None


def test_intelligence_summary_carries_released_synthesis_end_to_end(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    record_ai_release_decision(
        program_id="samplealpha",
        ai_run_id="run-1",
        terminal=ReleaseTerminal.RELEASED,
        reason="test",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    summary, findings = _build_intelligence_summary("samplealpha", programs_root=programs_root, observed_at=_OBSERVED_AT)
    assert summary.program_synthesis_through_line == "Program through-line."
    assert any(finding.finding_id == "intelligence.synthesis.released" for finding in findings)


def test_intelligence_summary_has_none_synthesis_when_nothing_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    summary, findings = _build_intelligence_summary("samplealpha", programs_root=programs_root, observed_at=_OBSERVED_AT)
    assert summary.program_synthesis_through_line is None
    assert not any(finding.finding_id == "intelligence.synthesis.released" for finding in findings)


@pytest.mark.parametrize("failure", ["invalid", "unavailable"])
def test_fresh_cockpit_retained_synthesis_failure_is_not_successful_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    programs_root = tmp_path / "programs"
    path = persist_program_synthesis(_synthesis(), programs_root=programs_root)
    assert path is not None
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="run-1", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=programs_root,
    )
    if failure == "invalid":
        path.write_text("null", encoding="utf-8")
    else:
        read_text = Path.read_text

        def fail_read(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("private candidate path")
            return read_text(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_read)
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_OBSERVED_AT)
    assert snapshot.intelligence_summary.program_synthesis_through_line is None
    assert snapshot.intelligence_summary.program_synthesis_generated_at is None
    assert snapshot.intelligence_summary.program_synthesis_ai_run_id is None
    finding = next(f for f in snapshot.findings if f.finding_id == "intelligence.synthesis.read_failed")
    assert finding.status == "blocked" and finding.area == "intelligence"
    assert "ConfigError" in finding.detail and "retry" in finding.detail
    assert "samplealpha" in finding.detail and "Inspect" in finding.detail
    assert "private" not in finding.detail and str(tmp_path) not in finding.detail
    assert finding.observed_at == _OBSERVED_AT and finding.evidence_refs == ()
    assert finding.next_command == "vertex cockpit explain --program samplealpha --finding intelligence.synthesis.read_failed"
    assert any(f.finding_id == "intelligence.lineage.no_facts" for f in snapshot.findings)
    assert not any(f.finding_id == "intelligence.synthesis.released" for f in snapshot.findings)


def test_synthesis_consumer_implementation_failure_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError("unexpected retained reader error")
    monkeypatch.setattr("src.core.cockpit_builder.load_latest_released_program_synthesis", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:
        build_cockpit_snapshot("samplealpha", programs_root=tmp_path, now=_OBSERVED_AT)
    assert raised.value is error


def test_cockpit_rejects_foreign_synthesis_without_content_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner
    from src.commands import cockpit as cockpit_command
    from src.core.cockpit_html import render_cockpit_html
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.risk_register_engine import save_risk_register
    from tests.unit.test_cockpit_view_models import _risk_entry
    from tests.unit.test_program_synthesis import _persist_foreign_synthesis

    programs_root = tmp_path / "programs"
    program = "fixture_prog"
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    save_fact_sor_state(program, mode="legacy", recorded_at=now, programs_root=programs_root)
    save_risk_register(program, (_risk_entry("known-high"),), programs_root=programs_root)
    _persist_foreign_synthesis(programs_root, program_id=program)
    before = {p.relative_to(programs_root): p.read_bytes() if p.is_file() else None for p in programs_root.rglob("*")}

    snapshot = build_cockpit_snapshot(program, programs_root=programs_root, now=now)
    finding = next(f for f in snapshot.findings if f.finding_id == "intelligence.synthesis.read_failed")
    assert snapshot.intelligence_summary.program_synthesis_through_line is None
    assert finding.status == "blocked" and finding.evidence_refs == ()
    assert snapshot.program_summary.overall_risk == "red" and snapshot.program_summary.blocker_count == 1
    assert "ConfigError" in finding.detail and "SYNTHETIC-FOREIGN" not in finding.detail
    html = render_cockpit_html(snapshot, retained=True)
    terminal = cockpit_command.render_cockpit_terminal(snapshot)
    assert "Program synthesis unavailable" in html and "synthesis unavailable (read failed)" in terminal
    assert "SYNTHETIC-FOREIGN" not in html + terminal

    monkeypatch.setattr(cockpit_command, "PROGRAMS_ROOT", programs_root)
    result = CliRunner().invoke(
        cockpit_command.app, ["show", "--program", program, "--format", "json", "--no-persist"],
    )
    assert result.exit_code == 0, repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["intelligence_summary"]["program_synthesis_through_line"] is None
    assert payload["program_summary"]["overall_risk"] == "red"
    assert any(f["finding_id"] == "intelligence.synthesis.read_failed" and f["status"] == "blocked" for f in payload["findings"])
    assert "SYNTHETIC-FOREIGN" not in result.stdout and str(tmp_path) not in result.stdout
    explain = CliRunner().invoke(
        cockpit_command.app, ["explain", "--program", program, "--finding", "intelligence.synthesis.read_failed"],
    )
    assert explain.exit_code == 0 and "Program synthesis unavailable" in explain.stdout
    assert "SYNTHETIC-FOREIGN" not in explain.stdout and str(tmp_path) not in explain.stdout
    assert {p.relative_to(programs_root): p.read_bytes() if p.is_file() else None for p in programs_root.rglob("*")} == before


def test_cockpit_reports_incomparable_released_synthesis_without_content_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner
    from src.commands import cockpit as cockpit_command
    from src.core.cockpit_html import render_cockpit_html
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.risk_register_engine import save_risk_register
    from tests.unit.test_cockpit_view_models import _risk_entry
    from tests.unit.test_program_synthesis import _persist_timestamp_pair

    programs_root = tmp_path / "programs"
    program = "fixture_prog"
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    save_fact_sor_state(program, mode="legacy", recorded_at=now, programs_root=programs_root)
    save_risk_register(program, (_risk_entry("known-high"),), programs_root=programs_root)
    _persist_timestamp_pair(programs_root, program_id=program)
    before = {p.relative_to(programs_root): p.read_bytes() if p.is_file() else None for p in programs_root.rglob("*")}

    snapshot = build_cockpit_snapshot(program, programs_root=programs_root, now=now)
    finding = next(f for f in snapshot.findings if f.finding_id == "intelligence.synthesis.read_failed")
    assert snapshot.intelligence_summary.program_synthesis_through_line is None
    assert finding.status == "blocked" and finding.evidence_refs == ()
    assert snapshot.program_summary.overall_risk == "red" and snapshot.program_summary.blocker_count == 1
    assert "ConfigError" in finding.detail and "SYNTHETIC-CANARY" not in finding.detail
    html = render_cockpit_html(snapshot, retained=True)
    terminal = cockpit_command.render_cockpit_terminal(snapshot)
    assert "Program synthesis unavailable" in html and "synthesis unavailable (read failed)" in terminal
    assert "SYNTHETIC-CANARY" not in html + terminal

    monkeypatch.setattr(cockpit_command, "PROGRAMS_ROOT", programs_root)
    result = CliRunner().invoke(
        cockpit_command.app, ["show", "--program", program, "--format", "json", "--no-persist"],
    )
    assert result.exit_code == 0, repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["intelligence_summary"]["program_synthesis_through_line"] is None
    assert payload["program_summary"]["overall_risk"] == "red"
    assert any(f["finding_id"] == "intelligence.synthesis.read_failed" and f["status"] == "blocked" for f in payload["findings"])
    assert "SYNTHETIC-CANARY" not in result.stdout and str(tmp_path) not in result.stdout
    explain = CliRunner().invoke(
        cockpit_command.app, ["explain", "--program", program, "--finding", "intelligence.synthesis.read_failed"],
    )
    assert explain.exit_code == 0 and "Program synthesis unavailable" in explain.stdout
    assert "SYNTHETIC-CANARY" not in explain.stdout and str(tmp_path) not in explain.stdout
    assert {p.relative_to(programs_root): p.read_bytes() if p.is_file() else None for p in programs_root.rglob("*")} == before


@pytest.mark.parametrize("damage", ["envelope", "payload"])
def test_cockpit_rejects_damaged_synthesis_release_ledger_without_repair(tmp_path: Path, damage: str) -> None:
    programs_root = tmp_path / "programs"
    persist_program_synthesis(_synthesis(), programs_root=programs_root)
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="run-1", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=programs_root,
    )
    event_dir = get_event_logs_dir("samplealpha", programs_root=programs_root)
    path = next(event_dir.glob("*.events.jsonl"))
    if damage == "envelope":
        path.write_bytes(path.read_bytes() + b'{"broken":')
    else:
        row = json.loads(path.read_text(encoding="utf-8"))
        del row["payload"]["validator_finding_count"]
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = {p.relative_to(event_dir): p.read_bytes() if p.is_file() else None for p in event_dir.rglob("*")}

    summary, findings = _build_intelligence_summary("samplealpha", programs_root=programs_root, observed_at=_OBSERVED_AT)
    assert summary.program_synthesis_through_line is None
    assert any(f.finding_id == "intelligence.synthesis.read_failed" and f.status == "blocked" for f in findings)
    assert not any(f.finding_id == "intelligence.synthesis.released" for f in findings)
    if damage == "envelope":
        with pytest.raises(ConfigError):
            build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_OBSERVED_AT)
    else:
        snapshot = build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_OBSERVED_AT)
        assert snapshot.intelligence_summary.program_synthesis_through_line is None
        assert any(f.finding_id == "intelligence.synthesis.read_failed" and f.status == "blocked" for f in snapshot.findings)
    assert {p.relative_to(event_dir): p.read_bytes() if p.is_file() else None for p in event_dir.rglob("*")} == before
