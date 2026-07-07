"""QG Validation Matrix — validates that every implemented quality gate is:
1. Defined in the codebase (quality_gates.py or confirm.py)
2. Covered by unit tests
3. Classified as forceable or hard-block

This contract test prevents silent gate regressions: adding a gate
without test coverage, or changing its forceable classification
without updating this registry.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_quality_gates_source() -> str:
    """Concatenate all source under the quality_gates package.

    quality_gates was a single module; D-09 split it into a package, so gate
    definitions are spread across ``src/core/quality_gates/*.py``. Reading the
    whole package keeps this contract robust to further cluster peels.
    """
    package_dir = REPO_ROOT / "src/core/quality_gates"
    return "\n".join(sorted(p.read_text(encoding="utf-8") for p in package_dir.glob("*.py")))


def _read_confirm_source() -> str:
    """Concatenate confirm.py and its confirm_stages package.

    The confirm decomposition (D-25) moved some gate definitions (e.g. QG-CE1's
    calibration gate) out of confirm.py into ``src/commands/confirm_stages/*.py``,
    so the gate-scan must include that package too.
    """
    parts = [(REPO_ROOT / "src/commands/confirm.py").read_text(encoding="utf-8")]
    stages_dir = REPO_ROOT / "src/commands/confirm_stages"
    parts.extend(sorted(p.read_text(encoding="utf-8") for p in stages_dir.glob("*.py")))
    return "\n".join(parts)


# ── Runtime QG registry ─────────────────────────────────────────────────────
# Each entry: (gate_id, forceable, tested_gate_id_in_test_file)
# forceable: True = can be overridden with --force; False = hard publish-block
# tested: gate ID string searched for in test_quality_gates.py or
#         test_commands_confirm.py

QG_REGISTRY: list[tuple[str, bool, str]] = [
    # Phase 1a gates
    ("QG-1", True, "QG-1"),    # Freshness gate
    ("QG-DM-1", False, "QG-DM-1"),  # Hash-chain integrity hard block
    ("QG-DM-4", False, "QG-DM-4"),  # Hardlock immutability hard block
    ("QG-DM-5", True, "QG-DM-5"),  # Gap-detection SLA advisory
    ("QG-DM-6", True, "QG-DM-6"),  # Candidate triage latency advisory
    ("QG-DM-7", True, "QG-DM-7"),  # Unresolved conflict budget advisory
    ("QG-DM-13", True, "QG-DM-13"),  # Claim freshness advisory
    ("QG-DM-10", True, "QG-DM-10"),  # Projection freshness advisory
    ("QG-9", True, "QG-9"),    # Overdue target dates
    ("QG-10", True, "QG-10"),  # Material-change narrative
    # Phase 1b gates
    ("QG-11", True, "QG-11"),  # Claim contradiction (forceable depends on L-level)
    ("QG-17", True, "QG-17"),  # Contradiction narrative
    ("QG-12", True, "QG-12"),  # Chronic high-risk escalation
    ("QG-13", False, "QG-13"), # Uncovered high-risk items
    ("QG-14", True, "QG-14"),  # High-risk next-best-action
    ("QG-15", True, "QG-15"),  # Open actions owner/due-date
    ("QG-16", True, "QG-16"),  # Milestone risk-link
    ("QG-19", True, "QG-19"),  # Cross-program dependency cascade
    # Phase 1c gates
    ("QG-4", False, "QG-4"),   # Ban-list validation
    ("QG-5", False, "QG-5"),   # Verbosity validation
    ("QG-6", False, "QG-6"),   # Manifest hash
    ("QG-8", False, "QG-8"),   # Missing risk levels (Needs Input)
    # Phase 2 gates
    ("QG-2", True, "QG-2"),    # Hygiene
    ("QG-3", True, "QG-3"),    # Review approval
    ("QG-7", True, "QG-7"),    # Archive index consistency
    # Email contract
    ("QG-18", True, "QG-18"),  # Outlook compatibility
    # Bridge gates
    ("QG-B1", True, "QG-B1"),  # Bridge section-roster
    ("QG-B2", True, "QG-B2"),  # Bridge scorecard-composition
    ("QG-B3", True, "QG-B3"),  # Bridge seeded-narrative revision
    # Claim-extraction calibration (renamed from QG-18)
    ("QG-CE1", True, "QG-CE1"),
    # Persona signal gate (persona governance spec)
    ("QG-P", False, "QG-P"),   # Persona signal compliance (hard block)
    # Context integrity gates (program-context-maturity §5)
    ("QG-CI-01", True, "QG-CI-01"),  # Stub placeholder WI IDs in milestones.yaml
    ("QG-CI-02", True, "QG-CI-02"),  # Informal OData filters in scorecards.yaml
    # Chart pipeline quality gates (charts spec R3)
    ("QG-20", True, "QG-20"),   # Chart freshness advisory
    ("QG-21", False, "QG-21"),  # Chart PNG size hard block
    ("QG-22", False, "QG-22"),  # Chart blocking freshness
    # Exec summary and ADO hygiene gates
    ("QG-23", True, "QG-23"),   # Exec summary staleness advisory
    ("QG-24", True, "QG-24"),   # Metric injection and ADO hygiene
    ("QG-25", True, "QG-25"),   # Email signal coverage advisory
    # Program Fact Store gates (signals spec §8.2)
    ("QG-SG-01", True, "QG-SG-01"),  # Source health gate — forceable bounded slice in confirm
    ("QG-SG-09", False, "QG-SG-09"),  # Contradiction gate — hard block on HIGH-confidence contradictions (FR-SG-35)
    ("QG-SG-20", False, "QG-SG-20"),  # State Drift Warning — hard block (INV-SG-12)
    # External dependency gate (WS-2)
    ("QG-26", True, "QG-26"),  # ExternalDependency state gate — forceable
    # AI budget gate (WS-5b) — renamed from QG-27 to avoid collision with truth/conflict gate (SD-17)
    ("QG-WS5B", True, "QG-WS5B"),  # AI per-run budget gate — forceable
    # KPI degradation gate (WS-1 PB-4)
    ("QG-28", True, "QG-28"),  # KPI degraded-query advisory gate — forceable
    # Newsletter-WorkIQ enrichment gates (spec §14.1, P4-4)
    ("QG-WIQ-1", False, "QG-WIQ-1"),  # Pending WorkIQ signal hard block (confirm)
    ("QG-WIQ-2", True, "QG-WIQ-2"),   # Evidence presence advisory (confirm, forceable)
    ("QG-WIQ-3", True, "QG-WIQ-3"),   # Per-source freshness advisory (confirm, forceable)
    ("QG-WIQ-4", False, "QG-WIQ-4"),  # WorkIQ run-cost info gate (report, never blocks)
    ("QG-WIQ-5", False, "QG-WIQ-5"),  # Transcript identifier doctor warning
    ("QG-WIQ-6", False, "QG-WIQ-6"),  # M365 signal recency doctor warning
    ("QG-WIQ-7", True, "QG-WIQ-7"),   # Blurb provenance gate (confirm, forceable)
    ("QG-WIQ-8", False, "QG-WIQ-8"),  # Transcript extraction block doctor warning
    ("QG-WIQ-9", False, "QG-WIQ-9"),  # workiq_latest divergence doctor warning
]


# CI-only gates implemented by contract tests rather than runtime GateEvaluation
# calls. Each entry: (gate_id, forceable, tested_gate_id_in_test_file).
CONTRACT_ONLY_QG_REGISTRY: list[tuple[str, bool, str]] = [
    ("QG-DM-2", False, "QG-DM-2"),
    ("QG-DM-3", False, "QG-DM-3"),
    ("QG-DM-8", False, "QG-DM-8"),
    ("QG-DM-9", False, "QG-DM-9"),   # Backfill batch acceptance (entity-resolution ≥ 90%)
    ("QG-DM-11", False, "QG-DM-11"),
    ("QG-DM-12", False, "QG-DM-12"),
]


def test_all_registered_qg_ids_exist_in_code() -> None:
    """Every runtime gate in the registry must be defined in quality_gates.py or confirm.py."""
    qg_source = _read_quality_gates_source()
    confirm_source = _read_confirm_source()
    combined = qg_source + "\n" + confirm_source

    missing: list[str] = []
    for gate_id, _, _ in QG_REGISTRY:
        if f'"{gate_id}"' not in combined:
            missing.append(gate_id)

    assert missing == [], (
        f"QG registry contains gates not found in code: {missing}"
    )


def test_all_qg_gates_in_code_are_registered() -> None:
    """Every QG gate defined in quality_gates.py must appear in the registry.
    Prevents adding gates without registering them here."""
    qg_source = _read_quality_gates_source()

    # Extract gate IDs from both positional and keyword-arg GateEvaluation calls:
    #   GateEvaluation("QG-N", ...)      — positional first arg
    #   GateEvaluation(gate_id="QG-N", ...)  — keyword arg
    positional = re.compile(r'GateEvaluation\(\s*"(QG-[^"]+)"')
    keyword = re.compile(r'gate_id\s*=\s*"(QG-[^"]+)"')
    code_gates = set(positional.findall(qg_source)) | set(keyword.findall(qg_source))

    registered_gates = {gate_id for gate_id, _, _ in QG_REGISTRY}

    unregistered = sorted(code_gates - registered_gates)
    assert unregistered == [], (
        f"QG gates in code but not in registry: {unregistered}"
    )


def test_all_registered_qg_gates_have_test_coverage() -> None:
    """Every runtime or contract-only gate in the registry must be tested in the test suite."""
    test_files = [
        REPO_ROOT / "tests/unit/test_quality_gates.py",
        REPO_ROOT / "tests/unit/test_quality_gate_editorial.py",
        REPO_ROOT / "tests/unit/test_commands_confirm.py",
        REPO_ROOT / "tests/unit/test_chart_quality_gates.py",
        REPO_ROOT / "tests/unit/test_persona_checker.py",
        REPO_ROOT / "tests/unit/test_qg26_external_dependency.py",
        REPO_ROOT / "tests/contracts/test_ws5b_ai_telemetry_contract.py",
        REPO_ROOT / "tests/contracts/test_ws1_kpi_degradation_gate_contract.py",
        REPO_ROOT / "tests/contracts/test_dm_ci_gate_contract_registry.py",
        REPO_ROOT / "tests/golden/test_ledger_projection.py",
        REPO_ROOT / "tests/golden/test_knowledge_context.py",
        REPO_ROOT / "tests/unit/test_quality_gates_workiq.py",
    ]
    test_sources = []
    for tf in test_files:
        if tf.exists():
            test_sources.append(tf.read_text(encoding="utf-8"))
    combined_tests = "\n".join(test_sources)

    untested: list[str] = []
    for gate_id, _, search_term in [*QG_REGISTRY, *CONTRACT_ONLY_QG_REGISTRY]:
        if search_term not in combined_tests:
            untested.append(gate_id)

    assert untested == [], (
        f"QG gates without test coverage: {untested}"
    )


def test_all_contract_only_qg_ids_are_listed_in_spec() -> None:
    """CI-only gates must still appear in either the canonical tech spec or the archived data-model spec."""
    # data-model.md was archived to .archive/specs/ after incorporation into vertex-tech-spec.md §9.17.
    # Search both locations so the test remains green on repos that have the archive.
    candidates = [
        REPO_ROOT / ".archive" / "specs" / "data-model.md",
        REPO_ROOT / "specs" / "vertex-tech-spec.md",
    ]
    spec_source = ""
    for candidate in candidates:
        if candidate.exists():
            spec_source += candidate.read_text(encoding="utf-8")

    missing: list[str] = []
    for gate_id, _, _ in CONTRACT_ONLY_QG_REGISTRY:
        if gate_id not in spec_source:
            missing.append(gate_id)

    assert missing == [], (
        f"Contract-only QG registry contains gates not found in any spec source "
        f"(.archive/specs/data-model.md or specs/vertex-tech-spec.md): {missing}"
    )


def _gate_call_text(source: str, gate_id_pos: int) -> str:
    """
    Given the position of a gate_id string in source, find the enclosing
    GateEvaluation(...) call text using paren-depth tracking.
    Returns the full call text or '' if not found.
    """
    pre = source[:gate_id_pos]
    call_start = pre.rfind("GateEvaluation")
    if call_start == -1:
        return ""
    depth = 0
    started = False
    for i in range(call_start, len(source)):
        ch = source[i]
        if ch == "(":
            depth += 1
            started = True
        elif ch == ")":
            depth -= 1
            if started and depth == 0:
                return source[call_start : i + 1]
    return ""


def test_forceable_classification_matches_code() -> None:
    """The forceable flag in the registry must match the code's forceable kwarg.
    Checks the GateEvaluation calls for each gate.  Uses paren-depth tracking
    to handle multi-line calls where message strings may contain ')' characters."""
    qg_source = _read_quality_gates_source()
    confirm_source = _read_confirm_source()
    combined_source = qg_source + "\n" + confirm_source

    code_forceable: dict[str, bool] = {}
    for gate_id in {gid for gid, _, _ in QG_REGISTRY}:
        escaped = re.escape(gate_id)
        pat = re.compile(rf'"{escaped}"')
        is_forceable = False
        for match in pat.finditer(combined_source):
            call_text = _gate_call_text(combined_source, match.start())
            if not call_text or "GateEvaluation" not in call_text:
                continue
            if "forceable=True" in call_text or "forceable=forceable" in call_text:
                is_forceable = True
        code_forceable[gate_id] = is_forceable

    mismatches: list[str] = []
    for gate_id, expected_forceable, _ in QG_REGISTRY:
        if gate_id not in code_forceable:
            continue  # Gate may be in confirm.py; skip
        code_is_forceable = code_forceable[gate_id]
        if code_is_forceable != expected_forceable:
            mismatches.append(
                f"{gate_id}: registry says forceable={expected_forceable}, "
                f"code says forceable={code_is_forceable}"
            )

    assert mismatches == [], (
        f"QG forceable classification mismatches: {mismatches}"
    )