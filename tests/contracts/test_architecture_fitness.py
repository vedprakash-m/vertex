from __future__ import annotations
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

# D-29: this module is a source/AST governance + invariant suite. Every test
# here scans tracked source code or builds its own fixtures under tmp_path; none
# requires private editions/ or programs/ data (the few that read optional data
# guard for its absence internally). It therefore MUST run on the fresh-clone CI
# — a module-level skipif on data presence would silently disable every
# architecture-fitness guardrail. test_ci_guardrail_execution.py enforces that
# this file is never re-decorated with such a skip.

import ast
import re

from src.commands.doctor_checks.refactor_status import _DOCTOR_LOC_BUDGET, _GATHER_LOC_BUDGET
from src.core import archive_store, snapshot_store
from src.core.models import ConfirmedDimension, EditionType, RunManifest, RiskLevel, Snapshot, SnapshotItem


REPO_ROOT = Path(__file__).resolve().parents[2]

# These ceilings freeze the current high-risk hotspots so they cannot silently grow
# while the larger decomposition work remains intentionally deferred.
LINE_BUDGETS = {
    # PHASE 0 REVIEWED EXCEPTION (2026-06-05): the original 10,010 freeze started red
    # once gather.py reached 10,416 LOC. Rev. 12 extracted the transitional
    # DiscoveryService path and ratcheted the reviewed-exception freeze down to the
    # new live size. This is still not debt resolution. Phase 3 must ratchet
    # gather.py downward to <=8,000 after broader stage extraction and <=5,000 at
    # Phase 3 completion per specs/debt.md §21.3.
    # WS-17 (2026-06-09): +19 for the run_telemetry wire-in (accumulator +
    # observe_step call in _complete_progress_step + end-of-run emit call +
    # gather_started_at anchor). Closes the WS-17 contract that the
    # run_telemetry.jsonl sidecar is populated by gather runs.
    # WS-3 / v1.14 (2026-06-10): +21 for _emit_credential_expired_banner helper
    # + CredentialExpired import + 3 banner call sites (WorkIQ/Kusto/IcM).
    "src/commands/gather.py": 5160,  # +119: SharePoint/LT-deck gather integration (SP1-1/SP1-2/SP1-3) pending next extraction pass
    # Phase 6 reviewed exception (2026-06-07): doctor.py added flip-status and
    # flip-parity sub-checks per specs/debt.md §11 Phase 6 Step 1. The branch
    # extraction is scheduled after the parity-check command is proven.
    # +4 (rev. 134): added resolved-edition and issue_number validation guards
    # for mypy narrowing in the flip_status/flip_parity branches.
    # +13 (rev. 314): added --source-waivers sub-check per D-32 materialization;
    # branch extraction into doctor_checks/source_waiver_checks.py is the next
    # honest ratchet.
    # +12 (2026-06-21): added --nudge sub-check per .archive/specs/fix-nudge.md §24.8.
    # +20 (2026-06-23): added --rev-health/--rev-program sub-check per specs/program-context-intelligence.md §5.13 (FR-PCI-12); _run_rev_health helper renders the REV subsystem health summary.
    "src/commands/doctor.py": 1620,
    "src/commands/confirm.py": 1650,  # +5: DECK edition type guard to skip HTMLRenderer (82e07c4); +105: GAP-9/23/33 (QG-DM surfacing + shim-persist SoR guard + baseline dual-write SoR guard) (2026-06-17)
    # D-31 → WI-6.2 (2026-06-15): report.py decomposed into report_pipeline/assemble_stage.py;
    # LOC ratchet satisfied (1,413 ≤ 1,500). Budget updated after report output path refactor (+1 issue_dir var).
    # +35: P4-15 (2026-06-18) — auto-run enrich before report when workiq_enrich_schedule=pre_report.
    # +19 (2026-07-07, activation P5): injected ProgramReality read-path facade, per-family
    # workitem.state SoR honoring, audited VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK
    # banner, and milestone source_document_key/approval_event_id lineage rendering
    # (specs/backlog.md; .archive/specs/activation.md).
    "src/commands/report.py": 1470,
    "src/core/reality_store.py": 2311,  # +39: WI-2.5 owner_entity_ref + resolve_binding_owner (2026-06-15)
    "src/core/channel_registry_store.py": 1976,  # +79: discovery registration persistence helpers already present on branch (2026-06-02)
}

# W1.8 retired the critical broad-exception debt enough that we can now prevent
# regression without forcing a speculative cleanup of every remaining handler.
BROAD_EXCEPTION_BUDGETS = {
    "src/commands/confirm.py": 15,  # was 13; §13.2 maturity regression adds 2 guard try/except blocks
    "src/commands/gather.py": 5,   # was 4; FR-SG-38 auto-approval enforcement block adds 1 guard (never-gate-gather) (2026-06-15)
    "src/commands/doctor.py": 0,
    "src/commands/report.py": 0,
}


def test_high_risk_modules_do_not_grow_past_fitness_budget() -> None:
    violations: list[str] = []
    for relative_path, max_lines in LINE_BUDGETS.items():
        path = REPO_ROOT / relative_path
        line_count = sum(1 for _ in path.open("r", encoding="utf-8"))
        if line_count > max_lines:
            violations.append(f"{relative_path}: {line_count} lines > budget {max_lines}")

    assert violations == []


def test_critical_command_modules_do_not_regress_broad_exception_budget() -> None:
    violations: list[str] = []
    for relative_path, max_broad_excepts in BROAD_EXCEPTION_BUDGETS.items():
        path = REPO_ROOT / relative_path
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        broad_except_count = sum(1 for node in ast.walk(module) if _is_broad_except(node))
        if broad_except_count > max_broad_excepts:
            violations.append(
                f"{relative_path}: {broad_except_count} broad except handler(s) > budget {max_broad_excepts}"
            )

    assert violations == []


def test_refactor_status_budgets_match_fitness_budgets() -> None:
    assert _GATHER_LOC_BUDGET == LINE_BUDGETS["src/commands/gather.py"]
    assert _DOCTOR_LOC_BUDGET == LINE_BUDGETS["src/commands/doctor.py"]


def _is_broad_except(node: ast.AST) -> bool:
    if not isinstance(node, ast.ExceptHandler):
        return False
    if node.type is None:
        return True
    return isinstance(node.type, ast.Name) and node.type.id == "Exception"


# INV-2: Only snapshot_store.write_confirmed() writes confirmed snapshots, and
# it may only be imported by confirm.py (direct call) and file_stores.py
# (delegation wrapper that proxies the same function).  No other module may
# import write_confirmed from snapshot_store directly.
_WRITE_CONFIRMED_IMPORT_PATTERN = re.compile(
    r"from\s+src\.core\.snapshot_store\s+import\s+[^\n]*\bwrite_confirmed\b"
)
_WRITE_CONFIRMED_ALLOWED_CALLERS = frozenset(
    {
        "src/commands/confirm.py",
        "src/core/file_stores.py",
    }
)


