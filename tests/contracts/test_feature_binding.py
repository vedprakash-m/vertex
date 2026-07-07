"""
Feature binding enforcement — §6 of the program-context-maturity spec.

No feature script may contain a string, email, date, ADO ID, area path, or threshold
value that appears in any program file. Sole exception: the program file path itself.

Patterns scanned (per spec §6 enforcement plan):
  - Email literals:   [a-zA-Z0-9._%+-]+@microsoft.com
  - Date literals:    date(20dd{2},  (hardcoded date constructors)
  - Area paths:      One\\[A-Z]  (hardcoded ADO area path strings)
  - Stub WI IDs:     9dddddd  (900000-999999 placeholder IDs)

Exclusions: test files, import statements, comments, docstrings,
           the program file path itself, and already-in-program-files strings
           (strings that appear in program YAML files are exempt because
           they ARE the source of truth, not hardcoded copies).

Zone A only. No AI. No M365 calls.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMANDS_ROOT = REPO_ROOT / "src" / "commands"
SCRIPTS_ROOT = REPO_ROOT / "scripts"

# Programs root must exist for the test to be meaningful: without it, all strings
# are treated as hardcoded (no exemptions) and pre-existing utility scripts cause
# false positives.  Skip when the gitignored programs/ directory is absent.
_PROGRAMS_ROOT = REPO_ROOT / "programs"
_PROGRAMS_EXIST = _PROGRAMS_ROOT.exists() and any(_PROGRAMS_ROOT.iterdir())

# One-time utility/maintenance scripts in scripts/ are not feature code.
# Prefixes for scripts excluded from feature-binding enforcement:
_EXCLUDED_SCRIPT_PREFIXES = (
    "_apply_", "_revise_", "_probe_", "_debug_", "_check_", "_fix_",
    "_enrich_", "_audit_", "_round", "check_", "fix_",
)

# Specific legacy scripts excluded individually
_EXCLUDED_SCRIPT_NAMES: frozenset[str] = frozenset()

# Patterns to detect hardcoded values that must come from program files
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email_literal", re.compile(r"[a-zA-Z0-9._%+-]+@microsoft\.com")),
    ("date_literal", re.compile(r"\bdate\(20\d{2}\s*,")),
    ("area_path_literal", re.compile(r"One\\[A-Z][a-zA-Z0-9_]*(?:\\|$)")),
    # Negative lookbehind for decimal point or digit to avoid false positives on
    # float constants like 0.999999 or date serials like 29991231.
    ("stub_wi_id", re.compile(r"(?<![0-9.])9[0-9]{5}\b")),
]


class Violation(NamedTuple):
    file: str
    line_number: int
    line: str
    pattern: str
    matched: str


def _load_program_strings(programs_root: Path) -> frozenset[str]:
    """
    Load all non-structure string values from program YAML files.

    These strings are the source of truth — features may reference them
    as long as they read from program files, not hardcode copies.
    We only flag strings that appear in feature code but are NOT in program files.
    """
    strings: set[str] = set()
    if not programs_root.exists():
        return frozenset(strings)

    for prog_dir in programs_root.iterdir():
        if not prog_dir.is_dir():
            continue
        for yaml_file in prog_dir.rglob("*.yaml"):
            try:
                import yaml
                with yaml_file.open("r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                strings.update(_extract_leaf_strings(data))
            except Exception:
                pass
    return frozenset(strings)


def _extract_leaf_strings(data: dict[str, object]) -> set[str]:
    """Recursively extract all non-structured leaf string values from a dict."""
    strings: set[str] = set()
    for key, value in data.items():
        if key in ("schema_version", "id", "program_id"):
            continue
        if isinstance(value, str) and value.strip() and len(value) > 2:
            strings.add(value.strip())
        elif isinstance(value, dict):
            strings.update(_extract_leaf_strings(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip() and len(item) > 2:
                    strings.add(item.strip())
                elif isinstance(item, dict):
                    strings.update(_extract_leaf_strings(item))
    return strings


def _scan_file(path: Path, program_strings: frozenset[str]) -> list[Violation]:
    """
    Scan a single Python file for hardcoded pattern violations.

    Excludes: test files, import statements, comments, block docstrings.
    A match is flagged ONLY if the matched string does NOT appear in program files
    (i.e., it's a hardcoded copy of a value that exists in the program model,
    not a reference to the source-of-truth file path).
    """
    violations: list[Violation] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return violations

    in_docstring = False
    docstring_delim: str | None = None

    for i, line in enumerate(lines, 1):
        stripped = line.rstrip()

        # Block-aware docstring tracking: toggle on triple-quote markers
        for delim in ('"""', "'''"):
            count = stripped.count(delim)
            if count == 0:
                continue
            if not in_docstring:
                # Opening delimiter found — skip entire line and enter docstring
                in_docstring = True
                docstring_delim = delim
                # If the closing delimiter also appears on this line (one-liner), exit immediately
                if count >= 2:
                    in_docstring = False
                    docstring_delim = None
                break
            elif delim == docstring_delim:
                # Closing delimiter — skip this line and exit docstring
                in_docstring = False
                docstring_delim = None
                break

        if in_docstring or stripped.count('"""') > 0 or stripped.count("'''") > 0:
            continue

        # Skip comment lines
        if stripped.lstrip().startswith("#"):
            continue

        for pattern_name, compiled_re in PATTERNS:
            for match in compiled_re.finditer(line):
                matched = match.group()

                # Skip import statements
                if "import" in stripped and matched in stripped.split("#")[0]:
                    continue

                # Skip the program file path itself (sole exception in spec)
                prog_file_fragments = ["program.yaml", "workstreams.yaml",
                                       "workstream_registry.yaml", "milestones.yaml",
                                       "risk_register.yaml", "decisions.yaml",
                                       "scorecards.yaml", "kpis.yaml", "dependencies.yaml",
                                       "assumptions.yaml", "editorial_rules.yaml"]
                if any(f"programs/{matched.split('/')[0] if '/' in matched else ''}" in line for f in prog_file_fragments):
                    continue

                # Skip if this string actually exists in the program files
                # (it's the source of truth, not a hardcode)
                if matched in program_strings:
                    continue

                violations.append(Violation(
                    file=str(path.relative_to(REPO_ROOT)),
                    line_number=i,
                    line=stripped,
                    pattern=pattern_name,
                    matched=matched,
                ))

    return violations


