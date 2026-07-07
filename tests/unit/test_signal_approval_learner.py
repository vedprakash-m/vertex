from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record
from src.core.feedback.signal_approval_learner import load_signal_approval_rule_proposals, refresh_signal_approval_rules


def test_refresh_signal_approval_rules_writes_yaml_and_audit(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_autonomy_audit(programs_root)

    proposals, path = refresh_signal_approval_rules(
        "acme",
        as_of=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )
    loaded = load_signal_approval_rule_proposals("acme", programs_root=programs_root)

    assert path is not None
    assert path.exists()
    assert loaded == proposals
    assert [proposal.action_type for proposal in proposals] == ["decision_ask_escalation", "decision_ask_nudge"]
    assert proposals[0].recommended_level == "L3"
    assert proposals[0].recommended_mode == "batch_approval"
    assert proposals[0].bootstrap is False
    assert proposals[1].recommended_level == "L1"
    assert proposals[1].recommended_mode == "manual_review"
    assert proposals[1].bootstrap is False
    assert proposals[1].acceptance_rate == 0.6667

    audit_path = programs_root / "acme" / "_feedback" / "_audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries[-1]["module"] == "signal_approval_learner"
    assert entries[-1]["file"] == "signal_approval_rules.yaml"


def test_refresh_signal_approval_rules_dry_run_skips_write(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_autonomy_audit(programs_root)

    proposals, path = refresh_signal_approval_rules(
        "acme",
        programs_root=programs_root,
        dry_run=True,
    )

    assert path is None
    assert len(proposals) == 2
    assert not any(programs_root.rglob("signal_approval_rules.yaml"))


def _seed_autonomy_audit(programs_root: Path) -> None:
    for index in range(10):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"escalation-{index}",
                level="l3",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"decision_ask:ask-{index}",),
                policy_rule="decision_ask_escalation",
                accepted=True,
                applied_at=datetime(2026, 5, 1 + index, 8, 0, tzinfo=timezone.utc),
                action_type="decision_ask_escalation",
                blast_radius="1 draft to 2 recipients",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=1.0,
            ),
            programs_root=programs_root,
        )

    for index, accepted in enumerate((True, False, True), start=1):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"nudge-{index}",
                level="l2",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"decision_ask:nudge-{index}",),
                policy_rule="decision_ask_nudge",
                accepted=accepted,
                applied_at=datetime(2026, 5, 15 + index, 9, 0, tzinfo=timezone.utc),
                action_type="decision_ask_nudge",
                blast_radius="1 draft to 1 recipient",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=0.5 if not accepted else 1.0,
            ),
            programs_root=programs_root,
        )