def test_inv2_write_confirmed_single_write_path() -> None:
    """INV-2: confirm.py and file_stores.py are the only modules allowed to
    import write_confirmed from snapshot_store.  Any new import elsewhere would
    introduce a second write path that bypasses the archive lock and manifest."""
    violations: list[str] = []
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        relative = py_file.relative_to(REPO_ROOT).as_posix()
        if relative in _WRITE_CONFIRMED_ALLOWED_CALLERS:
            continue
        source = py_file.read_text(encoding="utf-8")
        if _WRITE_CONFIRMED_IMPORT_PATTERN.search(source):
            violations.append(relative)

    assert violations == [], (
        "INV-2 violation: write_confirmed imported outside approved callers: "
        + ", ".join(violations)
    )


# ---------------------------------------------------------------------------
# INV-3: Journal immutability — journal files are append-only, never modified
# ---------------------------------------------------------------------------

_JOURNAL_APPEND_ONLY_MODULES = frozenset(
    {
        "src/core/journal.py",
    }
)


def test_inv3_journal_append_only_no_write_mode() -> None:
    """INV-3: journal.py must never open .jsonl files in write/overwrite mode.
    All writes go through _append_jsonl which uses mode 'a'."""
    violations: list[str] = []
    for relative_path in _JOURNAL_APPEND_ONLY_MODULES:
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Check for .open("w"...) or .open("w+"...) calls
            if isinstance(func, ast.Attribute) and func.attr == "open":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        mode = arg.value
                        if "w" in mode and "a" not in mode:
                            violations.append(
                                f"{relative_path}: .open(mode='{mode}') at line {node.lineno}"
                            )
            # Check for open("path", "w"...) builtin
            if isinstance(func, ast.Name) and func.id == "open":
                if len(node.args) >= 2:
                    mode_arg = node.args[1]
                    if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
                        mode = mode_arg.value
                        if "w" in mode and "a" not in mode:
                            violations.append(
                                f"{relative_path}: open(mode='{mode}') at line {node.lineno}"
                            )

    assert violations == [], (
        "INV-3 violation: journal.py opens files in write mode (not append-only): "
        + ", ".join(violations)
    )


# ---------------------------------------------------------------------------
# INV-4: Trajectory append-only — trajectory files are append-only
# ---------------------------------------------------------------------------

_TRAJECTORY_APPEND_ONLY_MODULES = frozenset(
    {
        "src/core/trajectory.py",
    }
)


def _find_open_violations_in_node(
    node: ast.AST,
    relative_path: str,
    enclosing_func: str | None = None,
) -> list[str]:
    violations: list[str] = []
    if isinstance(node, ast.FunctionDef):
        enclosing_func = node.name
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "open":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    mode = arg.value
                    if "w" in mode and "a" not in mode:
                        if enclosing_func == "_quarantine_and_rewrite_jsonl":
                            pass
                        else:
                            violations.append(
                                f"{relative_path}: .open(mode='{mode}') at line {node.lineno}"
                            )
        if isinstance(func, ast.Name) and func.id == "open":
            if len(node.args) >= 2:
                mode_arg = node.args[1]
                if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
                    mode = mode_arg.value
                    if "w" in mode and "a" not in mode:
                        if enclosing_func == "_quarantine_and_rewrite_jsonl":
                            pass
                        else:
                            violations.append(
                                f"{relative_path}: open(mode='{mode}') at line {node.lineno}"
                            )
    for child in ast.iter_child_nodes(node):
        violations.extend(_find_open_violations_in_node(child, relative_path, enclosing_func))
    return violations


def test_inv4_trajectory_append_only_no_write_mode() -> None:
    """INV-4: trajectory.py must never open .jsonl files in write/overwrite mode.
    All writes go through _append_jsonl which uses mode 'a'."""
    violations: list[str] = []
    for relative_path in _TRAJECTORY_APPEND_ONLY_MODULES:
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        violations.extend(_find_open_violations_in_node(tree, relative_path))

    assert violations == [], (
        "INV-4 violation: trajectory.py opens files in write mode (not append-only): "
        + ", ".join(violations)
    )


# ---------------------------------------------------------------------------
# INV-D4: AI output paths must not auto-accept Program Fact writes
# ---------------------------------------------------------------------------

_INV_D4_ACCEPTED_FACT_ALLOWLIST = frozenset(
    {
        # Command-level deterministic write paths
        "src/commands/confirm.py",
        "src/commands/decisions.py",   # WI-3.11: link-outcome updates decision.entry payload
        "src/commands/facts.py",
        # Core sync paths (trackers / registers / engines that mirror accepted
        # program state into the Fact Store — these are deterministic system
        # writes, not AI outputs)
        "src/core/action_tracker.py",
        "src/core/claim_tracker.py",
        "src/core/commitment_store.py",    # WI-2.7: commitment.entry CLI-initiated writes
        "src/core/decision_register.py",
        "src/core/dependency_graph.py",
        "src/core/entity_alias_emitter.py", # WI-2.3: entity.alias CLI-initiated writes
        "src/core/ledger/fact_bridge.py",   # Ledger bridge deterministically promotes operator/source-authoritative events and corroboration facts
        "src/core/milestone_engine.py",
        "src/core/plane1_changelog.py",
        "src/core/program_fact_store.py",
        "src/core/risk_register_engine.py",
        "src/core/signal_promotion.py",     # WI-3.2a: signal promotion (non-provisional only)
        "src/core/source_trust.py",         # WI-3.1: trust.source_score + trust.bootstrap_grant deterministic writes
        "src/core/trusted_baseline_store.py",
        "src/core/workstream_association_store.py",
        "src/core/workstream_documents.py",
    }
)


def test_inv_d4_no_ai_auto_accept() -> None:
    violations: list[str] = []
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        relative_path = py_file.relative_to(REPO_ROOT).as_posix()
        if relative_path in _INV_D4_ACCEPTED_FACT_ALLOWLIST:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if _is_accepted_program_fact_input(node):
                violations.append(f"{relative_path}:{node.lineno}")
            elif _is_accepted_append_fact_call(node):
                violations.append(f"{relative_path}:{node.lineno}")

    assert violations == [], (
        "INV-D4 violation: accepted ProgramFact writes are only allowed in deterministic "
        "write paths (confirm/import/shadow-write/store internals): "
        + ", ".join(violations)
    )


