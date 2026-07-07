from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

from src.core.program_paths import resolve_m365_registry_path_for_read


_SCHEMA_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)\s*$")


def load_yaml_document(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError, ValueError):
        return {}


def check_schema_versions(program_dir: Path) -> tuple[bool, list[str]]:
    """Check schema versions for all Plane 1 program files.

    Phase 1-B: ``m365_registry.yaml`` is now under ``runtime/`` (T-3b).
    Its YAML integrity is still worth validating (machine-written YAML can
    still be corrupt on an interrupted write). The resolver is used so the
    check works both before migration (legacy root path) and after (runtime/).
    Decision rationale: silent non-validation after the move is not acceptable
    (declutter.md §6 1-B B-5).
    """
    errors: list[str] = []
    # T-1 / T-2 authored-config and mutable-state files: always at root.
    known_root_files = [
        "program.yaml", "workstreams.yaml", "workstream_registry.yaml",
        "milestones.yaml", "scorecards.yaml", "kpis.yaml",
        "risk_register.yaml", "decisions.yaml", "assumptions.yaml",
        "dependencies.yaml", "editorial_rules.yaml", "trusted_baseline.yaml",
        "capability_status.yaml", "readiness.yaml",
    ]
    for filename in known_root_files:
        path = program_dir / filename
        if not path.exists():
            continue
        try:
            document = load_yaml_document(path)
            if isinstance(document, dict):
                schema_version = document.get("schema_version", "")
                if not schema_version:
                    errors.append(f"{filename}: missing schema_version")
                    continue
                if not _SCHEMA_VERSION_RE.match(str(schema_version)):
                    errors.append(f"{filename}: unparseable schema_version '{schema_version}'")
        except (OSError, yaml.YAMLError, TypeError, AttributeError) as error:
            errors.append(f"{filename}: failed to parse — {error}")

    # T-3b runtime file: use the read resolver so the check works both before
    # migration (m365_registry.yaml at root) and after (runtime/m365_registry.yaml).
    programs_root = program_dir.parent
    program_id = program_dir.name
    m365_path = resolve_m365_registry_path_for_read(program_id, programs_root=programs_root)
    if m365_path.exists():
        try:
            document = load_yaml_document(m365_path)
            if isinstance(document, dict):
                schema_version = document.get("schema_version", "")
                if not schema_version:
                    errors.append("m365_registry.yaml: missing schema_version")
                elif not _SCHEMA_VERSION_RE.match(str(schema_version)):
                    errors.append(f"m365_registry.yaml: unparseable schema_version '{schema_version}'")
        except (OSError, yaml.YAMLError, TypeError, AttributeError) as error:
            errors.append(f"m365_registry.yaml: failed to parse — {error}")

    return (len(errors) == 0, errors)
