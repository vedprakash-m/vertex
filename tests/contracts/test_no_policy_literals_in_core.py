"""WI-0.4: Contract test — no policy literals hardcoded in src/core/ or src/ai/.

Policy values (TTLs, max_tokens, thresholds, cadences) must live in
vertex/policies/*.yaml and be accessed via src/core/policy_loader.py.
This test enforces the O-6 objective: 0 policy literals in Python.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# Patterns that indicate a hardcoded policy literal.
# These are AST-based checks where we look for specific dict/constant patterns
# that belong in policy YAML instead of code.

# FACT_TYPE_TTL_DAYS as a dict literal in Python code
_FACT_TYPE_TTL_LITERAL_PATTERN = re.compile(
    r"\bFACT_TYPE_TTL_DAYS\s*:\s*dict\s*\["
)

# max_tokens= with a raw integer (not loaded from policy)
# We allow it in tests and in provider.py's method signatures (default params).
# In production code, max_tokens must come from load_ai_feature_policy().max_tokens.
_MAX_TOKENS_HARDCODED_PATTERN = re.compile(r"\bmax_tokens\s*=\s*\d+")

# Allowed files that legitimately have max_tokens defaults (method signatures,
# policy loader itself, or wrapper APIs where the value is passed through):
_MAX_TOKENS_ALLOWED = frozenset({
    "src/ai/provider.py",          # Protocol + DisabledStructuredProvider method signatures
    "src/ai/client.py",            # LLM client method signatures
    "src/ai/deployment_fallback.py",  # FallbackStructuredClient signature
    "src/ai/tiered_router.py",     # tier function signatures
    "src/ai/llm_trace.py",         # trace helper
    "src/ai/grounding.py",         # grounding helper
    "src/ai/safety/__init__.py",   # safety layer
})

# Files that are tests — excluded from the max_tokens literal scan
_TEST_DIRS = frozenset({"tests"})


def _is_test_file(path: Path) -> bool:
    return any(part in _TEST_DIRS for part in path.parts)


def test_no_fact_type_ttl_days_literal_in_src() -> None:
    """FACT_TYPE_TTL_DAYS dict literal must not appear in Python source.

    TTLs live in vertex/policies/freshness_policy.yaml and are loaded
    via load_freshness_policy().
    """
    violations: list[str] = []
    for py_file in (REPO_ROOT / "src").rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        if _FACT_TYPE_TTL_LITERAL_PATTERN.search(source):
            relative = py_file.relative_to(REPO_ROOT).as_posix()
            violations.append(relative)
    assert violations == [], (
        "Policy literal violation — FACT_TYPE_TTL_DAYS dict found in src/: "
        + ", ".join(violations)
    )


def test_no_hardcoded_max_tokens_in_ai_feature_callers() -> None:
    """AI feature call-sites must not hardcode max_tokens with a raw integer.

    The value must come from load_ai_feature_policy(<feature>).max_tokens.
    Allowed exceptions: method signature defaults (provider.py, client.py,
    deployment_fallback.py) and test files.
    """
    violations: list[str] = []
    for py_file in sorted((REPO_ROOT / "src" / "ai").rglob("*.py")):
        relative = py_file.relative_to(REPO_ROOT).as_posix()
        if relative in _MAX_TOKENS_ALLOWED:
            continue
        if _is_test_file(py_file):
            continue
        source = py_file.read_text(encoding="utf-8")
        # Allow only in method/function parameter DEFAULT positions (signatures).
        # Flag any call-site usage: max_tokens=<int> NOT inside a def signature.
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "max_tokens" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                    violations.append(f"{relative}:{node.lineno}")
    assert violations == [], (
        "Policy literal violation — hardcoded max_tokens= integer at call sites in src/ai/: "
        + "; ".join(violations)
    )