def _is_excluded_script(path: Path) -> bool:
    """True for one-time utility/maintenance scripts that are not feature code."""
    if path.name in _EXCLUDED_SCRIPT_NAMES:
        return True
    return any(path.name.startswith(prefix) for prefix in _EXCLUDED_SCRIPT_PREFIXES)


def _all_violations(
    dirs: list[Path],
    program_strings: frozenset[str],
) -> list[Violation]:
    """Scan all tracked .py files in the given directories, skipping utility scripts.

    Only files tracked by git are scanned — gitignored scripts (which may contain
    local operator data) are intentionally excluded.
    """
    # Pre-fetch tracked files once so we don't shell-out per file.
    try:
        result = subprocess.run(
            ["git", "ls-files", "--full-name"],
            capture_output=True, text=True, cwd=REPO_ROOT, check=True,
        )
        tracked_relative: frozenset[str] = frozenset(
            p.replace("\\", "/") for p in result.stdout.splitlines()
        )
    except Exception:
        tracked_relative = frozenset()  # fall back to scanning all if git unavailable

    all_violations: list[Violation] = []
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*.py"):
            rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
            if tracked_relative and rel not in tracked_relative:
                continue  # skip gitignored / untracked files
            if _is_excluded_script(path):
                continue
            all_violations.extend(_scan_file(path, program_strings))
    return all_violations


@pytest.mark.skipif(not _PROGRAMS_EXIST, reason="Requires programs/ data to exempt source-of-truth values")
def test_no_email_literals_in_feature_code() -> None:
    """Features must not contain hardcoded Microsoft email addresses."""
    program_strings = _load_program_strings(_PROGRAMS_ROOT)
    dirs = [COMMANDS_ROOT, SCRIPTS_ROOT]
    violations = [v for v in _all_violations(dirs, program_strings) if v.pattern == "email_literal"]
    if violations:
        lines = "\n".join(f"  {v.file}:{v.line_number} → {v.matched!r}" for v in violations[:10])
        fail = f"Email literals found in feature code:\n{lines}"
        if len(violations) > 10:
            fail += f"\n  ...and {len(violations) - 10} more"
        pytest.fail(fail)


@pytest.mark.skipif(not _PROGRAMS_EXIST, reason="Requires programs/ data to exempt source-of-truth values")
def test_no_date_literals_in_feature_code() -> None:
    """Features must not contain hardcoded date() constructors with year >= 2020."""
    program_strings = _load_program_strings(_PROGRAMS_ROOT)
    dirs = [COMMANDS_ROOT, SCRIPTS_ROOT]
    violations = [v for v in _all_violations(dirs, program_strings) if v.pattern == "date_literal"]
    if violations:
        lines = "\n".join(f"  {v.file}:{v.line_number} → {v.matched!r}" for v in violations[:10])
        fail = f"Hardcoded date() literals found in feature code:\n{lines}"
        if len(violations) > 10:
            fail += f"\n  ...and {len(violations) - 10} more"
        pytest.fail(fail)


