"""ADF-W5.12: the trust cockpit summary built by src/core/cockpit_builder.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.cockpit_models import cockpit_snapshot_from_json_dict, cockpit_snapshot_to_json_dict
from src.core.proposal_autonomy_ladder import PROPOSAL_CLASSES, advance_proposal_class_autonomy

_NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def test_trust_summary_present_with_all_classes_at_l0_when_unevaluated(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("xpf", programs_root=programs_root, now=_NOW)
    assert snapshot.trust_summary is not None
    assert {row.proposal_class for row in snapshot.trust_summary.classes} == set(PROPOSAL_CLASSES)
    assert all(row.level == "l0" for row in snapshot.trust_summary.classes)


def test_unevaluated_state_produces_a_trust_finding(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("xpf", programs_root=programs_root, now=_NOW)
    trust_findings = [f for f in snapshot.findings if f.area == "trust"]
    assert any(f.finding_id == "trust.autonomy_ladder.not_evaluated" for f in trust_findings)


def test_trust_summary_reflects_persisted_evaluation(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    advance_proposal_class_autonomy("xpf", "risk", now=_NOW, programs_root=programs_root)
    snapshot = build_cockpit_snapshot("xpf", programs_root=programs_root, now=_NOW)
    risk_row = next(row for row in snapshot.trust_summary.classes if row.proposal_class == "risk")
    assert risk_row.level == "l1"
    assert risk_row.permitted_action == "Advisory proposal"


def test_demotion_produces_a_warn_finding(tmp_path: Path) -> None:
    from src.core.proposal_autonomy_ladder import demote_proposal_class_explicit, promote_proposal_class_explicit

    programs_root = tmp_path / "programs"
    promote_proposal_class_explicit("xpf", "risk", "l1", "seed", now=_NOW, programs_root=programs_root)
    demote_proposal_class_explicit("xpf", "risk", "material contradiction observed", now=_NOW, programs_root=programs_root)
    snapshot = build_cockpit_snapshot("xpf", programs_root=programs_root, now=_NOW)
    demotion_findings = [f for f in snapshot.findings if f.finding_id == "trust.risk.demoted"]
    assert len(demotion_findings) == 1
    assert demotion_findings[0].status == "warn"


def test_trust_summary_round_trips_through_json(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    advance_proposal_class_autonomy("xpf", "risk", now=_NOW, programs_root=programs_root)
    snapshot = build_cockpit_snapshot("xpf", programs_root=programs_root, now=_NOW)
    payload = cockpit_snapshot_to_json_dict(snapshot)
    restored = cockpit_snapshot_from_json_dict(payload)
    assert restored.trust_summary == snapshot.trust_summary
