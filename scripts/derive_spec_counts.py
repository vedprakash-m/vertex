"""Compute spec-relevant counts (modules, commands, tests) for spec authors.

WS-9 step 2: counts in TRACKED specs must be derived from the live tree, not
hardcoded. This script is the canonical source — reference its output in
specs and let the `check_spec_drift.py` guard verify it doesn't drift.

Usage:
    python scripts/derive_spec_counts.py
    python scripts/derive_spec_counts.py --format json

Output is informational; the script is intentionally read-only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _count_py_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for _ in directory.rglob("*.py"))


def _count_test_files(tests_root: Path) -> int:
    if not tests_root.exists():
        return 0
    return sum(1 for _ in tests_root.rglob("test_*.py"))


def _count_collected_tests(tests_root: Path) -> int:
    """Best-effort: return the number of tests pytest would collect, or -1."""
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", str(tests_root), "-q", "--tb=no", "--co"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1
    # pytest --co output ends with "collected N items" or "no tests ran".
    match = re.search(r"collected\s+(\d+)\s+items", result.stdout)
    if match is None:
        return -1
    return int(match.group(1))


def _count_commands_in_cli() -> int:
    """Best-effort: count top-level commands by parsing cli.py's Typer app."""
    cli = REPO_ROOT / "src" / "vertex" / "cli.py"
    if not cli.exists():
        # The spec hints the entry is cli.py — try the original location.
        for candidate in ("src/vertex/cli.py", "src/cli.py"):
            p = REPO_ROOT / candidate
            if p.exists():
                cli = p
                break
    if not cli.exists():
        return -1
    text = cli.read_text(encoding="utf-8")
    # Heuristic: top-level commands are registered via @app.command("name")
    # or app.add_typer(sub_app, name="name"). Count distinct command names.
    commands = set(re.findall(r'@app\.command\("([^"]+)"', text))
    return len(commands)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Derive spec-relevant counts from the live tree.")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)

    src = REPO_ROOT / "src"
    zone_a = src / "core"
    zone_b = src / "ai"
    zone_c = src / "m365"
    commands = src / "commands"
    tests = REPO_ROOT / "tests"

    counts = {
        "zone_a_modules": _count_py_files(zone_a) if zone_a.exists() else 0,
        "zone_b_modules": _count_py_files(zone_b) if zone_b.exists() else 0,
        "zone_c_modules": _count_py_files(zone_c) if zone_c.exists() else 0,
        "command_modules": _count_py_files(commands) if commands.exists() else 0,
        "test_files": _count_test_files(tests),
        "collected_tests": _count_collected_tests(tests),
        "top_level_cli_commands": _count_commands_in_cli(),
    }

    failed = [k for k, v in counts.items() if v == -1]

    if args.format == "json":
        print(json.dumps(counts, indent=2))
        if failed:
            import sys
            print(
                f"\nERROR: {len(failed)} metric(s) could not be derived: {', '.join(failed)}."
                " Fix the underlying probe before using these counts in specs.",
                file=sys.stderr,
            )
    else:
        for key, value in counts.items():
            label = key.replace("_", " ")
            suffix = "  <-- PROBE FAILED" if value == -1 else ""
            print(f"{label:>28}: {value}{suffix}")
        if failed:
            print(
                f"\nERROR: {len(failed)} metric(s) could not be derived: {', '.join(failed)}."
                " Fix the underlying probe before using these counts in specs."
            )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
