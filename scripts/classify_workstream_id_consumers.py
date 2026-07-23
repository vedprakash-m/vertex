"""BL-F2 step (1) tooling: classify every `.workstream_id` reference under
src/ by syntactic call pattern, so a human triage pass (steps 2-4 -- the
actual per-file "is this safe to make plural" judgment) can start from a
bucketed list instead of re-deriving it by hand each time.

BL-F2's own text recommended exactly this: "script the classification (grep
with surrounding-line context, bucket by call pattern -- equality comparison
vs. attribute read vs. assignment vs. test fixture -- before any manual
reading) rather than opening 43+ files one at a time." The 2026-07-22
reconnaissance pass hand-counted 119 files (43 also importing `Signal`) and
explicitly did not classify further. Both counts had already drifted (132 /
43) by the time this script was written the same day -- proof the count
needs to be derived fresh each run, not transcribed once into prose, the
same lesson `derive_spec_counts.py` encodes for other spec-cited numbers.

This script makes no judgment about which consumers are safe to change --
only what syntactic pattern each reference matches, and whether the file
also imports `Signal` (a rough, imperfect proxy for "reads `Signal.
workstream_id` specifically" vs. some other type's own `workstream_id`
field, e.g. `DecisionAsk`/`ActionItem`/`KustoQuery`, exactly as this row's
own text warns). It does not itself close BL-F2; it produces the triage
starting point steps (2)-(4) still need a human/dedicated session to act on.

Usage:
    python scripts/classify_workstream_id_consumers.py
    python scripts/classify_workstream_id_consumers.py --format json
    python scripts/classify_workstream_id_consumers.py --format json --out output/workstream_id_consumers.json
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

_WORKSTREAM_ID_RE = re.compile(r"\.workstream_id\b")
_SIGNAL_IMPORT_RE = re.compile(r"\bimport\s+[^\n]*\bSignal\b")
_ASSIGNMENT_RE = re.compile(r"\.workstream_id\s*=(?!=)")
_COMPARISON_RE = re.compile(r"workstream_id\b\s*(==|!=)|(==|!=)\s*[\w.\[\]'\"]*workstream_id\b")
_MEMBERSHIP_RE = re.compile(r"\bworkstream_id\b\s+(in|not\s+in)\b")

# Patterns are checked in this priority order -- assignment and comparison
# are the two patterns steps (2)-(4) most need to distinguish (a comparison
# assumes singularity; an assignment is a construction site), so they take
# priority over the catch-all "attribute-read" bucket.
_PATTERN_CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("assignment", _ASSIGNMENT_RE),
    ("comparison", _COMPARISON_RE),
    ("membership", _MEMBERSHIP_RE),
)


@dataclass(frozen=True, slots=True)
class ConsumerMatch:
    file: str
    line_no: int
    snippet: str
    pattern: str


def classify_line(line: str) -> str:
    for pattern_name, pattern_re in _PATTERN_CHECKS:
        if pattern_re.search(line):
            return pattern_name
    return "attribute-read"


def find_matches(src_root: Path = SRC_ROOT, repo_root: Path = REPO_ROOT) -> list[ConsumerMatch]:
    matches: list[ConsumerMatch] = []
    if not src_root.exists():
        return matches
    for py_file in sorted(src_root.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if not _WORKSTREAM_ID_RE.search(text):
            continue
        rel = py_file.relative_to(repo_root).as_posix()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _WORKSTREAM_ID_RE.search(line):
                matches.append(
                    ConsumerMatch(file=rel, line_no=line_no, snippet=line.strip(), pattern=classify_line(line))
                )
    return matches


def imports_signal(file_path: Path) -> bool:
    if not file_path.exists():
        return False
    return bool(_SIGNAL_IMPORT_RE.search(file_path.read_text(encoding="utf-8")))


def build_report(src_root: Path = SRC_ROOT, repo_root: Path = REPO_ROOT) -> dict[str, object]:
    matches = find_matches(src_root, repo_root)

    by_file: dict[str, list[ConsumerMatch]] = {}
    for match in matches:
        by_file.setdefault(match.file, []).append(match)

    files_report = []
    for rel, file_matches in sorted(by_file.items()):
        pattern_counts: dict[str, int] = {}
        for match in file_matches:
            pattern_counts[match.pattern] = pattern_counts.get(match.pattern, 0) + 1
        files_report.append(
            {
                "file": rel,
                "imports_signal": imports_signal(repo_root / rel),
                "match_count": len(file_matches),
                "pattern_counts": pattern_counts,
                "matches": [asdict(m) for m in file_matches],
            }
        )

    total_pattern_counts: dict[str, int] = {}
    for match in matches:
        total_pattern_counts[match.pattern] = total_pattern_counts.get(match.pattern, 0) + 1

    signal_importing_count = sum(1 for f in files_report if f["imports_signal"])

    return {
        "total_files_with_matches": len(files_report),
        "total_matches": len(matches),
        "signal_importing_file_count": signal_importing_count,
        "pattern_counts": total_pattern_counts,
        "files": files_report,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify every src/ `.workstream_id` reference by syntactic pattern (BL-F2 step 1 tooling)."
    )
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument(
        "--out",
        default=None,
        help="Write the JSON report to this path in addition to stdout (human format prints a summary only).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report()

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.format == "json":
        print(json.dumps(report, indent=2))
        return 0

    print(f"Files with a `.workstream_id` reference: {report['total_files_with_matches']}")
    print(f"Total matches: {report['total_matches']}")
    print(f"Files also importing `Signal` (imperfect proxy, see docstring): {report['signal_importing_file_count']}")
    print("Pattern counts:")
    pattern_counts = cast("dict[str, int]", report["pattern_counts"])
    for pattern, count in sorted(pattern_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {pattern:<16} {count}")
    if args.out:
        print(f"\nFull per-file/per-line report written to {args.out}")
    else:
        print("\nRun with --format json --out <path> for the full per-file/per-line breakdown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
