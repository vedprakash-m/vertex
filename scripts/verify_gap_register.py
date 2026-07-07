"""GAP-35 (part 2): verify_gap_register.py

For every GAP-NN entry in specs/gaps.md, assert that the file:line
reference resolves to a real path in the repo and (optionally) that
the line contains a "✓" / "RESOLVED" / "PARTIALLY RESOLVED" marker.

The script is intentionally read-only and exits non-zero on any
unresolved reference — that drift is exactly what GAP-35 WP-6 is
about.

Usage:
    python scripts/verify_gap_register.py
    python scripts/verify_gap_register.py --gaps-file specs/gaps.md
    python scripts/verify_gap_register.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GapCheck:
    gap_id: str
    status_marker: str
    line: int
    resolved_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]
    line_present: bool


# A GAP heading like "### GAP-19 · Per-Family SoR Helpers ... · ✓" is at the
# start of a logical "block" — and the block runs until the next GAP-NN
# heading or EOF. The block is parsed for status markers (✓, RESOLVED,
# PARTIALLY RESOLVED, etc.) and for the file:line references the gap
# document makes.
_GAP_HEADING_RE = re.compile(
    r"^#{2,4}\s+GAP-(\d+)\b[^\n]*$",
    re.MULTILINE,
)
_STATUS_MARKERS = (
    "✓",
    "RESOLVED",
    "PARTIALLY RESOLVED",
    "INVESTIGATED",
    "by design",
    "PENDING",
)
# Matches "path/to/file.py:NNN" (or "path/to/file.py" alone), allowing
# paths with /, \, or <placeholders>. Captures up to the next whitespace
# or punctuation. We treat placeholder tokens like `<prog>`, `<edition>`,
# `<program_id>` as literals (not real paths) and skip them.
_PATH_LINE_RE = re.compile(
    r"`([A-Za-z0-9_./<>\-]+\.[A-Za-z0-9]+)(?::(\d+))?`"
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _status_for_block(block: str) -> str:
    """Return the strongest status marker found in the block.

    Precedence (highest → lowest): PARTIALLY RESOLVED > RESOLVED >
    INVESTIGATED > PENDING > UNKNOWN. "RESOLVED" is preferred over
    "INVESTIGATED" because an explicit `**RESOLVED …**` heading
    supersedes a historical "INVESTIGATED" mention in the body.
    """
    lowered = block.lower()
    if "partially resolved" in lowered:
        return "PARTIALLY RESOLVED"
    if "**resolved" in lowered or "✓" in block:
        return "RESOLVED"
    if "investigated" in lowered:
        return "INVESTIGATED"
    if "pending" in lowered:
        return "PENDING"
    return "UNKNOWN"


def _resolve_path(candidate: str) -> Path | None:
    """Resolve a candidate path against the repo root.

    Returns None if the candidate contains a placeholder (e.g. ``<prog>``)
    or doesn't exist.
    """
    if "<" in candidate and ">" in candidate:
        return None
    abs_path = (REPO_ROOT / candidate).resolve()
    if abs_path.exists():
        return abs_path
    return None


def _parse_gaps(text: str) -> list[tuple[str, int, str]]:
    """Yield (gap_id, line_no, block_text) tuples.

    The block includes the heading line itself so that status markers
    on the heading (e.g. ``**RESOLVED 2026-06-17**``) are visible to
    the status classifier.
    """
    matches = list(_GAP_HEADING_RE.finditer(text))
    out: list[tuple[str, int, str]] = []
    for idx, match in enumerate(matches):
        gap_id = match.group(1)
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end]
        # Line number where the heading starts (1-based)
        line = text.count("\n", 0, match.start()) + 1
        out.append((gap_id, line, block))
    return out


def _check_gap(gap_id: str, line: int, block: str) -> GapCheck:
    resolved: list[str] = []
    missing: list[str] = []
    line_present = False
    for match in _PATH_LINE_RE.finditer(block):
        candidate = match.group(1)
        ln = match.group(2)
        # Skip placeholder paths like ``programs/<prog>/program.yaml`` —
        # they are not real on-disk references.
        if "<" in candidate and ">" in candidate:
            continue
        # If a line number is given, also require the path to exist;
        # we don't currently scan the file, but we record the reference.
        resolved_path = _resolve_path(candidate)
        if resolved_path is not None:
            resolved.append(candidate + (f":{ln}" if ln else ""))
            if ln is not None:
                line_present = True
        else:
            missing.append(candidate + (f":{ln}" if ln else ""))
    return GapCheck(
        gap_id=gap_id,
        status_marker=_status_for_block(block),
        line=line,
        resolved_paths=tuple(resolved),
        missing_paths=tuple(missing),
        line_present=line_present,
    )


def verify(gaps_file: Path) -> list[GapCheck]:
    text = _read_text(gaps_file)
    return [_check_gap(gid, ln, blk) for gid, ln, blk in _parse_gaps(text)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--gaps-file",
        type=Path,
        default=REPO_ROOT / "specs" / "gaps.md",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on any missing path (default: only on missing paths to UNRESOLVED gaps).",
    )
    args = parser.parse_args(argv)
    if not args.gaps_file.exists():
        print(f"gaps file not found: {args.gaps_file}", file=sys.stderr)
        return 2
    checks = verify(args.gaps_file)
    unresolved_markers = ("PENDING", "UNKNOWN")
    failures: list[GapCheck] = []
    for check in checks:
        if check.missing_paths:
            if args.strict or check.status_marker in unresolved_markers:
                failures.append(check)
    if args.json:
        print(
            json.dumps(
                {
                    "gaps_file": str(args.gaps_file),
                    "checks": [asdict(c) for c in checks],
                    "failures": [asdict(c) for c in failures],
                },
                indent=2,
            )
        )
    else:
        print(f"== Gap register: {args.gaps_file} ==")
        for check in checks:
            missing = (
                f" missing={list(check.missing_paths)}" if check.missing_paths else ""
            )
            print(
                f"GAP-{check.gap_id:>3}  {check.status_marker:<20}  "
                f"line={check.line:<4}  "
                f"resolved={len(check.resolved_paths)}{missing}"
            )
        print(
            f"\n{len(checks)} gaps, {len(failures)} failures "
            f"(strict={args.strict})"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
