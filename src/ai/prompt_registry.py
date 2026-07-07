"""Prompt registry (D-22).

Centralizes resolution of versioned AI prompt templates. Prompt files live in
``src/ai/prompts/<version>.txt`` and the set of active versions is declared in
``src/ai/prompts/registry.yaml``. ``load_prompt`` validates that a requested
version is registered and present, then returns the (stripped, cached) text.

Callers pass their own ``error_factory`` so a missing/unregistered prompt keeps
surfacing as the caller's domain error (e.g. ``ActionExtractorError``) rather
than leaking a registry-internal type.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Callable

from src.core.yaml_utils import load_yaml_mapping


PROMPTS_DIR = Path(__file__).with_name("prompts")
REGISTRY_PATH = PROMPTS_DIR / "registry.yaml"
REGISTRY_SCHEMA_VERSION = "1"

ErrorFactory = Callable[[str], Exception]


class PromptRegistryError(Exception):
    """Raised when a prompt version is unregistered, missing, or the registry is malformed."""


@lru_cache(maxsize=1)
def registered_versions() -> frozenset[str]:
    """Return the set of prompt versions declared in registry.yaml."""
    document = load_yaml_mapping(REGISTRY_PATH)
    version = str(document.get("registry_schema_version") or "").strip()
    if version != REGISTRY_SCHEMA_VERSION:
        raise PromptRegistryError(
            f"Prompt registry schema mismatch: expected {REGISTRY_SCHEMA_VERSION}, got {version or '<missing>'}"
        )
    raw_prompts = document.get("prompts")
    if not isinstance(raw_prompts, list) or not raw_prompts:
        raise PromptRegistryError(f"'prompts' must be a non-empty list in {REGISTRY_PATH}")
    versions = {str(item).strip() for item in raw_prompts}
    if "" in versions:
        raise PromptRegistryError(f"Prompt versions must be non-empty strings in {REGISTRY_PATH}")
    return frozenset(versions)


@lru_cache(maxsize=None)
def _read_prompt_text(prompt_version: str) -> str:
    return (PROMPTS_DIR / f"{prompt_version}.txt").read_text(encoding="utf-8").strip()


def load_prompt(prompt_version: str, *, error_factory: ErrorFactory | None = None) -> str:
    """Resolve a registered prompt version to its template text.

    Raises ``error_factory(message)`` (default ``PromptRegistryError``) when the
    version is not registered in registry.yaml or its template file is missing.
    """
    err: ErrorFactory = error_factory or PromptRegistryError
    if prompt_version not in registered_versions():
        raise err(f"Prompt version not registered in {REGISTRY_PATH}: {prompt_version}")
    path = PROMPTS_DIR / f"{prompt_version}.txt"
    if not path.exists():
        raise err(f"Missing prompt template: {path}")
    return _read_prompt_text(prompt_version)
