"""Contract: no src/ file should construct a shadow path under the old output/ root.

Shadow constructors bypass the canonical path helpers and write to the old
`output/{edition}/` tree even after the path migration.  They are invisible to a
simple `OUTPUT_ROOT` grep because they use literals like `Path("output")`,
`repo_root / "output"`, or `.parent / "output"`.

This test is the single highest-value guard added by the move-output-newsletter
migration (specs/move-output-newsletter.md).  It turns a silent partial migration
into a hard red.

Allowlisted files permitted to contain `/ "output" /` or `"output"` path literals:
- edition_resolver.py   — the canonical resolver; owns all path construction
- migrate_nudge_layout.py — hardcodes legacy source path for nudge migration (§5.7)
"""
from __future__ import annotations

import pathlib
import re

import pytest


# Patterns that indicate a hardcoded "output" path segment OUTSIDE the resolver.
# Each pattern targets a specific class of bypass construction.
_SHADOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'Path\("output"\)'),                      # CWD-relative path default
    re.compile(r'repo_root\s*/\s*"output"'),              # explicit repo_root / "output"
    re.compile(r'\.parent\s*/\s*"output"'),               # .parent / "output"
    re.compile(r'parents\[\d+\]\s*/\s*"output"'),         # parents[N] / "output"
    re.compile(r'output_root\.glob\('),                   # old cross-edition glob variable name
    # Cross-program glob pattern strings embedding literal "output" — e.g.
    # programs_root.glob("*/output/*/ado_proposals/...").  The resolved form uses
    # _output_subdir() which is dynamic.
    re.compile(r'\.glob\(\s*[f"].*?[/\\]output[/\\]'),   # glob("*/output/...")
    re.compile(r'\.glob\(\s*f.*?[/\\]output[/\\]'),       # glob(f"*/output/...")
    # Root-scoped hardcoded path: programs_root / program_id / "output"
    # This targets constructions like `programs_root / x / "output"` or
    # `program_dir / "output"` outside edition_resolver.py.
    re.compile(r'/\s*"output"\s*/'),                      # / "output" / (path division)
]

# Shadow patterns for the new canonical name — prevent future callers from
# hardcoding "publications" outside the resolver.
_PUBLICATIONS_SHADOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'Path\("publications"\)'),
    re.compile(r'repo_root\s*/\s*"publications"'),
    re.compile(r'\.parent\s*/\s*"publications"'),
    re.compile(r'parents\[\d+\]\s*/\s*"publications"'),
    re.compile(r'/\s*"publications"\s*/'),                # / "publications" / hardcoded
]

# Files that are ALLOWED to contain "output" path literals because they deliberately
# reference the legacy layout (resolver itself, nudge migration script).
_ALLOWLISTED_FILES = frozenset({
    "edition_resolver.py",
    "migrate_nudge_layout.py",
    "migrate_edition_output.py",   # migration script — constructs both paths intentionally
})


def test_no_source_writes_to_repo_root_output() -> None:
    """No src/ file should construct a shadow path under the old output/ root."""
    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    violations: list[str] = []
    for py_file in sorted(src_root.rglob("*.py")):
        if py_file.name in _ALLOWLISTED_FILES:
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for pattern in _SHADOW_PATTERNS:
            for match in pattern.finditer(text):
                rel = py_file.relative_to(src_root)
                violations.append(f"{rel}: {match.group()!r} (pattern: {pattern.pattern!r})")
    assert not violations, (
        "Shadow output constructors detected — these bypass get_program_output_dir "
        "and silently write to the old output/ root:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_no_source_hardcodes_publications_path() -> None:
    """No src/ file should hardcode 'publications' as a path segment outside edition_resolver.py."""
    src_root = pathlib.Path(__file__).resolve().parents[2] / "src"
    violations: list[str] = []
    for py_file in sorted(src_root.rglob("*.py")):
        if py_file.name in _ALLOWLISTED_FILES:
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for pattern in _PUBLICATIONS_SHADOW_PATTERNS:
            for match in pattern.finditer(text):
                rel = py_file.relative_to(src_root)
                violations.append(f"{rel}: {match.group()!r} (pattern: {pattern.pattern!r})")
    assert not violations, (
        "Hardcoded 'publications' path segment detected — use get_program_output_dir() "
        "or get_program_output_root() instead:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
