#!/usr/bin/env python3
"""Audit remaining ADR-0006 human-decision gates (originally specs/consolidated.md §33.3, now folded into the core specs; the consolidated doc is archived at .archive/specs/consolidated.md, local-only).

This script is intentionally diagnostic: it does not approve policy, unblock
mutation, or flip authority.  It gathers the executable evidence behind the
§33.3.1 LLM-judge recommendations so Product/Governance can make decisions
from a reproducible report.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.acceptance_truth_policy import recommended_acceptance_truth_decisions  # noqa: E402
from src.core.config_loader import PROGRAMS_ROOT  # noqa: E402
from src.core.consolidated_gate_approval import load_consolidated_gate_approval_status  # noqa: E402
from src.core.consolidated_scope_policy import recommended_s0c_scope_decision  # noqa: E402
from src.core.ncfl_apply_policy import recommended_ncfl_apply_durability_decision  # noqa: E402
from src.core.ncfl_store_policy import (  # noqa: E402
    audit_plane1_store_policy,
    ncfl_apply_writable_target_stores,
)
from src.core.rev.authority_scope import assess_rev_authority_scope, recommended_v1_authoritative_claim_types  # noqa: E402
from src.core.rev.quality_metrics import compute_quality_report  # noqa: E402
from src.core.truth_model import load_source_authority_policy  # noqa: E402


@dataclass(frozen=True, slots=True)
class DecisionGateFinding:
    gate: str
    status: str
    evidence: dict[str, Any]
    recommendation: str
    next_action: str


def audit_decision_gates(*, program_id: str, programs_root: Path) -> tuple[DecisionGateFinding, ...]:
    policy = load_source_authority_policy()
    program_root = programs_root / program_id
    ncfl_failures = audit_plane1_store_policy(program_root)
    quality_report = compute_quality_report(program_id=program_id, programs_root=programs_root)

    workitem_secondary = tuple(policy.authority["workitem.state"].secondary)
    commitment_secondary = tuple(policy.authority["commitment"].secondary)
    judgment_secondary = tuple(policy.authority["judgment"].secondary)
    rev_sources = {
        source: policy.provenance_classes.get(source)
        for source in ("workiq", "teams", "transcript")
    }
    authority_scope = assess_rev_authority_scope(policy)
    ncfl_apply_decision = recommended_ncfl_apply_durability_decision()
    ncfl_apply_evidence = asdict(ncfl_apply_decision)
    ncfl_apply_evidence["terminal_states"] = sorted(ncfl_apply_decision.terminal_states)
    s0c_scope_decision = recommended_s0c_scope_decision()
    acceptance_truth_decisions = recommended_acceptance_truth_decisions()

    return (
        DecisionGateFinding(
            gate="S-0c",
            status="executable_recommendation_pending_acceptance",
            evidence=asdict(s0c_scope_decision),
            recommendation=(
                "Accept pilot-local, remove deliverable/incident from v1 authority, "
                "and narrow automation to automatic-after-deposit."
            ),
            next_action="Human Product/Governance approval; then remove pending wording from the spec.",
        ),
        DecisionGateFinding(
            gate="S-0d/PS-J",
            status="executable_recommendation_pending_acceptance",
            evidence={
                "truth_decisions": [asdict(decision) for decision in acceptance_truth_decisions],
            },
            recommendation="Accept the §33.3.1 truth table; acceptance is a review-state/write-authority transition, not a second fact.",
            next_action="Human approval; then treat the table as normative for acceptance code reviews.",
        ),
        DecisionGateFinding(
            gate="S-0f",
            status="executable_recommendation_pending_acceptance",
            evidence={
                "program_root": str(program_root),
                "audit_failures": ncfl_failures,
                "apply_writable_target_stores": sorted(ncfl_apply_writable_target_stores()),
            },
            recommendation="Accept the conservative NCFL apply-writable subset and keep knowledge_doc/dependencies blocked for v1 apply.",
            next_action="Human approval; only then implement ncfl_apply against the audited writable subset.",
        ),
        DecisionGateFinding(
            gate="S-0g",
            status="executable_recommendation_pending_acceptance",
            evidence={
                "rev_source_provenance": rev_sources,
                "workitem_state_secondary": workitem_secondary,
                "commitment_secondary": commitment_secondary,
                "judgment_secondary": judgment_secondary,
                "human_comms_in_workitem_state": "human_comms" in workitem_secondary,
                "human_comms_in_commitment": "human_comms" in commitment_secondary,
                "human_comms_in_judgment": "human_comms" in judgment_secondary,
                "recommended_v1_authoritative_claim_types": sorted(recommended_v1_authoritative_claim_types(policy)),
                "event_scope": [asdict(assessment) for assessment in authority_scope],
            },
            recommendation=(
                "Admit accepted REV human_comms for workitem.state and commitment after clean-cycle gates; "
                "do not add human_comms to judgment in v1; final v1 authoritative count becomes 4."
            ),
            next_action="Human Product/Governance approval; then update policy/tests and unblock S-5/S-8b for the approved families only.",
        ),
        DecisionGateFinding(
            gate="S-0j",
            status="satisfied_unless_governance_reverses",
            evidence={
                "source_of_truth": "vertex/policies/source_authority.yaml",
                "risk_entry_family": policy.family_map.get("risk.entry"),
                "decision_entry_family": policy.family_map.get("decision.entry"),
                "assumption_entry_family": policy.family_map.get("assumption.entry"),
                "workstream_entry_family": policy.family_map.get("workstream.entry"),
            },
            recommendation="Keep source_authority.yaml as the single source of truth for fact-type-to-authority-family mapping.",
            next_action="No implementation action unless Governance changes the source-of-truth decision.",
        ),
        DecisionGateFinding(
            gate="S-0k",
            status="mechanically_implemented_values_recommended",
            evidence={
                "defaults": asdict(policy.sor_flip.defaults),
                "per_family": {
                    family: asdict(config)
                    for family, config in sorted(policy.sor_flip.per_family.items())
                },
            },
            recommendation="Keep the validated sor_flip defaults and per-family overrides as the baseline unless Governance changes thresholds.",
            next_action="Human approval only if Product/Governance wants threshold changes; otherwise implementation can rely on the loaded schema.",
        ),
        DecisionGateFinding(
            gate="S-NC-apply",
            status="executable_recommendation_pending_acceptance",
            evidence={
                **ncfl_apply_evidence,
                "beta_outbox_exists": True,
                "requires_yaml_changelog_recovery": True,
            },
            recommendation="Reuse beta outbox for ledger/idempotency and add a minimal NCFL apply journal for YAML/changelog recovery.",
            next_action="Human Eng approval; then implement recoverable ncfl_apply state machine.",
        ),
        DecisionGateFinding(
            gate="Q7",
            status="blocked_on_corpus",
            evidence={
                "quality_report": quality_report.to_dict(),
            },
            recommendation="Keep deterministic extractor as production default; run LLM only in shadow/assist until S-9 corpus gates pass.",
            next_action="Collect/freeze S-9 corpus, dual-label it, then rerun rev_quality_check and judge comparison.",
        ),
    )


def render_markdown_packet(*, program_id: str, findings: tuple[DecisionGateFinding, ...]) -> str:
    approval = load_consolidated_gate_approval_status()
    lines = [
        f"# Consolidated Decision-Gate Packet: {program_id}",
        "",
        "Status: recommendation packet only; no gate is approved by this generated report.",
        "",
        "## Approval Record",
        "",
        f"- Path: `{approval.decision_record_path}`",
        f"- ADR status: `{approval.adr_status}`",
        f"- Approvers: `{approval.approvers}`",
        f"- Accepted: `{approval.accepted}`",
        "- Blocking reasons: "
        + (
            ", ".join(f"`{reason}`" for reason in approval.blocking_reasons)
            if approval.blocking_reasons
            else "`none`"
        ),
        "",
        "## Recommended Decisions",
        "",
    ]
    for finding in findings:
        lines.extend([
            f"### {finding.gate}",
            "",
            f"- Status: `{finding.status}`",
            f"- Recommendation: {finding.recommendation}",
            f"- Next action: {finding.next_action}",
        ])
        if finding.gate == "S-0c":
            lines.extend([
                f"- Security profile: `{finding.evidence.get('security_profile')}`",
                f"- Automation scope: `{finding.evidence.get('automation_scope')}`",
                "- Unsupported v1 authority domains: "
                + ", ".join(f"`{value}`" for value in finding.evidence.get("unsupported_v1_authority_domains", ())),
            ])
        elif finding.gate == "S-0d/PS-J":
            lines.append("- Truth table:")
            for decision in finding.evidence.get("truth_decisions", ()):
                lines.append(
                    "  - "
                    f"`{decision['confidence_tier']}` -> `{decision['precedence']}` / "
                    f"`{decision['review_state']}` / `{decision['write_authority']}`"
                )
        elif finding.gate == "S-0f":
            lines.append(
                "- Apply-writable target stores: "
                + ", ".join(f"`{store}`" for store in finding.evidence.get("apply_writable_target_stores", ()))
            )
            lines.append(f"- Inventory failures: `{len(finding.evidence.get('audit_failures', ()))}`")
        elif finding.gate == "S-0g":
            lines.append(
                "- Recommended v1-authoritative claim types: "
                + ", ".join(
                    f"`{claim}`"
                    for claim in finding.evidence.get("recommended_v1_authoritative_claim_types", ())
                )
            )
            lines.append("- Event scope:")
            for item in finding.evidence.get("event_scope", ()):
                lines.append(
                    "  - "
                    f"`{item['claim_event_type']}`: `{item['status']}` "
                    f"({item.get('authority_family') or 'no-family'})"
                )
        elif finding.gate == "S-0j":
            lines.extend([
                f"- Source of truth: `{finding.evidence.get('source_of_truth')}`",
                f"- `risk.entry` authority family: `{finding.evidence.get('risk_entry_family')}`",
            ])
        elif finding.gate == "S-0k":
            defaults = finding.evidence.get("defaults", {})
            lines.extend([
                f"- `clean_cycles_to_flip`: `{defaults.get('clean_cycles_to_flip')}`",
                f"- `divergence_tolerance`: `{defaults.get('divergence_tolerance')}`",
                f"- `critical_zero`: `{defaults.get('critical_zero')}`",
                f"- `max_persistent_cycles`: `{defaults.get('max_persistent_cycles')}`",
            ])
        elif finding.gate == "S-NC-apply":
            lines.extend([
                f"- Strategy: `{finding.evidence.get('strategy')}`",
                f"- Requires apply journal: `{finding.evidence.get('requires_apply_journal')}`",
                "- States: " + ", ".join(f"`{state}`" for state in finding.evidence.get("states", ())),
            ])
        elif finding.gate == "Q7":
            quality = finding.evidence.get("quality_report", {})
            lines.extend([
                f"- Labeled corpus rows: `{quality.get('n_total')}`",
                f"- Kappa: `{quality.get('kappa')}`",
            ])
            failures = quality.get("failures", ())
            if failures:
                lines.append(f"- Blocking failure: {failures[0]}")
        lines.append("")
    lines.extend([
        "## Human Approval Checklist",
        "",
        "- [ ] Product/Governance records S-0c decision.",
        "- [ ] Product/Governance records S-0d/PS-J decision.",
        "- [ ] Product/Governance records S-0f decision before NCFL apply.",
        "- [ ] Product/Governance records S-0g decision before any SoR flip.",
        "- [ ] Eng records S-NC-apply decision before NCFL apply.",
        "- [ ] S-9 corpus is collected and Q7 is rerun before production LLM extraction.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit remaining consolidated.md decision gates.")
    parser.add_argument("--program", default="nova", help="Program id to use for corpus and YAML inventory checks.")
    parser.add_argument("--programs-root", default=str(PROGRAMS_ROOT), help="Programs root directory.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a human report.")
    parser.add_argument("--markdown", action="store_true", help="Emit a Markdown decision packet.")
    parser.add_argument(
        "--require-accepted",
        action="store_true",
        help="Exit non-zero unless the tracked human approval ADR is accepted.",
    )
    args = parser.parse_args(argv)

    if args.json and args.markdown:
        parser.error("--json and --markdown are mutually exclusive")

    findings = audit_decision_gates(program_id=args.program, programs_root=Path(args.programs_root))
    approval = load_consolidated_gate_approval_status()
    payload = {
        "program_id": args.program,
        "approval_record": asdict(approval),
        "findings": [asdict(finding) for finding in findings],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.markdown:
        print(render_markdown_packet(program_id=args.program, findings=findings))
    else:
        print(f"Decision-gate audit for {args.program}")
        print(f"Approval record: {approval.decision_record_path}")
        print(f"Approval status: {approval.adr_status or 'missing'}")
        print(f"Approvers: {approval.approvers or 'missing'}")
        if approval.accepted:
            print("Gate approval: accepted")
        else:
            print("Gate approval: blocked")
            for reason in approval.blocking_reasons:
                print(f"  blocker: {reason}")
        for finding in findings:
            print(f"- {finding.gate}: {finding.status}")
            if finding.gate == "S-0c":
                print(f"  security_profile: {finding.evidence.get('security_profile')}")
                print(f"  automation_scope: {finding.evidence.get('automation_scope')}")
                unsupported = finding.evidence.get("unsupported_v1_authority_domains", ())
                print(f"  unsupported_v1_authority_domains: {', '.join(unsupported)}")
            if finding.gate == "S-0d/PS-J":
                for decision in finding.evidence.get("truth_decisions", ()):
                    print(
                        "  "
                        f"{decision['confidence_tier']}: {decision['precedence']} / "
                        f"{decision['review_state']} / {decision['write_authority']}"
                    )
            if finding.gate == "S-0f":
                stores = finding.evidence.get("apply_writable_target_stores", ())
                failures = finding.evidence.get("audit_failures", ())
                print(f"  apply_writable_target_stores: {', '.join(stores)}")
                print(f"  inventory_failures: {len(failures)}")
            if finding.gate == "S-0g":
                supported = finding.evidence.get("recommended_v1_authoritative_claim_types", ())
                print(f"  recommended_v1_authoritative_claim_types: {', '.join(supported)}")
                for item in finding.evidence.get("event_scope", ()):
                    print(
                        "  "
                        f"{item['claim_event_type']}: {item['status']} "
                        f"({item.get('authority_family') or 'no-family'})"
                    )
            if finding.gate == "S-0j":
                print(f"  source_of_truth: {finding.evidence.get('source_of_truth')}")
                print(f"  risk_entry_family: {finding.evidence.get('risk_entry_family')}")
            if finding.gate == "S-0k":
                defaults = finding.evidence.get("defaults", {})
                print(f"  clean_cycles_to_flip: {defaults.get('clean_cycles_to_flip')}")
                print(f"  divergence_tolerance: {defaults.get('divergence_tolerance')}")
                print(f"  critical_zero: {defaults.get('critical_zero')}")
                print(f"  max_persistent_cycles: {defaults.get('max_persistent_cycles')}")
            if finding.gate == "S-NC-apply":
                print(f"  strategy: {finding.evidence.get('strategy')}")
                print(f"  uses_beta_outbox: {finding.evidence.get('uses_beta_outbox')}")
                print(f"  requires_apply_journal: {finding.evidence.get('requires_apply_journal')}")
                print(f"  states: {', '.join(finding.evidence.get('states', ())) }")
            if finding.gate == "Q7":
                quality = finding.evidence.get("quality_report", {})
                failures = quality.get("failures", ())
                print(f"  quality_n_total: {quality.get('n_total')}")
                print(f"  quality_kappa: {quality.get('kappa')}")
                if failures:
                    print(f"  quality_failure: {failures[0]}")
            print(f"  recommendation: {finding.recommendation}")
            print(f"  next_action: {finding.next_action}")
    if args.require_accepted and not approval.accepted:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
