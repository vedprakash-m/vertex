from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# Phase 0 reviewed exception: the original 10,010 freeze started red once gather.py
# reached 10,416 LOC. Rev. 12 extracted the transitional DiscoveryService slice and
# ratcheted the freeze down to the new live size. Phase 3 must keep driving the
# ceiling downward per specs/debt.md §21.3.
# WS-17 (2026-06-09): +19 for the run_telemetry wire-in (see
# tests/contracts/test_architecture_fitness.py::LINE_BUDGETS).
_GATHER_LOC_BUDGET = 5436  # +11 (2026-07-15): ADF-W1.4 remainder -- overall WorkIQ phase wall-clock budget threading; +5 (2026-07-15): per-plan timeout_seconds=AgencyBridge.WORKIQ_TIMEOUT cap; +213 (2026-07-16, specs/armada.md): gather-run manifest lifecycle wrapper, discovery CLI flag threading, and gather_run_id stamping (D-13 rule 4); +36 (2026-07-21, specs/armada.md D-19/AG-2.12): completeness-oracle reconciliation (resolve_oracle_result wiring + --source-export CLI option)
# +5 (2026-07-15, ADF-W2.10 P7): risk/milestone/action status contradiction wiring (load_current_risk_entries/load_current_action_items + risks/milestones/actions kwargs at build_contradiction_packets call site).
# Phase 6 reviewed exception (2026-06-07): flip-status + flip-parity sub-checks
# added per specs/debt.md §11 Phase 6 Step 1. Branch extraction scheduled after
# parity-check command is proven.
# +4 (rev. 134): mypy narrowing guards for resolved-edition and issue_number.
# +13 (rev. 314): --source-waivers sub-check per D-32 materialization; branch
# extraction into doctor_checks/source_waiver_checks.py is the next honest
# ratchet.
# +12 (2026-06-21): --nudge sub-check per .archive/specs/fix-nudge.md §24.8.
# +20 (2026-06-23): --rev-health/--rev-program sub-check per specs/program-context-intelligence.md §5.13 (FR-PCI-12).
# +18 (2026-07-14, ADF-W5.10): --schedule-health sub-check wiring
# src/core/schedule_health.py (the primitive) into doctor's flag dispatch.
# +11 (2026-07-21): see tests/contracts/test_architecture_fitness.py's matching
# LINE_BUDGETS entry for what changed (schedule_health summary payload,
# --source-waivers evaluation-order fix, prefetch_enabled/kusto_enabled flags).
_DOCTOR_LOC_BUDGET = 1692
_AI_ROUTER_ALLOWED_FILES = frozenset(
    {
        Path("src/ai/client.py"),
        Path("src/ai/deployment_fallback.py"),
        Path("src/ai/provider.py"),
        Path("src/ai/request_router.py"),
    }
)
_YAML_DEF_ALLOWED_FILES = frozenset(
    {
        Path("src/core/yaml_utils.py"),
        Path("src/core/readiness_engine.py"),
    }
)
_ZONE_A_PROGRAM_LITERAL_ALLOWED_FILES = frozenset({Path("src/core/charts/deployment_velocity.py")})


@dataclass(frozen=True, slots=True)
class RefactorStatusMetric:
    name: str
    current: int | str
    budget: int | str | None
    status: str


@dataclass(frozen=True, slots=True)
class RefactorStatusReport:
    generated_on: str
    metrics: tuple[RefactorStatusMetric, ...]

    @property
    def failures(self) -> int:
        return sum(1 for metric in self.metrics if metric.status == "fail")


def build_refactor_status_report(*, repo_root: Path) -> RefactorStatusReport:
    gather_path = repo_root / "src" / "commands" / "gather.py"
    doctor_path = repo_root / "src" / "commands" / "doctor.py"
    gather_lines = _count_lines(gather_path)
    doctor_lines = _count_lines(doctor_path)
    private_yaml_defs = _count_private_yaml_defs(repo_root)
    zone_a_program_literals = _count_zone_a_program_literals(repo_root)
    ai_call_sites = _count_ai_call_sites_outside_router(repo_root)
    metrics = (
        RefactorStatusMetric(
            name="gather.py LOC",
            current=gather_lines,
            budget=_GATHER_LOC_BUDGET,
            status=_budget_status(gather_lines, _GATHER_LOC_BUDGET),
        ),
        RefactorStatusMetric(
            name="doctor.py LOC",
            current=doctor_lines,
            budget=_DOCTOR_LOC_BUDGET,
            status=_budget_status(doctor_lines, _DOCTOR_LOC_BUDGET),
        ),
        RefactorStatusMetric(
            name="private _load_yaml defs",
            current=private_yaml_defs,
            budget=0,
            status=_budget_status(private_yaml_defs, 0),
        ),
        RefactorStatusMetric(
            name="program literals in Zone A",
            current=zone_a_program_literals,
            budget=0,
            status=_budget_status(zone_a_program_literals, 0),
        ),
        RefactorStatusMetric(
            name="chat.completions outside router",
            current=ai_call_sites,
            budget=0,
            status=_budget_status(ai_call_sites, 0),
        ),
        RefactorStatusMetric(
            name="shadow-write emission rate",
            current=_shadow_write_emission_state(repo_root),
            budget=None,
            status="info",
        ),
    )
    return RefactorStatusReport(
        generated_on=datetime.now(timezone.utc).date().isoformat(),
        metrics=metrics,
    )


