"""ADF-W5.12: the trust cockpit summary built by src/core/cockpit_builder.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import src.core.cockpit_builder as cockpit_builder
from src.core.exceptions import ConfigError
from src.core.maturity_engine import get_earned_autonomy_state_path

from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.cockpit_models import cockpit_snapshot_from_json_dict, cockpit_snapshot_to_json_dict
from src.core.proposal_autonomy_ladder import PROPOSAL_CLASSES, advance_proposal_class_autonomy

_NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def test_trust_summary_present_with_all_classes_at_l0_when_unevaluated(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_NOW)
    assert snapshot.trust_summary is not None
    assert {row.proposal_class for row in snapshot.trust_summary.classes} == set(PROPOSAL_CLASSES)
    assert all(row.level == "l0" for row in snapshot.trust_summary.classes)


def test_unevaluated_state_produces_a_trust_finding(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_NOW)
    trust_findings = [f for f in snapshot.findings if f.area == "trust"]
    assert any(f.finding_id == "trust.autonomy_ladder.not_evaluated" for f in trust_findings)


def test_trust_summary_reflects_persisted_evaluation(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    advance_proposal_class_autonomy("samplealpha", "risk", now=_NOW, programs_root=programs_root)
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_NOW)
    risk_row = next(row for row in snapshot.trust_summary.classes if row.proposal_class == "risk")
    assert risk_row.level == "l1"
    assert risk_row.permitted_action == "Advisory proposal"


def test_demotion_produces_a_warn_finding(tmp_path: Path) -> None:
    from src.core.proposal_autonomy_ladder import demote_proposal_class_explicit, promote_proposal_class_explicit

    programs_root = tmp_path / "programs"
    promote_proposal_class_explicit("samplealpha", "risk", "l1", "seed", now=_NOW, programs_root=programs_root)
    demote_proposal_class_explicit("samplealpha", "risk", "material contradiction observed", now=_NOW, programs_root=programs_root)
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_NOW)
    demotion_findings = [f for f in snapshot.findings if f.finding_id == "trust.risk.demoted"]
    assert len(demotion_findings) == 1
    assert demotion_findings[0].status == "warn"


def test_trust_summary_round_trips_through_json(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    advance_proposal_class_autonomy("samplealpha", "risk", now=_NOW, programs_root=programs_root)
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=programs_root, now=_NOW)
    payload = cockpit_snapshot_to_json_dict(snapshot)
    restored = cockpit_snapshot_from_json_dict(payload)
    assert restored.trust_summary == snapshot.trust_summary


@pytest.mark.parametrize("content", [
    b"proposal_classes: [\n", b"\xff", b"earned_tier: private-marker\n",
    b"last_evaluated_at: 2026-13-01\n",
])
def test_failed_earned_state_is_blocked_not_unevaluated(tmp_path: Path, content: bytes) -> None:
    path = get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir()
    path.write_bytes(content)
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=tmp_path, now=_NOW)
    assert snapshot.trust_summary is not None and snapshot.trust_summary.classes == ()
    finding = next(f for f in snapshot.findings if f.finding_id == "trust.autonomy_ladder.read_failed")
    assert finding.status == "blocked"
    assert "earned_autonomy_state.yaml" in finding.detail and "samplealpha" in finding.detail
    assert "private-marker" not in finding.detail and str(tmp_path) not in finding.detail
    assert not any(f.finding_id == "trust.autonomy_ladder.not_evaluated" for f in snapshot.findings)
    assert cockpit_snapshot_from_json_dict(cockpit_snapshot_to_json_dict(snapshot)).trust_summary == snapshot.trust_summary
    assert path.read_bytes() == content


def test_unexpected_earned_state_failure_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_builder, "load_earned_autonomy_state", Mock(side_effect=RuntimeError("implementation")))
    with pytest.raises(RuntimeError):
        build_cockpit_snapshot("samplealpha", programs_root=tmp_path, now=_NOW)


def test_earned_state_failure_preserves_independent_summaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.cockpit_models import ProgramCockpitSummary, IntelligenceCockpitSummary

    program = ProgramCockpitSummary("red", None, 1, (), None)
    intelligence = IntelligenceCockpitSummary(0.5, None, (), 0)
    monkeypatch.setattr(cockpit_builder, "_build_program_summary", lambda *a, **k: (program, []))
    monkeypatch.setattr(cockpit_builder, "_build_intelligence_summary", lambda *a, **k: (intelligence, []))
    monkeypatch.setattr(cockpit_builder, "load_earned_autonomy_state", Mock(side_effect=ConfigError("safe")))
    snapshot = build_cockpit_snapshot("samplealpha", programs_root=tmp_path, now=_NOW)
    assert snapshot.program_summary.overall_risk == "red" and snapshot.program_summary.blocker_count == 1
    assert snapshot.intelligence_summary is intelligence
    assert snapshot.trust_summary.classes == ()


def test_real_earned_state_failure_preserves_high_risk_and_released_synthesis(tmp_path: Path) -> None:
    from dataclasses import replace
    from src.core.quality_gates.ai_release_audit import ReleaseTerminal, record_ai_release_decision
    from src.core.program_synthesis import persist_program_synthesis
    from src.core.risk_register_engine import save_risk_register
    from tests.unit.test_cockpit_program_synthesis import _synthesis
    from tests.unit.test_cockpit_view_models import _risk_entry

    save_risk_register("samplealpha", (replace(_risk_entry("known-high"), program_id="samplealpha"),), programs_root=tmp_path)
    persist_program_synthesis(_synthesis(), programs_root=tmp_path)
    record_ai_release_decision(
        program_id="samplealpha", ai_run_id="run-1", terminal=ReleaseTerminal.RELEASED,
        reason="synthetic release", validator_finding_count=0, programs_root=tmp_path,
    )
    before = build_cockpit_snapshot("samplealpha", programs_root=tmp_path, now=_NOW)
    assert before.program_summary.overall_risk == "red"
    assert before.intelligence_summary.program_synthesis_through_line == "Program through-line."
    path = get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.write_text("proposal_classes: [\n", encoding="utf-8")
    after = build_cockpit_snapshot("samplealpha", programs_root=tmp_path, now=_NOW)
    assert after.program_summary.overall_risk == before.program_summary.overall_risk
    assert after.program_summary.blocker_count == before.program_summary.blocker_count
    assert after.intelligence_summary == before.intelligence_summary
    assert after.trust_summary.classes == ()