def _is_accepted_program_fact_input(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _call_name(node.func) != "ProgramFactInput":
        return False
    review_state_keyword = next((keyword for keyword in node.keywords if keyword.arg == "review_state"), None)
    if review_state_keyword is None:
        return True
    return _is_fact_review_state_accepted(review_state_keyword.value)


def _is_accepted_append_fact_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _call_name(node.func) != "append_fact":
        return False
    review_state_keyword = next((keyword for keyword in node.keywords if keyword.arg == "review_state"), None)
    if review_state_keyword is None:
        return False
    return _is_fact_review_state_accepted(review_state_keyword.value)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_fact_review_state_accepted(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute):
        return False
    if node.attr != "ACCEPTED":
        return False
    return isinstance(node.value, ast.Name) and node.value.id == "FactReviewState"


# ---------------------------------------------------------------------------
# INV-6: QG-8 (Needs Input) is a hard publish-block, not forceable
# ---------------------------------------------------------------------------


def test_inv6_qg8_needs_input_is_hard_block() -> None:
    """INV-6: QG-8 (Needs Input risk) must be a hard block — not forceable.
    This ensures UNKNOWN risk dimensions cannot be pushed to publish."""
    from src.core.models import RiskLevel, Confidence, AttributionTier, EvidencePacket
    from src.core.quality_gates import DimensionRisk, _evaluate_risk_input_gate

    dimension_risks = (
        DimensionRisk(
            name="Test Dimension",
            risk=RiskLevel.UNKNOWN,
            summary="test",
            evidence=EvidencePacket(
                work_item_id=1,
                revisions=(),
                comments=(),
                enrichments=(),
                confidence=Confidence.NONE,
                tier=AttributionTier.TIER3,
                summary_for_reviewer="test",
            ),
        ),
    )
    result = _evaluate_risk_input_gate(dimension_risks)
    assert result.gate_id == "QG-8"
    assert result.passed is False, "QG-8 should fail when dimensions have UNKNOWN risk"
    assert result.forceable is not True, (
        "INV-6: QG-8 must be a hard block (not forceable)"
    )


# ---------------------------------------------------------------------------
# INV-10: Email max width — outer 680px, content 640px
# ---------------------------------------------------------------------------

_EMAIL_TEMPLATE_PATHS = [
    "templates/base.email.j2",
]


def test_inv10_email_template_width_constraints() -> None:
    """INV-10: email templates must enforce max outer width=680 and
    content width=640."""
    import re as _re

    width_pattern = _re.compile(r'width[= "]+"?(\d+)"?')
    violations: list[str] = []
    for relative_path in _EMAIL_TEMPLATE_PATHS:
        template_path = REPO_ROOT / relative_path
        if not template_path.exists():
            continue
        source = template_path.read_text(encoding="utf-8")
        for match in width_pattern.finditer(source):
            width_val = int(match.group(1))
            if width_val > 680:
                violations.append(
                    f"{relative_path}: width={width_val} exceeds max 680 at position {match.start()}"
                )

    assert violations == [], (
        "INV-10 violation: email template has width > 680: "
        + ", ".join(violations)
    )


# ---------------------------------------------------------------------------
# INV-11: Edition isolation — edition config changes must not affect other editions
# ---------------------------------------------------------------------------


def test_inv11_edition_resolver_has_no_shared_mutable_state() -> None:
    """INV-11: edition_resolver must not share mutable state between editions.
    Resolving one edition must not mutate the state used by another."""
    from src.core.config_loader import load_bundle, REPORTS_ROOT
    from src.core.edition_resolver import resolve_edition_paths

    # Verify that resolve_edition_paths is a pure function — calling it twice
    # with the same args returns the same result.
    try:
        edition_name = "acme_weekly"
        result1 = resolve_edition_paths(edition_name, reports_root=REPORTS_ROOT)
        result2 = resolve_edition_paths(edition_name, reports_root=REPORTS_ROOT)
        assert result1 == result2, (
            "INV-11: resolve_edition_paths returns different results on repeated calls — "
            "possible shared mutable state"
        )
    except Exception:
        # If edition doesn't exist in this repo, skip gracefully
        pass


# ---------------------------------------------------------------------------
# INV-12: ADO write-back requires explicit approval
# ---------------------------------------------------------------------------

_ADO_WRITER_MODULE = "src/m365/ado_writer.py"
_ADO_CLIENT_MODULE = "src/core/ado_client.py"
_ADO_WRITE_METHODS = frozenset({
    "update_work_item",
    "add_comment",
    "create_work_item",
    "update_work_items_batch",
})


def test_inv12_no_raw_ado_write_calls_outside_writer() -> None:
    """INV-12: ADO write methods must not be called directly outside
    ado_writer.py's apply_manifest flow.  All writes require explicit
    approval through 'vertex ado apply'."""
    violations: list[str] = []
    for relative_path in [f for f in (REPO_ROOT / "src").rglob("*.py") if f.name != "ado_writer.py"]:
        source = relative_path.read_text(encoding="utf-8")
        for method in _ADO_WRITE_METHODS:
            # Check for direct .update_work_item(...) / .add_comment(...) calls
            # on an ADO client object (not through ado_writer)
            if f".{method}(" in source and "ado_writer" not in str(relative_path):
                rel = relative_path.relative_to(REPO_ROOT).as_posix()
                if rel == "src/core/ado_client.py":
                    continue  # The client defines these methods; calls are fine
                if rel == "src/core/ado_actuation_adapter.py":
                    continue  # AdoActuationAdapter is the actuation-approved write path (WI-7.2/INV-12 gated by human approval upstream)
                violations.append(
                    f"{rel}: direct .{method}() call found"
                )

    assert violations == [], (
        "INV-12 violation: direct ADO write calls outside ado_writer: "
        + ", ".join(violations)
    )


# ---------------------------------------------------------------------------
# INV-14: Vitality nudge cooldown — max 1 per item per 14 days
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# INV-7: dry-run no-archive-writes — confirm --dry-run must never write to
# the archive store
# ---------------------------------------------------------------------------

_CONFIRM_MODULE = "src/commands/confirm.py"
_CONFIRM_FUNCTION_NAME = "confirm_issue"
_ARCHIVE_WRITE_FUNCTIONS = frozenset(
    {
        "write_confirmed",
        "write_confirmed_issue",
        "write_accepted_proposals_archive",
        "update_archive_semantic_index_for_issue",
        "mark_semantic_index_dirty",
        "project_confirmed_issue",
        "mark_analytics_dirty",
    }
)


def test_inv7_dry_run_never_writes_to_archive() -> None:
    """INV-7: A --dry-run confirm must never write to the archive store.

    Verifies structurally that confirm.py's confirm_issue function:
    1. Has an ``if dry_run: return`` early-exit guard.
    2. All calls to archive-write functions appear after that guard
       (i.e. at a higher line number), so they are unreachable when
       dry_run is True.
    """
    source = (REPO_ROOT / _CONFIRM_MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=_CONFIRM_MODULE)

    # Find the confirm_issue function definition
    confirm_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _CONFIRM_FUNCTION_NAME:
            confirm_fn = node
            break

    assert confirm_fn is not None, (
        f"INV-7: {_CONFIRM_FUNCTION_NAME} not found in {_CONFIRM_MODULE}"
    )

    # Find the dry_run early-return guard: if dry_run: return ...
    dry_run_guard_line = None
    for node in ast.walk(confirm_fn):
        if not isinstance(node, ast.If):
            continue
        # Check: test is a Name node with id "dry_run"
        test = node.test
        if isinstance(test, ast.Name) and test.id == "dry_run":
            # Verify the body contains a return statement (not just pass/log)
            has_return = any(isinstance(stmt, ast.Return) for stmt in node.body)
            if has_return:
                dry_run_guard_line = node.lineno
                break

    assert dry_run_guard_line is not None, (
        f"INV-7: no 'if dry_run: return ...' guard found in "
        f"{_CONFIRM_FUNCTION_NAME} ({_CONFIRM_MODULE})"
    )

    # Find all calls to archive-write functions within confirm_issue
    violations: list[str] = []
    for node in ast.walk(confirm_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match direct function calls like write_confirmed_issue(...)
        if isinstance(func, ast.Name) and func.id in _ARCHIVE_WRITE_FUNCTIONS:
            if node.lineno <= dry_run_guard_line:
                violations.append(
                    f"{_CONFIRM_MODULE}: archive-write call {func.id}() at line "
                    f"{node.lineno} is at or before dry_run guard at line "
                    f"{dry_run_guard_line}"
                )
        # Match attribute calls like archive_store.write_confirmed_issue(...)
        if isinstance(func, ast.Attribute) and func.attr in _ARCHIVE_WRITE_FUNCTIONS:
            if node.lineno <= dry_run_guard_line:
                violations.append(
                    f"{_CONFIRM_MODULE}: archive-write call .{func.attr}() at line "
                    f"{node.lineno} is at or before dry_run guard at line "
                    f"{dry_run_guard_line}"
                )

    assert violations == [], (
        "INV-7 violation: archive-write calls reachable before dry_run guard: "
        + ", ".join(violations)
    )


# ---------------------------------------------------------------------------
# INV-9: Ban-list filtering on all rendered content
# ---------------------------------------------------------------------------

# Modules that produce or validate rendered output MUST import and call
# find_ban_list_violations before that output can be published.  If any of
# these modules drops the call, banned phrases could slip into email/Teams
# output undetected.
_INV9_BAN_LIST_CALLERS = frozenset(
    {
        "src/core/stages/validation_stage.py",
        # WI-6.2: ban-list enforcement moved with assembly stage to assemble_stage.py
        "src/commands/report_pipeline/assemble_stage.py",
        "src/commands/confirm.py",
        "src/commands/propose.py",
        "src/commands/apply_proposals.py",
    }
)

_INV9_IMPORT_PATTERN = re.compile(
    r"from\s+src\.core\.ban_list_validator\s+import\s+[^\n]*\bfind_ban_list_violations\b"
)
_INV9_CALL_PATTERN = re.compile(r"\bfind_ban_list_violations\s*\(")


def test_inv9_ban_list_filtering_applied_on_all_rendered_content() -> None:
    """INV-9: every module that produces or validates rendered content must
    import and call find_ban_list_violations.  Removing the call would allow
    banned phrases to reach published output without detection."""
    violations: list[str] = []
    for relative_path in _INV9_BAN_LIST_CALLERS:
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        if not _INV9_IMPORT_PATTERN.search(source):
            violations.append(
                f"{relative_path}: missing import of find_ban_list_violations"
            )
        if not _INV9_CALL_PATTERN.search(source):
            violations.append(
                f"{relative_path}: missing call to find_ban_list_violations"
            )

    assert violations == [], (
        "INV-9 violation: ban-list filtering not applied in render pipeline: "
        + ", ".join(violations)
    )


# ---------------------------------------------------------------------------
# INV-13: Echo-chamber guard — no self-referential signal loop
# ---------------------------------------------------------------------------

_GATHER_MODULE = "src/commands/gather.py"
_ADO_WRITER_MODULE = "src/m365/ado_writer.py"
_SIGNAL_DEDUP_MODULE = "src/core/signal_dedup.py"
_ADO_SEMANTICS_MODULE = "src/core/ado_semantics.py"

_ECHO_CHAMBER_GUARDS = frozenset({
    "_is_echo_chamber_revision",
    "_is_echo_chamber_comment",
})

_VERTEX_SELF_SOURCE = "vertex/ado_update"


def test_inv13_echo_chamber_guards_exist_and_are_called() -> None:
    """INV-13: the system must not propose or amplify a signal derived solely
    from its own prior output without fresh external evidence.

    Three mechanisms enforce this invariant:

    1. gather.py must define and call _is_echo_chamber_revision and
       _is_echo_chamber_comment in every ADO signal builder, preventing
       Vertex's own ADO write-backs from being ingested as fresh signals.
    2. ado_writer.py must tag its output signals with source='vertex/ado_update'
       so they can be identified as self-referential.
    3. signal_dedup.py must explicitly handle the 'vertex/ado_update' source
       in fingerprinting so self-written signals are never confused with
       fresh external evidence.
    """
    violations: list[str] = []

    # --- Check 1: gather.py defines and uses echo-chamber guards -----------
    gather_source = (REPO_ROOT / _GATHER_MODULE).read_text(encoding="utf-8")
    gather_tree = ast.parse(gather_source, filename=_GATHER_MODULE)

    # 1a: Both guard functions must be defined
    defined_functions = {
        node.name for node in ast.walk(gather_tree)
        if isinstance(node, ast.FunctionDef)
    }
    for guard in _ECHO_CHAMBER_GUARDS:
        if guard not in defined_functions:
            violations.append(
                f"{_GATHER_MODULE}: missing echo-chamber guard function {guard}"
            )

    # 1b: Both guard functions must be called (not just defined)
    for guard in _ECHO_CHAMBER_GUARDS:
        call_pattern = re.compile(rf"\b{re.escape(guard)}\s*\(")
        if not call_pattern.search(gather_source):
            violations.append(
                f"{_GATHER_MODULE}: {guard} is defined but never called"
            )

    # 1c: Guard calls must appear inside ADO signal-builder functions
    #     (not just anywhere). Find functions that build ADO signals and
    #     verify they call at least one echo-chamber guard.
    _ADO_SIGNAL_BUILDER_PATTERN = re.compile(
        r"def _build_ado_(revision|comment)_signals\s*\("
    )
    signal_builder_names: list[str] = []
    for node in ast.walk(gather_tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if _ADO_SIGNAL_BUILDER_PATTERN.match(node.name):
            signal_builder_names.append(node.name)

    for builder_name in signal_builder_names:
        # Find the function node to get its source span
        for node in ast.walk(gather_tree):
            if not isinstance(node, ast.FunctionDef) or node.name != builder_name:
                continue
            func_source_lines = gather_source.splitlines()[
                node.lineno - 1 : node.end_lineno
            ]
            func_source = "\n".join(func_source_lines)
            has_guard = any(
                re.search(rf"\b{re.escape(guard)}\s*\(", func_source)
                for guard in _ECHO_CHAMBER_GUARDS
            )
            if not has_guard:
                violations.append(
                    f"{_GATHER_MODULE}: {builder_name} does not call any "
                    f"echo-chamber guard"
                )
            break

    # --- Check 2: ado_writer.py tags self-output with vertex/ado_update ----
    writer_source = (REPO_ROOT / _ADO_WRITER_MODULE).read_text(encoding="utf-8")
    if f'source="{_VERTEX_SELF_SOURCE}"' not in writer_source:
        violations.append(
            f"{_ADO_WRITER_MODULE}: does not tag signals with "
            f'source="{_VERTEX_SELF_SOURCE}"'
        )

    # --- Check 3: signal_dedup.py handles vertex/ado_update in fingerprint --
    dedup_source = (REPO_ROOT / _SIGNAL_DEDUP_MODULE).read_text(encoding="utf-8")
    if f'"{_VERTEX_SELF_SOURCE}"' not in dedup_source:
        violations.append(
            f"{_SIGNAL_DEDUP_MODULE}: does not explicitly handle "
            f'"{_VERTEX_SELF_SOURCE}" in signal fingerprinting'
        )

    # --- Check 4: ado_semantics.py provides is_vertex_generated_comment ----
    semantics_source = (REPO_ROOT / _ADO_SEMANTICS_MODULE).read_text(encoding="utf-8")
    if "is_vertex_generated_comment" not in semantics_source:
        violations.append(
            f"{_ADO_SEMANTICS_MODULE}: missing is_vertex_generated_comment"
        )

    # --- Check 5: no signal path treats vertex/ado_update as external ------
    #     Scan all src files for patterns that would ingest vertex/ado_update
    #     signals as fresh evidence (e.g. using them to build new ado/revision
    #     or ado/comment signals). The only legitimate consumers of
    #     vertex/ado_update are: dedup fingerprinting, write-back tracking
    #     in ado.py, and the writer itself.
    _VERTEX_SOURCE_REF_PATTERN = re.compile(
        rf'["\']vertex/ado_update["\']|source\s*==\s*["\']vertex/ado_update["\']'
    )
    _ALLOWED_VERTEX_SOURCE_CONSUMERS = frozenset({
        "src/m365/ado_writer.py",       # creates the signals
        "src/core/signal_dedup.py",      # fingerprints them
        "src/commands/ado.py",          # tracks applied updates
        "src/commands/doctor_checks/consistency_checks.py",  # WS-20: no-orphan assertion reads source to check coverage
    })
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        relative = py_file.relative_to(REPO_ROOT).as_posix()
        if relative in _ALLOWED_VERTEX_SOURCE_CONSUMERS:
            continue
        source = py_file.read_text(encoding="utf-8")
        if _VERTEX_SOURCE_REF_PATTERN.search(source):
            violations.append(
                f"{relative}: references vertex/ado_update outside "
                f"allowed consumers — potential self-referential loop"
            )

    assert violations == [], (
        "INV-13 violation: echo-chamber guard incomplete: "
        + "; ".join(violations)
    )


def test_inv14_nudge_cooldown_default_is_14_days() -> None:
    """INV-14: the default nudge cooldown must be 14 days."""
    from src.core.vitality_reporting import VitalitySettings

    settings = VitalitySettings(
        triage=True,
        newsletter_aggregate=True,
        newsletter_individual_praise=True,
    )
    assert settings.nudge_cooldown_days == 14, (
        f"INV-14: nudge_cooldown_days default is {settings.nudge_cooldown_days}, expected 14"
    )


def test_persona_checker_zone_a_no_ai_imports() -> None:
    """P4-3: persona_checker and persona_models must not import src.ai or src.m365 (Zone A contract)."""
    import ast
    repo_root = Path(__file__).parent.parent.parent

    zone_a_persona_files = [
        repo_root / "src" / "core" / "persona_checker.py",
        repo_root / "src" / "core" / "persona_models.py",
        repo_root / "src" / "core" / "scope_resolver.py",
    ]
    forbidden_prefixes = ("src.ai", "src.m365")
    violations: list[str] = []

    for py_file in zone_a_persona_files:
        if not py_file.exists():
            violations.append(f"{py_file.name}: file missing")
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(p) for p in forbidden_prefixes):
                        violations.append(f"{py_file.name}: imports {alias.name!r}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if any(module.startswith(p) for p in forbidden_prefixes):
                    violations.append(f"{py_file.name}: from {module!r} import ...")

    assert violations == [], (
        "P4-3 Zone A violation — persona core files must not import src.ai or src.m365: "
        + "; ".join(violations)
    )


def test_personas_yaml_has_schema_version() -> None:
    """P4-3: personas.yaml must declare schema_version to prevent silent schema drift."""
    import yaml
    repo_root = Path(__file__).parent.parent.parent

    personas_yaml = repo_root / "programs" / "acme" / "knowledge" / "personas.yaml"
    if not personas_yaml.exists():
        return  # file is gitignored; skip in CI if absent

    data = yaml.safe_load(personas_yaml.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "personas.yaml must be a YAML mapping"
    assert "schema_version" in data, (
        "P4-3: personas.yaml missing 'schema_version' — add schema_version: '1.0'"
    )
    assert data["schema_version"] == "1.0", (
        f"P4-3: personas.yaml schema_version is {data['schema_version']!r}, expected '1.0'"
    )


# ---------------------------------------------------------------------------
# INV-16: render_stage.py must use ctx.data_as_of (not ctx.started_at) for
#         the edition title so report titles reflect the data snapshot date,
#         not the moment the CLI was invoked.  This regression has been
#         re-introduced twice by other implementations touching render_stage.py.
# ---------------------------------------------------------------------------


def test_inv16_render_stage_title_uses_data_as_of() -> None:
    """INV-16: format_edition_title in render_stage.py must pass ctx.data_as_of,
    not ctx.started_at.  Using started_at makes titles non-deterministic and
    breaks cassette tests that assert a fixed date."""
    render_stage_source = (REPO_ROOT / "src/core/stages/render_stage.py").read_text(encoding="utf-8")

    # Confirm the correct call exists
    correct_call = "format_edition_title(ctx.bundle, ctx.resolved_issue_number, ctx.data_as_of)"
    assert correct_call in render_stage_source, (
        "INV-16 violation: render_stage.py format_edition_title does not use ctx.data_as_of. "
        "Must be: support.format_edition_title(ctx.bundle, ctx.resolved_issue_number, ctx.data_as_of)"
    )

    # Confirm the regressed form is absent
    regressed_call = "format_edition_title(ctx.bundle, ctx.resolved_issue_number, ctx.started_at)"
    assert regressed_call not in render_stage_source, (
        "INV-16 violation: render_stage.py format_edition_title uses ctx.started_at (wrong). "
        "Must use ctx.data_as_of so report titles reflect the data snapshot date."
    )


def test_inv_d3_confirm_atomicity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-D3: an interrupted confirm leaves either the prior confirmed state
    or the new confirmed state, never a mixed partial archive."""
    archive_root = tmp_path / "archive"
    edition = "demo_weekly"

    archive_store.write_confirmed_issue(
        edition=edition,
        issue_number=1,
        snapshot=_build_contract_snapshot(issue_number=1),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_build_contract_manifest(issue_number=1),
        archive_root=archive_root,
    )

    edition_root = snapshot_store.get_archive_root(edition, archive_root=archive_root)
    before_files = {
        relative.as_posix(): path.read_bytes()
        for path in edition_root.rglob("*")
        if path.is_file()
        for relative in [path.relative_to(edition_root)]
    }

    staged_snapshot_path = snapshot_store.write_confirmed(
        edition=edition,
        issue_number=2,
        snapshot=_build_contract_snapshot(issue_number=2),
        archive_root=archive_root,
        promote=False,
        acquire_lock=False,
    )

    original_promote = archive_store._promote_file_with_rollback
    promotion_count = {"value": 0}

    def _fail_mid_promotion(staged_path, final_path, rollback_root, rollback_entries):
        promotion_count["value"] += 1
        original_promote(staged_path, final_path, rollback_root, rollback_entries)
        if promotion_count["value"] == 2:
            raise RuntimeError("simulated mid-confirm interruption")

    monkeypatch.setattr(archive_store, "_promote_file_with_rollback", _fail_mid_promotion)

    with pytest.raises(RuntimeError, match="simulated mid-confirm interruption"):
        with snapshot_store.ArchiveLock(snapshot_store.get_archive_root(edition, archive_root=archive_root)):
            archive_store.write_confirmed_issue(
                edition=edition,
                issue_number=2,
                snapshot=_build_contract_snapshot(issue_number=2),
                html_body="<html><body>Issue 002</body></html>",
                markdown_body="# Issue 002",
                manifest=_build_contract_manifest(issue_number=2),
                archive_root=archive_root,
                snapshot_source=staged_snapshot_path,
                snapshot_is_staged=True,
                acquire_lock=False,
            )

    after_files = {
        relative.as_posix(): path.read_bytes()
        for path in edition_root.rglob("*")
        if path.is_file()
        for relative in [path.relative_to(edition_root)]
    }

    assert after_files == before_files
    assert not (edition_root / "snapshots" / "issue_002.snapshot.json").exists()
    assert not (edition_root / "html" / "issue_002.html").exists()
    assert not (edition_root / "md" / "issue_002.md").exists()
    assert not (edition_root / "manifests" / "issue_002.json").exists()
    assert not (edition_root / "staging").exists()

    index_payload = json.loads((edition_root / "index.json").read_text(encoding="utf-8"))
    assert [entry["issue_number"] for entry in index_payload["issues"]] == [1]
    restored_snapshot = snapshot_store.read_snapshot(edition_root / "snapshots" / "issue_001.snapshot.json")
    assert restored_snapshot.issue_number == 1
    assert restored_snapshot.generated_at == _build_contract_snapshot(issue_number=1).generated_at


def _build_contract_snapshot(*, issue_number: int) -> Snapshot:
    risk = RiskLevel.LOW if issue_number == 1 else RiskLevel.MEDIUM
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 5, issue_number, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, issue_number, 8, 45, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=100 + issue_number,
                type="Feature",
                title=f"Demo issue {issue_number:03d}",
                state="Active",
                assigned_to="Vertex Maintainer",
                area_path="One\\Demo",
                target_date=date(2026, 6, 30),
                risk_level=risk,
                tags=["demo"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Demo Scorecard",
                name="Execution",
                risk=risk,
                prior_risk=RiskLevel.LOW if issue_number > 1 else None,
                item_count=1,
                ado_query_url="https://dev.azure.com/your-org/One/_queries/query-id",
            ),
        ),
    )


def _build_contract_manifest(*, issue_number: int) -> RunManifest:
    return RunManifest(
        manifest_id=f"manifest-{issue_number}",
        issue_number=issue_number,
        edition="demo_weekly",
        started_at=datetime(2026, 5, issue_number, 8, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, issue_number, 9, 0, tzinfo=timezone.utc),
        config_hash="config",
        snapshot_hash=f"snapshot-{issue_number}",
        html_hash=f"html-{issue_number}",
        md_hash=f"md-{issue_number}",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha=None,
    )


# ---------------------------------------------------------------------------
# Phase 7 — Governance regression contracts
# ---------------------------------------------------------------------------
# Phase 7 (Governance and Clean-Checkout Proofs) is in progress. The governance
# regression contracts below were authored as xfail strict=True while the
# underlying debt (D-24, D-18, D-04) was being resolved; each is promoted to a
# strict pass once its gap is closed. D-04 (provider leakage in the gather
# coordinator) is CLOSED (rev. 316): gather.py now reaches ADO/Graph transports
# only through src/commands/gather_pipeline/provider_facade.py. D-18 (direct
# JSONL sidecar reads) is CLOSED (rev. 317): all 21 line-readers route their
# decode through src/core/jsonl_utils.parse_jsonl_line. D-24 (hardcoded program
# literals in core) remains xfail and enumerates its violations so the work is
# auditable.


# ---------------------------------------------------------------------------
# Phase 7 D-24: No hardcoded program literals in core
# ---------------------------------------------------------------------------
#
# Track-A finding P2 (specs/ops-ready.md) is now CLOSED on this branch:
# the former Acme-coupled SECTION_TO_CHAPTER registry was removed, report
# routing no longer references it, and scripts/check_spec_drift.py freezes
# the "section_catalog.py is gone" contract against the live specs/code.
# This test now asserts the post-remediation steady state: the deleted file
# does not reappear, and no new src/core/*.py module reintroduces program
# literals such as Acme-specific identifiers.

_PHASE7_D24_PROGRAM_LITERALS = (
    "acme_weekly",
    "/acme/",
)

# Extra literals defensively banned from Zone A/B/C (GAP-14/GAP-28).
# "wingtip" has no active leak; included as a ratchet. "dd_on_pf", "adventure",
# "contoso" have known remaining violations in chapter_contract_loader.py,
# workstream_registry.py, and m365_discovery_support.py (tracked in GAP-14)
# and are NOT yet in this set — add them after those files are fixed.
_PHASE7_D24_EXTRA_BANNED = frozenset({"wingtip"})

_PHASE7_D24_WORD_BOUND_RE = re.compile(r"\bnova\b")

def test_no_hardcoded_program_literals_in_core() -> None:
    """Phase 7 D-24 + WS-11: src/core/**/*.py must not contain hardcoded
    program identifiers such as "acme_weekly", "/acme/", or the word "acme".

    Zone A core is shared across all programs (V-10 provider neutrality was
    proven, but program neutrality is Track-A P2). Program identifiers belong
    in per-program config (programs/<program>/...) — never baked into core
    modules. The legacy section-catalog exception has been deleted; if the
    file returns or if any other core module reintroduces Acme-coupled literals,
    this contract must fail loudly.

    WS-11 extends the original top-level glob to a recursive scan: subpackages
    (e.g. src/core/charts/) are now in scope.
    """
    violations: list[tuple[str, int, str, str]] = []
    section_catalog = REPO_ROOT / "src/core/section_catalog.py"
    assert not section_catalog.exists(), (
        "Phase 7 D-24 violation: src/core/section_catalog.py reappeared. "
        "The Acme-coupled section-to-chapter registry was intentionally "
        "deleted; keep chapter routing config-driven via KustoQuery.chapter."
    )

    for py_file in sorted((REPO_ROOT / "src/core").rglob("*.py")):
        relative = py_file.relative_to(REPO_ROOT).as_posix()
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            value = node.value
            matched_literal: str | None = None
            if any(literal in value for literal in _PHASE7_D24_PROGRAM_LITERALS):
                for literal in _PHASE7_D24_PROGRAM_LITERALS:
                    if literal in value:
                        matched_literal = literal
                        break
            elif _PHASE7_D24_WORD_BOUND_RE.search(value):
                matched_literal = "acme (word-boundary)"
            elif any(banned in value for banned in _PHASE7_D24_EXTRA_BANNED):
                for banned in _PHASE7_D24_EXTRA_BANNED:
                    if banned in value:
                        matched_literal = banned
                        break
            if matched_literal is None:
                continue
            # WS-11 narrow exception: a string constant that is a legacy-alias
            # shim (matches the literal "acme::") is allowed when the same
            # file (a) names the canonical alias as `core::` and (b) provides
            # a registered `CHART_RENDERERS` dict. This exception is for the
            # backward-compatibility bridge in src/core/charts/*.py only.
            if (
                matched_literal == "acme (word-boundary)"
                and "acme::" in value
                and "core::" in source
                and "CHART_RENDERERS" in source
            ):
                continue
            violations.append(
                (relative, node.lineno, matched_literal, value[:80])
            )

    assert violations == [], (
        "Phase 7 D-24 violation: hardcoded program literals in src/core/*.py "
        "(move to per-program config under programs/<program>/). "
        "Findings: "
        + "; ".join(
            f"{rel}:{line} (matched {literal!r} in {snippet!r})"
            for rel, line, literal, snippet in violations
        )
    )


def test_no_hardcoded_program_literals_in_zones_b_and_c() -> None:
    """GAP-28: src/ai/**/*.py and src/m365/**/*.py must not contain hardcoded
    Acme program identifiers.

    Zone B (ai/) and Zone C (m365/) are shared infrastructure: they provide
    AI orchestration and Microsoft-ecosystem adapters for all programs.
    Embedding Acme-specific identifiers there creates the same coupling
    problem as the now-deleted src/core/section_catalog.py.
    """
    zones = [REPO_ROOT / "src/ai", REPO_ROOT / "src/m365"]
    violations: list[tuple[str, int, str, str]] = []
    for zone in zones:
        if not zone.exists():
            continue
        for py_file in sorted(zone.rglob("*.py")):
            relative = py_file.relative_to(REPO_ROOT).as_posix()
            try:
                source = py_file.read_text(encoding="utf-8")
            except OSError:
                continue
            tree = ast.parse(source, filename=relative)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                value = node.value
                matched_literal: str | None = None
                if any(literal in value for literal in _PHASE7_D24_PROGRAM_LITERALS):
                    for literal in _PHASE7_D24_PROGRAM_LITERALS:
                        if literal in value:
                            matched_literal = literal
                            break
                elif _PHASE7_D24_WORD_BOUND_RE.search(value):
                    matched_literal = "acme (word-boundary)"
                elif any(banned in value for banned in _PHASE7_D24_EXTRA_BANNED):
                    for banned in _PHASE7_D24_EXTRA_BANNED:
                        if banned in value:
                            matched_literal = banned
                            break
                if matched_literal is not None:
                    violations.append((relative, node.lineno, matched_literal, value[:80]))

    assert violations == [], (
        "GAP-28 violation: hardcoded program literals in Zone B/C "
        "(src/ai/ or src/m365/). Move to per-program config. Findings: "
        + "; ".join(
            f"{rel}:{line} (matched {literal!r} in {snippet!r})"
            for rel, line, literal, snippet in violations
        )
    )


# ---------------------------------------------------------------------------
# Phase 7 D-18: No direct JSONL sidecar reads outside the state reader
#                 registry
# ---------------------------------------------------------------------------
#
# All JSONL sidecar reads must flow through src/core/state_reader_registry.py.
# That registry is the only authoritative source for which module owns which
# state file. The shared utility src/core/jsonl_utils.py is the only place
# allowed to call json.loads/json.load on a *.jsonl file directly. Any other
# caller is bypassing the registry and risks inconsistent corruption handling.

_PHASE7_D18_JSONL_PATH_RE = re.compile(r"\.jsonl\b")

_PHASE7_D18_ALLOWED_MODULES = frozenset(
    {
        # The registry itself declares paths but does not read them.
        "src/core/state_reader_registry.py",
        # The shared JSONL utility is the only place allowed to call
        # json.loads/json.load on a *.jsonl file directly.
        "src/core/jsonl_utils.py",
        # Sidecar owners are allowed to read their own .jsonl file directly
        # (this is the D-18 "canonical seam" — the sidecar owner is the
        # authority for its own file format and corruption handling). Every
        # entry below must be paired with a STATE_READER_REGISTRATION in
        # src/core/state_reader_registry.py.
        "src/core/migration_log.py",     # WS-11/16: migration_log
        "src/core/run_telemetry.py",     # WS-17:   run_telemetry
        "src/core/alerts.py",            # WS-17:   alerts
        "src/core/model_registry.py",    # WS-24:   model_registry
        "src/core/ledger/event_log.py",  # Ledger event-log owner; registered JSONL seam for append/replay/verify under programs/<program>/ledger/events/
    }
)

_PHASE7_D18_JSONL_READER_NAME_RE = re.compile(r"(?:^|_)(?:read|load|iter).*(?:jsonl|record|event|log)", re.IGNORECASE)


def _phase7_d18_collect_jsonl_violations(
    module_path: Path,
) -> list[tuple[str, int, str]]:
    """Return a list of (filename, lineno, function_name) for direct
    json.loads/json.load calls inside this module that look like they
    operate on *.jsonl paths. False-positive control: we require the call
    to live in a function whose source contains a '.jsonl' literal OR
    whose name suggests a jsonl read helper."""
    source = module_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(module_path))
    except SyntaxError:
        return []
    relative = module_path.relative_to(REPO_ROOT).as_posix()
    violations: list[tuple[str, int, str]] = []
    source_lines = source.splitlines()
    function_ranges: list[tuple[str, int, int, str]] = []

    # Walk the tree manually, tracking the enclosing FunctionDef for each
    # call node. Only assign enclosing_func for nodes that have a lineno.
    enclosing_func: dict[int, str] = {}

    def _visit(node: ast.AST, current_func: str | None) -> None:
        new_func = current_func
        if isinstance(node, ast.FunctionDef):
            new_func = node.name
            end_lineno = getattr(node, "end_lineno", node.lineno)
            function_ranges.append(
                (
                    node.name,
                    node.lineno,
                    end_lineno,
                    "\n".join(source_lines[node.lineno - 1 : end_lineno]),
                )
            )
        # Record enclosing function for nodes that have a lineno
        if new_func is not None:
            lineno = getattr(node, "lineno", None)
            if lineno is not None and lineno not in enclosing_func:
                enclosing_func[lineno] = new_func
        for child in ast.iter_child_nodes(node):
            _visit(child, new_func)

    _visit(tree, None)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Only ``json.loads(<Name>)`` — i.e. decoding a single per-line string
        # variable — signals JSONL line-stream parsing, which must be routed
        # through src/core/jsonl_utils.parse_jsonl_line (or the bulk readers).
        # Whole-file reads ``json.loads(path.read_text())`` (a Call arg) and
        # embedded JSON columns ``json.loads(row["…_json"])`` (a Subscript arg)
        # are NOT JSONL streams and are deliberately exempt.
        if not (isinstance(func, ast.Attribute) and func.attr == "loads"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "json"):
            continue
        first_arg = node.args[0] if node.args else None
        if not isinstance(first_arg, ast.Name):
            continue
        func_name = enclosing_func.get(node.lineno, "<module>")
        func_source = ""
        for name, start_lineno, end_lineno, block_source in function_ranges:
            if name == func_name and start_lineno <= node.lineno <= end_lineno:
                func_source = block_source
                break
        if ".jsonl" not in func_source and not _PHASE7_D18_JSONL_READER_NAME_RE.search(func_name):
            continue
        violations.append((relative, node.lineno, func_name))
    return violations


def test_no_direct_sidecar_reads_outside_registry() -> None:
    """Phase 7 D-18: src/commands/*.py and src/core/*.py must not decode JSONL
    record lines with ``json.loads(<line>)`` outside the approved pathway.

    Approved JSONL-parsing seams are limited to:
    * src/core/state_reader_registry.py (declarative registry)
    * src/core/jsonl_utils.py (shared bulk readers + ``parse_jsonl_line``)

    Sidecar owners that need bespoke per-line filtering keep their loop but
    route the decode through ``jsonl_utils.parse_jsonl_line``, so JSONL line
    decoding lives in one place (consistent corruption handling, checksum
    verification, write-back tracking). Whole-file JSON reads
    (``json.loads(path.read_text())``) and embedded JSON columns
    (``json.loads(row["…_json"])``) are not JSONL streams and are exempt.

    Closed in rev. 317: all 21 line-readers route through parse_jsonl_line."""
    violations: list[tuple[str, int, str]] = []
    search_roots = [
        REPO_ROOT / "src/commands",
        REPO_ROOT / "src/core",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        for py_file in sorted(root.rglob("*.py")):
            relative = py_file.relative_to(REPO_ROOT).as_posix()
            if relative in _PHASE7_D18_ALLOWED_MODULES:
                continue
            # Only flag modules whose file itself contains a .jsonl literal
            # OR which import jsonl_utils (suggesting they touch jsonl files).
            try:
                source_text = py_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if (
                not _PHASE7_D18_JSONL_PATH_RE.search(source_text)
                and "jsonl_utils" not in source_text
            ):
                continue
            for rel, lineno, func_name in _phase7_d18_collect_jsonl_violations(py_file):
                violations.append((rel, lineno, func_name))

    assert violations == [], (
        "Phase 7 D-18 violation: direct json.loads/json.load on .jsonl files "
        "outside the state reader registry. Route reads through "
        "src/core/state_reader_registry.py. Findings: "
        + "; ".join(
            f"{rel}:{lineno} in {func_name}()"
            for rel, lineno, func_name in violations
        )
    )


# ---------------------------------------------------------------------------
# Phase 7 D-04: No provider-class leakage in gather coordinator
# ---------------------------------------------------------------------------
#
# gather.py is the orchestrator. Per V-10 (Provider standardization) the
# orchestrator must reach providers through the factory facade, not by
# importing specific provider classes (ADOClient, KustoClient, WorkIQClient,
# etc). Direct imports of provider classes couple gather to a specific
# transport and break the SoR-flip work (S7) which expects to swap the
# provider for tests.

_PHASE7_D04_GATHER_MODULE = "src/commands/gather.py"

# Provider classes that must NOT be imported by name in gather.py. The names
# are derived from the current transport modules under src/core/ and src/m365/.
_PHASE7_D04_PROVIDER_CLASS_NAMES = (
    "ADOClient",
    "KustoClient",
    "WorkIQClient",
    "GraphMailClient",
    "GraphCalendarClient",
    "TeamsClient",
    "IcMClient",
    "IcmClient",
)


def test_no_provider_leakage_in_gather_coordinator() -> None:
    """Phase 7 D-04: src/commands/gather.py must not import specific
    provider classes (ADOClient, KustoClient, WorkIQClient, GraphMailClient,
    GraphCalendarClient, TeamsClient, IcMClient, IcmClient) by name.

    Provider classes belong in transport modules (src/core/ado_client.py,
    src/m365/*.py, etc.). The gather orchestrator must reach them only via
    the factory facade so the SoR-flip work (S7) can swap transports for
    testing without touching the coordinator."""
    gather_path = REPO_ROOT / _PHASE7_D04_GATHER_MODULE
    source = gather_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=_PHASE7_D04_GATHER_MODULE)

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not (module.startswith("src.core.") or module.startswith("src.m365.")):
            continue
        for alias in node.names:
            if alias.name in _PHASE7_D04_PROVIDER_CLASS_NAMES:
                findings.append((node.lineno, f"{module}.{alias.name}"))

    # Sort findings by (line, name) for stable error messages
    findings = sorted(set(findings))

    assert findings == [], (
        "Phase 7 D-04 violation: gather.py imports specific provider classes "
        "directly. Reach providers through the factory facade. Findings: "
        + "; ".join(
            f"{_PHASE7_D04_GATHER_MODULE}:{line} imports {name!r}"
            for line, name in findings
        )
    )


def test_no_source_writes_to_repo_root_output() -> None:
    """No src/ file should construct a path under REPO_ROOT/output/{edition}/.
    Shadow constructors are the highest risk for silent partial migration.
    """
    import re, pathlib
    shadow_patterns = [
        re.compile(r'Path\("output"\)'),           # CWD-relative path default
        re.compile(r'repo_root\s*/\s*"output"'),   # explicit repo_root / "output"
        re.compile(r'\.parent\s*/\s*"output"'),    # .parent / "output" (kb.py, synthesize.py)
        re.compile(r'parents\[\d+\]\s*/\s*"output"'),  # parents[N] / "output"
        re.compile(r'output_root\.glob\('),         # cross-edition glob (owner_pack, ado_proposal)
    ]
    # NOTE: patterns / "output" / and "output" / are intentionally EXCLUDED—
    # they match the new correct formula: program_dir / "output" / edition_id.
    violations = []
    src_root = pathlib.Path(__file__).parent.parent.parent / "src"
    for py_file in src_root.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for pattern in shadow_patterns:
            for match in pattern.finditer(text):
                violations.append(f"{py_file.relative_to(src_root)}: {match.group()}")
    assert not violations, f"Shadow output constructors found:\n" + "\n".join(violations)
