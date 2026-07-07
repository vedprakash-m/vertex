from __future__ import annotations

from dataclasses import asdict

from scripts.audit_consolidated_decision_gates import audit_decision_gates, main, render_markdown_packet
from src.core.consolidated_gate_approval import load_consolidated_gate_approval_status
from src.core.config_loader import PROGRAMS_ROOT


def test_decision_gate_audit_covers_all_human_decision_rows() -> None:
    findings = audit_decision_gates(program_id="nova", programs_root=PROGRAMS_ROOT)

    assert {finding.gate for finding in findings} == {
        "S-0c",
        "S-0d/PS-J",
        "S-0f",
        "S-0g",
        "S-0j",
        "S-0k",
        "S-NC-apply",
        "Q7",
    }


def test_decision_gate_audit_reports_executable_artifact_statuses() -> None:
    findings = {
        finding.gate: finding
        for finding in audit_decision_gates(program_id="nova", programs_root=PROGRAMS_ROOT)
    }

    assert findings["S-0c"].status == "executable_recommendation_pending_acceptance"
    assert findings["S-0d/PS-J"].status == "executable_recommendation_pending_acceptance"
    assert findings["S-0f"].status == "executable_recommendation_pending_acceptance"
    assert findings["S-0g"].status == "executable_recommendation_pending_acceptance"
    assert findings["S-NC-apply"].status == "executable_recommendation_pending_acceptance"
    assert findings["Q7"].status == "blocked_on_corpus"


def test_decision_gate_audit_reports_s0j_and_s0k_evidence() -> None:
    findings = {
        finding.gate: finding
        for finding in audit_decision_gates(program_id="nova", programs_root=PROGRAMS_ROOT)
    }

    assert findings["S-0j"].evidence["risk_entry_family"] == "judgment"
    assert findings["S-0k"].evidence["defaults"] == {
        "clean_cycles_to_flip": 5,
        "divergence_tolerance": 0.02,
        "critical_zero": True,
        "max_persistent_cycles": 8,
        "require_s0g_policy": False,
    }


def test_decision_gate_audit_markdown_packet_is_human_approval_ready() -> None:
    findings = audit_decision_gates(program_id="nova", programs_root=PROGRAMS_ROOT)

    packet = render_markdown_packet(program_id="nova", findings=findings)

    assert "# Consolidated Decision-Gate Packet: nova" in packet
    assert "Status: recommendation packet only" in packet
    assert "## Approval Record" in packet
    # ADR-0006 is now Accepted
    assert "- ADR status: `Accepted`" in packet
    assert "- Accepted: `True`" in packet
    assert "### S-0g" in packet
    assert "`risk.blocking_milestone`: `recommended_unsupported_v1`" in packet
    assert "- [ ] S-9 corpus is collected and Q7 is rerun before production LLM extraction." in packet


def test_decision_gate_audit_json_payload_can_include_machine_readable_approval_record() -> None:
    status = load_consolidated_gate_approval_status()
    approval_payload = asdict(status)

    # ADR-0006 is now Accepted
    assert approval_payload["adr_status"] == "Accepted"
    assert approval_payload["record_exists"] is True
    assert status.accepted is True  # .accepted is a @property, not in asdict
    assert approval_payload["blocking_reasons"] == ()  # tuple from frozen dataclass


def test_decision_gate_audit_require_accepted_passes_now_that_adr_is_accepted() -> None:
    assert main(["--program", "nova", "--json", "--require-accepted"]) == 0
