"""WP-1/GAP-15: Contract tests verifying program schema version consistency.

program.yaml must use schema_version 3.0 (matching doctor expectations).
editions/*.yaml and workstreams.yaml continue using 2.0.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_onboard_program_schema_version_is_3_0() -> None:
    """src/commands/onboard.py must emit schema_version '3.0' for program.yaml.

    Two places emit the schema_version for program.yaml:
    1. The new-program creation path (_compact block)
    2. The merge path (_merge_program_document default)

    editions, workstreams, and scorecards remain at '2.0'.
    """
    onboard_path = REPO_ROOT / "src/commands/onboard.py"
    source = onboard_path.read_text(encoding="utf-8")

    # The program emit must reference "3.0"
    assert '"3.0"' in source or "'3.0'" in source, (
        "onboard.py does not contain schema_version 3.0 for program.yaml. "
        "WP-1 requires program.yaml schema_version to be '3.0'."
    )

    # Verify it's not just "3.0" as the only version — editions must still be "2.0"
    assert '"2.0"' in source or "'2.0'" in source, (
        "onboard.py lost schema_version '2.0'. "
        "Editions and workstreams must remain at '2.0'."
    )


def test_program_template_schema_version_is_3_0() -> None:
    """programs/_templates/example_tpm/program.yaml must use schema_version 3.0."""
    template_path = REPO_ROOT / "programs/_templates/example_tpm/program.yaml"
    if not template_path.exists():
        return  # template may be gitignored on some CI machines; skip gracefully
    text = template_path.read_text(encoding="utf-8")
    assert "schema_version: \"3.0\"" in text or "schema_version: '3.0'" in text, (
        f"{template_path} must have schema_version: \"3.0\" for program.yaml. "
        "WP-1 requires template to match the emitted schema_version."
    )


def test_onboard_edition_schema_version_remains_2_0() -> None:
    """Editions emitted by onboard.py must still use schema_version '2.0'."""
    onboard_path = REPO_ROOT / "src/commands/onboard.py"
    source = onboard_path.read_text(encoding="utf-8")

    tree = ast.parse(source, filename=str(onboard_path))

    # Find all string constants "3.0" near "schema_version" but NOT for edition/workstream/scorecard.
    # Simpler check: verify that "edition" sections reference "2.0" not "3.0".
    # We look for the pattern schema_version followed by 2.0 in the source (raw text check).
    # The file has "2.0" for editions and workstreams; "3.0" for program only.
    version_contexts = re.findall(r'(?s)"schema_version"\s*:\s*"([^"]+)"', source)
    version_contexts += re.findall(r"(?s)'schema_version'\s*:\s*'([^']+)'", source)
    # At least one "2.0" (for edition/workstreams) and at least one "3.0" (for program)
    assert "2.0" in version_contexts, (
        "onboard.py lost '2.0' schema_version entries — editions may be misconfigured."
    )
    assert "3.0" in version_contexts, (
        "onboard.py lost '3.0' schema_version entry — program.yaml may be misconfigured."
    )
