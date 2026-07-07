"""specs/declutter.md Phase 0.5 exit gate — R-1a inline-construction ban.

G-7 makes ``src/core/program_paths.py`` the single Zone-A source of truth for
where platform-owned artifacts live under ``programs/<id>/``. R-1a is the
architecture-fitness guard that keeps it that source of truth: after Phase 0.5
routes every call site through the canonical write getters and transitional
read resolvers, no other production module may *construct* a runtime-artifact
path by writing ``something / "channel_registry.sqlite3"`` inline. New code
must import the getter/resolver from ``program_paths``.

What this test bans (AST-level): a ``BinOp`` division whose right-hand operand is
a string ``Constant`` equal to one of ``RUNTIME_FILENAMES`` — i.e. the literal
``x / "<runtime_file>"`` path-construction pattern. It does NOT ban bare
filename strings in staleness tables / manifests (those carry the name only, no
path) nor ``x / variable`` loops (a Name operand) — those are handled by the
Phase 1 sweep (yaml_support / program_context / checkpoint_store), not by this
gate. Scanning ``src/`` and ``scripts/`` (production code); ``program_paths.py``
itself is the sanctioned registry and is excluded.

Companion checks assert the registry is internally complete: every
``RuntimeArtifact``'s ``canonical_getter_name`` and ``read_resolver_name``
resolve to callables on the module, and the T-3b artifacts that must survive
migration are checkpointed.
"""
from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

from src.core import program_paths as program_paths_module
from src.core.program_paths import (
    RUNTIME_ARTIFACTS,
    RUNTIME_FILENAMES,
    ROOT_WHITELIST,
    ROOT_ENTRY_NAMES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
SANCTIONED_REGISTRY = "src/core/program_paths.py"


def _iter_production_python_files() -> list[Path]:
    files: list[Path] = []
    for root in (SRC_ROOT, SCRIPTS_ROOT):
        if root.exists():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _runtime_path_divisions(tree: ast.AST) -> list[str]:
    """Return the runtime filenames appearing as the right operand of a ``/``
    BinOp (the inline ``x / "<runtime_file>"`` construction pattern)."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str) and right.value in RUNTIME_FILENAMES:
            hits.append(right.value)
    return hits


def test_r1a_no_inline_runtime_path_construction_outside_registry() -> None:
    """R-1a exit gate: runtime-artifact paths may only be constructed in
    ``program_paths.py``. Every ``src/`` + ``scripts/`` call site must go
    through a canonical getter (write) or transitional resolver (read).

    A file that does not parse (e.g. an untracked local throwaway with a syntax
    error) cannot be AST-walked, so it is skipped — but never silently: each
    unparseable path is emitted as a pytest warning so the coverage gap is
    visible. The gate fails only on real inline ``x / "<runtime_file>"``
    constructions.
    """
    violations: list[str] = []
    for file_path in _iter_production_python_files():
        relative = file_path.relative_to(REPO_ROOT).as_posix()
        if relative == SANCTIONED_REGISTRY:
            continue
        source = file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as error:
            warnings.warn(f"R-1a scanner: skipping unparseable {relative} ({error})")
            continue
        for filename in _runtime_path_divisions(tree):
            violations.append(f"{relative}: inline construction `... / \"{filename}\"`")

    assert violations == [], (
        "R-1a: inline runtime-artifact path construction found outside the sanctioned "
        f"registry ({SANCTIONED_REGISTRY}). Route through src.core.program_paths getters/"
        f"resolvers instead.\nViolations:\n  " + "\n  ".join(violations)
    )


def test_runtime_artifacts_registry_is_complete() -> None:
    """Every RuntimeArtifact names a getter and resolver that actually exist on
    the module, and the 7 T-3 artifacts are registered."""
    assert len(RUNTIME_ARTIFACTS) == 7, f"expected 7 runtime artifacts, got {len(RUNTIME_ARTIFACTS)}"
    for artifact in RUNTIME_ARTIFACTS:
        getter = getattr(program_paths_module, artifact.canonical_getter_name, None)
        resolver = getattr(program_paths_module, artifact.read_resolver_name, None)
        assert callable(getter), f"{artifact.name}: getter {artifact.canonical_getter_name} missing"
        assert callable(resolver), f"{artifact.name}: resolver {artifact.read_resolver_name} missing"
        assert artifact.filename in RUNTIME_FILENAMES


@pytest.mark.parametrize("artifact", RUNTIME_ARTIFACTS, ids=lambda a: a.name)
def test_getter_and_resolver_are_distinct_callables(artifact) -> None:
    """R-14: the canonical write getter and the transitional read resolver are
    separate function objects — a writer cannot accidentally call the fallback
    resolver by aliasing."""
    getter = getattr(program_paths_module, artifact.canonical_getter_name)
    resolver = getattr(program_paths_module, artifact.read_resolver_name)
    assert getter is not resolver


def test_checkpointed_t3b_artifacts_are_marked() -> None:
    """G-5 zero-silent-data-loss: the T-3b artifacts whose rebuild is not
    lossless (vertex_analytics, A-11) or that hold pm_confirmed state
    (m365_registry, channel_registry) must be checkpointed so Phase 1
    migration never treats them as disposable."""
    checkpointed_names = {a.name for a in RUNTIME_ARTIFACTS if a.checkpointed}
    must_checkpoint = {"vertex_analytics", "m365_registry", "channel_registry"}
    assert must_checkpoint.issubset(checkpointed_names), (
        f"expected {must_checkpoint} ⊆ checkpointed set {checkpointed_names}"
    )


def test_root_whitelist_is_superset_of_runtime_legacy_names() -> None:
    """DC-01-c whitelist must recognize every runtime artifact's legacy root
    filename so they are not flagged as clutter during the transition window."""
    assert RUNTIME_FILENAMES.issubset(ROOT_WHITELIST)
    assert ROOT_WHITELIST == ROOT_ENTRY_NAMES | RUNTIME_FILENAMES