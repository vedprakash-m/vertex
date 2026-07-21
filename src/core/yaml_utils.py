from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.core.exceptions import ConfigError

try:
    _FAST_YAML_LOADER: type = yaml.CSafeLoader
except AttributeError:  # LibYAML not available in this environment; pure-Python fallback.
    _FAST_YAML_LOADER = yaml.SafeLoader


def fast_safe_load(text: str) -> Any:
    """specs/people.md §8.5: "The loader uses LibYAML `CSafeLoader` when
    available with behavior-parity coverage for the pure-Python fallback."
    Deliberately scoped to the shared people-registry loaders that name
    this requirement (entities/people_directory/teams/memberships,
    Phase 2a/3) -- NOT a blanket replacement of `load_yaml_mapping`/
    `load_yaml_list`'s `yaml.safe_load` elsewhere in this module, which
    have many callers across the wider codebase outside this spec's
    scope and would need their own separate behavior-parity review."""
    return yaml.load(text, Loader=_FAST_YAML_LOADER)


def load_yaml_mapping(
    path: Path,
    *,
    required: bool = True,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Missing required file: {path}")
        return dict(default) if default is not None else {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    return document


def load_optional_yaml_mapping(path: Path) -> dict[str, Any] | None:
    """Like `load_yaml_mapping(path, required=False)`, but returns `None`
    (not `{}`) for a missing file -- the shape several registry modules
    need to distinguish "file absent" from "file present but empty"."""
    if not path.exists():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    return document


def load_yaml_list(path: Path, *, required: bool = True) -> list[Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Missing required file: {path}")
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, list):
        raise ConfigError(f"Expected list at top-level in {path}")
    return document
