"""GAP-35: derive snapshot-derived *code-completion* estimates per capability.

The ``Completion Snapshot`` table in ``specs/gaps.md`` was frozen at rev 2 as
editorial estimates ("~92%", "~85%", …) and explicitly flagged as *not
re-derived*. This script replaces those guesses with mechanically-derived
signals: for each capability it checks a curated set of on-disk path signals
(modules, packages, command entrypoints, contract tests) and reports
``present / total`` as the code-completion ratio, plus the raw supporting
counts (capability modules, capability test files).

Design constraints (matching ``derive_spec_counts.py``):
  - **Read-only** — never writes to the tree.
  - **No ``-1`` masking** — every signal is a concrete repo-relative path that
    either exists or does not; a probe never returns "unknown". The script
    exits ``1`` only when the repo root itself cannot be resolved (a genuine
    probe failure), printing ``<-- PROBE FAILED`` per the WP-6 convention.
  - **Stable capability map** — capabilities and their signals are declared
    in ``CAPABILITIES`` so the output is reproducible and diff-able across
    revisions. Adding a capability is a one-line edit.

Usage:
    python scripts/derive_code_completion.py
    python scripts/derive_code_completion.py --format json

Output is informational; the script is intentionally read-only. Reference its
output in ``specs/gaps.md`` (Completion Snapshot) and let ``check_spec_drift``
guard verify it does not silently drift.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Capability:
    key: str
    label: str
    # Repo-relative files/dirs whose presence indicates code for this capability.
    signals: tuple[str, ...]
    # Repo-relative dirs whose ``*.py`` count is a supporting raw metric.
    module_dirs: tuple[str, ...]
    # Repo-relative test dirs/files whose count is a supporting raw metric.
    test_globs: tuple[str, ...]


# Curated capability → on-disk-signal map. A signal is "present" if the path
# exists. The ratio present/total is the derived code-completion estimate.
# Signals are chosen to be *load-bearing* (their absence would indicate real
# missing work), not vanity counts. Update when a capability's footprint moves.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="report_pipeline",
        label="Newsletter / report pipeline",
        signals=(
            "src/commands/report_pipeline",
            "src/core/stages",
            "src/core/chart_cache_store.py",
            "src/core/chart_renderer_registry.py",
            "src/core/charts/declarative.py",
            "templates",
            "src/commands/report_ai.py",
        ),
        module_dirs=("src/commands/report_pipeline", "src/core/stages", "src/core/charts"),
        test_globs=("tests/unit/test_commands_report.py", "tests/contracts/test_qg_validation_matrix.py"),
    ),
    Capability(
        key="reality_substrate",
        label="Reality substrate (architecture)",
        signals=(
            "src/core/reality_store.py",
            "src/core/program_fact_store.py",
            "src/core/program_reality.py",
            "src/core/truth_model.py",
            "src/core/state_reader_registry.py",
            "src/ai/tiered_router.py",
            "src/core/edition_resolver.py",
            "src/core/snapshot_store.py",
            "src/core/archive_store.py",
        ),
        module_dirs=("src/core",),
        test_globs=("tests/unit/test_program_fact_store.py", "tests/unit/test_trusted_baseline.py"),
    ),
    Capability(
        key="ai_safety_pipeline",
        label="AI safety pipeline",
        signals=(
            "src/ai",
            "src/ai/ai_stage.py",
            "src/ai/blurb_generator.py",
            "src/ai/claim_extractor.py",
            "src/ai/tiered_router.py",
            "src/ai/decision_brief_advisor.py",
            "src/ai/edit_learner.py",
            "governance/roles",
        ),
        module_dirs=("src/ai",),
        test_globs=("tests/contracts/test_ai_disabled_write_paths.py",),
    ),
    Capability(
        key="multi_program",
        label="Multi-program support",
        signals=(
            "programs/_templates",
            "programs/_templates/example_tpm/editions/example_tpm_weekly.yaml",
            "src/core/config_loader_v2.py",
            "src/core/edition_resolver.py",
            "src/commands/onboard.py",
            "src/commands/list.py",
        ),
        module_dirs=("programs/_templates",),
        test_globs=("tests/unit/test_commands_onboard.py",),
    ),
    Capability(
        key="source_channel",
        label="Source / channel coverage",
        signals=(
            "src/m365",
            "src/m365/enricher.py",
            "src/m365/local_kb_reader.py",
            "src/m365/icm_client.py",
            "src/m365/teams_reader.py",
            "src/core/channel_registry_store.py",
            "src/adapters/microsoft",
        ),
        module_dirs=("src/m365", "src/adapters/microsoft"),
        test_globs=("tests/unit/test_m365_enricher.py",),
    ),
    Capability(
        key="actuation_autonomy",
        label="Actuation / autonomy",
        signals=(
            "src/commands/actuate.py",
            "src/commands/apply_proposals.py",
            "src/commands/override.py",
            "src/commands/escalate.py",
            "src/commands/decision_brief.py",
            "src/core/audit_query.py",
            "src/core/analytics_store.py",
        ),
        module_dirs=("src/commands",),
        test_globs=("tests/unit/test_commands_escalate.py",),
    ),
    Capability(
        key="operator_onboarding",
        label="Operator onboarding",
        signals=(
            "src/commands/onboard.py",
            "src/commands/setup.py",
            "src/core/setup_state.py",
            "src/ai/setup_assistant.py",
            "src/commands/doctor_checks",
            "src/commands/freshness.py",
        ),
        module_dirs=("src/commands/doctor_checks",),
        test_globs=("tests/unit/test_commands_onboard.py", "tests/unit/test_commands_doctor.py"),
    ),
    Capability(
        key="quality_gates",
        label="Quality gates",
        signals=(
            "src/core/quality_gates",
            "src/core/quality_gates/bridge.py",
            "src/core/quality_gates/continuity.py",
            "src/core/quality_gates/context_integrity.py",
            "src/commands/doctor_checks",
            "src/commands/confirm_stages",
            "src/core/stages/validation_stage.py",
        ),
        module_dirs=("src/core/quality_gates", "src/commands/doctor_checks"),
        test_globs=("tests/contracts/test_qg_validation_matrix.py",),
    ),
    Capability(
        key="contract_invariant",
        label="Contract / invariant coverage",
        signals=(
            "tests/contracts/test_architecture_fitness.py",
            "tests/contracts/test_import_boundaries.py",
            "tests/contracts/test_entity_ref_contracts.py",
            "tests/contracts/test_signal_review_gate_contract.py",
            "tests/contracts/test_qg_validation_matrix.py",
            "tests/contracts/test_e2e_recovery_drill_contract.py",
        ),
        module_dirs=("tests/contracts",),
        test_globs=("tests/contracts/test_architecture_fitness.py",),
    ),
    Capability(
        key="evidence_provenance",
        label="Evidence / provenance / audit",
        signals=(
            "src/core/evidence_models.py",
            "src/core/evidence_provenance.py",
            "src/ai/content_extractor.py",
            "src/core/judgment_backfill.py",
            "src/core/schema_evolution.py",
            "src/core/backup.py",
        ),
        module_dirs=("src/core",),
        test_globs=(
            "tests/unit/test_evidence_models_phase2.py",
            "tests/unit/test_phase4_provenance.py",
            "tests/unit/test_judgment_backfill.py",
            "tests/unit/test_schema_evolution.py",
        ),
    ),
)


def _resolve(signal: str) -> Path:
    # Path division accepts forward-slash-relative strings on every platform.
    return REPO_ROOT / signal


def _signal_present(signal: str) -> bool:
    return _resolve(signal).exists()


def _count_py_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*.py"))


def _count_test_globs(globs: tuple[str, ...]) -> int:
    total = 0
    for pattern in globs:
        # Each glob entry is a repo-relative path; count it if it exists.
        resolved = REPO_ROOT / pattern
        if resolved.exists():
            total += 1
    return total


def _ratio(present: int, total: int) -> float:
    if total == 0:
        return 0.0
    return present / total


def derive() -> dict[str, dict]:
    if not REPO_ROOT.exists():
        raise FileNotFoundError(f"REPO_ROOT not found: {REPO_ROOT}")
    result: dict[str, dict] = {}
    for cap in CAPABILITIES:
        present = sum(1 for s in cap.signals if _signal_present(s))
        module_count = sum(_count_py_files(REPO_ROOT / d) for d in cap.module_dirs)
        test_count = _count_test_globs(cap.test_globs)
        result[cap.key] = {
            "label": cap.label,
            "present": present,
            "total": len(cap.signals),
            "ratio": round(_ratio(present, len(cap.signals)), 4),
            "modules": module_count,
            "tests": test_count,
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive snapshot-derived code-completion estimates per capability."
    )
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)

    try:
        derived = derive()
    except FileNotFoundError as error:
        print(f"ERROR: {error}  <-- PROBE FAILED")
        return 1

    if args.format == "json":
        print(json.dumps(derived, indent=2))
        return 0

    print(f"{'capability':>28}  {'ratio':>6}  {'present/total':>14}  {'modules':>8}  {'tests':>6}")
    print("-" * 70)
    for key, data in derived.items():
        ratio_pct = f"{data['ratio'] * 100:5.1f}%"
        present_total = f"{data['present']}/{data['total']}"
        print(
            f"{key:>28}  {ratio_pct:>6}  {present_total:>14}  {data['modules']:>8}  {data['tests']:>6}"
        )
    overall = sum(d["present"] for d in derived.values()) / max(
        1, sum(d["total"] for d in derived.values())
    )
    print("-" * 70)
    print(f"{'overall':>28}  {overall * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())