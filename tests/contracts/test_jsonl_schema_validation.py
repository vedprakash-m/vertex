"""Contract: every primary JSONL sidecar loader must validate row field presence.

This contract ensures the six primary sidecar loaders (claims, trajectory,
actions, AI proposals, edit patterns, risk register) all enforce field
presence on each decoded row. Loaders may either call the shared
``validate_jsonl_row`` helper from ``src.core.jsonl_utils`` or perform
equivalent inline strict validation (e.g. explicit ``if "x" not in record``
checks, dict-subscript key access that raises ``KeyError``/``TypeError``,
or calls to ``_required_*`` / ``_require_*`` helpers that raise on missing
fields).

If a new primary sidecar is added to Vertex, the
``PRIMARY_LOADER_TARGETS`` table must be extended to cover it.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from src.core import jsonl_utils

REPO_ROOT = Path(__file__).resolve().parents[2]


# Each entry: (module_path, loader_callable, loader_description, validation_strategy).
#   validation_strategy is one of:
#     - "call":   loader body must call validate_jsonl_row (or something it itself calls)
#     - "any":    loader body has *some* strict presence validation (call, inline check,
#                 or _required_*/_require_* helper invocation)
PRIMARY_LOADER_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    (
        "src/core/claim_tracker.py",
        "load_claim_entries",
        "claim log entries loader",
        "any",
    ),
    (
        "src/core/claim_tracker.py",
        "load_decision_asks",
        "decision asks loader",
        "any",
    ),
    (
        "src/core/action_tracker.py",
        "load_actions",
        "action log entries loader",
        "any",
    ),
    (
        "src/core/ai_proposal_store.py",
        "load_ai_proposals",
        "AI proposal log loader",
        "any",
    ),
    (
        "src/ai/edit_learner.py",
        "read_edit_patterns",
        "edit pattern log reader",
        "any",
    ),
    (
        "src/core/risk_register_engine.py",
        "load_risk_register",
        "risk register loader (YAML, inline strict validation)",
        "any",
    ),
)


# Helper-name patterns that we treat as "equivalent strict validation".
_REQUIRED_FIELD_HELPER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brequired_(?:string|int|float|date|datetime|bool|field)\b"),
    re.compile(r"\brequire_[a-z_]+(?:_string|_int|_float|_date|_datetime|_bool|_field)\b"),
    re.compile(r"\b_?required_string\b"),
    re.compile(r"\b_?required_int\b"),
    re.compile(r"\b_?required_field\b"),
)


# AST node patterns considered "strict inline validation": a subscript on a
# `record`/`row`/raw dict that is not guarded by a presence check upstream.
_INLINE_PRESENCE_CHECK_PATTERNS: tuple[str, ...] = (
    'if "',
    "if '",
    'if "id" not in',
    "if 'id' not in",
    "if not isinstance(record",
    "if not isinstance(raw_record",
    "if not isinstance(raw_entry",
    "if not isinstance(row",
    'record["',
    "record['",
    "raw_record[",
    "raw_entry[",
    "row[",
    "if not record:",
    "if not raw_record:",
    "if not raw_entry:",
)


def _load_module_ast(relative_module_path: str) -> ast.Module:
    module_path = REPO_ROOT / relative_module_path
    source = module_path.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(module_path))


def _find_function_node(module_ast: ast.Module, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(module_ast):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise AssertionError(f"Function {function_name!r} not found in module AST")


def _function_calls_names(function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return all direct call names found inside the function body."""
    names: set[str] = set()
    for node in ast.walk(function_node):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _function_source_text(function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.unparse(function_node)


def _has_validate_jsonl_row_call(function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A loader counts as 'calling validate_jsonl_row' if its body — or any
    local module-level helper it directly calls — invokes validate_jsonl_row.
    Method calls on external objects (e.g. ``path.read_text``) are NOT
    recursed, since they refer to stdlib/Pathlib calls, not local helpers."""
    direct = _function_calls_names(function_node)
    if "validate_jsonl_row" in direct:
        return True
    module = getattr(function_node, "_module", None)
    if module is None:
        return False
    # Only recurse into functions defined in the same module; never into
    # stdlib/imported names like ``read_text``, ``load``, ``get`` etc.
    module_function_names = {
        node.name
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for helper_name in direct:
        if helper_name not in module_function_names:
            continue
        if helper_name == function_node.name:
            continue
        try:
            helper_node = _find_function_node(module, helper_name)
        except AssertionError:
            continue
        if "validate_jsonl_row" in _function_calls_names(helper_node):
            return True
    return False


def _has_inline_strict_validation(function_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if the function body contains patterns that count as
    equivalent inline strict validation."""
    text = _function_source_text(function_node)
    for pattern in _REQUIRED_FIELD_HELPER_PATTERNS:
        if pattern.search(text):
            return True
    for pattern in _INLINE_PRESENCE_CHECK_PATTERNS:
        if pattern in text:
            return True
    # Detect raise ... from KeyError/TypeError on record.get("foo") patterns
    if re.search(r'raise\s+\w*Error\([^)]*record\.get\(', text):
        return True
    if re.search(r"if\s+[\w.]+\s+is\s+None\s+.*raise", text, flags=re.DOTALL):
        return True
    return False


def _attach_module_to_functions(module_ast: ast.Module) -> None:
    """Stash the module on each function node so the helper-resolution code
    can find sibling functions."""
    for node in ast.walk(module_ast):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            setattr(node, "_module", module_ast)


def _load_resolved_callable(module_path: str, function_name: str) -> Callable[..., object]:
    """Import the module and return the named function object for AST cross-check."""
    import importlib

    parts = module_path[:-3].split("/")
    module_name = ".".join(parts)
    module = importlib.import_module(module_name)
    return getattr(module, function_name)


def test_jsonl_utils_exposes_validate_jsonl_row() -> None:
    """Sanity: the helper must exist on src.core.jsonl_utils."""
    assert hasattr(jsonl_utils, "validate_jsonl_row"), (
        "src.core.jsonl_utils must expose validate_jsonl_row — the contract below "
        "depends on it."
    )
    assert callable(jsonl_utils.validate_jsonl_row)


def test_validate_jsonl_row_signature() -> None:
    """Sanity: the helper's signature matches the contract spec."""
    signature = inspect.signature(jsonl_utils.validate_jsonl_row)
    params = list(signature.parameters)
    assert params[:3] == ["row", "required_fields", "field_name"], (
        f"validate_jsonl_row signature is {params!r}; expected (row, required_fields, field_name=...)."
    )


@pytest.mark.parametrize(
    ("relative_module_path", "function_name", "loader_description", "strategy"),
    PRIMARY_LOADER_TARGETS,
    ids=[f"{module.split('/')[-1]}::{name}" for module, name, _desc, _strategy in PRIMARY_LOADER_TARGETS],
)
def test_primary_jsonl_loader_validates_row_fields(
    relative_module_path: str,
    function_name: str,
    loader_description: str,
    strategy: str,
) -> None:
    """Each primary sidecar loader must enforce field-presence validation."""
    module_ast = _load_module_ast(relative_module_path)
    _attach_module_to_functions(module_ast)
    function_node = _find_function_node(module_ast, function_name)

    # Cross-check: the named function is actually importable.
    _load_resolved_callable(relative_module_path, function_name)

    call_validates = _has_validate_jsonl_row_call(function_node)
    inline_validates = _has_inline_strict_validation(function_node)

    if strategy == "call":
        assert call_validates, (
            f"{loader_description} ({relative_module_path}::{function_name}) must call "
            "validate_jsonl_row on each row it loads."
        )
    elif strategy == "any":
        assert call_validates or inline_validates, (
            f"{loader_description} ({relative_module_path}::{function_name}) must enforce "
            "field presence on each decoded row — either by calling validate_jsonl_row "
            "or by equivalent inline strict validation. Found neither."
        )
    else:  # pragma: no cover — defensive
        raise AssertionError(f"Unknown strategy {strategy!r}")


def test_all_six_primary_sidecars_are_covered() -> None:
    """Regression guard: PRIMARY_LOADER_TARGETS must list all six primary sidecars."""
    assert len(PRIMARY_LOADER_TARGETS) == 6, (
        f"PRIMARY_LOADER_TARGETS must list all 6 primary sidecar loaders; "
        f"found {len(PRIMARY_LOADER_TARGETS)}."
    )
    loader_names = {(module, name) for module, name, _desc, _strategy in PRIMARY_LOADER_TARGETS}
    expected = {
        ("src/core/claim_tracker.py", "load_claim_entries"),
        ("src/core/claim_tracker.py", "load_decision_asks"),
        ("src/core/action_tracker.py", "load_actions"),
        ("src/core/ai_proposal_store.py", "load_ai_proposals"),
        ("src/ai/edit_learner.py", "read_edit_patterns"),
        ("src/core/risk_register_engine.py", "load_risk_register"),
    }
    assert loader_names == expected, (
        f"PRIMARY_LOADER_TARGETS drifted from the canonical six-sidecar set. "
        f"Expected {expected}, got {loader_names}."
    )
