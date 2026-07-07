from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMAND_ROOT = REPO_ROOT / "src" / "commands"


_TRACE_CONTEXT_MODULES = {
    "backfill.py",
    "confirm.py",
    "kb.py",
    "onboard.py",
    "review_full.py",
    "summarize.py",
    "synthesize.py",
}


def _module_ast(file_name: str) -> ast.AST:
    path = COMMAND_ROOT / file_name
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _confirm_package_ast() -> ast.Module:
    """Treat confirm.py + its confirm_stages package as one trace-context unit.

    The confirm decomposition (D-25) moved the AITraceContext budget builder into
    ``confirm_stages/learning_distiller.py`` while the handoff call stayed in
    confirm.py. Parse each file independently (so per-file ``from __future__``
    imports stay legal) and merge their bodies into a synthetic module.
    """
    body: list[ast.stmt] = []
    sources = [COMMAND_ROOT / "confirm.py", *sorted((COMMAND_ROOT / "confirm_stages").glob("*.py"))]
    for path in sources:
        body.extend(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body)
    return ast.Module(body=body, type_ignores=[])


def _constant_string_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dict_contains_run_budget(node: ast.Dict) -> bool:
    return any(_constant_string_key(key) == "run_budget_usd" for key in node.keys)


def _has_trace_context_builder_with_budget(module: ast.AST) -> bool:
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "AITraceContext":
            continue
        for keyword in node.keywords:
            if keyword.arg != "metadata" or not isinstance(keyword.value, ast.Dict):
                continue
            if _dict_contains_run_budget(keyword.value):
                return True
    return False


def _has_trace_context_handoff_call(module: ast.AST) -> bool:
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if any(keyword.arg == "trace_context" for keyword in node.keywords):
            return True
    return False


def test_command_modules_preserve_trace_context_contract() -> None:
    missing: list[str] = []
    for file_name in sorted(_TRACE_CONTEXT_MODULES):
        module = _confirm_package_ast() if file_name == "confirm.py" else _module_ast(file_name)
        if not _has_trace_context_builder_with_budget(module):
            missing.append(f"{file_name}:missing_ai_trace_context_budget")
        if not _has_trace_context_handoff_call(module):
            missing.append(f"{file_name}:missing_trace_context_handoff")

    assert missing == []


def test_report_ai_client_accepts_and_forwards_trace_context() -> None:
    # WI-6.2: _create_ai_client extracted to report_pipeline/assemble_stage.py
    assemble_stage = COMMAND_ROOT / "report_pipeline" / "assemble_stage.py"
    module = ast.parse(assemble_stage.read_text(encoding="utf-8"), filename=str(assemble_stage))
    create_ai_client = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_create_ai_client"
    )

    keyword_only_arg_names = {arg.arg for arg in create_ai_client.args.kwonlyargs}
    assert "trace_context" in keyword_only_arg_names

    ai_client_calls = [
        node
        for node in ast.walk(create_ai_client)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AIClient"
    ]
    assert ai_client_calls, "Expected _create_ai_client to instantiate AIClient"
    assert any(any(keyword.arg == "trace_context" for keyword in call.keywords) for call in ai_client_calls)
