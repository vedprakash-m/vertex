from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCTOR_PATH = REPO_ROOT / "src" / "commands" / "doctor.py"

_EXPECTED_BRANCH_RUNNERS = {
    "check_auth": "_run_auth_doctor",
    "operator_gates": "_run_operator_gates_doctor",
    "platform_readiness": "_run_platform_readiness_doctor",
    "kb": "_run_kb_doctor",
    "context": "_run_context_doctor",
    "ids": "_run_id_doctor",
    "cadence": "_run_cadence_doctor",
    "channels": "_run_channel_doctor",
    "privacy": "_run_privacy_doctor",
    "kusto": "_run_kusto_doctor",
    "milestones": "_run_milestone_doctor",
    "dependencies": "_run_dependency_doctor",
    "actions": "_run_action_doctor",
    "risks": "_run_risk_doctor",
    "escalations": "_run_escalation_doctor",
    "decisions": "_run_decision_doctor",
    "assumptions": "_run_assumption_doctor",
    "readiness": "_run_readiness_doctor",
    "semantic_index": "_run_semantic_index_doctor",
    "personas": "_run_persona_doctor",
    "metric_bindings": "_run_metric_binding_doctor",
    "consistency": "DoctorReport",
    "checkpoints": "_run_checkpoint_doctor",
    "storage": "_run_storage_doctor",
    "watch_sources": "_run_watch_source_doctor",
    "catchup_log": "_run_catchup_log_doctor",
    "nudge": "DoctorReport",
    "circuit_breakers": "_run_circuit_breaker_doctor",
    "charts": "_run_charts_doctor",
    "flip_status": "_run_flip_status_doctor",
    "flip_parity": "_run_flip_parity_doctor",
    "source_waivers": "_run_source_waiver_doctor",
    "fact_parity": "_run_fact_parity_doctor",
    "confirm_readiness": "_run_confirm_readiness_doctor",
    "adapter_cert": "_run_adapter_cert_doctor",
    "sharepoint": "_run_sharepoint_doctor",
}


def test_run_doctor_mutually_exclusive_check_registry_is_complete() -> None:
    run_doctor = _load_run_doctor()
    exclusivity_tuple = _find_exclusivity_tuple(run_doctor)

    assert set(exclusivity_tuple) == set(_EXPECTED_BRANCH_RUNNERS)
    assert len(exclusivity_tuple) == len(set(exclusivity_tuple)) == len(_EXPECTED_BRANCH_RUNNERS)


def test_run_doctor_branch_registry_routes_every_check_flag() -> None:
    run_doctor = _load_run_doctor()
    branch_mapping = _extract_branch_mapping(run_doctor)

    assert branch_mapping == _EXPECTED_BRANCH_RUNNERS


def _load_run_doctor() -> ast.FunctionDef:
    tree = ast.parse(DOCTOR_PATH.read_text(encoding="utf-8"), filename=str(DOCTOR_PATH))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_doctor":
            return node
    raise AssertionError("run_doctor() not found in src/commands/doctor.py")


def _find_exclusivity_tuple(run_doctor: ast.FunctionDef) -> tuple[str, ...]:
    for statement in run_doctor.body:
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        if not isinstance(test, ast.Compare):
            continue
        if not isinstance(test.left, ast.Call):
            continue
        call = test.left
        if not isinstance(call.func, ast.Name) or call.func.id != "sum":
            continue
        if len(call.args) != 1 or not isinstance(call.args[0], ast.GeneratorExp):
            continue
        generator = call.args[0]
        tuple_arg = generator.generators[0].iter
        if not isinstance(tuple_arg, ast.Tuple):
            continue
        return tuple(_name_of(element) for element in tuple_arg.elts)
    raise AssertionError("Could not find the mutually-exclusive check tuple in run_doctor()")


def _extract_branch_mapping(run_doctor: ast.FunctionDef) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for statement in run_doctor.body:
        if not isinstance(statement, ast.If):
            continue
        if not isinstance(statement.test, ast.Name):
            continue
        flag_name = statement.test.id
        if flag_name not in _EXPECTED_BRANCH_RUNNERS:
            continue
        return_stmt = next(
            (body_stmt for body_stmt in reversed(statement.body) if isinstance(body_stmt, ast.Return)),
            None,
        )
        if return_stmt is None:
            raise AssertionError(f"Doctor branch '{flag_name}' has no return statement")
        if not isinstance(return_stmt.value, ast.Call):
            raise AssertionError(f"Doctor branch '{flag_name}' does not return a function call")
        mapping[flag_name] = _name_of(return_stmt.value.func)
    return mapping


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    raise AssertionError(f"Unsupported AST node for name extraction: {ast.dump(node)}")
