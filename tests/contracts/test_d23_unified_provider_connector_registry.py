"""D-23: codify the unified Provider/Connector Registry.

D-23 (rev. 334) folds `ExternalConnector` registrations into the
unified `ProviderRegistry` so the gather-time channel path and the
slice-contract external-connector path share a single registry
mechanism. The legacy `CONNECTOR_REGISTRY` dict is retained for
back-compat (direct imports from tests / third-party code), but new
code should resolve connectors through `ProviderRegistry`.

This contract file freezes the unification invariants:
  (a) `ProviderRegistry` exposes `register_connector`,
      `resolve_connector`, and `connector_types` methods.
  (b) The connectors in `CONNECTOR_REGISTRY` (github_issues,
      sharepoint_lists) are registered in the unified registry via
      `register_with_provider_registry(registry)`.
  (c) `make_connector(config)` resolves through the unified
      registry (not the legacy dict directly).
  (d) `build_provider_registry()` (the gather-time bootstrap path)
      also registers the connectors, so the gather-time path can
      resolve them.

Why:** D-23 was at 0% in debt.md. This contract ensures the
unification doesn't silently drift (e.g. someone adding a new
connector to `CONNECTOR_REGISTRY` but forgetting to wire it into the
unified registry, or someone adding a `ProviderRegistry()` instance
that bypasses the bootstrap path).
**How to apply:** when adding a new connector type:
  1. Add an `ExternalConnector` subclass.
  2. Add it to `CONNECTOR_REGISTRY` in
     `src/core/connectors/__init__.py`.
  3. The `register_with_provider_registry(registry)` helper will
     pick it up automatically; the contract test enforces this.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDER_REGISTRY = REPO_ROOT / "src/core/provider_registry.py"
CONNECTORS_INIT = REPO_ROOT / "src/core/connectors/__init__.py"
EXTERNAL_CONNECTOR = REPO_ROOT / "src/core/external_connector.py"
CHANNEL_WIRING = REPO_ROOT / "src/commands/channel_wiring.py"


def _parse(relpath: Path) -> ast.Module:
    return ast.parse(relpath.read_text(encoding="utf-8"), filename=str(relpath))


def _class_methods(cls: ast.ClassDef) -> set[str]:
    return {stmt.name for stmt in cls.body if isinstance(stmt, ast.FunctionDef)}


def test_provider_registry_exposes_connector_methods() -> None:
    """`ProviderRegistry` must expose `register_connector`,
    `resolve_connector`, and `connector_types` methods. These are
    the D-23 surface for unifying connector resolution with channel
    resolution."""
    tree = _parse(PROVIDER_REGISTRY)
    cls = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ProviderRegistry"),
        None,
    )
    assert cls is not None, "ProviderRegistry class not found"
    methods = _class_methods(cls)
    for required in {"register_connector", "resolve_connector", "connector_types"}:
        assert required in methods, (
            f"D-23: ProviderRegistry must expose `{required}()` method. "
            f"Found methods: {sorted(methods)}."
        )


def test_register_with_provider_registry_helper_exists() -> None:
    """`register_with_provider_registry(registry)` must exist in
    `src/core/connectors/__init__.py` and accept a registry
    argument. The helper is the D-23 entry point for folding the
    connector types into a unified registry instance."""
    tree = _parse(CONNECTORS_INIT)
    func = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "register_with_provider_registry"),
        None,
    )
    assert func is not None, (
        "D-23: `register_with_provider_registry(registry)` helper not "
        "found in src/core/connectors/__init__.py. Add a module-level "
        "function that registers all connectors in CONNECTOR_REGISTRY "
        "into the provided ProviderRegistry."
    )
    # Must take exactly one positional argument (the registry).
    args = func.args.args
    assert len(args) == 1, (
        f"D-23: `register_with_provider_registry` must take exactly one "
        f"positional argument (the registry). Found {len(args)}: "
        f"{[arg.arg for arg in args]}"
    )


def test_make_connector_resolves_through_unified_registry() -> None:
    """`make_connector` in `src/core/external_connector.py` must
    resolve connector types through `ProviderRegistry.resolve_connector`,
    not the legacy `CONNECTOR_REGISTRY` dict directly. This is the
    D-23 unification contract."""
    source = EXTERNAL_CONNECTOR.read_text(encoding="utf-8")
    assert "ProviderRegistry" in source, (
        "D-23: `make_connector` does not import ProviderRegistry. "
        "It should resolve connector types through the unified "
        "ProviderRegistry.resolve_connector method."
    )
    assert "resolve_connector" in source, (
        "D-23: `make_connector` does not call ProviderRegistry.resolve_connector. "
        "It should delegate to the unified registry's resolve_connector method."
    )
    # The legacy dict should no longer be the direct lookup path.
    assert "CONNECTOR_REGISTRY.get" not in source, (
        "D-23: `make_connector` still uses `CONNECTOR_REGISTRY.get(...)` "
        "as the direct lookup path. It should delegate to "
        "ProviderRegistry.resolve_connector instead."
    )


def test_channel_wiring_registers_connectors() -> None:
    """`build_provider_registry()` in `src/commands/channel_wiring.py`
    must register the external connectors in the same registry it
    builds. This ensures the gather-time path can resolve connector
    types from a registry that has both gather-time channels and
    slice-contract external connectors."""
    source = CHANNEL_WIRING.read_text(encoding="utf-8")
    assert "register_with_provider_registry" in source, (
        "D-23: `build_provider_registry()` in channel_wiring.py does not "
        "call `register_with_provider_registry(registry)`. The gather-time "
        "path must fold the connectors into the same registry."
    )


def test_connector_types_visible_via_unified_registry() -> None:
    """`ProviderRegistry.connector_types()` must include the
    connector types from `CONNECTOR_REGISTRY` (github_issues,
    sharepoint_lists) after `register_with_provider_registry(registry)`
    is called on a fresh registry instance.

    This is the round-trip invariant that D-23 promises: a caller
    that builds a registry, registers the connectors, and asks for
    the connector types, sees the same set as the legacy
    `CONNECTOR_REGISTRY.keys()`."""
    from src.core.connectors import CONNECTOR_REGISTRY, register_with_provider_registry
    from src.core.provider_registry import ProviderRegistry

    registry = ProviderRegistry()
    register_with_provider_registry(registry)
    assert set(registry.connector_types()) == set(CONNECTOR_REGISTRY.keys()), (
        f"D-23: unified registry's connector_types {set(registry.connector_types())} "
        f"does not match CONNECTOR_REGISTRY.keys() {set(CONNECTOR_REGISTRY.keys())}. "
        f"register_with_provider_registry must populate the unified registry "
        f"with every connector in CONNECTOR_REGISTRY."
    )


def test_resolve_connector_returns_correct_class() -> None:
    """`ProviderRegistry.resolve_connector(connector_type)` must
    return the same class as `CONNECTOR_REGISTRY[connector_type]`.
    This is the behavioral invariant: the unified registry gives
    the same answer as the legacy dict for any registered type."""
    from src.core.connectors import CONNECTOR_REGISTRY, register_with_provider_registry
    from src.core.provider_registry import ProviderRegistry

    registry = ProviderRegistry()
    register_with_provider_registry(registry)
    for connector_type, expected_cls in CONNECTOR_REGISTRY.items():
        actual_cls = registry.resolve_connector(connector_type)
        assert actual_cls is expected_cls, (
            f"D-23: ProviderRegistry.resolve_connector({connector_type!r}) "
            f"returned {actual_cls!r}, expected {expected_cls!r}."
        )


def test_resolve_connector_raises_for_unknown_type() -> None:
    """`ProviderRegistry.resolve_connector(unknown_type)` must raise
    `ValueError` with a message that lists the registered types.
    This matches the legacy `make_connector` error contract."""
    from src.core.connectors import register_with_provider_registry
    from src.core.provider_registry import ProviderRegistry

    registry = ProviderRegistry()
    register_with_provider_registry(registry)
    with pytest.raises(ValueError) as excinfo:
        registry.resolve_connector("definitely_not_a_real_connector_type")
    assert "Unknown connector type" in str(excinfo.value)
    assert "definitely_not_a_real_connector_type" in str(excinfo.value)
    # The error should list at least one known type so the operator
    # can see what's available.
    assert "github_issues" in str(excinfo.value) or "sharepoint_lists" in str(excinfo.value)


def test_runtime_code_no_longer_reads_legacy_connector_registry_directly() -> None:
    """D-23 close-out: runtime code under ``src/`` must not read
    ``CONNECTOR_REGISTRY`` directly outside ``src/core/connectors/__init__.py``.

    The legacy dict is retained only as a back-compat/test surface. The
    production runtime should resolve through ``ProviderRegistry`` or the
    connector bootstrap helper instead.
    """
    allowed = {CONNECTORS_INIT.resolve()}
    violations: list[str] = []

    for path in (REPO_ROOT / "src").rglob("*.py"):
        resolved = path.resolve()
        if resolved in allowed:
            continue
        tree = _parse(path)
        uses_legacy_registry = any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module == "src.core.connectors"
                and any(alias.name == "CONNECTOR_REGISTRY" for alias in node.names)
            )
            or (isinstance(node, ast.Name) and node.id == "CONNECTOR_REGISTRY")
            for node in ast.walk(tree)
        )
        if uses_legacy_registry:
            violations.append(str(path.relative_to(REPO_ROOT)))

    assert violations == [], (
        "D-23: runtime code still reads the legacy CONNECTOR_REGISTRY directly: "
        + ", ".join(sorted(violations))
        + ". Resolve connectors through ProviderRegistry/register_with_provider_registry instead."
    )