@pytest.mark.skipif(not _PROGRAMS_EXIST, reason="Requires programs/ data to exempt source-of-truth values")
def test_no_area_path_literals_in_feature_code() -> None:
    """Features must not contain hardcoded ADO area path strings (One\\\\...)."""
    program_strings = _load_program_strings(_PROGRAMS_ROOT)
    dirs = [COMMANDS_ROOT, SCRIPTS_ROOT]
    violations = [v for v in _all_violations(dirs, program_strings) if v.pattern == "area_path_literal"]
    if violations:
        lines = "\n".join(f"  {v.file}:{v.line_number} → {v.matched!r}" for v in violations[:10])
        fail = f"Hardcoded area path literals found in feature code:\n{lines}"
        if len(violations) > 10:
            fail += f"\n  ...and {len(violations) - 10} more"
        pytest.fail(fail)


@pytest.mark.skipif(not _PROGRAMS_EXIST, reason="Requires programs/ data to exempt source-of-truth values")
def test_no_stub_wi_id_literals_in_feature_code() -> None:
    """Features must not contain hardcoded stub WI IDs (900000-999999)."""
    program_strings = _load_program_strings(_PROGRAMS_ROOT)
    dirs = [COMMANDS_ROOT, SCRIPTS_ROOT]
    violations = [v for v in _all_violations(dirs, program_strings) if v.pattern == "stub_wi_id"]
    if violations:
        lines = "\n".join(f"  {v.file}:{v.line_number} → {v.matched!r}" for v in violations[:10])
        fail = f"Hardcoded stub WI ID literals found in feature code (900000-999999):\n{lines}"
        if len(violations) > 10:
            fail += f"\n  ...and {len(violations) - 10} more"
        pytest.fail(fail)


# Program/product-name literals that must never be hardcoded in generic runtime paths.
# This regex matches the fictional example names used in test fixtures.  Replace
# with your deployment's actual program name set to catch regressions.
PROGRAM_LITERALS_RE = re.compile(r"(?i)\b(?:Acme|Fabrikam|Contoso|Northwind|Adventure|Wingtip)\b")
PROGRAM_LITERAL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "templates/partials/nudge_hygiene_consolidated.j2",
    }
)


def _generic_program_literal_paths() -> tuple[Path, ...]:
    core_paths = tuple((REPO_ROOT / "src" / "core").rglob("*.py"))
    template_paths = tuple((REPO_ROOT / "templates" / "archetypes").rglob("*.j2")) + tuple(
        (REPO_ROOT / "templates" / "partials").rglob("*.j2")
    )
    command_paths = (REPO_ROOT / "src" / "commands" / "report.py",)
    return core_paths + template_paths + command_paths


def _scan_program_literal_file(path: Path) -> list[Violation]:
    relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    if relative in PROGRAM_LITERAL_ALLOWLIST:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    violations: list[Violation] = []
    in_docstring = False
    docstring_delim: str | None = None
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        scan_line = line
        if path.suffix == ".py":
            for delim in ('"""', "'''"):
                count = stripped.count(delim)
                if count == 0:
                    continue
                if not in_docstring:
                    in_docstring = True
                    docstring_delim = delim
                    if count >= 2:
                        in_docstring = False
                        docstring_delim = None
                    break
                if delim == docstring_delim:
                    in_docstring = False
                    docstring_delim = None
                    break
            if in_docstring or stripped.startswith("#"):
                continue
            scan_line = line.split("#", 1)[0]
        elif stripped.startswith("{#"):
            continue
        for match in PROGRAM_LITERALS_RE.finditer(scan_line):
            violations.append(
                Violation(
                    file=relative,
                    line_number=i,
                    line=stripped,
                    pattern="program_literal",
                    matched=match.group(),
                )
            )
    return violations


@pytest.mark.skipif(not _PROGRAMS_EXIST, reason="Requires programs/ data to identify program extensions")
def test_generic_runtime_paths_do_not_hardcode_program_literals() -> None:
    violations: list[Violation] = []
    for path in _generic_program_literal_paths():
        violations.extend(_scan_program_literal_file(path))
    if violations:
        lines = "\n".join(f"  {v.file}:{v.line_number} → {v.matched!r}" for v in violations[:20])
        fail = f"Program literals found in generic runtime paths:\n{lines}"
        if len(violations) > 20:
            fail += f"\n  ...and {len(violations) - 20} more"
        pytest.fail(fail)