def render_refactor_status_output(report: RefactorStatusReport, *, format: str) -> str:
    if format == "human":
        return _render_human(report)
    if format == "json":
        import json

        return json.dumps(
            {
                "generated_on": report.generated_on,
                "failures": report.failures,
                "metrics": [
                    {
                        "name": metric.name,
                        "current": metric.current,
                        "budget": metric.budget,
                        "status": metric.status,
                    }
                    for metric in report.metrics
                ],
            },
            indent=2,
        )
    if format == "csv":
        import csv
        from io import StringIO

        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("metric", "current", "budget", "status"))
        for metric in report.metrics:
            writer.writerow((metric.name, metric.current, metric.budget if metric.budget is not None else "", metric.status))
        return buffer.getvalue()
    raise ValueError(f"Unsupported format '{format}'.")


def _render_human(report: RefactorStatusReport) -> str:
    metric_width = max(len("Metric"), *(len(metric.name) for metric in report.metrics))
    current_values = tuple(_display_value(metric.current) for metric in report.metrics)
    current_width = max(len("Current"), *(len(value) for value in current_values))
    budget_values = tuple(_display_value(metric.budget) for metric in report.metrics)
    budget_width = max(len("Budget"), *(len(value) for value in budget_values))
    divider = "-" * (metric_width + current_width + budget_width + 16)
    lines = [
        divider,
        f"Debt Remediation Progress ({report.generated_on})",
        divider,
        f"{'Metric':<{metric_width}}  {'Current':>{current_width}}  {'Budget':>{budget_width}}  Status",
    ]
    for metric, current_value, budget_value in zip(report.metrics, current_values, budget_values):
        lines.append(
            f"{metric.name:<{metric_width}}  {current_value:>{current_width}}  {budget_value:>{budget_width}}  {metric.status.upper()}"
        )
    return "\n".join(lines)


def _display_value(value: int | str | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return f"{value:,}"
    return value


def _budget_status(current: int, budget: int) -> str:
    return "ok" if current <= budget else "fail"


def _count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def _count_private_yaml_defs(repo_root: Path) -> int:
    count = 0
    for path in _python_files(repo_root / "src" / "core"):
        if path.relative_to(repo_root) in _YAML_DEF_ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"_load_yaml", "_load_yaml_mapping"}:
                count += 1
    return count


def _count_zone_a_program_literals(repo_root: Path) -> int:
    """Count hardcoded program-name string literals in Zone A (src/core/).

    The exact set of names to guard against is deployment-specific; this
    function checks for any string that matches patterns typical of internal
    program identifiers.  New programs are added to the allowlist via
    _ZONE_A_PROGRAM_LITERAL_ALLOWED_FILES, not by expanding this check.
    """
    # Names that must never be hardcoded in generic Zone A code.
    # Use lowercase comparison; add new names here if they slip in.
    _GUARDED_LITERALS: frozenset[str] = frozenset({
        "acme", "fabrikam", "contoso", "northwind", "adventure", "wingtip",
    })
    count = 0
    for path in _python_files(repo_root / "src" / "core"):
        if path.relative_to(repo_root) in _ZONE_A_PROGRAM_LITERAL_ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstring_nodes = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in docstring_nodes:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.lower()
                if any(name in val for name in _GUARDED_LITERALS):
                    count += 1
    return count


def _count_ai_call_sites_outside_router(repo_root: Path) -> int:
    count = 0
    for path in _python_files(repo_root / "src"):
        relative_path = path.relative_to(repo_root)
        if relative_path in _AI_ROUTER_ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_direct_ai_call(node):
                count += 1
    return count


def _shadow_write_emission_state(repo_root: Path) -> str:
    gather_path = repo_root / "src" / "commands" / "gather.py"
    tree = ast.parse(gather_path.read_text(encoding="utf-8"), filename=str(gather_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _attribute_chain(node.func) == ("shadow_write_plane1_snapshot",):
            return "active"
    return "inactive"


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(parent, "body", ())
        if not body:
            continue
        first_statement = body[0]
        if not isinstance(first_statement, ast.Expr):
            continue
        value = first_statement.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            nodes.add(id(value))
    return nodes


def _is_direct_ai_call(node: ast.Call) -> bool:
    chain = _attribute_chain(node.func)
    if chain == ("structured",):
        return True
    return chain[-3:] == ("chat", "completions", "create")


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))
