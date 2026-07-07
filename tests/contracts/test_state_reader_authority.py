from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core.state_reader_registry import STATE_READER_REGISTRY


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
# src/core/program_paths.py is the single sanctioned Zone-A path registry
# (specs/declutter.md Phase 0.5, G-7). It references sidecar filenames as path
# constants — it returns paths, it does not read state — so it is allowlisted
# for every state the way state_reader_registry.py is.
_SANCTIONED_PATH_MODULES = frozenset({"src/core/program_paths.py"})
_GATHER_STATE_ALLOWLIST = {
    "src/core/gather_state_store.py",
    "src/core/state_reader_registry.py",
    "src/core/privacy_matrix.py",  # retention metadata only — not a state read
    "src/core/support_bundle.py",  # WS-17: SRE support bundle includes gather_state.json
    "src/core/program_paths.py",  # centralized path registry (G-7)
}
_CLAIMS_ALLOWLIST = {
    "src/core/claim_tracker.py",
    "src/core/state_reader_registry.py",
}
_AUTONOMY_AUDIT_ALLOWLIST = {
    "src/core/analytics_store.py",
    "src/core/state_reader_registry.py",
}
_REVIEW_POLICY_AUDIT_ALLOWLIST = {
    "src/core/signal_review.py",
    "src/core/state_reader_registry.py",
}
_REVIEWS_ALLOWLIST = {
    "src/core/journal.py",
    "src/core/state_reader_registry.py",
}
_SIGNAL_THREADS_ALLOWLIST = {
    "src/core/journal.py",
    "src/core/state_reader_registry.py",
}
_ACTIONS_ALLOWLIST = {
    "src/core/action_tracker.py",
    "src/core/state_reader_registry.py",
}
_AI_PROPOSALS_ALLOWLIST = {
    "src/core/ai_proposal_store.py",
    "src/core/state_reader_registry.py",
}
_EDIT_PATTERNS_ALLOWLIST = {
    "src/core/feedback/salience_modeler.py",
    "src/ai/edit_learner.py",
    "src/core/feedback/trust_profile_store.py",
    "src/core/intervention_ranker.py",
    "src/commands/trust.py",
    "src/core/state_reader_registry.py",
}
_RISK_UPDATES_ALLOWLIST = {
    "src/core/risk_register_engine.py",
    "src/core/state_reader_registry.py",
}


def test_gather_state_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _GATHER_STATE_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_gather_state_filename(tree):
            violations.append(relative)

    assert violations == []


def test_claim_log_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _CLAIMS_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "claims.jsonl"):
            violations.append(relative)

    assert violations == []


def test_autonomy_audit_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _AUTONOMY_AUDIT_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "autonomy_audit.jsonl"):
            violations.append(relative)

    assert violations == []


def test_review_policy_audit_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _REVIEW_POLICY_AUDIT_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "review_policy_audit.jsonl"):
            violations.append(relative)

    assert violations == []


def test_review_log_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _REVIEWS_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "reviews.jsonl"):
            violations.append(relative)

    assert violations == []


def test_signal_threads_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _SIGNAL_THREADS_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "signal_threads.jsonl"):
            violations.append(relative)

    assert violations == []


def test_actions_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _ACTIONS_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "actions.jsonl"):
            violations.append(relative)

    assert violations == []


def test_ai_proposals_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _AI_PROPOSALS_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "ai_proposals.jsonl"):
            violations.append(relative)

    assert violations == []


def test_edit_patterns_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _EDIT_PATTERNS_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "edit_patterns.jsonl"):
            violations.append(relative)

    assert violations == []


def test_risk_updates_reads_flow_through_owner_module() -> None:
    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in _RISK_UPDATES_ALLOWLIST:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, "risk_updates.jsonl"):
            violations.append(relative)

    assert violations == []


def _references_gather_state_filename(tree: ast.AST) -> bool:
    return _references_filename(tree, "gather_state.json")


