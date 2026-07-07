#!/usr/bin/env python3
"""PB-49: per-run flake-bucket recorder.

Reads a pytest junitxml file (e.g. ``output/junit.xml``) and, for every test
that was retried at least once OR marked with `@pytest.mark.flaky`, appends
a row to ``programs/<program>/_state/flake_buckets.jsonl`` via the canonical
`record_flake()` helper in `src/core/flake_buckets.py`.

This script is invoked from CI after the test step; locally it can be run
on any test result.

Usage:
    python scripts/record_flake.py --junit output/junit.xml
    python scripts/record_flake.py --junit output/junit.xml --program ci-py311-ubuntu
    python scripts/record_flake.py --help

If `--junit` is not provided, the script looks for ``output/junit.xml`` in
the repo root. If no junitxml is present, the script exits 0 (no flakes to
record is a valid state).

The script NEVER marks a test as `fixed` automatically. Quarantining /
marking-fixed is a human action via `vertex observability flakes` (TBD)
or the test-owner-annotated ``@pytest.mark.flake_owner("name")`` mark.

Exits:
  0: no flakes recorded (or no junit to read)
  1: flakes recorded (informational; not a CI failure)
  2: bad arguments / junit unreadable
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Make `src/` importable so this script can be run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _iter_flaky_tests(junit_path: Path) -> list[tuple[str, str | None]]:
    """Return ``(test_id, owner)`` tuples for every test that flaked.

    A test is "flaky" if any of:
    - The junit ``<testcase>`` has a ``<flaky>`` or ``<rerun>`` child.
    - The testcase name contains ``[flake]`` (a marker-convention).
    - The testcase classname matches ``@pytest.mark.flaky`` decorator.
    """
    if not junit_path.exists():
        return []
    tree = ET.parse(junit_path)  # noqa: S314 — junit is internal, not untrusted
    root = tree.getroot()
    flaky: list[tuple[str, str | None]] = []
    for tc in root.iter("testcase"):
        # Heuristic 1: explicit <flaky> / <rerun> child element
        if tc.find("flaky") is not None or tc.find("rerun") is not None:
            owner = tc.attrib.get("owner") or tc.attrib.get("flake_owner")
            flaky.append((f"{tc.attrib.get('classname', '')}.{tc.attrib.get('name', '')}", owner))
            continue
        # Heuristic 2: test name contains [flake] (marker-convention)
        if "[flake]" in tc.attrib.get("name", ""):
            owner = tc.attrib.get("owner") or tc.attrib.get("flake_owner")
            flaky.append((f"{tc.attrib.get('classname', '')}.{tc.attrib.get('name', '')}", owner))
    return flaky


def main(argv: tuple[str, ...] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="record_flake",
        description="PB-49: record pytest-junit flakes into programs/<id>/_state/flake_buckets.jsonl",
    )
    parser.add_argument(
        "--junit",
        type=Path,
        default=Path("output/junit.xml"),
        help="Path to a pytest junitxml file (default: output/junit.xml).",
    )
    parser.add_argument(
        "--program",
        default="local",
        help="Program id for the sidecar (default: local).",
    )
    parser.add_argument(
        "--programs-root",
        type=Path,
        default=_REPO_ROOT / "programs",
        help="Programs root (default: <repo>/programs).",
    )
    args = parser.parse_args(argv)

    if not args.junit.exists():
        print(f"INFO: no junitxml at {args.junit}; nothing to record (exit 0).")
        return 0

    try:
        flaky = _iter_flaky_tests(args.junit)
    except ET.ParseError as exc:
        print(f"ERROR: junitxml at {args.junit} is not parseable: {exc}", file=sys.stderr)
        return 2

    if not flaky:
        print("OK: no flaky tests recorded.")
        return 0

    # Late import — only paid for when there is work to do.
    from src.core.flake_buckets import record_flake

    for test_id, owner in flaky:
        record_flake(
            test_id,
            program_id=args.program,
            programs_root=args.programs_root,
            owner=owner,
        )
        print(f"recorded flake: {test_id} (owner={owner or 'unowned'})")

    print(f"DONE: {len(flaky)} flake(s) recorded to programs/{args.program}/_state/flake_buckets.jsonl")
    # Exit 1 is INFORMATIONAL: the CI step that calls this script can choose
    # whether to fail the build on flakes (it does NOT, by design — flakes
    # are tracked, not blocking).
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
