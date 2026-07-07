from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.core.exceptions import ConfigError


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
