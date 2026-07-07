from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "src" / "core"
ALLOWED_DEFINITION_FILES = {
    CORE_ROOT / "yaml_utils.py",
    CORE_ROOT / "readiness_engine.py",
}


def test_yaml_helper_definitions_are_centralized() -> None:
    violations: list[str] = []
    pattern = re.compile(r"def\s+_load_yaml(?:_mapping)?\s*\(")
    for file_path in CORE_ROOT.rglob("*.py"):
        if file_path in ALLOWED_DEFINITION_FILES:
            continue
        if pattern.search(file_path.read_text(encoding="utf-8")):
            violations.append(str(file_path.relative_to(REPO_ROOT)))
    assert violations == []


def test_no_private_yaml_helper_is_imported_from_edition_resolver() -> None:
    channel_wiring_path = REPO_ROOT / "src" / "commands" / "channel_wiring.py"
    source = channel_wiring_path.read_text(encoding="utf-8")
    assert "from src.core.edition_resolver import _load_yaml" not in source