def _references_filename(tree: ast.AST, filename: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == filename:
            return True
    return False


# States covered by dedicated per-state tests above, or with variable path patterns.
# These are excluded from the umbrella parametrized test to avoid redundant coverage.
_UMBRELLA_SKIP = {
    "gather_state",        # dedicated: test_gather_state_reads_flow_through_owner_module
    "claim_log",           # dedicated: test_claim_log_reads_flow_through_owner_module
    "trajectory",          # variable filename (<work_item>.jsonl) — not amenable to string-match
    "autonomy_audit",      # dedicated: test_autonomy_audit_reads_flow_through_owner_module
    "review_policy_audit", # dedicated: test_review_policy_audit_reads_flow_through_owner_module
    "review_log",          # dedicated: test_review_log_reads_flow_through_owner_module
    "signal_threads",      # dedicated: test_signal_threads_reads_flow_through_owner_module
    "actions",             # dedicated: test_actions_reads_flow_through_owner_module
    "ai_proposals",        # dedicated: test_ai_proposals_reads_flow_through_owner_module
    "edit_patterns",       # dedicated: test_edit_patterns_reads_flow_through_owner_module (multi-owner)
    "risk_updates",        # dedicated: test_risk_updates_reads_flow_through_owner_module
    "program_reality",     # directory-level facade (programs/<program>/) — no single sidecar file;
                           # WI-1.3 test_program_reality_authority.py covers authority contract.
}

# Per-state extra allowlists for modules that reference a sidecar filename for
# snapshotting/copying purposes rather than reading program state from it.
_UMBRELLA_EXTRA_ALLOWLISTS: dict[str, set[str]] = {
    # checkpoint_store.py lists the filename in CHECKPOINT_FILE_PATHS (a copy manifest),
    # not as a state-read. The authority contract covers state reads, not file copies.
    "chronicle": {"src/core/checkpoint_store.py"},
    # privacy_matrix.py references these filenames in SidecarRetentionRule metadata
    # (data classification / retention policy), not as state reads.
    "migration_log": {"src/core/privacy_matrix.py"},
    "external_dependencies": {"src/core/privacy_matrix.py"},
    # support_bundle.py (WS-17) names run_telemetry.jsonl + alerts.jsonl as
    # the in-tar filenames when assembling an SRE support bundle; the actual
    # reads go through `run_telemetry.read_run_telemetry` and
    # `alerts.read_alerts` (the owner modules), so the literal filename
    # here is a copy target, not a state read.
    "run_telemetry": {"src/core/support_bundle.py"},
    "alerts": {"src/core/support_bundle.py"},
    # audit_chain_proof shares the autonomy_audit.jsonl filename with the
    # autonomy_audit state. analytics_store.py is the legacy owner of the
    # same file (it writes the v1 records without chain hashes); audit_query
    # is the new v2 owner. Both are legitimate writers; the test still
    # catches anyone ELSE touching the file directly.
    "audit_chain_proof": {"src/core/analytics_store.py"},
    # redaction.py is the sole INV-DM-1 exception: it must read and atomically
    # rewrite event files to replace payloads with {redacted: true}. This is
    # a physical mutation, not a state read, so it legitimately bypasses the
    # read-only event_log owner module API (§10.8).
    "ledger_event_log": {"src/core/ledger/redaction.py"},
    # ncfl_store_policy.py references these filenames as string literals in the
    # Plane 1 store classification table (policy metadata), not as state reads.
    "readiness_snapshot": {"src/core/ncfl_store_policy.py"},
    "m365_registry": {"src/core/ncfl_store_policy.py"},
}


@pytest.mark.parametrize(
    "state_name",
    sorted(s for s in STATE_READER_REGISTRY if s not in _UMBRELLA_SKIP),
)
def test_no_direct_sidecar_reads_outside_registry(state_name: str) -> None:
    """For each registered sidecar state not already covered by a dedicated test,
    assert that no .py file in src/ references the filename directly as a string
    constant outside the registered owner module. This prevents accretion of direct
    reads that bypass the authority contract as the registry grows."""
    reg = STATE_READER_REGISTRY[state_name]
    filename = reg.path_pattern.rsplit("/", 1)[-1]
    owner_path = reg.owner_module.replace(".", "/") + ".py"
    allowlist = (
        {owner_path, "src/core/state_reader_registry.py"}
        | _UMBRELLA_EXTRA_ALLOWLISTS.get(state_name, set())
        | _SANCTIONED_PATH_MODULES
    )

    violations: list[str] = []
    for file_path in sorted(SRC_ROOT.rglob("*.py")):
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative in allowlist:
            continue
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _references_filename(tree, filename):
            violations.append(relative)

    assert violations == [], (
        f"{state_name!r}: {len(violations)} direct reference(s) to {filename!r} "
        f"found outside owner module ({owner_path}). "
        f"Read this state via its owner module or add to the allowlist.\n"
        f"Violations: {violations}"
    )
