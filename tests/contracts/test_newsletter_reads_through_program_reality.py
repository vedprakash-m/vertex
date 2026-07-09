from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_STAGE_PATH = REPO_ROOT / "src" / "core" / "stages" / "risk_stage.py"
MILESTONE_STAGE_PATH = REPO_ROOT / "src" / "core" / "stages" / "milestone_stage.py"
LOOKBACK_PATH = REPO_ROOT / "src" / "commands" / "report_lookback.py"


def _load_tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class_method(tree: ast.AST, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method_name:
                    return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def _top_level_function(tree: ast.AST, function_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return node
    raise AssertionError(f"{function_name} not found")


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _call_constants(method: ast.FunctionDef, callee: str) -> list[str]:
    values: list[str] = []
    for node in ast.walk(method):
        if isinstance(node, ast.Call) and _called_name(node) == callee:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    values.append(arg.value)
    return values


def _find_nonlegacy_branch(method: ast.FunctionDef) -> ast.If:
    for node in ast.walk(method):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        if not isinstance(test.left, ast.Name) or test.left.id != "sor_mode":
            continue
        if len(test.ops) != 1 or not isinstance(test.ops[0], ast.NotEq):
            continue
        if len(test.comparators) != 1:
            continue
        comparator = test.comparators[0]
        if isinstance(comparator, ast.Constant) and comparator.value == "legacy":
            return node
    raise AssertionError("sor_mode != 'legacy' branch not found")


def _called_functions(nodes: list[ast.stmt]) -> set[str]:
    called: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = _called_name(child)
                if name is not None:
                    called.add(name)
    return called


def _call_kwarg_constant(call: ast.Call, kwarg_name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == kwarg_name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _find_call(method: ast.FunctionDef, callee: str) -> ast.Call:
    for node in ast.walk(method):
        if isinstance(node, ast.Call) and _called_name(node) == callee:
            return node
    raise AssertionError(f"no call to {callee!r} found")


def test_risk_stage_so_r_gate_uses_judgment_and_keeps_legacy_branch() -> None:
    """Track B.5 (fix-data-flow.md §6.2b): RiskStage now routes through the
    shared `sor_gated_family_load` helper rather than a hand-written
    inline `if sor_mode != "legacy":` branch (that shape is still used by
    milestone_stage.py, tested separately below, and remains valid — Track
    B.5 does not require every existing family to be refactored, only that
    at least one uses the extracted helper). This durability guardrail
    updates to assert the new call shape: `family="judgment"` (not the
    invented `"risk"` string PS-15/PS-16 originally assumed, per v1.5's
    correction), with legacy/reality accessors still present as keyword
    arguments — this is what actually prevents a future PR from silently
    reverting to an unconditional `load_program_facts()` call, since that
    would require deleting the `sor_gated_family_load` call entirely, not
    just editing a branch.
    """
    method = _class_method(_load_tree(RISK_STAGE_PATH), "RiskStage", "execute")

    call = _find_call(method, "sor_gated_family_load")
    assert _call_kwarg_constant(call, "family") == "judgment"
    assert _call_kwarg_constant(call, "cross_check_label") == "risk"
    called_in_method = _called_functions([method])
    assert "_load_current_risks" in called_in_method, (
        "RiskStage.execute must still reference the legacy loader (as the "
        "sor_gated_family_load legacy_loader= argument) -- its presence is what "
        "keeps a legacy-mode rollback path possible."
    )


def test_milestone_stage_baseline_and_dependency_overlay_remain_so_r_gated() -> None:
    milestone_tree = _load_tree(MILESTONE_STAGE_PATH)
    execute = _class_method(milestone_tree, "MilestoneStage", "execute")

    assert "workitem.state" in _call_constants(execute, "resolve_family_sor_mode")
    nonlegacy = _find_nonlegacy_branch(execute)
    assert "_load_milestones_via_reality" in _called_functions(nonlegacy.body)
    assert "_load_current_milestones" in _called_functions(nonlegacy.orelse)
    assert "_load_current_dependencies" in _called_functions(nonlegacy.orelse)

    helper = next(
        node for node in milestone_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_load_milestones_via_reality"
    )
    dependency_calls = [
        child
        for child in ast.walk(helper)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "dependencies"
    ]
    assert dependency_calls, "milestone reality helper must read dependencies via ProgramReality.dependencies()"


def test_lookback_assumption_loader_uses_judgment_so_r_gate() -> None:
    helper = _top_level_function(_load_tree(LOOKBACK_PATH), "_load_lookback_assumptions")

    call = _find_call(helper, "sor_gated_family_load")
    assert _call_kwarg_constant(call, "family") == "judgment"
    assert _call_kwarg_constant(call, "cross_check_label") == "assumption"
    called_in_helper = _called_functions([helper])
    assert "load_current_assumptions" in called_in_helper, (
        "_load_lookback_assumptions must still reference the legacy loader "
        "so the audited rollback path remains available."
    )
