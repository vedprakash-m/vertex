"""Executable NCFL Plane 1 store-policy audit.

This module intentionally does **not** enable NCFL apply.  It records the
decision packet from the ADR-0006 STOP gates (originally
specs/consolidated.md §33.3.1, now folded into the core specs; the
consolidated doc is archived at .archive/specs/consolidated.md, local-only)
as executable data so tests can prove every program-root YAML and every NCFL
target store is explicitly classified before the STOP gates are accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.ncfl_models import TARGET_STORES


@dataclass(frozen=True, slots=True)
class Plane1StorePolicy:
    root_yaml: str
    classification: str
    owner: str
    save_function: str | None
    ncfl_writable: bool


@dataclass(frozen=True, slots=True)
class NcflTargetStorePolicy:
    target_store: str
    root_yaml: str | None
    apply_writable: bool
    reason: str


PLANE1_STORE_POLICIES: tuple[Plane1StorePolicy, ...] = (
    Plane1StorePolicy("assumptions.yaml", "writable", "Eng/TPM", "save_assumptions", True),
    Plane1StorePolicy("backfill.yaml", "runtime", "Eng", None, False),
    Plane1StorePolicy("baseline.yaml", "proof-log", "Eng/Governance", None, False),
    Plane1StorePolicy("capability_status.yaml", "compiled-context", "TPM", None, False),
    Plane1StorePolicy("chapter_contract.yaml", "config", "Eng/Editorial", None, False),
    Plane1StorePolicy("decisions.yaml", "writable", "Eng/TPM", "save_decisions / upsert_decisions", True),
    Plane1StorePolicy("dependencies.yaml", "writable", "Eng/TPM", "save_dependencies", False),
    Plane1StorePolicy("earned_autonomy_state.yaml", "runtime", "Eng", None, False),
    Plane1StorePolicy("editorial_rules.yaml", "config", "Editorial", None, False),
    Plane1StorePolicy("escalation_rules.yaml", "config", "TPM", None, False),
    Plane1StorePolicy("fact_store_family_cycles.yaml", "runtime", "Eng", None, False),
    Plane1StorePolicy("fact_store_sor.yaml", "runtime", "Eng", None, False),
    Plane1StorePolicy("kpis.yaml", "compiled-context", "TPM/EM", None, False),
    Plane1StorePolicy("m365_registry.yaml", "config", "Eng/IT", None, False),
    Plane1StorePolicy("manual_metrics.yaml", "compiled-context", "TPM/EM", None, False),
    Plane1StorePolicy("milestones.yaml", "writable", "Eng/TPM", "save_milestones", True),
    Plane1StorePolicy("platform_proof_log.yaml", "proof-log", "Eng/Governance", None, False),
    Plane1StorePolicy("program.yaml", "config", "TPM", None, False),
    Plane1StorePolicy("readiness.yaml", "compiled-context", "TPM/EM", None, False),
    Plane1StorePolicy("readiness_snapshot.yaml", "runtime", "Eng", None, False),
    Plane1StorePolicy("review.yaml", "config", "Eng/TPM", None, False),
    Plane1StorePolicy("risk_register.yaml", "writable", "Eng/TPM", "save_risk_register", True),
    Plane1StorePolicy("scorecards.yaml", "compiled-context", "TPM/EM", None, False),
    Plane1StorePolicy("slice_contracts.yaml", "config", "Eng/Editorial", None, False),
    Plane1StorePolicy("source_contracts.yaml", "config", "Eng/IT", None, False),
    Plane1StorePolicy("source_waivers.yaml", "config", "Governance/IT", None, False),
    Plane1StorePolicy("template_contract.yaml", "config", "Eng/Editorial", None, False),
    Plane1StorePolicy("trusted_baseline.yaml", "proof-log", "Governance", None, False),
    Plane1StorePolicy("workstream_registry.yaml", "compiled-context", "TPM", None, False),
    Plane1StorePolicy("workstreams.yaml", "writable", "Eng/TPM", "save_workstreams_document", True),
)


NCFL_TARGET_STORE_POLICIES: tuple[NcflTargetStorePolicy, ...] = (
    NcflTargetStorePolicy("assumptions", "assumptions.yaml", True, "canonical writer exists and target is in NCFL scope"),
    NcflTargetStorePolicy("decisions", "decisions.yaml", True, "canonical writer exists and target is in NCFL scope"),
    NcflTargetStorePolicy(
        "knowledge_doc",
        None,
        True,
        "Zone B synthesis target (Phase 5); apply writes knowledge/<doc>.md with a dated .bak — no Plane 1 YAML record store",
    ),
    NcflTargetStorePolicy("milestones", "milestones.yaml", True, "canonical writer exists and target is in NCFL scope"),
    NcflTargetStorePolicy("risk_register", "risk_register.yaml", True, "canonical writer exists and target is in NCFL scope"),
    NcflTargetStorePolicy("workstreams", "workstreams.yaml", True, "canonical writer exists and target is in NCFL scope"),
)


def plane1_policy_by_root_yaml() -> dict[str, Plane1StorePolicy]:
    return {policy.root_yaml: policy for policy in PLANE1_STORE_POLICIES}


def target_policy_by_store() -> dict[str, NcflTargetStorePolicy]:
    return {policy.target_store: policy for policy in NCFL_TARGET_STORE_POLICIES}


def ncfl_apply_writable_target_stores() -> frozenset[str]:
    return frozenset(policy.target_store for policy in NCFL_TARGET_STORE_POLICIES if policy.apply_writable)


def is_ncfl_apply_writable_target_store(target_store: str) -> bool:
    policy = target_policy_by_store().get(target_store)
    return bool(policy and policy.apply_writable)


def is_ncfl_target_store(target_store: str) -> bool:
    """True if *target_store* is a recognized NCFL target store (§23.3 taxonomy)."""
    return target_store in TARGET_STORES


def audit_plane1_store_policy(program_root: Path) -> tuple[str, ...]:
    """Return audit failures for a program root without mutating anything."""
    failures: list[str] = []
    by_root = plane1_policy_by_root_yaml()
    actual_yaml = frozenset(path.name for path in program_root.glob("*.yaml"))
    missing = sorted(actual_yaml - set(by_root))
    if missing:
        failures.append(f"unclassified root YAML files: {', '.join(missing)}")

    target_by_store = target_policy_by_store()
    missing_targets = sorted(TARGET_STORES - set(target_by_store))
    if missing_targets:
        failures.append(f"unclassified NCFL target stores: {', '.join(missing_targets)}")

    for target, target_policy in sorted(target_by_store.items()):
        if target_policy.root_yaml is None:
            continue
        root_policy = by_root.get(target_policy.root_yaml)
        if root_policy is None:
            failures.append(f"target store {target!r} references unknown root YAML {target_policy.root_yaml!r}")
            continue
        if target_policy.apply_writable and not root_policy.ncfl_writable:
            failures.append(f"target store {target!r} is apply-writable but {target_policy.root_yaml} is not ncfl_writable")
    return tuple(failures)
